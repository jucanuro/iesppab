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

    # Encabezado de una sección real de referencias / anexos: una línea
    # propia y corta cuyo contenido es SOLO el nombre de la sección
    # (opcionalmente numerada o en mayúsculas). No se busca la palabra
    # suelta dentro de un párrafo de prosa ("...se detalla en el anexo
    # correspondiente...") — eso truncaba el cuerpo del documento.
    REFERENCE_HEADING = re.compile(
        r"^[ \t]*"
        r"(?:(?:cap[ií]tulo\s+)?[ivxlcdm]+\.?[ \t]*|\d+\.?\d*\.?[ \t]*)?"
        r"(?:"
        r"referencias(?:[ \t]+bibliogr[aá]ficas)?"
        r"|referencias[ \t]+y[ \t]+bibliograf[ií]a"
        r"|lista[ \t]+de[ \t]+referencias"
        r"|bibliograf[ií]a"
        r"|fuentes[ \t]+(?:de[ \t]+informaci[oó]n|bibliogr[aá]ficas|consultadas)"
        r"|anexos?"
        r"|ap[eé]ndices?"
        r")"
        r"[ \t]*:?[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )

    # El encabezado solo se acepta como corte si aparece en la parte final
    # del documento (últimos ~40 %), nunca en una mención temprana.
    REFERENCE_TAIL_MIN_RATIO = 0.6

    LOW_VALUE_HEADINGS = [
        "dedicatoria",
        "dedicatorias",
        "agradecimiento",
        "agradecimientos",
        "índice",
        "indice",
        "tabla de contenido",
        "resumen",
        "abstract",
    ]

    # Marcadores PROPIOS de una carátula: fórmulas fijas de la portada de
    # una tesis. NO se usan palabras temáticas genéricas ("escuela",
    # "docente", "universidad") porque toda introducción de una tesis de
    # formación docente las menciona y no por eso es una portada.
    STRONG_COVER_MARKERS = [
        "tesis para optar",
        "para optar el título",
        "para optar el titulo",
        "para optar por el título",
        "para optar por el titulo",
        "para optar al título",
        "para optar al titulo",
        "para obtener el título",
        "para obtener el titulo",
        "para optar el grado",
        "para obtener el grado",
        "para optar el bachiller",
        "presentado por",
        "presentada por",
        "tesis presentada",
        "informe de investigación presentado",
        "línea de investigación",
        "linea de investigacion",
        "asesor:",
        "asesora:",
        "autor:",
        "autora:",
        "autores:",
        "presentado por:",
    ]

    # Una línea de carátula es un fragmento suelto y corto. Una línea con
    # una oración completa de prosa (varias palabras y cierre de oración)
    # NO puede pertenecer a una portada.
    COVER_MIN_LINES = 3
    COVER_SHORT_LINE_MAX_LENGTH = 80
    COVER_SHORT_LINE_MIN_RATIO = 0.75
    COVER_PROSE_LINE_MIN_WORDS = 8
    COVER_MAX_LENGTH = 1500
    COVER_UPPERCASE_MIN_RATIO = 0.6
    # Dos o más cierres de oración (punto/exclamación/interrogación, NO los
    # dos puntos de un rótulo como "AUTOR:") seguidos de palabra
    # capitalizada = prosa corrida de varias oraciones, nunca una portada.
    COVER_SENTENCE_BREAK = re.compile(r"[.!?]\s+[A-ZÁÉÍÓÚÑ¿¡]")
    COVER_MAX_SENTENCE_BREAKS = 1

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

    # Encabezado/pie de página repetido en cada página del PDF (nombre de
    # la institución, título del autor, número de página). Tras la
    # extracción queda pegado DENTRO de párrafos de prosa real, sin su
    # propio párrafo aislado, así que ningún filtro por párrafo puede
    # tocarlo. Se detecta por REPETICIÓN a lo largo del documento entero
    # (genérico, no depende del nombre de ninguna institución en
    # particular) — ver `_remove_running_headers_and_footers`.
    #
    # Umbral alto a propósito: un encabezado/pie real se repite ~una vez
    # por PÁGINA (decenas a cientos de veces en una tesis). Contenido
    # legítimo que también se repite bastante — encabezados de sección
    # numerados ("SESIÓN DE APRENDIZAJE N° 01..12"), leyendas de tablas o
    # figuras, viñetas de una escala tipo Likert — repite muchísimo menos
    # (una docena de veces, atado a la cantidad real de secciones/ítems,
    # no de páginas). Verificado contra documentos reales del corpus.
    RUNNING_LINE_MIN_REPEATS = 15
    RUNNING_LINE_MIN_LENGTH = 6
    RUNNING_LINE_MAX_LENGTH = 120
    RUNNING_LINE_TRAILING_NUMBER = re.compile(r"[ \t]*(\d{1,4})[ \t]*$")
    # Fracción mínima de apariciones de una línea repetida que deben traer
    # un número al final para considerarla numerada.
    RUNNING_LINE_NUMBER_PRESENCE_MIN_RATIO = 0.5
    # De esas apariciones numeradas, qué fracción del valor debe ser
    # DISTINTA entre sí. Un número de página real es casi siempre distinto
    # en cada repetición (65, 95, 97, 99...); un código fijo que por
    # casualidad termina en dígitos (p. ej. un código de colegio "82737"
    # repetido igual siempre) o una numeración corta de lista (viñetas
    # "• 1".."• 6" reutilizadas en cada ítem de una encuesta) NO varía lo
    # suficiente y no debe tratarse como paginación.
    RUNNING_LINE_NUMBER_DISTINCT_MIN_RATIO = 0.7
    # Un pie/encabezado de página real se repite una vez por página, la
    # periodicidad más fina posible en un documento entero — así que, de
    # todas las numeraciones secuenciales candidatas (que también pueden
    # incluir tablas, figuras o sesiones numeradas), es casi siempre la
    # que MÁS veces se repite. Solo se acepta como ancla la que se acerca
    # al máximo observado; una numeración secuencial legítima pero con
    # muchas menos repeticiones (atada a la cantidad de tablas/figuras,
    # no de páginas) queda descartada aunque también "parezca" paginación
    # por sí sola.
    RUNNING_LINE_ANCHOR_RELATIVE_MIN_RATIO = 0.8
    # Piso absoluto además del relativo: si un documento no tiene ningún
    # encabezado/pie real, pero sí, por ejemplo, 20 tablas numeradas
    # "Tabla Nº 1..20" (con números todos distintos, aprobando la prueba
    # de "parece paginación"), esa secuencia sería la única candidata y
    # "el máximo" pasaría a ser ella misma por descarte. Un documento de
    # extensión de tesis real (decenas de páginas) tiene un pie/encabezado
    # que se repite muy por encima de lo que razonablemente suman las
    # tablas/figuras/sesiones numeradas de ese mismo documento. Sin este
    # piso, una secuencia de títulos numerados sin competencia se trataría
    # como ancla igual.
    RUNNING_LINE_ANCHOR_ABSOLUTE_MIN_REPEATS = 50
    # Ventana (en líneas, incluyendo blancos) alrededor de cada aparición
    # de una línea "ancla" (ver más abajo) donde se busca otra línea que
    # también se repite mucho: el nombre de la institución y el título del
    # autor viajan pegados al número de página en el mismo bloque de
    # encabezado/pie. Ventana chica a propósito: solo debe capturar la
    # línea inmediatamente vecina del mismo bloque, no cualquier cosa que
    # ocurra "cerca" en un documento denso en tablas repetidas.
    RUNNING_LINE_PROXIMITY_WINDOW = 3
    RUNNING_LINE_PROXIMITY_MIN_RATIO = 0.8
    # Una línea "compañera" del ancla (ver más abajo) debe repetirse casi
    # tantas veces como el ancla misma — viajan juntas en cada página. Un
    # contenido legítimo que también se repite bastante (leyenda de
    # tabla, fila de totales de encuesta) casi nunca alcanza ese volumen,
    # porque no está atado a la cantidad de páginas sino a la cantidad de
    # ítems/tablas del documento.
    RUNNING_LINE_COMPANION_MIN_COUNT_RATIO = 0.8

    def filter_for_similarity(self, content: str) -> FilteredText:
        if not content:
            return FilteredText(content="", excluded_sections=[])

        normalized = self._normalize_line_breaks(content)
        excluded: list[str] = []

        normalized, removed_running = self._remove_running_headers_and_footers(
            normalized,
        )
        excluded.extend(removed_running)

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

    def _remove_running_headers_and_footers(
        self,
        content: str,
    ) -> tuple[str, list[str]]:
        """
        Elimina líneas de encabezado/pie de página que el extractor de PDF
        pegó dentro del cuerpo del texto, repetidas una vez por cada
        página (nombre de la institución, título del alumno, número de
        página). No se puede filtrar por párrafo: quedan incrustadas
        dentro de párrafos de prosa real larguísimos, sin salto de línea
        propio que las aísle.

        Se detectan por REPETICIÓN, no por contenido fijo (funciona para
        cualquier institución, no solo la de este despliegue):

        1. Se normaliza cada línea quitándole el número final (el número
           de página cambia en cada repetición: "...Lingán 12", "...Lingán
           13" deben contar como la MISMA línea).
        2. Una línea normalizada es "ancla" de pie de página si:
           - se repite `RUNNING_LINE_MIN_REPEATS` veces o más (umbral alto:
             una vez por página, no una docena de veces como un
             encabezado de sección numerado o una viñeta de encuesta),
           - es corta y no es una oración de prosa, y
           - en la mayoría de sus repeticiones traía un número al final
             (`RUNNING_LINE_NUMBER_PRESENCE_MIN_RATIO`) Y esos números son
             en su mayoría DISTINTOS entre sí
             (`RUNNING_LINE_NUMBER_DISTINCT_MIN_RATIO`) — un número de
             página real cambia casi en cada aparición; un código fijo
             que casualmente termina en dígitos, o una numeración corta
             de lista reutilizada (viñetas "1".."6"), no.
        3. Un documento puede tener MÁS de una numeración secuencial que
           por sí sola parece paginación (tablas, figuras, sesiones
           numeradas). Solo se trata como ancla real la que tiene MÁS
           repeticiones — la periodicidad más fina posible es una vez por
           página — o se acerca al máximo observado
           (`RUNNING_LINE_ANCHOR_RELATIVE_MIN_RATIO`); las demás quedan
           descartadas aunque también cumplan el punto 2.
        4. Cualquier OTRA línea que también se repite mucho (al menos
           `RUNNING_LINE_COMPANION_MIN_COUNT_RATIO` de las veces que se
           repite el ancla), y que además aparece pegada (dentro de
           `RUNNING_LINE_PROXIMITY_WINDOW` líneas) a una aparición de una
           línea ancla en la mayoría de sus propias repeticiones, se
           considera parte del mismo bloque de encabezado/pie (el nombre
           de la institución no lleva número, pero siempre viaja junto al
           que sí lo lleva).

        Esto evita borrar contenido legítimo que también se repite bastante
        (etiquetas de una escala Likert, leyendas de tabla/figura,
        encabezados de sección numerados): ninguno de esos alcanza el
        volumen de repetición de un header/footer de página completo, y
        los que sí llevan un número no lo tienen realmente VARIANDO como
        una paginación.
        """
        lines = content.split("\n")

        keys: list[str | None] = []
        number_values: list[int | None] = []

        for line in lines:
            stripped = line.strip()

            if not stripped:
                keys.append(None)
                number_values.append(None)
                continue

            match = self.RUNNING_LINE_TRAILING_NUMBER.search(stripped)
            key = self.RUNNING_LINE_TRAILING_NUMBER.sub("", stripped).strip()
            keys.append(key or None)
            number_values.append(int(match.group(1)) if match and key else None)

        occurrences: dict[str, list[int]] = {}

        for index, key in enumerate(keys):
            if key:
                occurrences.setdefault(key, []).append(index)

        def is_repeated_line_candidate(key: str) -> bool:
            if not (self.RUNNING_LINE_MIN_LENGTH <= len(key) <= self.RUNNING_LINE_MAX_LENGTH):
                return False

            words = key.split()

            if (
                len(words) >= self.COVER_PROSE_LINE_MIN_WORDS
                and key[-1] in ".;:!?"
            ):
                return False

            return True

        def looks_like_page_numbering(positions: list[int]) -> bool:
            numbers = [number_values[index] for index in positions]
            numbered = [value for value in numbers if value is not None]

            if len(numbered) / len(numbers) < self.RUNNING_LINE_NUMBER_PRESENCE_MIN_RATIO:
                return False

            distinct_ratio = len(set(numbered)) / len(numbered)

            return distinct_ratio >= self.RUNNING_LINE_NUMBER_DISTINCT_MIN_RATIO

        numbering_like_keys: dict[str, int] = {}

        for key, positions in occurrences.items():
            if (
                len(positions) < self.RUNNING_LINE_MIN_REPEATS
                or not is_repeated_line_candidate(key)
            ):
                continue

            if looks_like_page_numbering(positions):
                numbering_like_keys[key] = len(positions)

        if not numbering_like_keys:
            return content, []

        # Un documento puede tener MÁS de una numeración secuencial que
        # "parece" paginación por sí sola (tablas "Tabla Nº 1, 2, 3...",
        # figuras "Figura N° 1, 2, 3...", sesiones numeradas...). Un
        # encabezado/pie real es, por construcción, la numeración con más
        # repeticiones del documento entero: se repite una vez por
        # PÁGINA, la periodicidad más fina posible. Cualquier otra
        # numeración secuencial legítima está atada a la cantidad de
        # tablas/figuras/sesiones, casi siempre bastante menor. Por eso
        # solo se trata como ancla real la(s) que se acerque(n) al máximo
        # de repeticiones observado entre las candidatas.
        max_repeats = max(numbering_like_keys.values())
        anchor_keys = {
            key
            for key, count in numbering_like_keys.items()
            if count >= self.RUNNING_LINE_ANCHOR_RELATIVE_MIN_RATIO * max_repeats
            and count >= self.RUNNING_LINE_ANCHOR_ABSOLUTE_MIN_REPEATS
        }

        if not anchor_keys:
            return content, []

        removable_keys: set[str] = set(anchor_keys)

        # Una línea "compañera" (nombre de la institución, título del
        # autor) viaja SIEMPRE junto al número de página, así que se
        # repite un número de veces casi idéntico al del ancla, pegada a
        # ella. Un contenido legítimo que también se repite bastante
        # (leyenda de tabla, fila de totales de una encuesta) normalmente
        # aparece muchas menos veces que el ancla — exigir que el conteo
        # sea COMPARABLE al del ancla (no solo "aparece cerca alguna vez")
        # es lo que evita arrastrarlo: con un ancla que se repite ~110
        # veces por página, una tabla de resultados que solo se repite 40
        # veces (una vez por cada ÍTEM de la encuesta, no por página) se
        # queda fuera aunque a veces caiga dentro de la ventana.
        for anchor_key in anchor_keys:
            anchor_positions = occurrences[anchor_key]
            anchor_count = len(anchor_positions)

            anchor_coverage = [False] * len(lines)

            for position in anchor_positions:
                start = max(0, position - self.RUNNING_LINE_PROXIMITY_WINDOW)
                end = min(
                    len(lines),
                    position + self.RUNNING_LINE_PROXIMITY_WINDOW + 1,
                )

                for index in range(start, end):
                    anchor_coverage[index] = True

            for key, positions in occurrences.items():
                if key in removable_keys or not is_repeated_line_candidate(key):
                    continue

                if len(positions) < self.RUNNING_LINE_MIN_REPEATS:
                    continue

                if len(positions) < self.RUNNING_LINE_COMPANION_MIN_COUNT_RATIO * anchor_count:
                    continue

                near_anchor = sum(
                    1 for position in positions if anchor_coverage[position]
                )

                if near_anchor / len(positions) >= self.RUNNING_LINE_PROXIMITY_MIN_RATIO:
                    removable_keys.add(key)

        remove_indices = {
            index for key in removable_keys for index in occurrences[key]
        }

        if not remove_indices:
            return content, []

        kept_lines = [
            line for index, line in enumerate(lines) if index not in remove_indices
        ]

        return "\n".join(kept_lines), ["encabezado/pie de página repetido"]

    def _remove_reference_tail(self, content: str) -> tuple[str, list[str]]:
        if not content:
            return content, []

        min_position = len(content) * self.REFERENCE_TAIL_MIN_RATIO
        cut_position: int | None = None

        # Se corta en el PRIMER encabezado aislado de referencias/anexos que
        # caiga en la parte final del documento, no en el último: el cierre
        # más común de una tesis es "REFERENCIAS BIBLIOGRÁFICAS" seguido más
        # adelante de "ANEXOS", ambos encabezados aislados válidos. Cortar en
        # el último ("ANEXOS") dejaría toda la bibliografía dentro del texto
        # analizado. Cortando en el primero se descarta referencias Y anexos
        # completos, que es lo correcto: todo lo que sigue es material de
        # cierre, no cuerpo del documento.
        # Una mención casual en prosa ("...véase el anexo correspondiente...")
        # no es una línea propia y nunca casa con REFERENCE_HEADING, así que
        # no trunca nada.
        for match in self.REFERENCE_HEADING.finditer(content):
            if match.start() >= min_position:
                cut_position = match.start()
                break

        if cut_position is None:
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
        """
        Una carátula real NO es prosa: es un bloque de líneas cortas y
        sueltas (institución, título, "tesis para optar", autor, asesor,
        año, ciudad). Se distingue de una introducción real por la forma,
        no por mencionar palabras temáticas educativas.
        """
        if len(paragraph) >= self.COVER_MAX_LENGTH:
            return False

        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]

        if not lines:
            return False

        # Prosa corrida => nunca es portada. Dos señales:
        #  - alguna línea es una oración completa (>= 8 palabras y cierre de
        #    oración), o
        #  - el bloque encadena varias oraciones (cierre de oración seguido
        #    de palabra capitalizada, 2+ veces).
        for line in lines:
            if (
                len(line.split()) >= self.COVER_PROSE_LINE_MIN_WORDS
                and line[-1] in ".;:"
            ):
                return False

        if (
            len(self.COVER_SENTENCE_BREAK.findall(paragraph))
            > self.COVER_MAX_SENTENCE_BREAKS
        ):
            return False

        lowered = paragraph.lower()
        has_strong_marker = any(
            marker in lowered for marker in self.STRONG_COVER_MARKERS
        )

        letters = [char for char in paragraph if char.isalpha()]
        uppercase_ratio = (
            sum(1 for char in letters if char.isupper()) / len(letters)
            if letters
            else 0.0
        )

        short_lines = [
            line
            for line in lines
            if len(line) <= self.COVER_SHORT_LINE_MAX_LENGTH
        ]
        is_short_line_block = (
            len(lines) >= self.COVER_MIN_LINES
            and len(short_lines) / len(lines) >= self.COVER_SHORT_LINE_MIN_RATIO
        )

        # (a) Bloque multilínea de líneas cortas sin oraciones, con fórmula
        #     fija de portada o predominio de MAYÚSCULAS; o
        # (b) un blob de una sola corrida (la extracción aplastó los saltos)
        #     sin estructura de oraciones y con una fórmula fija de portada
        #     ("tesis para optar el título", "presentado por", "asesor:").
        if is_short_line_block:
            return (
                has_strong_marker
                or uppercase_ratio >= self.COVER_UPPERCASE_MIN_RATIO
            )

        return has_strong_marker

    def _is_low_value_paragraph(self, paragraph: str) -> bool:
        lowered = paragraph.lower().strip()

        if len(lowered) < 40:
            return True

        # Se compara la primera línea "real" contra el encabezado, nunca un
        # startswith literal sobre todo el párrafo. El texto extraído de
        # PDF trae dos residuos frecuentes que rompían la comparación
        # anterior:
        #  - un espacio suelto antes del salto de línea ("ÍNDICE \n..."),
        #    que rompía startswith(f"{heading}\n"); se corrige comparando
        #    cada línea ya despojada de sus propios espacios;
        #  - un número de página suelto pegado justo ANTES del encabezado
        #    ("5 \nÍNDICE \n1.2.1..."), residuo de la extracción; se
        #    ignora buscando la primera línea que no sea puramente
        #    numérica.
        first_lines = [line.strip() for line in lowered.split("\n")]

        while first_lines and first_lines[0].isdigit():
            first_lines.pop(0)

        if first_lines and first_lines[0] in self.LOW_VALUE_HEADINGS:
            return True

        # Muchísimas líneas cortas suelen ser índice.
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]

        if len(lines) >= 5:
            short_lines = [line for line in lines if len(line) <= 65]
            dotted_lines = [line for line in lines if "..." in line or re.search(r"\s+\d+$", line)]

            if len(short_lines) / len(lines) > 0.75 and len(dotted_lines) >= 2:
                return True

        return False