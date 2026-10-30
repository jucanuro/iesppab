from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.accounts.models import User
from apps.analysis.engines.ai_detector import AIAnalysisResult, SpanishAIDetector
from apps.analysis.engines.exclusions import calculate_similarity_percent
from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.ai_segments import SegmentedPerplexityDetector
from apps.analysis.engines.perplexity_detector import SpanishPerplexityDetector
from apps.analysis.engines.similarity import (
    CandidateKnowledgeChunk,
    InternalSimilarityEngine,
    SimilarityAnalysisResult,
    SimilarityMatch,
)
from apps.analysis.engines.span_similarity import (
    ALGORITHM_SPAN_INTERNAL,
    ALGORITHM_SPAN_WEB,
    MATCH_TYPE_LABELS,
    SOURCE_KIND_INTERNAL,
    SOURCE_KIND_OAI,
    SOURCE_KIND_WEB,
    SpanAlignment,
    SpanAnalysisResult,
    SpanSimilarityEngine,
    SpanSource,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_search import WebCandidateCollector, WebPageContent
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
    DocumentFingerprint,
    DocumentKnowledgeChunk,
    OaiRecord,
)
from apps.analysis.retrieval import CandidateRetriever
from apps.documents.extractors import DocumentTextExtractor
from apps.documents.models import Document, DocumentStatus
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportFinding,
    ReportSource,
    SourceType,
)
from apps.reports.services import SpanReportWriter, resolve_risk_level

logger = logging.getLogger(__name__)

# Entregas máximas de la tarea Celery para un mismo intento de análisis. La
# segunda cubre un reinicio del worker; más allá suele ser un documento que
# tumba el worker (p. ej. OOM) y reintentarlo solo repetiría el fallo.
MAX_ANALYSIS_DELIVERIES = 2


class DocumentAnalysisError(Exception):
    pass


ALGORITHM_CHUNK_INTERNAL = "knowledge-chunks-shingling"
ALGORITHM_CHUNK_WEB = "brave-search-html-pdf-shingling"


