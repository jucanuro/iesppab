from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, Q
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.views import View

from apps.accounts.models import User
from apps.analysis.models import AnalysisJob, AnalysisJobStatus
from apps.certificates.models import Certificate
from apps.documents.models import Document, DocumentStatus
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportFinding,
    ReportSource,
)
from apps.reports.services import (
    HighlightedDocumentPdfError,
    build_highlighted_document_pdf,
    resolve_analyzed_content,
)
from apps.reports.suggestions import build_improvement_suggestions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HighlightSegment:
    text: str
    finding_type: str | None
    confidence_percent: str | None


class ReportDetailView(LoginRequiredMixin, View):
    """
    Vista del reporte final del documento.

    Muestra:
    - Resumen del análisis.
    - Texto extraído con resaltados.
    - Fuentes detectadas.
    - Hallazgos de similitud e IA.
    - Certificado generado, si existe.
    """

    template_name = "reports/detail.html"

    def get(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        user = request.user

        try:
            document = self._get_allowed_document(
                document_id=pk,
                user=user,
            )

            report = self._get_report(document=document)

            certificate = None
            highlighted_segments: list[HighlightSegment] = []
            analysis_job = None

            if report:
                certificate = self._get_certificate(report=report)

                highlighted_segments = self._build_highlight_segments(
                    content=resolve_analyzed_content(report),
                    findings=list(report.findings.all()),
                )
            else:
                analysis_job = self._get_latest_job(document=document)

            # "En progreso" cubre tanto el hueco entre encolar la tarea y que
            # el worker cree el AnalysisJob (ahí solo cambia Document.status a
            # QUEUED) como el trabajo ya corriendo.
            analysis_in_progress = document.status in {
                DocumentStatus.QUEUED,
                DocumentStatus.PROCESSING,
            } or (
                analysis_job is not None
                and analysis_job.status
                in {
                    AnalysisJobStatus.PENDING,
                    AnalysisJobStatus.QUEUED,
                    AnalysisJobStatus.RUNNING,
                }
            )

            context = {
                "document": document,
                "report": report,
                "certificate": certificate,
                "highlighted_segments": highlighted_segments,
                "analysis_job": analysis_job,
                "analysis_in_progress": analysis_in_progress,
                "sources": report.sources.all() if report else [],
                "similarity_findings": report.findings.filter(
                    finding_type=FindingType.SIMILARITY,
                ) if report else [],
                "ai_findings": report.findings.filter(
                    finding_type=FindingType.AI_GENERATED,
                ) if report else [],
                "improvement_suggestions": (
                    build_improvement_suggestions(report) if report else []
                ),
            }

            return render(request, self.template_name, context)

        except PermissionDenied:
            logger.warning(
                "Permiso denegado al ver reporte. user_id=%s document_id=%s",
                user.id,
                pk,
                exc_info=True,
            )
            raise

        except Document.DoesNotExist as exc:
            raise Http404("Documento no encontrado.") from exc

    @staticmethod
    def _get_allowed_document(
        document_id: UUID,
        user: User,
    ) -> Document:
        queryset = Document.objects.select_related(
            "institution",
            "owner",
            "uploaded_by",
            "extracted_text",
        ).filter(id=document_id)

        if user.is_superuser or user.is_admin_role:
            if not user.is_superuser:
                if user.institution_id is None:
                    raise PermissionDenied(
                        "Tu usuario no tiene institución asignada."
                    )

                queryset = queryset.filter(institution=user.institution)

        else:
            queryset = queryset.filter(Q(owner=user) | Q(uploaded_by=user))

        document = queryset.first()

        if document is None:
            raise Document.DoesNotExist

        return document

    def _get_report(
        self,
        document: Document,
    ) -> AnalysisReport | None:
        return (
            AnalysisReport.objects.select_related(
                "document",
                "analysis_job",
            )
            .prefetch_related(
                Prefetch(
                    "sources",
                    queryset=ReportSource.objects.order_by(
                        "-matched_percent",
                    ),
                ),
                Prefetch(
                    "findings",
                    queryset=ReportFinding.objects.select_related(
                        "source",
                    ).order_by("start_offset"),
                ),
            )
            .filter(document=document)
            .first()
        )

    def _get_certificate(
        self,
        report: AnalysisReport,
    ) -> Certificate | None:
        return (
            Certificate.objects.filter(
                report=report,
                is_active=True,
            )
            .order_by("-created_at")
            .first()
        )

    def _get_latest_job(
        self,
        document: Document,
    ) -> AnalysisJob | None:
        return (
            AnalysisJob.objects.filter(document=document)
            .order_by("-created_at")
            .first()
        )

    def _build_highlight_segments(
        self,
        content: str,
        findings: list[ReportFinding],
    ) -> list[HighlightSegment]:
        """
        Convierte el texto plano en segmentos resaltables.

        Prioridad:
        - Similitud en rojo.
        - IA en azul.
        - Si hay solapamiento, se respeta el primer hallazgo válido.
        """

        if not content:
            return []

        valid_findings = sorted(
            [
                finding
                for finding in findings
                if finding.start_offset < finding.end_offset
                and finding.start_offset < len(content)
            ],
            key=lambda item: item.start_offset,
        )

        segments: list[HighlightSegment] = []
        cursor = 0

        for finding in valid_findings:
            start = max(finding.start_offset, 0)
            end = min(finding.end_offset, len(content))

            if start < cursor:
                continue

            if cursor < start:
                segments.append(
                    HighlightSegment(
                        text=content[cursor:start],
                        finding_type=None,
                        confidence_percent=None,
                    )
                )

            segments.append(
                HighlightSegment(
                    text=content[start:end],
                    finding_type=finding.finding_type,
                    confidence_percent=str(finding.confidence_percent),
                )
            )

            cursor = end

        if cursor < len(content):
            segments.append(
                HighlightSegment(
                    text=content[cursor:],
                    finding_type=None,
                    confidence_percent=None,
                )
            )

        return segments


class ReportStatusView(LoginRequiredMixin, View):
    """
    Devuelve el estado del análisis de un documento en JSON, para que la
    página del reporte lo consulte por polling mientras el análisis está en
    curso (en vez de recargar la página entera cada pocos segundos).
    """

    _IN_PROGRESS = {
        DocumentStatus.QUEUED,
        DocumentStatus.PROCESSING,
    }

    def get(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> JsonResponse:
        try:
            document = ReportDetailView._get_allowed_document(
                document_id=pk,
                user=request.user,
            )
        except PermissionDenied:
            return JsonResponse({"detail": "forbidden"}, status=403)
        except Document.DoesNotExist:
            raise Http404("Documento no encontrado.")

        return JsonResponse(
            {
                "status": document.status,
                "status_display": document.get_status_display(),
                "in_progress": document.status in self._IN_PROGRESS,
            }
        )


class DownloadHighlightedDocumentView(LoginRequiredMixin, View):
    """
    Descarga en PDF el texto del documento con los mismos hallazgos
    resaltados que el visor interactivo del reporte. No reemplaza al
    certificado institucional: es el documento señalado, no un resumen.
    """

    def get(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        user = request.user

        try:
            document = self._get_allowed_document(
                document_id=pk,
                user=user,
            )

            report = self._get_report(document=document)

            if report is None:
                raise Http404("Este documento aún no tiene reporte.")

            pdf_content = build_highlighted_document_pdf(report=report)

            return FileResponse(
                BytesIO(pdf_content),
                as_attachment=True,
                filename=f"informe-originalidad-{document.id}.pdf",
                content_type="application/pdf",
            )

        except PermissionDenied:
            logger.warning(
                "Permiso denegado al descargar documento señalado. "
                "user_id=%s document_id=%s",
                user.id,
                pk,
                exc_info=True,
            )
            raise

        except Document.DoesNotExist as exc:
            raise Http404("Documento no encontrado.") from exc

        except HighlightedDocumentPdfError as exc:
            messages.error(request, str(exc))
            return redirect("reports:detail", pk=pk)

    def _get_allowed_document(
        self,
        document_id: UUID,
        user: User,
    ) -> Document:
        queryset = Document.objects.select_related(
            "institution",
            "owner",
            "uploaded_by",
            "extracted_text",
        ).filter(id=document_id)

        if user.is_superuser or user.is_admin_role:
            if not user.is_superuser:
                if user.institution_id is None:
                    raise PermissionDenied(
                        "Tu usuario no tiene institución asignada."
                    )

                queryset = queryset.filter(institution=user.institution)

        else:
            queryset = queryset.filter(Q(owner=user) | Q(uploaded_by=user))

        document = queryset.first()

        if document is None:
            raise Document.DoesNotExist

        return document

    def _get_report(
        self,
        document: Document,
    ) -> AnalysisReport | None:
        return (
            AnalysisReport.objects.select_related(
                "document",
                "document__institution",
                "document__owner",
            )
            .prefetch_related(
                Prefetch(
                    "sources",
                    queryset=ReportSource.objects.order_by(
                        "-matched_percent",
                    ),
                ),
                Prefetch(
                    "findings",
                    queryset=ReportFinding.objects.select_related(
                        "source",
                    ).order_by("start_offset"),
                ),
            )
            .filter(document=document)
            .first()
        )
