from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from apps.accounts.models import User
from apps.analysis.engines.span_similarity import (
    ALGORITHM_SPAN_INTERNAL,
    ALGORITHM_SPAN_WEB,
    MATCH_TYPE_LABELS,
    SOURCE_KIND_INTERNAL,
    SOURCE_KIND_OAI,
    SOURCE_KIND_WEB,
    SpanAlignmentError,
    SpanAnalysisResult,
    SpanMatch,
    SpanSimilarityEngine,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.certificates.models import Certificate
from apps.documents.models import Document
from apps.reports.models import (
    AI_ESTIMATE_DISCLAIMER,
    AnalysisReport,
    FindingType,
    ReportFinding,
    ReportRiskLevel,
    ReportSource,
    SourceType,
)

logger = logging.getLogger(__name__)


def resolve_risk_level(similarity_percent: Decimal) -> str:
    """
    El riesgo (y con él el bloqueo del certificado) depende solo de la
    similitud. El % de IA es una estimación indicativa, no concluyente, y se
    muestra aparte para revisión del docente sin afectar este nivel.
    """
    if similarity_percent >= Decimal("50.00"):
        return ReportRiskLevel.CRITICAL

    if similarity_percent >= Decimal("30.00"):
        return ReportRiskLevel.HIGH

    if similarity_percent >= Decimal("15.00"):
        return ReportRiskLevel.MEDIUM

    return ReportRiskLevel.LOW


INTERNAL_SOURCE_KINDS = (SOURCE_KIND_INTERNAL, SOURCE_KIND_OAI)

# Una fuente interna que cubre al menos este % del documento (o que tiene el
# mismo sha256) se marca como posible reenvío del mismo trabajo.
RESUBMISSION_COVERAGE_PERCENT = Decimal("95.00")


def resubmission_warning_sources(sources: list[ReportSource]) -> list[ReportSource]:
    """
    Fuentes que disparan el aviso de reenvío arriba del reporte: las no
    excluidas marcadas como posible reenvío y, siempre, las que tienen el
    mismo sha256 que el documento (aunque el docente las haya excluido).
    """
    return [
        source
        for source in sources
        if source.is_possible_resubmission
        and (not source.is_excluded or source.resubmission.get("same_hash"))
    ]


class SpanReportWriter:
    """
    Persiste las fuentes y hallazgos de similitud de un resultado del motor
    span. Lo usan el análisis (`reset=True`: reemplaza todo, incluidas las
    exclusiones de un análisis anterior) y el recálculo tras excluir o
    restaurar una fuente (`reset=False`: conserva las filas excluidas y la
    identidad de las demás por `source_key`).
    """

    # Tope de tramos que se persisten como ReportFinding por tipo (interno /
    # web); el % se calcula siempre sobre todos.
    MAX_PERSISTED_MATCHES = 200

    def write(
        self,
        report: AnalysisReport,
        result: SpanAnalysisResult,
        reset: bool,
    ) -> None:
        alignment = report.span_alignment or {}
        details_by_key = {
            item["key"]: item.get("details", {})
            for item in alignment.get("sources", [])
        }
        countable_word_count = alignment.get("countable_word_count", 0)

        report.findings.filter(finding_type=FindingType.SIMILARITY).delete()

        if reset:
            report.sources.all().delete()
            existing: dict[str, ReportSource] = {}
        else:
            existing = {source.source_key: source for source in report.sources.all()}

        rows: dict[str, ReportSource] = {}

        for item in result.sources:
            source = item.source
            row = existing.get(source.key) or ReportSource(
                report=report,
                source_key=source.key,
            )
            details = details_by_key.get(source.key, {})

            row.source_type = (
                SourceType.WEB if source.kind == SOURCE_KIND_WEB else SourceType.INTERNAL
            )
            row.title = (source.title or source.domain or "Fuente")[:300]
            row.url = source.url[:1000]
            row.domain = source.domain[:180]
            row.matched_percent = item.percent
            row.word_count = item.word_count
            row.metadata = self._source_metadata(
                kind=source.kind,
                source_id=source.source_id,
                owner_name=source.owner_name,
                url=source.url,
                details=details,
                countable_word_count=countable_word_count,
            )
            row.save()
            rows[source.key] = row

        for key, row in existing.items():
            if key not in rows and not row.is_excluded:
                row.delete()

        snippets: dict[str, str] = {}

        for kinds, algorithm in (
            (INTERNAL_SOURCE_KINDS, ALGORITHM_SPAN_INTERNAL),
            ((SOURCE_KIND_WEB,), ALGORITHM_SPAN_WEB),
        ):
            matches = result.matches_for(kinds)

            # El extracto de la fuente es el de su primer tramo en el texto.
            for match in matches:
                snippets.setdefault(match.source.key, match.source_excerpt)

            ranked = sorted(
                matches,
                key=lambda match: (-match.source_percent, match.start_offset),
            )[: self.MAX_PERSISTED_MATCHES]

            ReportFinding.objects.bulk_create(
                [
                    self._finding(
                        report=report,
                        row=rows[match.source.key],
                        match=match,
                        algorithm=algorithm,
                    )
                    for match in ranked
                ]
            )

        for key, row in rows.items():
            snippet = snippets.get(key, "")

            if row.snippet != snippet:
                row.snippet = snippet
                row.save(update_fields=["snippet", "updated_at"])

        report.similarity_percent = result.similarity_percent
        report.internal_similarity_percent = result.percent_for(INTERNAL_SOURCE_KINDS)
        report.web_similarity_percent = result.percent_for((SOURCE_KIND_WEB,))
        report.risk_level = resolve_risk_level(
            similarity_percent=report.similarity_percent,
        )
        report.save(
            update_fields=[
                "similarity_percent",
                "internal_similarity_percent",
                "web_similarity_percent",
                "risk_level",
                "updated_at",
            ]
        )

    def _source_metadata(
        self,
        kind: str,
        source_id: UUID | None,
        owner_name: str,
        url: str,
        details: dict,
        countable_word_count: int,
    ) -> dict:
        if kind == SOURCE_KIND_WEB:
            return {"source": "web", "url": url}

        if kind == SOURCE_KIND_OAI:
            return {
                "source": "oai",
                "oai_identifier": details.get("oai_identifier", ""),
                "repository_name": details.get("repository_name", ""),
            }

        metadata: dict[str, Any] = {
            "source_document_id": str(source_id) if source_id else "",
            "source_owner": owner_name,
        }
        resubmission = self._resubmission(
            details=details,
            countable_word_count=countable_word_count,
        )

        if resubmission:
            metadata["resubmission"] = resubmission

        return metadata

    def _resubmission(self, details: dict, countable_word_count: int) -> dict:
        coverage_percent = (
            (
                Decimal(details.get("coverage_word_count", 0))
                / Decimal(countable_word_count)
                * Decimal("100")
            ).quantize(Decimal("0.01"))
            if countable_word_count
            else Decimal("0.00")
        )
        same_hash = bool(details.get("same_hash"))

        if not same_hash and coverage_percent < RESUBMISSION_COVERAGE_PERCENT:
            return {}

        return {
            "same_hash": same_hash,
            "coverage_percent": str(min(coverage_percent, Decimal("100.00"))),
            "original_owner": details.get("original_owner", ""),
            "original_created_at": details.get("original_created_at", ""),
        }

    def _finding(
        self,
        report: AnalysisReport,
        row: ReportSource,
        match: SpanMatch,
        algorithm: str,
    ) -> ReportFinding:
        source = match.source

        if source.kind == SOURCE_KIND_WEB:
            origin = {"source": "web", "url": source.url}
        else:
            origin = {
                "source": source.kind,
                "source_document_id": str(source.source_id or ""),
            }

        return ReportFinding(
            report=report,
            source=row,
            finding_type=FindingType.SIMILARITY,
            start_offset=match.start_offset,
            end_offset=match.end_offset,
            text_excerpt=match.text_excerpt,
            confidence_percent=match.source_percent,
            metadata={
                "algorithm": algorithm,
                **origin,
                "word_count": match.word_count,
                "match_type": match.match_type,
                "match_type_label": MATCH_TYPE_LABELS.get(
                    match.match_type,
                    match.match_type,
                ),
            },
        )


class ReportSourceExclusionError(Exception):
    """
    Error controlado al excluir o restaurar una fuente del reporte.
    """


class ReportSourceExclusionPermissionError(ReportSourceExclusionError):
    pass


CERTIFICATE_REVOKE_REASON = "Reporte recalculado por exclusión de fuentes"


class ReportSourceExclusionService:
    """
    Excluye o restaura una fuente del reporte (como "excluir fuente" de
    Turnitin) y recalcula el % total, interno y web con la misma regla del
    motor span: se vuelve a repartir cada palabra entre las fuentes no
    excluidas a partir de la alineación guardada en el reporte, así que las
    palabras de la fuente excluida pasan a la siguiente fuente que las
    cubría. No se re-extrae el texto ni se consulta internet.

    Al cambiar el %, el certificado emitido queda revocado y el docente
    debe volver a emitirlo.
    """

    REASON_MAX_LENGTH = 255

    def __init__(self, requested_by: User) -> None:
        self.requested_by = requested_by

    @staticmethod
    def can_manage(user: User, document: Document) -> bool:
        if not user.is_authenticated or user.is_student_role:
            return False

        if user.is_superuser:
            return True

        if not (user.is_admin_role or user.is_director_role or user.is_teacher_role):
            return False

        return (
            user.institution_id is not None
            and user.institution_id == document.institution_id
        )

    def exclude(
        self,
        document_id: UUID,
        source_id: UUID,
        reason: str,
    ) -> ReportSource:
        reason = (reason or "").strip()

        if not reason:
            raise ReportSourceExclusionError(
                "Indica el motivo para excluir la fuente."
            )

        if len(reason) > self.REASON_MAX_LENGTH:
            raise ReportSourceExclusionError(
                f"El motivo no puede superar los {self.REASON_MAX_LENGTH} caracteres."
            )

        with transaction.atomic():
            source = self._get_source(document_id=document_id, source_id=source_id)

            if source.is_excluded:
                raise ReportSourceExclusionError("La fuente ya está excluida.")

            source.is_excluded = True
            source.excluded_by = self.requested_by
            source.excluded_at = timezone.now()
            source.exclusion_reason = reason
            source.save(
                update_fields=[
                    "is_excluded",
                    "excluded_by",
                    "excluded_at",
                    "exclusion_reason",
                    "updated_at",
                ]
            )

            self._recalculate(report=source.report)

        logger.info(
            "Fuente excluida del reporte. report_id=%s source_id=%s user_id=%s "
            "total=%s internal=%s web=%s motivo=%r",
            source.report.id,
            source.id,
            self.requested_by.id,
            source.report.similarity_percent,
            source.report.internal_similarity_percent,
            source.report.web_similarity_percent,
            reason,
        )

        return source

    def restore(self, document_id: UUID, source_id: UUID) -> ReportSource:
        with transaction.atomic():
            source = self._get_source(document_id=document_id, source_id=source_id)

            if not source.is_excluded:
                raise ReportSourceExclusionError("La fuente no está excluida.")

            source.is_excluded = False
            source.excluded_by = None
            source.excluded_at = None
            source.exclusion_reason = ""
            source.save(
                update_fields=[
                    "is_excluded",
                    "excluded_by",
                    "excluded_at",
                    "exclusion_reason",
                    "updated_at",
                ]
            )

            self._recalculate(report=source.report)

        logger.info(
            "Fuente restaurada en el reporte. report_id=%s source_id=%s "
            "user_id=%s total=%s internal=%s web=%s",
            source.report.id,
            source.id,
            self.requested_by.id,
            source.report.similarity_percent,
            source.report.internal_similarity_percent,
            source.report.web_similarity_percent,
        )

        return source

    def _get_source(self, document_id: UUID, source_id: UUID) -> ReportSource:
        report = (
            AnalysisReport.objects.select_for_update()
            .select_related("document")
            .filter(document_id=document_id)
            .first()
        )

        if report is None:
            raise ReportSourceExclusionError("Este documento aún no tiene reporte.")

        if not self.can_manage(self.requested_by, report.document):
            raise ReportSourceExclusionPermissionError(
                "No tienes permiso para excluir fuentes de este reporte."
            )

        if not report.allows_source_exclusion:
            raise ReportSourceExclusionError(
                "Vuelve a analizar el documento para poder excluir fuentes."
            )

        source = report.sources.filter(id=source_id).first()

        if source is None:
            raise ReportSourceExclusionError("La fuente no pertenece a este reporte.")

        source.report = report

        return source

    def _recalculate(self, report: AnalysisReport) -> None:
        engine = SpanSimilarityEngine()

        try:
            alignment = engine.alignment_from_json(
                report.span_alignment,
                content=report.analyzed_content,
                excluded_ranges=report.excluded_quote_ranges,
            )
        except SpanAlignmentError as exc:
            logger.warning(
                "Alineación del reporte inválida al recalcular. report_id=%s",
                report.id,
                exc_info=True,
            )
            raise ReportSourceExclusionError(
                "Vuelve a analizar el documento para poder excluir fuentes."
            ) from exc

        excluded_keys = set(
            report.sources.filter(is_excluded=True).values_list(
                "source_key",
                flat=True,
            )
        )
        result = engine.attribute(
            content=report.analyzed_content,
            alignment=alignment,
            excluded_keys=excluded_keys,
        )

        SpanReportWriter().write(report=report, result=result, reset=False)
        self._revoke_certificate(report=report)

    def _revoke_certificate(self, report: AnalysisReport) -> None:
        revoked = Certificate.objects.filter(report=report, is_active=True).update(
            is_active=False,
            revoked_at=timezone.now(),
            revoke_reason=CERTIFICATE_REVOKE_REASON,
            updated_at=timezone.now(),
        )

        if revoked:
            logger.info(
                "Certificado revocado por recálculo del reporte. report_id=%s",
                report.id,
            )


def resolve_analyzed_content(report: AnalysisReport) -> str:
    """
    Texto sobre el que se calcularon los offsets de `report.findings`.

    `DocumentAnalysisService.execute` persiste este mismo texto filtrado en
    `AnalysisReport.analyzed_content`, así que el visor y el PDF señalado
    deben pintar los resaltados sobre ÉL, nunca sobre
    `document.extracted_text.content` (crudo): los offsets no corresponden
    a ese texto crudo (portada/índice/bibliografía ya fueron recortados
    antes de analizar), y pintarlos ahí desalinea los resaltados
    (auditoría, Hallazgo 1).

    Para reportes generados ANTES de que existiera esta columna,
    `analyzed_content` viene vacío: se reconstruye aplicando el filtro
    ACTUAL sobre el texto crudo. No es una garantía perfecta si el filtro
    cambió entre medio (offsets viejos podrían no calzar contra un
    filtrado distinto), pero sigue siendo mejor que pintar sobre el crudo,
    y evita que el visor reviente para reportes antiguos.
    """
    if report.analyzed_content:
        return report.analyzed_content

    extracted_text = getattr(report.document, "extracted_text", None)

    if extracted_text is None or not extracted_text.content:
        return ""

    filtered = AcademicTextFilter().filter_for_similarity(
        content=extracted_text.content,
        exclude_bibliography=report.exclude_bibliography,
    )

    return filtered.content or extracted_text.content


def split_similarity_around_exclusions(
    start: int,
    end: int,
    excluded_ranges: list[tuple[int, int]],
) -> list[tuple[int, int, bool]]:
    """
    Parte el tramo [start, end) de un hallazgo de similitud en piezas
    (inicio, fin, pintar): las que caen dentro de una cita excluida no se
    pintan, porque no sumaron al % de similitud.
    """
    pieces: list[tuple[int, int, bool]] = []
    cursor = start

    for excluded_start, excluded_end in excluded_ranges:
        if excluded_end <= cursor:
            continue

        if excluded_start >= end:
            break

        if excluded_start > cursor:
            pieces.append((cursor, excluded_start, True))

        piece_end = min(excluded_end, end)
        pieces.append((max(cursor, excluded_start), piece_end, False))
        cursor = piece_end

    if cursor < end:
        pieces.append((cursor, end, True))

    return pieces


# Tipo de segmento del visor y del PDF: no es un hallazgo (`FindingType`).
COMMON_TEXT_SEGMENT = "COMMON_TEXT"


def split_plain_text_around_common(
    start: int,
    end: int,
    common_ranges: list[tuple[int, int]],
) -> list[tuple[int, int, bool]]:
    """
    Parte un tramo sin hallazgos en piezas (inicio, fin, es texto común).
    """
    return [
        (piece_start, piece_end, not outside)
        for piece_start, piece_end, outside in split_similarity_around_exclusions(
            start=start,
            end=end,
            excluded_ranges=common_ranges,
        )
    ]


class HighlightedDocumentPdfError(Exception):
    """
    Error controlado al generar el PDF del documento señalado.
    """

    pass


def build_highlighted_document_pdf(report: AnalysisReport) -> bytes:
    """
    Genera un PDF descargable del texto del documento con los mismos
    hallazgos resaltados que el visor interactivo de `reports/detail.html`
    (similitud según `report.similarity_level`, IA en azul), reemplazando
    el hover del visor web por notas al pie numeradas hacia una sección
    "Fuentes citadas".
    """
    return _HighlightedDocumentPdfBuilder(report=report).build()


class _HighlightedDocumentPdfBuilder:
    # Mismos colores dinámicos que templates/reports/detail.html según
    # report.similarity_level (bajo/moderado/alto). Los hallazgos de IA
    # siempre usan el azul institucional, igual que en el visor web.
    SIMILARITY_COLORS = {
        "alto": ("#FEE2E2", "#7F1D1D"),
        "moderado": ("#FFF3CF", "#8A6100"),
        "bajo": ("#D1FAE5", "#064E3B"),
    }
    AI_BACKGROUND = "#E3ECFF"
    AI_TEXT_COLOR = "#123F9E"
    COMMON_TEXT_BACKGROUND = "#E2E8F0"
    COMMON_TEXT_COLOR = "#475569"

    # Colores para numerar las fuentes primarias (chip en el texto + fila en
    # la lista), al estilo de un informe de originalidad.
    SOURCE_PALETTE = [
        "#E2483D",
        "#D6249F",
        "#7A3FF2",
        "#0E9E96",
        "#3F9C35",
        "#9A6B1E",
        "#7A241D",
        "#1D3F9E",
        "#B0208A",
        "#0F766E",
    ]

    SOURCE_TYPE_LABELS = {
        SourceType.WEB: "Internet",
        SourceType.REPOSITORY: "Repositorio académico",
        SourceType.INTERNAL: "Base interna",
    }

    def __init__(self, report: AnalysisReport) -> None:
        self.report = report
        self.document = report.document

    def _source_color(self, number: int) -> str:
        return self.SOURCE_PALETTE[(number - 1) % len(self.SOURCE_PALETTE)]

    def _source_type_label(self, source: ReportSource) -> str:
        return self.SOURCE_TYPE_LABELS.get(source.source_type, "Fuente")

    def build(self) -> bytes:
        content = resolve_analyzed_content(self.report)

        if not content:
            raise HighlightedDocumentPdfError(
                "El documento no tiene texto extraído para generar el PDF señalado."
            )

        buffer = BytesIO()
        institution = self.document.institution

        self._prepare_letterhead(institution=institution)

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=1.7 * cm,
            leftMargin=1.7 * cm,
            topMargin=1.8 * cm,
            bottomMargin=1.5 * cm,
            title=f"Informe de originalidad - {self.document.title}",
            author=institution.name,
        )

        styles = self._build_styles()
        story: list[Any] = []

        story.append(Paragraph(self._escape(self.document.title), styles["title"]))
        story.append(Spacer(1, 0.15 * cm))
        story.append(Paragraph("INFORME DE ORIGINALIDAD", styles["kicker"]))
        story.append(Spacer(1, 0.35 * cm))

        story.append(self._build_metrics_row(styles=styles))
        story.append(Spacer(1, 0.15 * cm))
        story.append(
            Paragraph(
                f"<b>IA estimada:</b> {AI_ESTIMATE_DISCLAIMER}",
                styles["legend_note"],
            )
        )
        story.append(Spacer(1, 0.4 * cm))

        findings = list(
            self.report.findings.select_related("source").order_by(
                "start_offset",
            )
        )
        sources_by_id = {source.id: source for source in self.report.sources.all()}

        body_markup, cited_sources, word_counts = self._render_body_markup(
            content=content,
            findings=findings,
            sources_by_id=sources_by_id,
        )

        all_sources = list(sources_by_id.values())
        resubmissions = resubmission_warning_sources(all_sources)

        if resubmissions:
            story.extend(self._build_resubmission_warning(resubmissions, styles))
            story.append(Spacer(1, 0.4 * cm))

        if cited_sources:
            story.extend(
                self._build_primary_sources(
                    cited_sources=cited_sources,
                    word_counts=word_counts,
                    styles=styles,
                )
            )
            story.append(Spacer(1, 0.5 * cm))

        excluded_sources = [source for source in all_sources if source.is_excluded]

        if excluded_sources:
            story.extend(self._build_excluded_sources(excluded_sources, styles))
            story.append(Spacer(1, 0.5 * cm))

        story.append(self._build_legend(styles=styles))
        story.append(Spacer(1, 0.35 * cm))
        story.append(Paragraph("TEXTO ANALIZADO", styles["kicker"]))
        story.append(Spacer(1, 0.2 * cm))
        story.append(Paragraph(body_markup, styles["body"]))

        doc.build(
            story,
            onFirstPage=self._decorate_page,
            onLaterPages=self._decorate_page,
        )

        return buffer.getvalue()

    def _render_body_markup(
        self,
        content: str,
        findings: list[ReportFinding],
        sources_by_id: dict[UUID, ReportSource],
    ) -> tuple[str, list[tuple[int, ReportSource]], dict[UUID, int]]:
        segments = self._build_segments(content=content, findings=findings)

        similarity_bg, similarity_text = self.SIMILARITY_COLORS.get(
            self.report.similarity_level,
            self.SIMILARITY_COLORS["bajo"],
        )

        source_numbers: dict[UUID, int] = {}
        cited_sources: list[tuple[int, ReportSource]] = []
        word_counts: dict[UUID, int] = {}
        parts: list[str] = []

        for text, finding_type, source_id in segments:
            escaped = self._escape(text)

            if (
                finding_type == FindingType.SIMILARITY
                and source_id is not None
                and source_id in sources_by_id
            ):
                number = source_numbers.get(source_id)

                if number is None:
                    number = len(source_numbers) + 1
                    source_numbers[source_id] = number
                    cited_sources.append((number, sources_by_id[source_id]))

                word_counts[source_id] = word_counts.get(source_id, 0) + len(
                    text.split()
                )

                chip_color = self._source_color(number)
                parts.append(
                    f'<span backColor="{similarity_bg}" color="{similarity_text}">'
                    f"{escaped}</span>"
                    f'<super><font size="6"> '
                    f'<span backColor="{chip_color}" color="#FFFFFF">'
                    f"&nbsp;{number}&nbsp;</span></font></super>"
                )

            elif finding_type == FindingType.AI_GENERATED:
                parts.append(
                    f'<span backColor="{self.AI_BACKGROUND}" color="{self.AI_TEXT_COLOR}">'
                    f"{escaped}</span>"
                )

            elif finding_type == COMMON_TEXT_SEGMENT:
                parts.append(
                    f'<span backColor="{self.COMMON_TEXT_BACKGROUND}" '
                    f'color="{self.COMMON_TEXT_COLOR}">{escaped}</span>'
                )

            else:
                parts.append(escaped)

        return "".join(parts), cited_sources, word_counts

    def _build_segments(
        self,
        content: str,
        findings: list[ReportFinding],
    ) -> list[tuple[str, str | None, UUID | None]]:
        """
        Misma lógica de `ReportDetailView._build_highlight_segments`
        (apps/reports/views.py): recorre el texto en orden, respeta el
        primer hallazgo válido en caso de solapamiento, y deja el resto
        como texto plano. Aquí además se conserva el `source_id` de cada
        hallazgo para poder numerarlo como nota al pie.
        """
        if not content:
            return []

        excluded_ranges = self.report.excluded_quote_ranges
        common_ranges = self.report.common_text_ranges

        def plain(start: int, end: int) -> list[tuple[str, str | None, None]]:
            return [
                (
                    content[piece_start:piece_end],
                    COMMON_TEXT_SEGMENT if common else None,
                    None,
                )
                for piece_start, piece_end, common in split_plain_text_around_common(
                    start=start,
                    end=end,
                    common_ranges=common_ranges,
                )
            ]

        valid_findings = sorted(
            [
                finding
                for finding in findings
                if finding.start_offset < finding.end_offset
                and finding.start_offset < len(content)
            ],
            key=lambda item: item.start_offset,
        )

        segments: list[tuple[str, str | None, UUID | None]] = []
        cursor = 0

        for finding in valid_findings:
            start = max(finding.start_offset, 0)
            end = min(finding.end_offset, len(content))

            if start < cursor:
                continue

            if cursor < start:
                segments.extend(plain(cursor, start))

            if finding.finding_type == FindingType.SIMILARITY:
                for piece_start, piece_end, painted in (
                    split_similarity_around_exclusions(
                        start=start,
                        end=end,
                        excluded_ranges=excluded_ranges,
                    )
                ):
                    segments.append(
                        (
                            content[piece_start:piece_end],
                            finding.finding_type if painted else None,
                            finding.source_id if painted else None,
                        )
                    )
            else:
                segments.append(
                    (
                        content[start:end],
                        finding.finding_type,
                        finding.source_id,
                    )
                )

            cursor = end

        if cursor < len(content):
            segments.extend(plain(cursor, len(content)))

        return segments

    def _escape(self, text: str) -> str:
        escaped = html.escape(text, quote=False)
        return escaped.replace("\n", "<br/>\n")

    def _build_metrics_row(self, styles: dict[str, ParagraphStyle]) -> Table:
        owner = self.document.owner

        def cell(value: Decimal, label: str, accent: bool = False) -> Paragraph:
            number_style = styles["metric_number_accent" if accent else "metric_number"]
            return Paragraph(
                f'<font size="20"><b>{self._format_percent(value)}</b></font>'
                f'<font size="9">%</font><br/>'
                f'<font size="7.5" color="#64748B">{label}</font>',
                number_style,
            )

        metrics = Table(
            [
                [
                    cell(self.report.similarity_percent, "ÍNDICE DE SIMILITUD", accent=True),
                    cell(self.report.web_similarity_percent, "FUENTES DE INTERNET"),
                    cell(self.report.internal_similarity_percent, "TRABAJOS / REPOSITORIOS"),
                    cell(self.report.ai_probability_percent, "IA ESTIMADA"),
                ]
            ],
            colWidths=[4.15 * cm] * 4,
        )
        metrics.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#0F172A")),
                    ("LINEABOVE", (0, 0), (-1, -1), 1.2, colors.HexColor("#0F172A")),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )

        header = Table(
            [
                [
                    Paragraph(
                        f"<b>Autor:</b> {self._escape(owner.get_full_name() or owner.username)}"
                        f" &nbsp;·&nbsp; <b>Tipo:</b> {self.document.get_kind_display()}"
                        f" &nbsp;·&nbsp; <b>Riesgo:</b> {self.report.get_risk_level_display()}",
                        styles["small"],
                    )
                ]
            ],
            colWidths=[16.6 * cm],
        )
        header.setStyle(
            TableStyle(
                [
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )

        wrapper = Table([[header], [metrics]], colWidths=[16.6 * cm])
        wrapper.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return wrapper

    def _build_legend(self, styles: dict[str, ParagraphStyle]) -> Table:
        similarity_bg, similarity_text = self.SIMILARITY_COLORS.get(
            self.report.similarity_level,
            self.SIMILARITY_COLORS["bajo"],
        )

        return Table(
            [
                [
                    Paragraph(
                        f'<span backColor="{similarity_bg}" color="{similarity_text}">'
                        "&nbsp;Similitud&nbsp;</span>",
                        styles["legend"],
                    ),
                    Paragraph(
                        f'<span backColor="{self.AI_BACKGROUND}" color="{self.AI_TEXT_COLOR}">'
                        "&nbsp;Posible IA&nbsp;</span>",
                        styles["legend"],
                    ),
                    Paragraph(
                        "Los números junto a cada pasaje remiten a la lista "
                        "«Fuentes primarias»."
                        + (
                            f" Excluidas: {self.report.excluded_options_label}."
                            if self.report.excluded_options_label
                            else ""
                        )
                        + (
                            " En gris: texto común, no cuenta en el %."
                            if self.report.common_text_ranges
                            else ""
                        ),
                        styles["legend_note"],
                    ),
                ]
            ],
            colWidths=[3.4 * cm, 3.4 * cm, 9.8 * cm],
        )

    def _build_primary_sources(
        self,
        cited_sources: list[tuple[int, ReportSource]],
        word_counts: dict[UUID, int],
        styles: dict[str, ParagraphStyle],
    ) -> list[Any]:
        section: list[Any] = [
            Paragraph("FUENTES PRIMARIAS", styles["kicker"]),
            Spacer(1, 0.2 * cm),
        ]

        rows: list[list[Any]] = []
        row_styles: list[tuple] = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
            ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.HexColor("#0F172A")),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
        ]

        for index, (number, source) in enumerate(cited_sources):
            color = self._source_color(number)
            row_styles.append(
                ("BACKGROUND", (0, index), (0, index), colors.HexColor(color))
            )

            # Con el motor span la fuente guarda sus palabras exactas; en
            # reportes anteriores se aproximan contando las resaltadas.
            words = source.word_count or word_counts.get(source.id, 0)
            label = source.domain or self._truncate(source.title, 60) or "Fuente"
            badge = (
                '<br/><font size="7" color="#B91C1C"><b>Posible reenvío del mismo '
                "trabajo</b></font>"
                if source.is_possible_resubmission
                else ""
            )

            rows.append(
                [
                    Paragraph(f'<font color="#FFFFFF"><b>{number}</b></font>', styles["source_num"]),
                    Paragraph(
                        f'<font color="{color}"><b>{self._escape(label)}</b></font><br/>'
                        f'<font size="7" color="#64748B">{self._source_type_label(source)}</font>'
                        f"{badge}",
                        styles["source_name"],
                    ),
                    Paragraph(
                        f'{words} palabra{"" if words == 1 else "s"} &nbsp;—&nbsp; '
                        f'<font size="11"><b>{self._format_percent(source.matched_percent)}%</b></font>',
                        styles["source_meta"],
                    ),
                ]
            )

        table = Table(rows, colWidths=[0.8 * cm, 11.3 * cm, 4.5 * cm])
        table.setStyle(TableStyle(row_styles))
        section.append(table)

        return section

    def _build_resubmission_warning(
        self,
        sources: list[ReportSource],
        styles: dict[str, ParagraphStyle],
    ) -> list[Any]:
        lines = []

        for source in sources:
            data = source.resubmission
            reason = (
                "archivo idéntico"
                if data.get("same_hash")
                else f"cubre el {data.get('coverage_percent', '')}% del documento"
            )
            uploaded = (
                f", subido el {data['original_created_at'][:10]}"
                if data.get("original_created_at")
                else ""
            )
            excluded = "; fuente excluida" if source.is_excluded else ""
            lines.append(
                f"• «{self._escape(self._truncate(source.title, 80))}» de "
                f"<b>{self._escape(data.get('original_owner') or 'otro usuario')}</b>"
                f"{uploaded} ({reason}{excluded})."
            )

        table = Table(
            [
                [
                    Paragraph(
                        '<font color="#B91C1C"><b>POSIBLE REENVÍO DEL MISMO TRABAJO</b></font><br/>'
                        + "<br/>".join(lines)
                        + '<br/><font color="#64748B">La fuente no se excluye '
                        "automáticamente: decide el docente.</font>",
                        styles["small"],
                    )
                ]
            ],
            colWidths=[16.6 * cm],
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF2F2")),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#FCA5A5")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )

        return [table]

    def _build_excluded_sources(
        self,
        sources: list[ReportSource],
        styles: dict[str, ParagraphStyle],
    ) -> list[Any]:
        section: list[Any] = [
            Paragraph(f"FUENTES EXCLUIDAS: {len(sources)}", styles["kicker"]),
            Spacer(1, 0.2 * cm),
        ]

        for source in sources:
            badge = (
                ' &nbsp;<font color="#B91C1C"><b>Posible reenvío del mismo trabajo</b></font>'
                if source.is_possible_resubmission
                else ""
            )
            section.append(
                Paragraph(
                    f"<b>{self._escape(self._truncate(source.title, 90))}</b>"
                    f" ({self._format_percent(source.matched_percent)}% antes de "
                    f"excluir){badge}<br/>"
                    f'<font color="#64748B">Motivo: '
                    f"{self._escape(source.exclusion_reason or '—')}</font>",
                    styles["small"],
                )
            )
            section.append(Spacer(1, 0.12 * cm))

        if self.report.original_similarity_percent is not None:
            section.append(
                Paragraph(
                    f'<font color="#64748B">Similitud original antes de excluir: '
                    f"{self._format_percent(self.report.original_similarity_percent)}%."
                    "</font>",
                    styles["small"],
                )
            )

        return section

    def _truncate(self, value: str, max_length: int) -> str:
        value = value or ""

        if len(value) <= max_length:
            return value

        return f"{value[: max_length - 1].rstrip()}…"

    def _format_decimal(self, value: Decimal) -> str:
        return f"{value:.2f}"

    def _format_percent(self, value: Decimal) -> str:
        return f"{value:.0f}"

    def _build_styles(self) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()

        return {
            "title": ParagraphStyle(
                "hd_title",
                parent=base["Title"],
                fontName="Helvetica-Bold",
                fontSize=15,
                leading=19,
                alignment=TA_LEFT,
                textColor=colors.HexColor("#334155"),
                spaceAfter=2,
            ),
            "kicker": ParagraphStyle(
                "hd_kicker",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=9,
                leading=12,
                textColor=colors.HexColor("#E2483D"),
            ),
            "metric_number": ParagraphStyle(
                "hd_metric_number",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=20,
                leading=22,
                alignment=TA_LEFT,
                textColor=colors.HexColor("#334155"),
            ),
            "metric_number_accent": ParagraphStyle(
                "hd_metric_number_accent",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=20,
                leading=22,
                alignment=TA_LEFT,
                textColor=colors.HexColor("#E2483D"),
            ),
            "source_num": ParagraphStyle(
                "hd_source_num",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=9,
                leading=11,
                alignment=TA_CENTER,
            ),
            "source_name": ParagraphStyle(
                "hd_source_name",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=9,
                leading=12,
            ),
            "source_meta": ParagraphStyle(
                "hd_source_meta",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8.5,
                leading=13,
                alignment=TA_LEFT,
                textColor=colors.HexColor("#334155"),
            ),
            "subtitle": ParagraphStyle(
                "hd_subtitle",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=9,
                leading=12,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#64748B"),
            ),
            "small": ParagraphStyle(
                "hd_small",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=11,
                textColor=colors.HexColor("#334155"),
            ),
            "legend": ParagraphStyle(
                "hd_legend",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=14,
            ),
            "legend_note": ParagraphStyle(
                "hd_legend_note",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=7.5,
                leading=11,
                textColor=colors.HexColor("#64748B"),
            ),
            "body": ParagraphStyle(
                "hd_body",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=9.5,
                leading=15,
                alignment=TA_LEFT,
                textColor=colors.HexColor("#0F172A"),
            ),
            "sources_title": ParagraphStyle(
                "hd_sources_title",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=11,
                leading=14,
                textColor=colors.HexColor("#123F9E"),
            ),
            "sources_header": ParagraphStyle(
                "hd_sources_header",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=11,
                textColor=colors.white,
            ),
            "sources_cell": ParagraphStyle(
                "hd_sources_cell",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=11,
                textColor=colors.HexColor("#334155"),
            ),
            "sources_percent": ParagraphStyle(
                "hd_sources_percent",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=11,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#123F9E"),
            ),
        }

    def _prepare_letterhead(self, institution: Any) -> None:
        self._letterhead_name = institution.name.upper()
        self._letterhead_logo: ImageReader | None = None

        if not institution.logo:
            return

        try:
            institution.logo.open("rb")
            image_bytes = institution.logo.read()
            self._letterhead_logo = ImageReader(BytesIO(image_bytes))
        except Exception:
            logger.warning(
                "No se pudo cargar el logo institucional para el documento "
                "señalado. institution_id=%s",
                institution.id,
                exc_info=True,
            )
        finally:
            institution.logo.close()

    def _decorate_page(self, canvas: Any, doc: Any) -> None:
        width, height = A4
        bar_height = 1.0 * cm

        canvas.saveState()

        canvas.setFillColor(colors.HexColor("#123F9E"))
        canvas.rect(0, height - bar_height, width, bar_height, fill=1, stroke=0)

        canvas.setFillColor(colors.HexColor("#F5B400"))
        canvas.rect(0, height - bar_height - 0.06 * cm, width, 0.06 * cm, fill=1, stroke=0)

        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.rect(0, 0, width, 0.45 * cm, fill=1, stroke=0)

        text_x = 1.7 * cm
        logo = getattr(self, "_letterhead_logo", None)

        if logo is not None:
            logo_size = bar_height - 0.2 * cm
            canvas.drawImage(
                logo,
                1.4 * cm,
                height - bar_height + 0.1 * cm,
                width=logo_size,
                height=logo_size,
                preserveAspectRatio=True,
                mask="auto",
            )
            text_x = 1.4 * cm + logo_size + 0.3 * cm

        institution_name = getattr(self, "_letterhead_name", "INSTITUCIÓN")

        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(
            text_x,
            height - 0.65 * cm,
            f"{institution_name} - INFORME DE ORIGINALIDAD",
        )

        canvas.setFont("Helvetica", 6.5)
        canvas.drawRightString(
            width - 1.7 * cm,
            0.17 * cm,
            f"Pagina {doc.page}",
        )

        canvas.restoreState()
