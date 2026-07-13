from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from apps.accounts.models import User
from apps.certificates.models import Certificate
from apps.documents.models import Document
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportFinding,
    ReportSource,
)

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

            if report:
                certificate = self._get_certificate(report=report)

                if hasattr(document, "extracted_text"):
                    highlighted_segments = self._build_highlight_segments(
                        content=document.extracted_text.content,
                        findings=list(report.findings.all()),
                    )

            context = {
                "document": document,
                "report": report,
                "certificate": certificate,
                "highlighted_segments": highlighted_segments,
                "sources": report.sources.all() if report else [],
                "similarity_findings": report.findings.filter(
                    finding_type=FindingType.SIMILARITY,
                ) if report else [],
                "ai_findings": report.findings.filter(
                    finding_type=FindingType.AI_GENERATED,
                ) if report else [],
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

        if user.is_student_role:
            queryset = queryset.filter(owner=user)

        elif not user.is_superuser:
            if user.institution_id is None:
                raise PermissionDenied(
                    "Tu usuario no tiene institución asignada."
                )

            queryset = queryset.filter(institution=user.institution)

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