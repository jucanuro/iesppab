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
    SourceType,
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
        story.append(Spacer(1, 0.5 * cm))

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

        if cited_sources:
            story.extend(
                self._build_primary_sources(
                    cited_sources=cited_sources,
                    word_counts=word_counts,
                    styles=styles,
                )
            )
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
                        "«Fuentes primarias».",
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

            words = word_counts.get(source.id, 0)
            label = source.domain or self._truncate(source.title, 60) or "Fuente"

            rows.append(
                [
                    Paragraph(f'<font color="#FFFFFF"><b>{number}</b></font>', styles["source_num"]),
                    Paragraph(
                        f'<font color="{color}"><b>{self._escape(label)}</b></font><br/>'
                        f'<font size="7" color="#64748B">{self._source_type_label(source)}</font>',
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