@dataclass(frozen=True)
class SimilarityRunResult:
    internal_result: SimilarityAnalysisResult
    web_result: WebSimilarityAnalysisResult
    total_similarity_percent: Decimal
    oai_records_by_id: dict[UUID, OaiRecord]
    engine: str
    retrieval: str
    # Solo con el motor span: resultado completo y alineación serializada
    # para `AnalysisReport.span_alignment` (permite excluir fuentes después).
    span_result: SpanAnalysisResult | None = None
    span_alignment: dict | None = None

    @property
    def internal_algorithm(self) -> str:
        return (
            ALGORITHM_SPAN_INTERNAL
            if self.engine == "span"
            else ALGORITHM_CHUNK_INTERNAL
        )

    @property
    def web_algorithm(self) -> str:
        return ALGORITHM_SPAN_WEB if self.engine == "span" else ALGORITHM_CHUNK_WEB


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

    # Cuando `PERPLEXITY_AI_DETECTION_ENABLED` está activo, la señal de
    # perplejidad+burstiness REEMPLAZA por completo a la heurística de
    # conectores/estilo (ver `perplexity_detector.py`) en vez de combinarse
    # con ella. Se probaron ambos enfoques: el benchmark local
    # (`evaluate_ai_detector.py`, ver `apps/analysis/evaluation/`) midió un
    # AUC-like de ~0.92-0.95 para perplejidad contra ~0.42 para la
    # heurística (peor que azar) sobre el mismo dataset de prueba. Combinar
    # ambas señales (como se hacía antes, 60/40) solo diluye la señal buena
    # con una que activamente empeora la clasificación. El resultado de la
    # heurística se sigue calculando y guardando en `ai_score_breakdown`
    # para comparación/depuración, pero no participa en el score final.
    PERPLEXITY_SIGNAL_WEIGHT = Decimal("1.00")
    HEURISTIC_SIGNAL_WEIGHT = Decimal("0.00")

    AI_DETECTOR_HEURISTIC = "spanish-heuristic-v1"
    AI_DETECTOR_PERPLEXITY = "perplexity-burstiness-v1"
    AI_DETECTOR_PERPLEXITY_SEGMENTS = "perplexity-segments-v1"

    def __init__(self, requested_by: User) -> None:
        self.requested_by = requested_by

        self.extractor = DocumentTextExtractor()
        self.text_filter = AcademicTextFilter()
        self.knowledge_indexer = DocumentKnowledgeIndexer()

        self.internal_similarity_engine = InternalSimilarityEngine()
        self.web_candidate_collector = WebCandidateCollector()
        self.web_similarity_engine = WebSimilarityEngine()
        self.candidate_retriever = CandidateRetriever()
        self.span_similarity_engine = SpanSimilarityEngine()

        self.ai_detector = SpanishAIDetector()
        self.perplexity_detector = SpanishPerplexityDetector()
        self.segmented_perplexity_detector = SegmentedPerplexityDetector()

    def execute(
        self,
        document_id: UUID,
        celery_task_id: str = "",
        exclude_quotes: bool = True,
        exclude_bibliography: bool = True,
    ) -> AnalysisReport:
        """
        `exclude_quotes`/`exclude_bibliography` son las opciones del
        análisis (como en Turnitin): las citas cortas entre comillas y la
        bibliografía/anexos no cuentan para el % de similitud. Se guardan
        en el reporte para mostrar qué se excluyó.
        """
        document = self._get_allowed_document(document_id=document_id)
        job = self._get_or_create_job(
            document=document,
            celery_task_id=celery_task_id,
        )

        if job.delivery_count > MAX_ANALYSIS_DELIVERIES:
            message = (
                "El análisis se interrumpió repetidamente porque el proceso "
                "de análisis terminó de forma inesperada. Vuelve a intentarlo "
                "o contacta al administrador."
            )
            logger.error(
                "Análisis abandonado tras %s entregas. document_id=%s job_id=%s",
                job.delivery_count,
                document.id,
                job.id,
            )
            self._mark_as_failed(document=document, job=job, message=message)
            raise DocumentAnalysisError(message)

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
                exclude_bibliography=exclude_bibliography,
            )

            analysis_content = filtered_text.content or document_text.content

            # Las citas se MARCAN, no se borran: los offsets de los hallazgos
            # se calculan sobre `analysis_content` y el visor los pinta ahí.
            quoted_ranges = (
                self.text_filter.find_quoted_ranges(analysis_content)
                if exclude_quotes
                else []
            )

            # La base de conocimiento nunca aprende bibliografía/anexos,
            # aunque este análisis los incluya: son referencias que todos
            # citan y ensuciarían las comparaciones de otros documentos.
            if exclude_bibliography:
                indexing_content = analysis_content
            else:
                indexing_content = (
                    self.text_filter.filter_for_similarity(
                        content=document_text.content,
                    ).content
                    or document_text.content
                )

            self._mark_job_step(
                job=job,
                step="Aprendiendo fragmentos internos del documento",
            )

            self.knowledge_indexer.index(
                document_text=document_text,
                content=indexing_content,
            )

            similarity = self._run_similarity(
                document=document,
                analysis_content=analysis_content,
                quoted_ranges=quoted_ranges,
                collect_web_pages=lambda: self.web_candidate_collector.collect(
                    content=analysis_content,
                ),
                on_step=lambda step: self._mark_job_step(job=job, step=step),
            )
            internal_result = similarity.internal_result
            web_result = similarity.web_result

            self._mark_job_step(
                job=job,
                step="Estimando patrones de redacción IA",
            )

            ai_result = self.ai_detector.analyze(
                content=analysis_content,
            )

            ai_probability_percent, ai_score_breakdown, ai_detector = (
                self._estimate_ai_probability(
                    document=document,
                    analysis_content=analysis_content,
                    heuristic_result=ai_result,
                    quoted_ranges=quoted_ranges,
                )
            )

            with transaction.atomic():
                report = self._save_report(
                    document=document,
                    job=job,
                    total_similarity_percent=similarity.total_similarity_percent,
                    internal_similarity_percent=internal_result.similarity_percent,
                    web_similarity_percent=web_result.web_similarity_percent,
                    ai_probability_percent=ai_probability_percent,
                    ai_detector=ai_detector,
                    excluded_sections=filtered_text.excluded_sections,
                    analyzed_content=analysis_content,
                    ai_score_breakdown=ai_score_breakdown,
                    exclude_quotes=exclude_quotes,
                    exclude_bibliography=exclude_bibliography,
                    quoted_ranges=quoted_ranges,
                    similarity=similarity,
                )

                if similarity.span_result is not None:
                    SpanReportWriter().write(
                        report=report,
                        result=similarity.span_result,
                        reset=True,
                    )
                else:
                    self._replace_internal_similarity_findings(
                        report=report,
                        matches=internal_result.matches,
                        oai_records_by_id=similarity.oai_records_by_id,
                        algorithm=similarity.internal_algorithm,
                    )

                    self._replace_web_similarity_findings(
                        report=report,
                        web_result=web_result,
                        algorithm=similarity.web_algorithm,
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

            if isinstance(exc, SoftTimeLimitExceeded):
                message = (
                    "El análisis superó el tiempo máximo permitido "
                    f"({settings.ANALYSIS_TASK_SOFT_TIME_LIMIT // 60} min) "
                    "y fue cancelado. Puedes volver a intentarlo."
                )
            else:
                message = str(exc)

            self._mark_as_failed(
                document=document,
                job=job,
                message=message,
            )

            raise DocumentAnalysisError(
                "No se pudo completar el análisis del documento."
            ) from exc

    def _estimate_ai_probability(
        self,
        document: Document,
        analysis_content: str,
        heuristic_result: AIAnalysisResult,
        quoted_ranges: list[tuple[int, int]] | None = None,
    ) -> tuple[Decimal, dict, str]:
        """
        Cuando `PERPLEXITY_AI_DETECTION_ENABLED` está activo (desactivada por
        defecto — ver `perplexity_detector.py`), la señal de
        perplejidad+burstiness REEMPLAZA a la heurística existente como
        `ai_probability_percent` (ver justificación en `PERPLEXITY_SIGNAL_WEIGHT`
        arriba). La heurística se sigue ejecutando y su score queda en
        `breakdown["heuristic_ai_score"]` solo para comparación/depuración. Si
        la señal de perplejidad no está habilitada o falla (p. ej.
        torch/transformers no instalados en este entorno), se conserva el
        comportamiento original: solo la heurística.

        Con `AI_DETECTION_MODE=segments` la perplejidad se evalúa sobre todo
        el documento por segmentos (ver `_estimate_ai_probability_by_segments`).
        """
        breakdown = {
            "heuristic_ai_score": str(heuristic_result.ai_probability_percent),
            "perplexity_ai_score": None,
            "mean_perplexity": None,
            "burstiness": None,
            "sentence_count": None,
            "combination": "heuristic-only",
        }

        if not settings.PERPLEXITY_AI_DETECTION_ENABLED:
            return (
                heuristic_result.ai_probability_percent,
                breakdown,
                self.AI_DETECTOR_HEURISTIC,
            )

        if settings.AI_DETECTION_MODE == "segments":
            return self._estimate_ai_probability_by_segments(
                document=document,
                analysis_content=analysis_content,
                heuristic_result=heuristic_result,
                quoted_ranges=quoted_ranges,
                fallback_breakdown=breakdown,
            )

        started_at = time.monotonic()

        try:
            perplexity_result = self.perplexity_detector.analyze(
                content=analysis_content,
            )
        except Exception:
            logger.exception(
                "Fallo calculando señal de perplejidad; se usa solo la heurística. "
                "document_id=%s",
                document.id,
            )
            return (
                heuristic_result.ai_probability_percent,
                breakdown,
                self.AI_DETECTOR_HEURISTIC,
            )

        elapsed_seconds = time.monotonic() - started_at

        final_probability = (
            heuristic_result.ai_probability_percent * self.HEURISTIC_SIGNAL_WEIGHT
            + perplexity_result.ai_probability_percent * self.PERPLEXITY_SIGNAL_WEIGHT
        ).quantize(Decimal("0.01"))

        breakdown = {
            "heuristic_ai_score": str(heuristic_result.ai_probability_percent),
            "perplexity_ai_score": str(perplexity_result.ai_probability_percent),
            "mean_perplexity": perplexity_result.mean_perplexity,
            "burstiness": perplexity_result.burstiness,
            "sentence_count": perplexity_result.sentence_count,
            "combination": "perplexity-replaces-heuristic",
        }

        logger.info(
            "Señal de perplejidad calculada (reemplaza a la heurística). "
            "document_id=%s heuristic_descartada=%s perplexity_final=%s "
            "mean_perplexity=%s burstiness=%s sentences=%s tiempo=%.2fs",
            document.id,
            heuristic_result.ai_probability_percent,
            final_probability,
            perplexity_result.mean_perplexity,
            perplexity_result.burstiness,
            perplexity_result.sentence_count,
            elapsed_seconds,
        )

        return final_probability, breakdown, self.AI_DETECTOR_PERPLEXITY

    def _estimate_ai_probability_by_segments(
        self,
        *,
        document: Document,
        analysis_content: str,
        heuristic_result: AIAnalysisResult,
        quoted_ranges: list[tuple[int, int]] | None,
        fallback_breakdown: dict,
    ) -> tuple[Decimal, dict, str]:
        """
        % IA = % de palabras evaluables en segmentos con score >= 50. Los
        segmentos (offsets sobre `analysis_content`) quedan en el breakdown
        para pintarlos en el visor. Las citas marcadas no se evalúan.
        """
        started_at = time.monotonic()

        try:
            result = self.segmented_perplexity_detector.analyze(
                analysis_content,
                excluded_ranges=quoted_ranges,
            )
        except Exception:
            logger.exception(
                "Fallo calculando la señal de perplejidad por segmentos; se usa "
                "solo la heurística. document_id=%s",
                document.id,
            )
            return (
                heuristic_result.ai_probability_percent,
                fallback_breakdown,
                self.AI_DETECTOR_HEURISTIC,
            )

        elapsed_seconds = time.monotonic() - started_at

        breakdown = {
            "heuristic_ai_score": str(heuristic_result.ai_probability_percent),
            "perplexity_ai_score": str(result.ai_probability_percent),
            "mean_perplexity": None,
            "burstiness": None,
            "sentence_count": sum(len(s.sentences) for s in result.segments),
            "combination": "perplexity-segments-replaces-heuristic",
            "mode": "segments",
            **result.to_breakdown(),
        }

        logger.info(
            "Señal de perplejidad por segmentos calculada. document_id=%s "
            "ia=%s segmentos=%s evaluables=%s palabras_evaluables=%s tiempo=%.2fs",
            document.id,
            result.ai_probability_percent,
            len(result.segments),
            len(result.evaluable_segments),
            result.evaluable_word_count,
            elapsed_seconds,
        )

        return (
            result.ai_probability_percent,
            breakdown,
            self.AI_DETECTOR_PERPLEXITY_SEGMENTS,
        )

    def check_permission(self, document_id: UUID) -> Document:
        """
        Valida que ``requested_by`` puede analizar el documento sin
        ejecutar el pipeline. Permite rechazar con 403 de forma síncrona
        antes de encolar la tarea de análisis en background.

        Devuelve el documento para que la vista pueda inspeccionar su
        estado (p. ej. evitar re-encolar uno que ya está en cola).
        """

        return self._get_allowed_document(document_id=document_id)

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

    def _get_or_create_job(
        self,
        document: Document,
        celery_task_id: str,
    ) -> AnalysisJob:
        """
        Reutiliza el AnalysisJob abierto del documento en vez de crear otro.

        Con acks_late, si el worker muere la tarea se reentrega y vuelve a
        llamar a execute(): reutilizar el job evita dejar el anterior huérfano
        en RUNNING. Si el job abierto es de la misma tarea, es una reentrega y
        se cuenta; si es de otra tarea, es un huérfano previo y se reinicia.
        """

        open_job = (
            AnalysisJob.objects.filter(
                document=document,
                analysis_type=AnalysisType.FULL,
                status__in=[
                    AnalysisJobStatus.PENDING,
                    AnalysisJobStatus.QUEUED,
                    AnalysisJobStatus.RUNNING,
                ],
            )
            .order_by("-attempt_number")
            .first()
        )

        if open_job is None:
            return self._create_job(
                document=document,
                celery_task_id=celery_task_id,
            )

        is_redelivery = bool(celery_task_id) and (
            open_job.celery_task_id == celery_task_id
        )

        logger.warning(
            "Reutilizando AnalysisJob abierto. document_id=%s job_id=%s "
            "reentrega=%s",
            document.id,
            open_job.id,
            is_redelivery,
        )

        open_job.delivery_count = (
            open_job.delivery_count + 1 if is_redelivery else 1
        )
        open_job.celery_task_id = celery_task_id
        open_job.requested_by = self.requested_by
        open_job.current_step = "Preparando análisis"
        open_job.error_message = ""
        open_job.save(
            update_fields=[
                "delivery_count",
                "celery_task_id",
                "requested_by",
                "current_step",
                "error_message",
                "updated_at",
            ]
        )

        return open_job

    def _create_job(
        self,
        document: Document,
        celery_task_id: str = "",
    ) -> AnalysisJob:
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
            celery_task_id=celery_task_id,
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

    # Tope de tramos que se persisten como ReportFinding con el motor span
    # (el % se calcula siempre sobre todos).
    SPAN_MAX_PERSISTED_MATCHES = SpanReportWriter.MAX_PERSISTED_MATCHES

    def _run_similarity(
        self,
        document: Document,
        analysis_content: str,
        quoted_ranges: list[tuple[int, int]],
        collect_web_pages: Callable[[], list[WebPageContent]],
        on_step: Callable[[str], None] | None = None,
        engine: str | None = None,
        retrieval: str | None = None,
    ) -> SimilarityRunResult:
        """
        Similitud interna + OAI + web con el motor y la recuperación
        configurados (`SIMILARITY_ENGINE`, `SIMILARITY_RETRIEVAL`), o los
        que se pasen explícitamente (lo usa `benchmark_similarity`).

        - chunk + vieja: comportamiento histórico.
        - chunk + nueva: mismo motor chunk, candidatos de CandidateRetriever.
        - span: siempre CandidateRetriever; una sola atribución de palabras
          entre fuentes internas, OAI y web, así que internal + web = total.
        """
        engine = engine or getattr(settings, "SIMILARITY_ENGINE", "chunk")
        retrieval = (
            "nueva"
            if engine == "span"
            else retrieval or getattr(settings, "SIMILARITY_RETRIEVAL", "vieja")
        )
        step = on_step or (lambda message: None)

        step("Comparando contra repositorio institucional")

        if retrieval == "nueva":
            retrieved = self.candidate_retriever.retrieve(
                document=document,
                analysis_content=analysis_content,
                excluded_ranges=quoted_ranges,
            )
            oai_records_by_id = retrieved.oai_records_by_id
            candidates = (
                retrieved.to_chunk_candidates(
                    max_candidates=self.candidate_retriever.max_candidates,
                )
                if engine == "chunk"
                else []
            )
        else:
            retrieved = None
            candidates, oai_records_by_id = self._get_fingerprint_candidates(
                document=document,
                analysis_content=analysis_content,
            )

        internal_result: SimilarityAnalysisResult | None = None

        if engine == "chunk":
            internal_result = self.internal_similarity_engine.analyze(
                content=analysis_content,
                candidates=candidates,
                excluded_ranges=quoted_ranges,
            )

        step("Buscando coincidencias públicas en internet")

        web_pages = collect_web_pages()

        step("Comparando contra fuentes web encontradas")

        if engine == "span":
            alignment = self.span_similarity_engine.align(
                content=analysis_content,
                sources=retrieved.to_span_sources()
                + self._web_span_sources(web_pages),
                excluded_ranges=quoted_ranges,
            )
            span_result = self.span_similarity_engine.attribute(
                content=analysis_content,
                alignment=alignment,
            )
            internal_result, web_result = self._split_span_result(span_result)

            logger.info(
                "Similitud por tramos. document_id=%s fuentes_alineadas=%s "
                "fuentes_con_coincidencia=%s palabras_contables=%s total=%s",
                document.id,
                len(retrieved.sources) + len(web_pages),
                len(span_result.sources),
                span_result.countable_word_count,
                span_result.similarity_percent,
            )

            return SimilarityRunResult(
                internal_result=internal_result,
                web_result=web_result,
                total_similarity_percent=span_result.similarity_percent,
                oai_records_by_id=oai_records_by_id,
                engine=engine,
                retrieval=retrieval,
                span_result=span_result,
                span_alignment=self.span_similarity_engine.alignment_to_json(
                    alignment,
                    details=self._span_source_details(
                        document=document,
                        alignment=alignment,
                        oai_records_by_id=oai_records_by_id,
                    ),
                ),
            )

        web_result = self.web_similarity_engine.analyze(
            content=analysis_content,
            web_pages=web_pages,
            excluded_ranges=quoted_ranges,
        )

        return SimilarityRunResult(
            internal_result=internal_result,
            web_result=web_result,
            total_similarity_percent=self._calculate_total_similarity_percent(
                content_length=len(analysis_content),
                internal_ranges=internal_result.all_matched_ranges,
                web_ranges=web_result.all_matched_ranges,
                excluded_ranges=quoted_ranges,
            ),
            oai_records_by_id=oai_records_by_id,
            engine=engine,
            retrieval=retrieval,
        )

    def _span_source_details(
        self,
        document: Document,
        alignment: SpanAlignment,
        oai_records_by_id: dict[UUID, OaiRecord],
    ) -> dict[str, dict]:
        """
        Metadatos por fuente que se guardan con la alineación, para poder
        rearmar las fuentes del reporte al excluir una sin consultar nada:
        datos OAI, y para los documentos internos lo necesario para el aviso
        de posible reenvío (mismo sha256, cobertura bruta, dueño y fecha del
        original).
        """
        details: dict[str, dict] = {}
        internal_ids = [
            aligned.source.source_id
            for aligned in alignment.sources
            if aligned.source.kind == SOURCE_KIND_INTERNAL
        ]
        originals = Document.objects.select_related("owner").in_bulk(internal_ids)

        for aligned in alignment.sources:
            source = aligned.source

            if source.kind == SOURCE_KIND_OAI:
                record = oai_records_by_id.get(source.source_id)
                details[source.key] = {
                    "oai_identifier": record.oai_identifier if record else "",
                    "repository_name": record.repository_name if record else "",
                }
                continue

            if source.kind != SOURCE_KIND_INTERNAL:
                continue

            original = originals.get(source.source_id)
            details[source.key] = {
                "coverage_word_count": (
                    self.span_similarity_engine.coverage_word_count(aligned)
                ),
                "same_hash": bool(
                    original is not None
                    and document.sha256_hash
                    and original.sha256_hash == document.sha256_hash
                ),
                "original_owner": (
                    (original.owner.get_full_name() or original.owner.username)
                    if original is not None
                    else source.owner_name
                ),
                "original_created_at": (
                    original.created_at.isoformat() if original is not None else ""
                ),
            }

        return details

    def _web_span_sources(self, web_pages: list[WebPageContent]) -> list[SpanSource]:
        return [
            SpanSource(
                key=f"web:{page.url}",
                kind=SOURCE_KIND_WEB,
                title=page.title or page.domain,
                url=page.url,
                domain=page.domain,
                segments=(page.text,),
            )
            for page in web_pages
        ]

    def _split_span_result(
        self,
        span_result: SpanAnalysisResult,
    ) -> tuple[SimilarityAnalysisResult, WebSimilarityAnalysisResult]:
        """
        Adapta el resultado del motor span a los resultados que ya consume
        la persistencia: un SimilarityMatch / WebSimilarityMatch por tramo,
        con el % de su fuente como `matched_percent`.
        """
        internal_kinds = (SOURCE_KIND_INTERNAL, SOURCE_KIND_OAI)
        internal_spans = span_result.matches_for(internal_kinds)
        web_spans = span_result.matches_for((SOURCE_KIND_WEB,))

        def by_relevance(match) -> tuple:
            return (-match.source_percent, match.start_offset)

        internal_matches = [
            SimilarityMatch(
                source_document_id=match.source.source_id,
                source_title=match.source.title,
                source_owner_name=match.source.owner_name,
                matched_percent=match.source_percent,
                text_excerpt=match.text_excerpt,
                source_excerpt=match.source_excerpt,
                start_offset=match.start_offset,
                end_offset=match.end_offset,
                match_type=match.match_type,
            )
            for match in sorted(internal_spans, key=by_relevance)
        ]
        web_matches = [
            WebSimilarityMatch(
                title=match.source.title,
                url=match.source.url,
                domain=match.source.domain,
                matched_percent=match.source_percent,
                text_excerpt=match.text_excerpt,
                source_excerpt=match.source_excerpt,
                start_offset=match.start_offset,
                end_offset=match.end_offset,
                match_type=match.match_type,
            )
            for match in sorted(web_spans, key=by_relevance)
        ]

        return (
            SimilarityAnalysisResult(
                similarity_percent=span_result.percent_for(internal_kinds),
                matches=internal_matches[: self.SPAN_MAX_PERSISTED_MATCHES],
                all_matched_ranges=[
                    (match.start_offset, match.end_offset)
                    for match in internal_spans
                ],
            ),
            WebSimilarityAnalysisResult(
                web_similarity_percent=span_result.percent_for((SOURCE_KIND_WEB,)),
                matches=web_matches[: self.SPAN_MAX_PERSISTED_MATCHES],
                all_matched_ranges=[
                    (match.start_offset, match.end_offset) for match in web_spans
                ],
            ),
        )

    def _get_fingerprint_candidates(
        self,
        document: Document,
        analysis_content: str,
    ) -> tuple[list[CandidateKnowledgeChunk], dict[UUID, OaiRecord]]:
        """
        Usa el índice invertido de huellas (Winnowing) para acotar los
        candidatos que se comparan contra el documento, en vez de traer
        todo el banco interno + OAI (hasta 5000 de cada uno) como se hacía
        antes.

        Calcula las huellas del documento completo una sola vez, busca qué
        fuentes (internas u OAI) comparten más huellas con él, y solo
        resuelve esas top-40 como candidatos reales para el motor de
        similitud (que sigue comparando fragmento por fragmento, sin
        cambios en _compare_chunk).
        """
        normalized_content = self.internal_similarity_engine._normalize(
            analysis_content,
        )
        fingerprints = compute_fingerprints(normalized_content)

        if not fingerprints:
            return [], {}

        oai_enabled = getattr(settings, "OAI_HARVEST_ENABLED", True)

        own_chunk_ids = list(
            DocumentKnowledgeChunk.objects.filter(
                document=document,
            ).values_list("id", flat=True)
        )

        fingerprint_queryset = DocumentFingerprint.objects.filter(
            hash__in=fingerprints,
        )

        if own_chunk_ids:
            # El documento ya fue re-indexado en este mismo pipeline antes
            # de llegar aquí, así que sus propios fragmentos ya tienen
            # fingerprints. Si no se excluyen aquí, acaparan el top-40 (son
            # el candidato más parecido a sí mismo) y no dejan lugar para
            # coincidencias reales de otros documentos.
            fingerprint_queryset = fingerprint_queryset.exclude(
                source_type="internal",
                source_id__in=own_chunk_ids,
            )

        if not oai_enabled:
            fingerprint_queryset = fingerprint_queryset.exclude(
                source_type="oai",
            )

        top_sources = (
            fingerprint_queryset.values("source_type", "source_id")
            .annotate(shared=Count("id"))
            .order_by("-shared")[:40]
        )

        internal_ids: list[UUID] = []
        oai_ids: list[UUID] = []

        for row in top_sources:
            if row["source_type"] == "internal":
                internal_ids.append(row["source_id"])
            else:
                oai_ids.append(row["source_id"])

        candidates: list[CandidateKnowledgeChunk] = []

        if internal_ids:
            internal_chunks = (
                DocumentKnowledgeChunk.objects.select_related(
                    "document",
                    "document__owner",
                )
                .filter(
                    id__in=internal_ids,
                    document__institution=document.institution,
                    is_active=True,
                )
                .exclude(document=document)
                # Un alumno nunca debe aparecer marcado por similitud contra
                # su propio trabajo anterior (borradores, versiones
                # corregidas, entregas previas). Solo aplica al corpus
                # interno: los candidatos OAI son siempre fuentes externas.
                .exclude(document__owner=document.owner)
            )

            for chunk in internal_chunks:
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

        oai_records_by_id: dict[UUID, OaiRecord] = {}

        if oai_ids:
            oai_records = OaiRecord.objects.filter(
                id__in=oai_ids,
                is_active=True,
            )

            for record in oai_records:
                oai_records_by_id[record.id] = record

                candidates.append(
                    CandidateKnowledgeChunk(
                        document_id=record.id,
                        title=record.title,
                        owner_name=record.repository_name,
                        text_excerpt=record.text_excerpt,
                        normalized_text=record.normalized_text,
                    )
                )

        return candidates, oai_records_by_id

    def _save_report(
        self,
        document: Document,
        job: AnalysisJob,
        total_similarity_percent: Decimal,
        internal_similarity_percent: Decimal,
        web_similarity_percent: Decimal,
        ai_probability_percent: Decimal,
        ai_detector: str,
        excluded_sections: list[str],
        analyzed_content: str,
        ai_score_breakdown: dict | None = None,
        exclude_quotes: bool = True,
        exclude_bibliography: bool = True,
        quoted_ranges: list[tuple[int, int]] | None = None,
        similarity: SimilarityRunResult | None = None,
    ) -> AnalysisReport:
        risk_level = self._resolve_risk_level(
            similarity_percent=total_similarity_percent,
        )

        report, _ = AnalysisReport.objects.update_or_create(
            document=document,
            defaults={
                "analysis_job": job,
                "similarity_percent": total_similarity_percent,
                "web_similarity_percent": web_similarity_percent,
                "internal_similarity_percent": internal_similarity_percent,
                "ai_probability_percent": ai_probability_percent,
                "ai_detector": ai_detector,
                "risk_level": risk_level,
                "analyzed_content": analyzed_content,
                "exclude_quotes": exclude_quotes,
                "exclude_bibliography": exclude_bibliography,
                "span_alignment": (
                    similarity.span_alignment
                    if similarity and similarity.span_alignment
                    else {}
                ),
                "original_similarity_percent": (
                    total_similarity_percent
                    if similarity and similarity.span_alignment
                    else None
                ),
                "summary": {
                    "engine": "internal-web-academic-v2",
                    "similarity_engine": similarity.engine if similarity else "chunk",
                    "similarity_retrieval": (
                        similarity.retrieval if similarity else "vieja"
                    ),
                    "internal_similarity_algorithm": (
                        similarity.internal_algorithm
                        if similarity
                        else ALGORITHM_CHUNK_INTERNAL
                    ),
                    "web_similarity_algorithm": (
                        similarity.web_algorithm if similarity else ALGORITHM_CHUNK_WEB
                    ),
                    "ai_score_breakdown": ai_score_breakdown or {},
                    "excluded_sections": excluded_sections,
                    # Rangos de `analyzed_content` descontados del %: el
                    # visor y el PDF no pintan similitud dentro de ellos.
                    "excluded_quote_ranges": [
                        [start, end] for start, end in quoted_ranges or []
                    ],
                    "note": "La similitud web depende de fuentes públicas encontradas y accesibles.",
                },
                "engine_version": "vql-web-academic-v2.1.0",
                "generated_at": timezone.now(),
                "is_final": True,
            },
        )

        return report

    def _replace_internal_similarity_findings(
        self,
        report: AnalysisReport,
        matches: list,
        oai_records_by_id: dict[UUID, OaiRecord] | None = None,
        algorithm: str = ALGORITHM_CHUNK_INTERNAL,
    ) -> None:
        oai_records_by_id = oai_records_by_id or {}

        ReportFinding.objects.filter(
            report=report,
            finding_type=FindingType.SIMILARITY,
            source__source_type=SourceType.INTERNAL,
        ).delete()

        ReportSource.objects.filter(
            report=report,
            source_type=SourceType.INTERNAL,
        ).delete()

        # El motor de similitud compara fragmentos solapados del documento
        # actual (misma fuente puede quedar como "best_match" de varios
        # fragmentos consecutivos), así que agrupamos por fuente antes de
        # crear ReportSource para no duplicar la misma fuente en la lista.
        matches_by_source_id: dict[UUID, list] = {}

        for match in matches:
            matches_by_source_id.setdefault(
                match.source_document_id, []
            ).append(match)

        for source_document_id, source_matches in matches_by_source_id.items():
            representative = max(
                source_matches,
                key=lambda item: item.matched_percent,
            )
            oai_record = oai_records_by_id.get(source_document_id)

            if oai_record is not None:
                domain = oai_record.repository_name
                url = oai_record.source_url
                source_metadata = {
                    "source": "oai",
                    "oai_identifier": oai_record.oai_identifier,
                    "repository_name": oai_record.repository_name,
                }
                finding_source = "oai"
            else:
                domain = "Repositorio interno"
                url = ""
                source_metadata = {
                    "source_document_id": str(source_document_id),
                    "source_owner": representative.source_owner_name,
                }
                finding_source = "internal"

            source = ReportSource.objects.create(
                report=report,
                source_type=SourceType.INTERNAL,
                title=representative.source_title,
                url=url,
                domain=domain,
                matched_percent=representative.matched_percent,
                snippet=representative.source_excerpt,
                metadata=source_metadata,
            )

            for match in source_matches:
                ReportFinding.objects.create(
                    report=report,
                    source=source,
                    finding_type=FindingType.SIMILARITY,
                    start_offset=match.start_offset,
                    end_offset=match.end_offset,
                    text_excerpt=match.text_excerpt,
                    confidence_percent=match.matched_percent,
                    metadata={
                        "algorithm": algorithm,
                        "source": finding_source,
                        "source_document_id": str(source_document_id),
                        **self._match_type_metadata(match),
                    },
                )

    def _replace_web_similarity_findings(
        self,
        report: AnalysisReport,
        web_result: WebSimilarityAnalysisResult,
        algorithm: str = ALGORITHM_CHUNK_WEB,
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

        # Mismo motivo que en el similitud interna: fragmentos solapados del
        # documento actual pueden compartir la misma página web como mejor
        # candidato, así que agrupamos por URL antes de crear ReportSource.
        matches_by_url: dict[str, list] = {}

        for match in web_result.matches:
            matches_by_url.setdefault(match.url, []).append(match)

        for url, url_matches in matches_by_url.items():
            representative = max(
                url_matches,
                key=lambda item: item.matched_percent,
            )

            source = ReportSource.objects.create(
                report=report,
                source_type=SourceType.WEB,
                title=representative.title or representative.domain,
                url=url,
                domain=representative.domain,
                matched_percent=representative.matched_percent,
                snippet=representative.source_excerpt,
                metadata={
                    "source": "web",
                    "url": url,
                },
            )

            for match in url_matches:
                ReportFinding.objects.create(
                    report=report,
                    source=source,
                    finding_type=FindingType.SIMILARITY,
                    start_offset=match.start_offset,
                    end_offset=match.end_offset,
                    text_excerpt=match.text_excerpt,
                    confidence_percent=match.matched_percent,
                    metadata={
                        "algorithm": algorithm,
                        "source": "web",
                        "url": url,
                        **self._match_type_metadata(match),
                    },
                )

    def _match_type_metadata(self, match) -> dict[str, str]:
        if not match.match_type:
            return {}

        return {
            "match_type": match.match_type,
            "match_type_label": MATCH_TYPE_LABELS.get(
                match.match_type,
                match.match_type,
            ),
        }

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
        internal_ranges: Iterable[tuple[int, int]],
        web_ranges: Iterable[tuple[int, int]],
        excluded_ranges: Iterable[tuple[int, int]] = (),
    ) -> Decimal:
        # Recibe el conjunto COMPLETO de rangos coincidentes (todos los que
        # superaron el umbral en cada motor), no la lista `.matches` ya
        # truncada a MAX_MATCHES. El truncado solo aplica a la persistencia
        # del detalle (ReportSource/ReportFinding), nunca a este número
        # principal que define `risk_level`. Las citas excluidas no cuentan
        # ni en el numerador ni en el denominador.
        ranges: list[tuple[int, int]] = list(internal_ranges)
        ranges.extend(web_ranges)

        return calculate_similarity_percent(
            content_length=content_length,
            matched_ranges=ranges,
            excluded_ranges=excluded_ranges,
        )

    def _resolve_risk_level(
        self,
        similarity_percent: Decimal,
    ) -> str:
        return resolve_risk_level(
            similarity_percent=similarity_percent,
        )