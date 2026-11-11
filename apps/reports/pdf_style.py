"""
Identidad visual común de los PDF (informe de similitud, informe de IA y
certificado): paleta institucional, cabecera, pie con página y hash, y
medidores circulares dibujados con gráficos vectoriales de ReportLab.

Solo presentación: aquí no hay lógica de negocio.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from django.conf import settings
from reportlab.graphics.shapes import Drawing, String, Wedge
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

logger = logging.getLogger(__name__)

# Paleta (misma que la web: tokens de templates/components/_head.html).
BRAND_900 = "#0B2A6B"
BRAND_700 = "#123F9E"
BRAND_500 = "#236BFD"
BRAND_200 = "#BCD2FF"
BRAND_100 = "#DCE8FF"
BRAND_50 = "#EEF3FF"
GOLD = "#F5B400"
GOLD_50 = "#FFF8E6"
GOLD_700 = "#8A6100"
INK = "#0F172A"
SLATE_700 = "#334155"
SLATE_500 = "#64748B"
SLATE_300 = "#CBD5E1"
SLATE_200 = "#E2E8F0"
SLATE_100 = "#F1F5F9"
SLATE_50 = "#F8FAFC"
GREEN = "#10B981"
AMBER = "#F5B400"
RED = "#DC2626"

# Tamaño mínimo de texto en los PDF (lo verifica una prueba).
MIN_PDF_FONT_SIZE = 8

HEADER_HEIGHT = 1.25 * cm
FOOTER_LINE_Y = 1.0 * cm
FOOTER_TEXT_Y = 0.55 * cm


def level_color(level: int) -> str:
    """0 bajo → verde, 1 medio → ámbar, 2 alto → rojo."""
    return (GREEN, AMBER, RED)[max(0, min(2, int(level)))]


def fallback_logo() -> ImageReader | None:
    """Escudo institucional estático, si la institución no tiene logo cargado."""
    path = Path(settings.BASE_DIR) / "static" / "img" / "logo-iesppabl.png"
    try:
        return ImageReader(str(path)) if path.exists() else None
    except Exception:  # noqa: BLE001 - el logo es decorativo
        logger.warning("No se pudo cargar el logo estático para el PDF.", exc_info=True)
        return None


def gauge_drawing(
    percent: float,
    text: str,
    color: str,
    size: float = 2.7 * cm,
    font_size: int = 14,
) -> Drawing:
    """Medidor circular (anillo) de 0 a 100 %, con el valor en el centro."""
    drawing = Drawing(size, size)
    cx = cy = size / 2
    radius = size / 2 - 1
    inner = radius * 0.76

    drawing.add(
        Wedge(cx, cy, radius, 0, 360, radius1=inner,
              fillColor=colors.HexColor(BRAND_100), strokeColor=None)
    )

    fraction = max(0.0, min(100.0, float(percent))) / 100
    if fraction >= 0.999:
        drawing.add(
            Wedge(cx, cy, radius, 0, 360, radius1=inner,
                  fillColor=colors.HexColor(color), strokeColor=None)
        )
    elif fraction > 0:
        drawing.add(
            Wedge(cx, cy, radius, 90 - 360 * fraction, 90, radius1=inner,
                  fillColor=colors.HexColor(color), strokeColor=None)
        )

    drawing.add(
        String(cx, cy - font_size * 0.35, text, textAnchor="middle",
               fontName="Helvetica-Bold", fontSize=font_size,
               fillColor=colors.HexColor(INK))
    )
    return drawing


def draw_page_chrome(
    canvas: Canvas,
    *,
    institution_name: str,
    label: str,
    logo: ImageReader | None,
    footer_text: str,
) -> None:
    """Cabecera (banda azul + filete dorado + escudo) y pie con hash."""
    width, height = A4

    canvas.saveState()

    canvas.setFillColor(colors.HexColor(BRAND_700))
    canvas.rect(0, height - HEADER_HEIGHT, width, HEADER_HEIGHT, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(GOLD))
    canvas.rect(0, height - HEADER_HEIGHT - 0.07 * cm, width, 0.07 * cm, fill=1, stroke=0)

    text_x = 1.7 * cm
    if logo is not None:
        chip = HEADER_HEIGHT - 0.3 * cm
        chip_x = 1.5 * cm
        chip_y = height - HEADER_HEIGHT + 0.15 * cm
        canvas.setFillColor(colors.white)
        canvas.roundRect(chip_x, chip_y, chip, chip, 0.18 * cm, fill=1, stroke=0)
        pad = 0.05 * cm
        canvas.drawImage(
            logo, chip_x + pad, chip_y + pad, width=chip - 2 * pad, height=chip - 2 * pad,
            preserveAspectRatio=True, mask="auto",
        )
        text_x = chip_x + chip + 0.3 * cm

    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", MIN_PDF_FONT_SIZE)
    canvas.drawString(text_x, height - HEADER_HEIGHT / 2 - 0.1 * cm, institution_name)

    canvas.setFillColor(colors.HexColor(BRAND_200))
    canvas.setFont("Helvetica", MIN_PDF_FONT_SIZE)
    canvas.drawRightString(width - 1.7 * cm, height - HEADER_HEIGHT / 2 - 0.1 * cm, label)

    canvas.setStrokeColor(colors.HexColor(SLATE_200))
    canvas.setLineWidth(0.6)
    canvas.line(1.7 * cm, FOOTER_LINE_Y, width - 1.7 * cm, FOOTER_LINE_Y)
    canvas.setFillColor(colors.HexColor(SLATE_500))
    canvas.setFont("Helvetica", MIN_PDF_FONT_SIZE)
    canvas.drawString(1.7 * cm, FOOTER_TEXT_Y, footer_text)

    canvas.restoreState()


class NumberedCanvas(Canvas):
    """
    Canvas de dos pasadas: guarda cada página y, al cerrar el documento,
    dibuja "Página X de N" cuando ya se conoce el total (REQ-CER-12).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total_pages = len(self._saved_page_states)

        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(total_pages)
            super().showPage()

        super().save()

    def _draw_page_number(self, total_pages: int) -> None:
        width, _height = A4
        self.saveState()
        self.setFillColor(colors.HexColor(BRAND_700))
        self.setFont("Helvetica-Bold", MIN_PDF_FONT_SIZE)
        self.drawRightString(
            width - 1.7 * cm,
            FOOTER_TEXT_Y,
            f"Página {self._pageNumber} de {total_pages}",
        )
        self.restoreState()
