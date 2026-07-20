from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterable
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.analysis.engines.ai_detector import SpanishAIDetector
from apps.analysis.engines.similarity import (
    CandidateKnowledgeChunk,
    InternalSimilarityEngine,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_search import WebCandidateCollector
from apps.analysis.engines.web_similarity import (
    WebSimilarityAnalysisResult,
    WebSimilarityEngine,
    WebSimilarityMatch,
)
from apps.analysis.indexers import DocumentKnowledgeIndexer
from apps.analysis.models import (
    AnalysisJob,
    AnalysisJobStatus,
    AnalysisType,
    DocumentKnowledgeChunk,
)
from apps.documents.extractors import DocumentTextExtractor
from apps.documents.models import Document, DocumentStatus
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportFinding,
    ReportRiskLevel,
    ReportSource,
    SourceType,
)

logger = logging.getLogger(__name__)


class DocumentAnalysisError(Exception):
    pass


class DocumentAnalysisService:
    """
    Pipeline completo:

    1. Valida permisos.
    2. Extrae texto.
    3. Limpia portada / índice / bibliografía / anexos.
    4. Aprende el documento para repositorio interno.
    5. Compara contra documentos internos.
    6. Busca coincidencias en internet.
    7. Lee páginas HTML y PDFs públicos.
    8. Compara contra fuentes web.
    9. Estima patrones IA.
    10. Genera reporte final.
    """

    def __init__(self, requested_by: User) -> None:
        self.requested_by = requested_by

        self.extractor = DocumentTextExtractor()
        self.text_filter = AcademicTextFilter()
        self.knowledge_indexer = DocumentKnowledgeIndexer()

        self.internal_similarity_engine = InternalSimilarityEngine()
        self.web_candidate_collector = WebCandidateCollector()
        self.web_similarity_engine = WebSimilarityEngine()

        self.ai_detector = SpanishAIDetector()

    def execute(self, document_id: UUID) -> AnalysisReport:
        document = self._get_allowed_document(document_id=document_id)
        job = self._create_job(document=document)

        try:
            self._mark_as_processing(
                document=document,
                job=job,
                step="Extrayendo texto del documento",
            )

            document_text = self.extractor.extract_and_save(
                document=document,
            )

            filtered_text = self.text_filter.filter_for_similarity(
                content=document_text.content,
            )

            analysis_content = filtered_text.content or document_text.content

            self._mark_job_step(
                job=job,
                step="Aprendiendo fragmentos internos del documento",
            )

            self.knowledge_indexer.index(
                document_text=document_text,
            )

            self._mark_job_step(
                job=job,
                step="Comparando contra repositorio institucional",
            )

            internal_candidates = self._get_internal_candidates(
                document=document,
            )

            internal_result = self.internal_similarity_engine.analyze(
                content=analysis_content,
                candidates=internal_candidates,
            )

            self._mark_job_step(
                job=job,
                step="Buscando coincidencias públicas en internet",
            )

            web_pages = self.web_candidate_collector.collect(
                content=analysis_content,
            )

            self._mark_job_step(
                job=job,
                step="Comparando contra fuentes web encontradas",
            )

            web_result = self.web_similarity_engine.analyze(
                content=analysis_content,
                web_pages=web_pages,
            )

            self._mark_job_step(
                job=job,
                step="Estimando patrones de redacción IA",
            )

            ai_result = self.ai_detector.analyze(
                content=analysis_content,
            )

            total_similarity_percent = self._calculate_total_similarity_percent(
                content_length=len(analysis_content),
                internal_matches=internal_result.matches,
                web_matches=web_result.matches,
            )

            with transaction.atomic():
                report = self._save_report(
                    document=document,
                    job=job,
                    total_similarity_percent=total_similarity_percent,
                    internal_similarity_percent=internal_result.similarity_percent,
                    web_similarity_percent=web_result.web_similarity_percent,
                    ai_probability_percent=ai_result.ai_probability_percent,
                    excluded_sections=filtered_text.excluded_sections,
                )

                self._replace_internal_similarity_findings(
                    report=report,
                    matches=internal_result.matches,
                )

                self._replace_web_similarity_findings(
                    report=report,
                    web_result=web_result,
                )

                self._replace_ai_findings(
                    report=report,
                    findings=ai_result.findings,
                )

                self._mark_as_success(
                    document=document,
                    job=job,
                )

            logger.info(
                "Análisis completado. document_id=%s report_id=%s total=%s internal=%s web=%s ai=%s",
                document.id,
                report.id,
                report.similarity_percent,
                report.internal_similarity_percent,
                report.web_similarity_percent,
                report.ai_probability_percent,
            )

            return report

        except Exception as exc:
            logger.exception(
                "Error analizando documento. document_id=%s job_id=%s",
                document.id,
                job.id,
            )

            self._mark_as_failed(
                document=document,
                job=job,
                message=str(exc),
            )

            raise DocumentAnalysisError(
                "No se pudo completar el análisis del documento."
            ) from exc

    def _get_allowed_document(self, document_id: UUID) -> Document:
        queryset = Document.objects.select_related(
            "institution",
            "owner",
            "uploaded_by",
        ).filter(id=document_id)

        if self.requested_by.is_student_role:
            queryset = queryset.filter(owner=self.requested_by)

        elif not self.requested_by.is_superuser:
            if self.requested_by.institution_id is None:
                raise PermissionDenied(
                    "Tu usuario no tiene institución asignada."
                )

            queryset = queryset.filter(
                institution=self.requested_by.institution,
            )

        document = queryset.first()

        if document is None:
            raise PermissionDenied(
                "No tienes permiso para analizar este documento."
            )

        return document

    def _create_job(self, document: Document) -> AnalysisJob:
        latest_attempt = (
            AnalysisJob.objects.filter(
                document=document,
                analysis_type=AnalysisType.FULL,
            )
            .order_by("-attempt_number")
            .values_list("attempt_number", flat=True)
            .first()
            or 0
        )

        return AnalysisJob.objects.create(
            document=document,
            requested_by=self.requested_by,
            analysis_type=AnalysisType.FULL,
            status=AnalysisJobStatus.PENDING,
            attempt_number=latest_attempt + 1,
            current_step="Preparando análisis",
        )

    def _mark_as_processing(
        self,
        document: Document,
        job: AnalysisJob,
        step: str,
    ) -> None:
        document.status = DocumentStatus.PROCESSING
        document.error_message = ""
        document.save(
            update_fields=[
                "status",
                "error_message",
                "updated_at",
            ]
        )

        job.status = AnalysisJobStatus.RUNNING
        job.started_at = timezone.now()
        job.current_step = step
        job.save(
            update_fields=[
                "status",
                "started_at",
                "current_step",
                "updated_at",
            ]
        )

    def _mark_job_step(
        self,
        job: AnalysisJob,
        step: str,
    ) -> None:
        job.current_step = step
        job.save(
            update_fields=[
                "current_step",
                "updated_at",
            ]
        )

    def _mark_as_success(
        self,
        document: Document,
        job: AnalysisJob,
    ) -> None:
        document.status = DocumentStatus.COMPLETED
        document.error_message = ""
        document.save(
            update_fields=[
                "status",
                "error_message",
                "updated_at",
            ]
        )

        job.status = AnalysisJobStatus.SUCCESS
        job.finished_at = timezone.now()
        job.current_step = "Análisis completado"
        job.error_message = ""
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "current_step",
                "error_message",
                "updated_at",
            ]
        )

    def _mark_as_failed(
        self,
        document: Document,
        job: AnalysisJob,
        message: str,
    ) -> None:
        document.status = DocumentStatus.FAILED
        document.error_message = message[:1000]
        document.save(
            update_fields=[
                "status",
                "error_message",
                "updated_at",
            ]
        )

        job.status = AnalysisJobStatus.FAILED
        job.finished_at = timezone.now()
        job.current_step = "Error durante el análisis"
        job.error_message = message[:2000]
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "current_step",
                "error_message",
                "updated_at",
            ]
        )

    def _get_internal_candidates(
        self,
        document: Document,
    ) -> list[CandidateKnowledgeChunk]:
        chunks = (
            DocumentKnowledgeChunk.objects.select_related(
                "document",
                "document__owner",
            )
            .filter(
                document__institution=document.institution,
                is_active=True,
            )
            .exclude(document=document)
            .order_by("-created_at")[:5000]
        )

        candidates: list[CandidateKnowledgeChunk] = []

        for chunk in chunks:
            owner_name = (
                chunk.document.owner.get_full_name()
                or chunk.document.owner.username
            )

            candidates.append(
                CandidateKnowledgeChunk(
                    document_id=chunk.document.id,
                    title=chunk.document.title,
                    owner_name=owner_name,
                    text_excerpt=chunk.text_excerpt,
                    normalized_text=chunk.normalized_text,
                )
            )

        return candidates

    def _save_report(
        self,
        document: Document,
        job: AnalysisJob,
        total_similarity_percent: Decimal,
        internal_similarity_percent: Decimal,
        web_similarity_percent: Decimal,
        ai_probability_percent: Decimal,
        excluded_sections: list[str],
    ) -> AnalysisReport:
        risk_level = self._resolve_risk_level(
            similarity_percent=total_similarity_percent,
            ai_probability_percent=ai_probability_percent,
        )

        report, _ = AnalysisReport.objects.update_or_create(
            document=document,
            defaults={
                "analysis_job": job,
                "similarity_percent": total_similarity_percent,
                "web_similarity_percent": web_similarity_percent,
                "internal_similarity_percent": internal_similarity_percent,
                "ai_probability_percent": ai_probability_percent,
                "risk_level": risk_level,
                "summary": {
                    "engine": "internal-web-academic-v2",
                    "internal_similarity_algorithm": "knowledge-chunks-shingling",
                    "web_similarity_algorithm": "brave-search-html-pdf-shingling",
                    "ai_algorithm": "spanish-heuristic-v1",
                    "excluded_sections": excluded_sections,
                    "note": "La similitud web depende de fuentes públicas encontradas y accesibles.",
                },
                "engine_version": "vql-web-academic-v2.0.0",
                "generated_at": timezone.now(),
                "is_final": True,
            },
        )

        return report

    def _replace_internal_similarity_findings(
        self,
        report: AnalysisReport,
        matches: list,
    ) -> None:
        ReportFinding.objects.filter(
            report=report,
            finding_type=FindingType.SIMILARITY,
            source__source_type=SourceType.INTERNAL,
        ).delete()

        ReportSource.objects.filter(
            report=report,
            source_type=SourceType.INTERNAL,
        ).delete()

        for match in matches:
            source = ReportSource.objects.create(
                report=report,
                source_type=SourceType.INTERNAL,
                title=match.source_title,
                url="",
                domain="Repositorio interno",
                matched_percent=match.matched_percent,
                snippet=match.source_excerpt,
                metadata={
                    "source_document_id": str(match.source_document_id),
                    "source_owner": match.source_owner_name,
                },
            )

            ReportFinding.objects.create(
                report=report,
                source=source,
                finding_type=FindingType.SIMILARITY,
                start_offset=match.start_offset,
                end_offset=match.end_offset,
                text_excerpt=match.text_excerpt,
                confidence_percent=match.matched_percent,
                metadata={
                    "algorithm": "knowledge-chunks-shingling",
                    "source": "internal",
                    "source_document_id": str(match.source_document_id),
                },
            )

    def _replace_web_similarity_findings(
        self,
        report: AnalysisReport,
        web_result: WebSimilarityAnalysisResult,
    ) -> None:
        ReportFinding.objects.filter(
            report=report,
            finding_type=FindingType.SIMILARITY,
            source__source_type=SourceType.WEB,
        ).delete()

        ReportSource.objects.filter(
            report=report,
            source_type=SourceType.WEB,
        ).delete()

        for match in web_result.matches:
            source = ReportSource.objects.create(
                report=report,
                source_type=SourceType.WEB,
                title=match.title or match.domain,
                url=match.url,
                domain=match.domain,
                matched_percent=match.matched_percent,
                snippet=match.source_excerpt,
                metadata={
                    "source": "web",
                    "url": match.url,
                },
            )

            ReportFinding.objects.create(
                report=report,
                source=source,
                finding_type=FindingType.SIMILARITY,
                start_offset=match.start_offset,
                end_offset=match.end_offset,
                text_excerpt=match.text_excerpt,
                confidence_percent=match.matched_percent,
                metadata={
                    "algorithm": "brave-search-html-pdf-shingling",
                    "source": "web",
                    "url": match.url,
                },
            )

    def _replace_ai_findings(
        self,
        report: AnalysisReport,
        findings: list,
    ) -> None:
        ReportFinding.objects.filter(
            report=report,
            finding_type=FindingType.AI_GENERATED,
        ).delete()

        for finding in findings:
            ReportFinding.objects.create(
                report=report,
                source=None,
                finding_type=FindingType.AI_GENERATED,
                start_offset=finding.start_offset,
                end_offset=finding.end_offset,
                text_excerpt=finding.text_excerpt,
                confidence_percent=finding.confidence_percent,
                metadata={
                    "algorithm": "spanish-heuristic-v1",
                    "note": "Estimación técnica, no prueba concluyente.",
                },
            )

    def _calculate_total_similarity_percent(
        self,
        content_length: int,
        internal_matches: Iterable,
        web_matches: Iterable[WebSimilarityMatch],
    ) -> Decimal:
        ranges: list[tuple[int, int]] = []

        for match in internal_matches:
            ranges.append(
                (
                    match.start_offset,
                    match.end_offset,
                )
            )

        for match in web_matches:
            ranges.append(
                (
                    match.start_offset,
                    match.end_offset,
                )
            )

        if content_length <= 0 or not ranges:
            return Decimal("0.00")

        merged_ranges = self._merge_ranges(ranges=ranges)
        matched_chars = sum(end - start for start, end in merged_ranges)

        percent = (
            Decimal(matched_chars) / Decimal(content_length)
        ) * Decimal("100")

        return min(percent, Decimal("100.00")).quantize(Decimal("0.01"))

    def _merge_ranges(
        self,
        ranges: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        sorted_ranges = sorted(ranges)
        merged: list[tuple[int, int]] = []

        for start, end in sorted_ranges:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                previous_start, previous_end = merged[-1]
                merged[-1] = (
                    previous_start,
                    max(previous_end, end),
                )

        return merged

    def _resolve_risk_level(
        self,
        similarity_percent: Decimal,
        ai_probability_percent: Decimal,
    ) -> str:
        if similarity_percent >= Decimal("50.00") or ai_probability_percent >= Decimal("80.00"):
            return ReportRiskLevel.CRITICAL

        if similarity_percent >= Decimal("30.00") or ai_probability_percent >= Decimal("60.00"):
            return ReportRiskLevel.HIGH

        if similarity_percent >= Decimal("15.00") or ai_probability_percent >= Decimal("35.00"):
            return ReportRiskLevel.MEDIUM

        return ReportRiskLevel.LOW