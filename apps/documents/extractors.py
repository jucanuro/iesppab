from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from docx import Document as DocxDocument
from pypdf import PdfReader

from apps.documents.models import Document, DocumentText

logger = logging.getLogger(__name__)


# --- Normalización del texto de PDF ---
#
# pypdf devuelve cada línea VISUAL de la página con su propio "\n", el
# número de página y los encabezados/pies repetidos en medio del texto, y
# las palabras cortadas con guion al final de línea. GPT-2 lee todo eso:
# la misma tesis daba ~60 % de IA en DOCX y ~5-10 % en PDF, y los saltos
# también parten frases en la búsqueda web. Aquí se reconstruyen los
# párrafos para que el PDF quede con la misma forma que ya produce el DOCX
# (un párrafo por bloque, separados por "\n\n").

# Un encabezado/pie se repite en el borde de al menos esta fracción de las
# páginas (y en un mínimo de PDF_RUNNING_MIN_PAGES).
PDF_RUNNING_MIN_PAGE_RATIO = 0.3
PDF_RUNNING_MIN_PAGES = 3
# Líneas del borde superior/inferior de cada página donde se buscan.
PDF_RUNNING_EDGE_LINES = 2
PDF_RUNNING_MAX_CHARS = 120

# Una línea es "corta" (posible fin de párrafo o título) si mide menos de
# esta fracción del ancho típico de las líneas de su página.
PDF_SHORT_LINE_RATIO = 0.75
# Un título que encabeza la prosa siguiente no pasa de este largo.
PDF_HEADING_MAX_CHARS = 100
# Sangría (espacios al inicio) que marca el comienzo de un párrafo.
PDF_INDENT_MIN_SPACES = 3

_PDF_PAGE_NUMBER_RE = re.compile(
    r"^[\W_]*(?:p[áa]g(?:ina)?\.?\s*)?(?:\d{1,4}|[ivxlc]{1,6})"
    r"(?:\s*(?:de|/)\s*\d{1,4})?[\W_]*$",
    re.IGNORECASE,
)
# Viñetas, numeraciones ("1.", "2.3.", "a)", "IV.") y rótulos de tablas o
# figuras: siempre empiezan un bloque nuevo.
_PDF_BLOCK_START_RE = re.compile(
    r"^(?:[•●▪◦■□➢►✓*\-–—]\s*|\d+(?:\.\d+)*\.?\)?\s|[a-zA-Z]\)\s|"
    r"[IVX]+\.\s|(?:Tabla|Figura|Cuadro|Gráfico|Anexo)\s+\d)"
)
_PDF_SENTENCE_END = (".", ":", "?", "!", ";", "»", "”", '"', ")")
_PDF_DIGITS_RE = re.compile(r"\d+")


def _running_line_key(line: str) -> str:
    return _PDF_DIGITS_RE.sub("#", " ".join(line.split()).lower())


def _strip_page_furniture(pages: list[list[str]]) -> list[list[str]]:
    """
    Quita números de página y encabezados/pies repetidos. Solo mira las
    primeras y últimas `PDF_RUNNING_EDGE_LINES` líneas no vacías de cada
    página: ahí vive el mobiliario de página, y una línea repetida en medio
    del cuerpo (una etiqueta de escala, un subtítulo) no se toca.
    """
    def edge_indices(lines: list[str]) -> list[int]:
        filled = [index for index, line in enumerate(lines) if line.strip()]
        edge = PDF_RUNNING_EDGE_LINES
        return sorted(set(filled[:edge] + filled[-edge:]))

    counts: dict[str, int] = {}
    for lines in pages:
        for key in {_running_line_key(lines[i]) for i in edge_indices(lines)}:
            counts[key] = counts.get(key, 0) + 1

    min_pages = max(PDF_RUNNING_MIN_PAGES, len(pages) * PDF_RUNNING_MIN_PAGE_RATIO)
    running = {
        key
        for key, count in counts.items()
        if count >= min_pages and len(key) <= PDF_RUNNING_MAX_CHARS
    }

    cleaned = []
    for lines in pages:
        drop = {
            index
            for index in edge_indices(lines)
            if _PDF_PAGE_NUMBER_RE.match(lines[index].strip())
            or _running_line_key(lines[index]) in running
        }
        cleaned.append([line for i, line in enumerate(lines) if i not in drop])

    return cleaned


