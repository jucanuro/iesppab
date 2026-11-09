from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from apps.certificates.models import Certificate
from apps.certificates.services import (
    CertificateGenerationError,
    CertificateGenerationService,
)
from apps.reports.ai_estimate import build_ai_estimate
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportSource,
    SourceType,
)

logger = logging.getLogger(__name__)


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _is_internal_only(source: ReportSource) -> bool:
    """
    Documento de la base interna. Los registros OAI también se guardan con
    `source_type` INTERNAL pero son públicos y conservan su título.
    """
    return source.source_type == SourceType.INTERNAL and not source.is_oai


def split_public_sources(
    report: AnalysisReport,
) -> tuple[list[ReportSource], dict[str, Any] | None]:
    """
    Fuentes visibles en la verificación pública (RN-CER-04, REQ-CER-08).

    Devuelve (fuentes web/OAI no excluidas, resumen de la base interna). Las
    fuentes internas nunca salen individualmente: solo "N fuentes, X %".
    X es la unión de palabras cubiertas por fuentes internas (sin OAI), sin
    sumar los % por fuente (RN-CER-06); se calcula aquí desde los hallazgos.
    """
    active = list(report.sources.filter(is_excluded=False))
    public = [source for source in active if not _is_internal_only(source)]
    internal = [source for source in active if _is_internal_only(source)]

    if not internal:
        return public, None

    return public, {
        "count": len(internal),
        "percent": _internal_union_percent(report, internal),
    }


def _internal_union_percent(
    report: AnalysisReport,
    internal: list[ReportSource],
) -> Decimal | None:
    findings = list(
        report.findings.filter(
            finding_type=FindingType.SIMILARITY,
            source__in=internal,
        ).values("start_offset", "end_offset", "metadata")
    )

    if not findings:
        return Decimal("0.0")

    # Motor span: cada palabra se asigna a una sola fuente, así que los
    # tramos no se solapan y basta sumar sus palabras contables.
    countable = (report.span_alignment or {}).get("countable_word_count", 0)

    if countable and all(
        isinstance((item["metadata"] or {}).get("word_count"), int)
        for item in findings
    ):
        covered = sum(item["metadata"]["word_count"] for item in findings)
        return _percent(covered, countable)

    # Motor por fragmentos: los tramos de distintas fuentes pueden solaparse;
    # se unen los intervalos y se cuentan las palabras del texto cubiertas.
    try:
        content = report.document.extracted_text.content
    except Exception:
        return None

    total_words = len(_WORD_RE.findall(content))

    if not total_words:
        return None

    intervals = sorted((item["start_offset"], item["end_offset"]) for item in findings)
    merged: list[list[int]] = []

    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    covered = sum(
        len(_WORD_RE.findall(content[start:end])) for start, end in merged
    )
    return _percent(covered, total_words)


def _percent(part: int, total: int) -> Decimal:
    if not total:
        return Decimal("0.0")

    value = Decimal(part) / Decimal(total) * Decimal("100")
    return min(value, Decimal("100")).quantize(Decimal("0.1"))


class CertificateGenerateView(LoginRequiredMixin, View):
    """
    Genera el certificado PDF desde un reporte final.
    """

    def post(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        try:
            base_url = request.build_absolute_uri("/")
            service = CertificateGenerationService(
                issued_by=request.user,
                absolute_base_url=base_url,
            )

            service.execute(document_id=pk)

            messages.success(
                request,
                "Certificado generado correctamente.",
            )

            # Volvemos a la página del reporte (no directo a la descarga): así
            # la página recarga y el botón pasa a "Descargar certificado", en
            # vez de quedarse el navegador en la página anterior con el botón
            # de "Generando…" bloqueado tras bajar el PDF. El parámetro
            # `descargar_certificado` hace que la página dispare la descarga
            # automáticamente al cargar.
            detail_url = reverse("reports:detail", kwargs={"pk": pk})
            return redirect(f"{detail_url}?descargar_certificado=1")

        except PermissionDenied as exc:
            logger.warning(
                "Permiso denegado al generar certificado. user_id=%s document_id=%s",
                request.user.id,
                pk,
                exc_info=True,
            )
            return HttpResponse(str(exc), status=403)

        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("reports:detail", pk=pk)

        except CertificateGenerationError as exc:
            messages.error(request, str(exc))
            return redirect("reports:detail", pk=pk)

        except Exception:
            logger.exception(
                "Error inesperado al generar certificado. user_id=%s document_id=%s",
                request.user.id,
                pk,
            )
            messages.error(
                request,
                "Ocurrió un error inesperado al generar el certificado.",
            )
            return redirect("reports:detail", pk=pk)


class CertificateDownloadView(LoginRequiredMixin, View):
    """
    Descarga segura del certificado PDF.
    """

    def get(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> FileResponse:
        certificate = self._get_allowed_certificate(
            certificate_id=pk,
            request=request,
        )

        if not certificate.pdf_file:
            raise Http404("El certificado aún no tiene archivo PDF.")

        return FileResponse(
            certificate.pdf_file.open("rb"),
            as_attachment=True,
            filename=f"{certificate.code}.pdf",
            content_type="application/pdf",
        )

    def _get_allowed_certificate(
        self,
        certificate_id: UUID,
        request: HttpRequest,
    ) -> Certificate:
        queryset = Certificate.objects.select_related(
            "report",
            "report__document",
            "report__document__institution",
            "report__document__owner",
        ).filter(
            id=certificate_id,
            is_active=True,
        )

        user = request.user

        if user.is_student_role:
            queryset = queryset.filter(report__document__owner=user)

        elif not user.is_superuser:
            if user.institution_id is None:
                raise PermissionDenied(
                    "Tu usuario no tiene institución asignada."
                )

            queryset = queryset.filter(
                report__document__institution=user.institution,
            )

        certificate = queryset.first()

        if certificate is None:
            raise Http404("Certificado no encontrado.")

        return certificate


class CertificateVerifyView(View):
    """
    Vista pública simple para validar autenticidad por hash.
    """

    template_name = "certificates/verify.html"

    def get(
        self,
        request: HttpRequest,
        verification_hash: str,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        certificate = None
        sources: list = []
        internal_summary = None
        ai_estimate = None

        try:
            certificate = (
                Certificate.objects.select_related(
                    "report",
                    "report__document",
                    "report__document__institution",
                    "report__document__owner",
                )
                .filter(
                    verification_hash=verification_hash,
                    is_active=True,
                )
                .first()
            )

            if certificate is not None:
                report = certificate.report
                document = report.document
                # Fuerza la carga de las relaciones que usa la plantilla: si
                # alguna está rota, falla aquí y no al renderizar (REQ-CER-02).
                _ = (document.owner, document.institution)
                sources, internal_summary = split_public_sources(report)
                ai_estimate = build_ai_estimate(report)
        except Exception:
            logger.exception(
                "Error al verificar el certificado. verification_hash=%s",
                verification_hash,
            )
            certificate = None
            sources = []
            internal_summary = None
            ai_estimate = None

        return render(
            request,
            self.template_name,
            {
                "certificate": certificate,
                "verification_hash": verification_hash,
                "sources": sources,
                "internal_summary": internal_summary,
                "ai_estimate": ai_estimate,
            },
        )