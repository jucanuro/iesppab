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

    # Prefijos típicos de una entrada de índice de tablas/figuras/anexos,
    # independientemente de si el número de página sobrevivió a la
    # extracción del texto.
    INDEX_ENTRY_PREFIX = re.compile(
        r"^(tabla|figura|cuadro|gr[aá]fico|anexo|ilustraci[oó]n)\s+n?[°º]?\.?\s*\d+",
        re.IGNORECASE,
    )
    # Línea con relleno de puntos ("....") o dos+ espacios antes del número
    # de página, o simplemente terminada en un número corto (típico de
    # líneas de índice cuando el relleno de puntos no sobrevive a la
    # extracción).
    INDEX_TRAILING_PAGE = re.compile(r"(\.{2,}|\s{2,})\s*\d{1,4}\s*$")
    INDEX_SHORT_TRAILING_NUMBER = re.compile(r"\s\d{1,4}\s*$")
    INDEX_LINE_MAX_LENGTH = 110
    INDEX_RUN_MIN_LINES = 5

    def filter_for_similarity(self, content: str) -> FilteredText:
        if not content:
            return FilteredText(content="", excluded_sections=[])

        normalized = self._normalize_line_breaks(content)
        excluded: list[str] = []

        normalized, removed_tail = self._remove_reference_tail(normalized)
        excluded.extend(removed_tail)

        normalized, removed_index_runs = self._remove_index_like_line_runs(
            normalized,
        )
        excluded.extend(removed_index_runs)

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

    def _remove_index_like_line_runs(
        self,
        content: str,
    ) -> tuple[str, list[str]]:
        """
        Detecta y elimina bloques de líneas que parecen entradas de un
        índice (de tablas, figuras, cuadros, anexos, etc.) por su forma,
        sin depender del título exacto de la sección ni de que las
        entradas estén agrupadas en un mismo párrafo. Esto cubre casos
        donde cada entrada queda como su propio párrafo tras la
        extracción del texto, algo que el filtro por encabezado/párrafo
        no detecta.
        """
        lines = content.split("\n")
        line_kinds = [self._classify_index_like_line(line) for line in lines]

        remove = [False] * len(lines)
        run_start: int | None = None
        run_indices: list[int] = []
        run_strong_count = 0

        def flush_run(end_index: int) -> None:
            nonlocal run_start, run_indices, run_strong_count

            if (
                run_start is not None
                and len(run_indices) >= self.INDEX_RUN_MIN_LINES
                # Al menos 2 líneas con una señal fuerte (prefijo "Tabla N"
                # o relleno de puntos/espacios ante el número de página),
                # igual que exigía la heurística original, para no marcar
                # como índice un párrafo normal que solo por coincidencia
                # tiene varias líneas cortas terminadas en un número.
                and run_strong_count >= 2
            ):
                for index in range(run_start, end_index):
                    remove[index] = True

            run_start = None
            run_indices = []
            run_strong_count = 0

        for position, line in enumerate(lines):
            stripped = line.strip()
            kind = line_kinds[position]

            if kind is not None:
                if run_start is None:
                    run_start = position

                run_indices.append(position)

                if kind == "strong":
                    run_strong_count += 1

                continue

            if not stripped and run_start is not None:
                # Línea en blanco dentro de una posible racha de índice:
                # no la corta, solo no cuenta como entrada.
                continue

            flush_run(end_index=position)

        flush_run(end_index=len(lines))

        if not any(remove):
            return content, []

        kept_lines = [
            line for index, line in enumerate(lines) if not remove[index]
        ]

        return "\n".join(kept_lines), ["índice de tablas/figuras"]

    def _classify_index_like_line(self, line: str) -> str | None:
        stripped = line.strip()

        if not stripped:
            return None

        # Las señales fuertes (prefijo "Tabla N"/relleno de puntos ante el
        # número de página) se evalúan sin tope de longitud: el relleno de
        # puntos de una entrada de índice real puede superar por sí solo
        # `INDEX_LINE_MAX_LENGTH` cuando el PDF exporta el "leader" del
        # índice como una racha larga de puntos literales.
        if self.INDEX_ENTRY_PREFIX.match(stripped):
            return "strong"

        if self.INDEX_TRAILING_PAGE.search(stripped):
            return "strong"

        # La señal débil (línea corta terminada en un número, sin relleno
        # de puntos) sí necesita un tope de longitud para no confundir un
        # párrafo de prosa normal que solo por coincidencia termina en un
        # número.
        if (
            len(stripped) <= self.INDEX_LINE_MAX_LENGTH
            and len(stripped) <= 90
            and self.INDEX_SHORT_TRAILING_NUMBER.search(stripped)
        ):
            return "weak"

        return None

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