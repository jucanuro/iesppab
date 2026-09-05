from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Any
from uuid import UUID

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

from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportFinding,
    ReportSource,
)

logger = logging.getLogger(__name__)


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
    )

    return filtered.content or extracted_text.content


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

    def __init__(self, report: AnalysisReport) -> None:
        self.report = report
        self.document = report.document

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
            title=f"Documento señalado - {self.document.title}",
            author=institution.name,
        )

        styles = self._build_styles()
        story: list[Any] = []

        story.append(
            Paragraph(
                "DOCUMENTO CON HALLAZGOS SEÑALADOS",
                styles["title"],
            )
        )
        story.append(Spacer(1, 0.2 * cm))
        story.append(
            Paragraph(
                "Copia técnica del documento con los pasajes señalados por el análisis de originalidad",
                styles["subtitle"],
            )
        )
        story.append(Spacer(1, 0.55 * cm))

        story.append(self._build_info_table(styles=styles))
        story.append(Spacer(1, 0.4 * cm))
        story.append(self._build_legend(styles=styles))
        story.append(Spacer(1, 0.5 * cm))

        findings = list(
            self.report.findings.select_related("source").order_by(
                "start_offset",
            )
        )
        sources_by_id = {source.id: source for source in self.report.sources.all()}

        body_markup, cited_sources = self._render_body_markup(
            content=content,
            findings=findings,
            sources_by_id=sources_by_id,
        )

        story.append(Paragraph(body_markup, styles["body"]))

        if cited_sources:
            story.append(Spacer(1, 0.6 * cm))
            story.extend(
                self._build_sources_section(
                    cited_sources=cited_sources,
                    styles=styles,
                )
            )

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
    ) -> tuple[str, list[tuple[int, ReportSource]]]:
        segments = self._build_segments(content=content, findings=findings)

        similarity_bg, similarity_text = self.SIMILARITY_COLORS.get(
            self.report.similarity_level,
            self.SIMILARITY_COLORS["bajo"],
        )

        source_numbers: dict[UUID, int] = {}
        cited_sources: list[tuple[int, ReportSource]] = []
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

                parts.append(
                    f'<span backColor="{similarity_bg}" color="{similarity_text}">'
                    f"{escaped}</span>"
                    f'<super><font size="6">{number}</font></super>'
                )

            elif finding_type == FindingType.AI_GENERATED:
                parts.append(
                    f'<span backColor="{self.AI_BACKGROUND}" color="{self.AI_TEXT_COLOR}">'
                    f"{escaped}</span>"
                )

            else:
                parts.append(escaped)

        return "".join(parts), cited_sources

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
                segments.append((content[cursor:start], None, None))

            segments.append(
                (
                    content[start:end],
                    finding.finding_type,
                    finding.source_id,
                )
            )

            cursor = end

        if cursor < len(content):
            segments.append((content[cursor:], None, None))

        return segments

    def _escape(self, text: str) -> str:
        escaped = html.escape(text, quote=False)
        return escaped.replace("\n", "<br/>\n")

    def _build_info_table(self, styles: dict[str, ParagraphStyle]) -> Table:
        owner = self.document.owner

        table = Table(
            [
                [
                    Paragraph(
                        f"<b>Documento:</b><br/>{self.document.title}",
                        styles["small"],
                    ),
                    Paragraph(
                        "<b>Autor / Alumno:</b><br/>"
                        f"{owner.get_full_name() or owner.username}",
                        styles["small"],
                    ),
                    Paragraph(
                        "<b>Similitud / IA:</b><br/>"
                        f"{self._format_decimal(self.report.similarity_percent)}% / "
                        f"{self._format_decimal(self.report.ai_probability_percent)}%",
                        styles["small"],
                    ),
                ]
            ],
            colWidths=[7.4 * cm, 5.1 * cm, 4.1 * cm],
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return table

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
                        "Los números en superíndice remiten a la sección "
                        "«Fuentes citadas» al final del documento.",
                        styles["legend_note"],
                    ),
                ]
            ],
            colWidths=[3.4 * cm, 3.4 * cm, 9.8 * cm],
        )

    def _build_sources_section(
        self,
        cited_sources: list[tuple[int, ReportSource]],
        styles: dict[str, ParagraphStyle],
    ) -> list[Any]:
        section: list[Any] = []

        accent_bar = Table([[""]], colWidths=[16.6 * cm], rowHeights=[0.12 * cm])
        accent_bar.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5B400")),
                ]
            )
        )
        section.append(accent_bar)
        section.append(Spacer(1, 0.25 * cm))

        section.append(Paragraph("Fuentes citadas", styles["sources_title"]))
        section.append(Spacer(1, 0.25 * cm))

        rows: list[list[Any]] = [
            [
                Paragraph("#", styles["sources_header"]),
                Paragraph("Dominio", styles["sources_header"]),
                Paragraph("Título", styles["sources_header"]),
                Paragraph("%", styles["sources_header"]),
            ]
        ]

        for number, source in cited_sources:
            rows.append(
                [
                    Paragraph(str(number), styles["sources_percent"]),
                    Paragraph(
                        source.domain or "—",
                        styles["sources_cell"],
                    ),
                    Paragraph(
                        self._truncate(source.title, 90),
                        styles["sources_cell"],
                    ),
                    Paragraph(
                        f"{self._format_decimal(source.matched_percent)}%",
                        styles["sources_percent"],
                    ),
                ]
            )

        table = Table(
            rows,
            colWidths=[1.0 * cm, 4.0 * cm, 9.1 * cm, 2.5 * cm],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123F9E")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#F0F5FF")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 0), (0, -1), "CENTER"),
                    ("ALIGN", (3, 0), (3, -1), "CENTER"),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        section.append(table)

        return section

    def _truncate(self, value: str, max_length: int) -> str:
        value = value or ""

        if len(value) <= max_length:
            return value

        return f"{value[: max_length - 1].rstrip()}…"

    def _format_decimal(self, value: Decimal) -> str:
        return f"{value:.2f}"

    def _build_styles(self) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()

        return {
            "title": ParagraphStyle(
                "hd_title",
                parent=base["Title"],
                fontName="Helvetica-Bold",
                fontSize=16,
                leading=20,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#123F9E"),
                spaceAfter=4,
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
            f"{institution_name} - DOCUMENTO SEÑALADO",
        )

        canvas.setFont("Helvetica", 6.5)
        canvas.drawRightString(
            width - 1.7 * cm,
            0.17 * cm,
            f"Pagina {doc.page}",
        )

        canvas.restoreState()
