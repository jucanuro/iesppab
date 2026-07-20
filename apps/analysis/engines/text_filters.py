from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FilteredText:
    content: str
    excluded_sections: list[str]


class AcademicTextFilter:
    """
    Filtro académico previo al análisis.

    Objetivo:
    - Evitar que portada, índice, bibliografía, anexos o datos institucionales
      distorsionen el porcentaje de similitud.
    """

    SECTION_STOP_MARKERS = [
        r"\breferencias\b",
        r"\bbibliografía\b",
        r"\bbibliografia\b",
        r"\banexos\b",
        r"\banexo\b",
    ]

    LOW_VALUE_HEADINGS = [
        "dedicatoria",
        "agradecimiento",
        "agradecimientos",
        "índice",
        "indice",
        "tabla de contenido",
        "resumen",
        "abstract",
    ]

    COVER_MARKERS = [
        "universidad",
        "facultad",
        "escuela",
        "instituto",
        "tesis",
        "trabajo de investigación",
        "autor",
        "asesor",
        "docente",
        "curso",
    ]

    def filter_for_similarity(self, content: str) -> FilteredText:
        if not content:
            return FilteredText(content="", excluded_sections=[])

        normalized = self._normalize_line_breaks(content)
        excluded: list[str] = []

        normalized, removed_tail = self._remove_reference_tail(normalized)
        excluded.extend(removed_tail)

        paragraphs = re.split(r"\n\s*\n", normalized)
        kept: list[str] = []

        for index, paragraph in enumerate(paragraphs):
            clean = paragraph.strip()

            if not clean:
                continue

            if index <= 3 and self._looks_like_cover(paragraph=clean):
                excluded.append("portada")
                continue

            if self._is_low_value_paragraph(clean):
                excluded.append("sección no académica")
                continue

            kept.append(clean)

        return FilteredText(
            content="\n\n".join(kept).strip(),
            excluded_sections=excluded,
        )

    def _normalize_line_breaks(self, content: str) -> str:
        content = content.replace("\x00", " ")
        content = re.sub(r"[ \t]+", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()

    def _remove_reference_tail(self, content: str) -> tuple[str, list[str]]:
        lowered = content.lower()
        positions: list[int] = []

        for pattern in self.SECTION_STOP_MARKERS:
            match = re.search(pattern, lowered, flags=re.IGNORECASE)

            if match:
                positions.append(match.start())

        if not positions:
            return content, []

        cut_position = min(positions)

        # Solo corta si la sección aparece después de una parte razonable del documento.
        if cut_position < len(content) * 0.45:
            return content, []

        return content[:cut_position].strip(), ["bibliografía/anexos"]

    def _looks_like_cover(self, paragraph: str) -> bool:
        lowered = paragraph.lower()
        marker_count = sum(1 for marker in self.COVER_MARKERS if marker in lowered)

        return marker_count >= 3 and len(paragraph) < 1200

    def _is_low_value_paragraph(self, paragraph: str) -> bool:
        lowered = paragraph.lower().strip()

        if len(lowered) < 40:
            return True

        for heading in self.LOW_VALUE_HEADINGS:
            if lowered == heading or lowered.startswith(f"{heading}\n"):
                return True

        # Muchísimas líneas cortas suelen ser índice.
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]

        if len(lines) >= 5:
            short_lines = [line for line in lines if len(line) <= 65]
            dotted_lines = [line for line in lines if "..." in line or re.search(r"\s+\d+$", line)]

            if len(short_lines) / len(lines) > 0.75 and len(dotted_lines) >= 2:
                return True

        return False