def _join_hyphenated(previous: str, line: str) -> str | None:
    """'investi-' + 'gación' -> 'investigación'; None si no hay corte."""
    if (
        len(previous) >= 2
        and previous[-1] in "-‐"
        and previous[-2].isalpha()
        and line[:1].islower()
    ):
        return previous[:-1] + line
    return None


def _page_paragraphs(lines: list[str]) -> list[list]:
    """
    Párrafos de una página como pares [texto, líneas originales unidas].
    """
    widths = sorted(len(line.strip()) for line in lines if line.strip())
    if not widths:
        return []

    # Ancho de una línea "llena": percentil 75, para que los títulos y los
    # finales de párrafo (cortos) no lo arrastren hacia abajo.
    full_width = widths[(len(widths) * 3) // 4 - (1 if len(widths) % 4 == 0 else 0)]
    paragraphs: list[list] = []
    current: list | None = None
    previous = ""

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            if current:
                paragraphs.append(current)
            current, previous = None, ""
            continue

        if current:
            joined = _join_hyphenated(current[0], line)
            if joined is not None:
                current = [joined, current[1] + 1]
                previous = line
                continue

            indent = len(raw_line) - len(raw_line.lstrip(" "))
            previous_short = len(previous) < full_width * PDF_SHORT_LINE_RATIO
            new_block = (
                indent >= PDF_INDENT_MIN_SPACES
                or _PDF_BLOCK_START_RE.match(line)
                # Fin de párrafo: la línea anterior quedó corta y cerró
                # una oración.
                or (previous_short and previous.endswith(_PDF_SENTENCE_END))
                # Título: línea corta sin puntuación seguida de mayúscula.
                or (
                    previous_short
                    and not previous.endswith((",", "-"))
                    and line[:1].isupper()
                )
            )

            if not new_block:
                current = [f"{current[0]} {line}", current[1] + 1]
                previous = line
                continue

            paragraphs.append(current)

        current = [line, 1]
        previous = line

    if current:
        paragraphs.append(current)

    return paragraphs


def normalize_pdf_pages(pages_text: list[str]) -> str:
    """
    Texto de un PDF (una entrada por página) con los párrafos
    reconstruidos: sin números de página ni encabezados/pies repetidos, sin
    guiones de corte y con las líneas de un mismo párrafo unidas, también
    entre páginas cuando el párrafo continúa en la siguiente.

    Las líneas que quedan solas (títulos, entradas del índice, viñetas,
    filas de tablas, portada) se agrupan entre sí con "\n" en un mismo
    bloque, como venían del PDF: el filtro académico reconoce portadas e
    índices por rachas de líneas cortas dentro de un párrafo. Solo la
    prosa reconstruida va separada por "\n\n".
    """
    pages = _strip_page_furniture([text.split("\n") for text in pages_text])
    paragraphs: list[list] = []

    for lines in pages:
        page_paragraphs = _page_paragraphs(lines)
        if not page_paragraphs:
            continue

        first = page_paragraphs[0]
        if paragraphs and not paragraphs[-1][0].endswith(_PDF_SENTENCE_END):
            last = paragraphs[-1]
            joined = _join_hyphenated(last[0], first[0])
            if joined is None and first[0][:1].islower():
                joined = f"{last[0]} {first[0]}"
            if joined is not None:
                paragraphs[-1] = [joined, last[1] + first[1]]
                page_paragraphs = page_paragraphs[1:]

        paragraphs.extend(page_paragraphs)

    blocks: list[str] = []
    previous_single = False

    for text, line_count in paragraphs:
        single = line_count == 1

        if single and previous_single:
            blocks[-1] = f"{blocks[-1]}\n{text}"
        elif not single and previous_single and blocks:
            # Un título suelto ("Resumen", "Dedicatoria", "2.1. Bases
            # teóricas") encabeza la prosa que le sigue, en el mismo
            # párrafo: el filtro académico descarta secciones no académicas
            # mirando la primera línea del párrafo.
            *rest, heading = blocks[-1].split("\n")
            if len(heading) <= PDF_HEADING_MAX_CHARS and not heading.endswith(
                _PDF_SENTENCE_END
            ):
                if rest:
                    blocks[-1] = "\n".join(rest)
                    blocks.append(f"{heading}\n{text}")
                else:
                    blocks[-1] = f"{heading}\n{text}"
            else:
                blocks.append(text)
        else:
            blocks.append(text)

        previous_single = single

    return "\n\n".join(blocks)


def read_pdf_pages(file_obj) -> list[str]:
    """
    Texto crudo de cada página. Lo comparten la extracción de documentos y
    la cosecha de texto completo OAI, para que ambas lean el PDF igual.
    """
    return [page.extract_text() or "" for page in PdfReader(file_obj).pages]


class TextExtractionError(Exception):
    """
    Error controlado durante extracción de texto.
    """

    pass


@dataclass(frozen=True)
class ExtractedTextResult:
    content: str
    word_count: int
    character_count: int
    extraction_engine: str


class DocumentTextExtractor:
    """
    Extrae texto desde documentos PDF o DOCX.
    """

    PDF_MIME = "application/pdf"
    DOCX_MIME = (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    )

    @transaction.atomic
    def extract_and_save(self, document: Document) -> DocumentText:
        result = self.extract(document=document)

        document_text, _ = DocumentText.objects.update_or_create(
            document=document,
            defaults={
                "content": result.content,
                "word_count": result.word_count,
                "character_count": result.character_count,
                "extraction_engine": result.extraction_engine,
                "extracted_at": timezone.now(),
            },
        )

        logger.info(
            "Texto extraído correctamente. document_id=%s words=%s",
            document.id,
            result.word_count,
        )

        return document_text

    def extract(self, document: Document) -> ExtractedTextResult:
        if document.mime_type == self.PDF_MIME:
            raw_text = self._extract_pdf(document=document)
            engine = "pypdf"

        elif document.mime_type == self.DOCX_MIME:
            raw_text = self._extract_docx(document=document)
            engine = "python-docx"

        else:
            raise TextExtractionError(
                "Formato no soportado para extracción de texto."
            )

        content = self._normalize_text(raw_text)

        if len(content) < 100:
            raise TextExtractionError(
                "No se pudo extraer suficiente texto del documento."
            )

        words = re.findall(r"\b\w+\b", content, flags=re.UNICODE)

        return ExtractedTextResult(
            content=content,
            word_count=len(words),
            character_count=len(content),
            extraction_engine=engine,
        )

    def _extract_pdf(self, document: Document) -> str:
        try:
            with document.original_file.open("rb") as file_obj:
                return normalize_pdf_pages(read_pdf_pages(file_obj))

        except Exception as exc:
            logger.exception(
                "Error extrayendo PDF. document_id=%s",
                document.id,
            )
            raise TextExtractionError(
                "No se pudo extraer texto del PDF."
            ) from exc

    def _extract_docx(self, document: Document) -> str:
        try:
            with document.original_file.open("rb") as file_obj:
                docx = DocxDocument(file_obj)
                parts: list[str] = []

                for paragraph in docx.paragraphs:
                    if paragraph.text.strip():
                        parts.append(paragraph.text.strip())

                for table in docx.tables:
                    for row in table.rows:
                        row_text = " ".join(
                            cell.text.strip()
                            for cell in row.cells
                            if cell.text.strip()
                        )
                        if row_text:
                            parts.append(row_text)

                # Cada párrafo de Word va separado por una línea en blanco:
                # el filtro académico, la detección de IA y la búsqueda web
                # parten el texto en párrafos por "\n\n". Con un solo "\n"
                # todo el DOCX quedaba como UN párrafo.
                return "\n\n".join(parts)

        except Exception as exc:
            logger.exception(
                "Error extrayendo DOCX. document_id=%s",
                document.id,
            )
            raise TextExtractionError(
                "No se pudo extraer texto del DOCX."
            ) from exc

    def _normalize_text(self, text: str) -> str:
        text = text.replace("\x00", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()