from __future__ import annotations

import heapq
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from django.conf import settings

from apps.analysis.engines.exclusions import merge_ranges

WORD_PATTERN = re.compile(r"\b[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9]+\b", flags=re.UNICODE)

SOURCE_KIND_INTERNAL = "internal"
SOURCE_KIND_OAI = "oai"
SOURCE_KIND_WEB = "web"

MATCH_TYPE_EXACT = "exacta"
MATCH_TYPE_FUZZY = "aproximada"
MATCH_TYPE_LABELS = {
    MATCH_TYPE_EXACT: "coincidencia exacta",
    MATCH_TYPE_FUZZY: "paráfrasis/coincidencia aproximada",
}


@dataclass(frozen=True)
class SpanSource:
    """
    Una fuente contra la que se alinea el documento: un documento interno,
    un registro OAI o una página web.

    `segments` son tramos de texto crudo de la fuente que no son contiguos
    entre sí (p. ej. los fragmentos aprendidos de un documento interno que
    tuvieron huellas en común). Ninguna coincidencia cruza de un tramo a
    otro.
    """

    key: str
    kind: str
    title: str
    segments: tuple[str, ...]
    source_id: UUID | None = None
    owner_name: str = ""
    url: str = ""
    domain: str = ""


@dataclass(frozen=True)
class SpanMatch:
    source: SpanSource
    # % del documento atribuido a esta fuente (no solo a este tramo): es lo
    # que se muestra junto a la fuente en el reporte.
    source_percent: Decimal
    start_offset: int
    end_offset: int
    # Palabras que cuentan para el %: en un tramo aproximado, solo las
    # idénticas (puede ser menor que las palabras resaltadas).
    word_count: int
    text_excerpt: str
    source_excerpt: str
    # MATCH_TYPE_FUZZY si alguna palabra del tramo vino solo de la unión de
    # tramos cortos (paráfrasis ligera); MATCH_TYPE_EXACT si no.
    match_type: str = MATCH_TYPE_EXACT


@dataclass(frozen=True)
class SpanSourceResult:
    source: SpanSource
    word_count: int
    percent: Decimal


@dataclass(frozen=True)
class SpanAnalysisResult:
    similarity_percent: Decimal
    countable_word_count: int
    # Ordenadas de mayor a menor cantidad de palabras atribuidas.
    sources: list[SpanSourceResult] = field(default_factory=list)
    # Todos los tramos atribuidos, en orden de aparición en el documento.
    matches: list[SpanMatch] = field(default_factory=list)

    def word_count_for(self, kinds: Iterable[str]) -> int:
        kinds = set(kinds)
        return sum(
            item.word_count for item in self.sources if item.source.kind in kinds
        )

    def percent_for(self, kinds: Iterable[str]) -> Decimal:
        return _percent(self.word_count_for(kinds), self.countable_word_count)

    def matches_for(self, kinds: Iterable[str]) -> list[SpanMatch]:
        kinds = set(kinds)
        return [match for match in self.matches if match.source.kind in kinds]


def _percent(part: int, total: int) -> Decimal:
    if total <= 0 or part <= 0:
        return Decimal("0.00")

    percent = Decimal(part) / Decimal(total) * Decimal("100")
    return min(percent, Decimal("100.00")).quantize(Decimal("0.01"))


class SpanSimilarityEngine:
    """
    Similitud por tramos de palabras idénticas, al estilo de Turnitin.

    1. Normaliza cada palabra (minúsculas, sin tildes) conservando su
       offset en `content`.
    2. Para cada fuente, marca las palabras del documento que forman parte
       de un tramo idéntico de al menos `min_match_words` palabras (unión
       de los k-gramas de ese largo presentes en ambos textos: una palabra
       está en un tramo idéntico >= k si y solo si está en algún k-grama
       común).
    3. Une tramos de una misma fuente separados por <= `max_gap_words`
       palabras distintas (las palabras del hueco también cuentan).
    4. Atribución voraz: la fuente que cubre más palabras se queda con
       ellas; las demás compiten solo por las palabras que quedan libres.
       Ninguna palabra se cuenta dos veces.
    5. % = palabras atribuidas / palabras contables (las de las citas
       excluidas no son contables ni pueden coincidir). En los bloques de
       paráfrasis ligera solo cuentan las palabras idénticas.

    Paráfrasis ligera (paso 2 bis): además, encadena tramos cortos de
    >= `min_short_run` palabras idénticas, en el mismo orden en el documento
    y en la fuente, separados por huecos de <= `max_gap_words` palabras en
    ambos lados. Un bloque encadenado cuenta si suma >= `min_fuzzy_span`
    palabras (huecos incluidos) y al menos `min_fuzzy_ratio` de ellas son
    idénticas. Se desactiva con `min_short_run >= min_match_words`.
    """

    # Un k-grama corto que aparece más veces que esto en el documento es una
    # muletilla ("de la presente investigación") y no sirve como semilla.
    SHORT_RUN_MAX_DOCUMENT_OCCURRENCES = 8

    EXCERPT_MAX_CHARS = 900

    def __init__(
        self,
        min_match_words: int | None = None,
        max_gap_words: int | None = None,
        min_short_run: int | None = None,
        min_fuzzy_span: int | None = None,
        min_fuzzy_ratio: float | None = None,
    ) -> None:
        self.min_match_words = max(
            2,
            min_match_words
            if min_match_words is not None
            else getattr(settings, "SIMILARITY_MIN_MATCH_WORDS", 8),
        )
        self.max_gap_words = max(
            0,
            max_gap_words
            if max_gap_words is not None
            else getattr(settings, "SIMILARITY_MAX_GAP_WORDS", 2),
        )
        self.min_short_run = max(
            2,
            min_short_run
            if min_short_run is not None
            else getattr(settings, "SIMILARITY_MIN_SHORT_RUN", 4),
        )
        self.min_fuzzy_span = (
            min_fuzzy_span
            if min_fuzzy_span is not None
            else getattr(settings, "SIMILARITY_MIN_FUZZY_SPAN", 15)
        )
        self.min_fuzzy_ratio = (
            min_fuzzy_ratio
            if min_fuzzy_ratio is not None
            else getattr(settings, "SIMILARITY_MIN_FUZZY_RATIO", 0.7)
        )
        self._normalized_words: dict[str, str] = {}

    def analyze(
        self,
        content: str,
        sources: Sequence[SpanSource],
        excluded_ranges: Sequence[tuple[int, int]] = (),
    ) -> SpanAnalysisResult:
        words = self._document_words(content, excluded_ranges)
        tokens = [word[0] for word in words]
        countable_word_count = sum(1 for token in tokens if token is not None)

        if countable_word_count == 0 or not sources:
            return SpanAnalysisResult(
                similarity_percent=Decimal("0.00"),
                countable_word_count=countable_word_count,
            )

        document_kgrams = self._kgram_positions(tokens, self.min_match_words)
        document_short_kgrams = (
            self._kgram_positions(tokens, self.min_short_run)
            if self.fuzzy_enabled
            else {}
        )

        if not document_kgrams:
            return SpanAnalysisResult(
                similarity_percent=Decimal("0.00"),
                countable_word_count=countable_word_count,
            )

        # Por fuente: palabra del documento -> palabra de la fuente con la
        # que coincide (None para las palabras de un hueco unido), y los
        # tokens de la fuente para armar el extracto después.
        coverages: list[dict[int, int | None]] = []
        source_tokens: list[list[tuple[str | None, int, int, int]]] = []
        covered_sources: list[SpanSource] = []
        # Por fuente: palabras que solo cubre la paráfrasis ligera.
        fuzzy_words: list[set[int]] = []

        for source in sources:
            tokens_of_source = self._source_tokens(source)
            source_words = [token[0] for token in tokens_of_source]
            coverage = self._coverage(
                document_kgrams=document_kgrams,
                source_words=source_words,
            )

            if coverage:
                self._fill_gaps(coverage=coverage, tokens=tokens)

            fuzzy_coverage = (
                self._fuzzy_coverage(
                    document_short_kgrams=document_short_kgrams,
                    source_words=source_words,
                    tokens=tokens,
                )
                if document_short_kgrams
                else {}
            )
            only_fuzzy = set(fuzzy_coverage) - set(coverage)

            for word_index in only_fuzzy:
                coverage[word_index] = fuzzy_coverage[word_index]

            if not coverage:
                continue

            coverages.append(coverage)
            source_tokens.append(tokens_of_source)
            covered_sources.append(source)
            fuzzy_words.append(only_fuzzy)

        owner = self._attribute(coverages=coverages, word_count=len(tokens))

        assigned_by_source: dict[int, list[int]] = {}

        for word_index, source_index in enumerate(owner):
            if source_index >= 0:
                assigned_by_source.setdefault(source_index, []).append(word_index)

        # Tramos de cada fuente con su tipo y las palabras que cuentan para
        # el %: en un bloque aproximado solo las idénticas (las cambiadas del
        # hueco se resaltan pero no suman); en uno exacto, todo el tramo.
        runs_by_source: dict[int, list[tuple[int, int, str, int]]] = {}
        counted_by_source: dict[int, int] = {}

        for source_index, assigned in assigned_by_source.items():
            coverage = coverages[source_index]
            runs: list[tuple[int, int, str, int]] = []

            for first, last in self._runs(assigned):
                if any(
                    index in fuzzy_words[source_index]
                    for index in range(first, last + 1)
                ):
                    match_type = MATCH_TYPE_FUZZY
                    counted = sum(
                        1
                        for index in range(first, last + 1)
                        if coverage[index] is not None
                    )
                else:
                    match_type = MATCH_TYPE_EXACT
                    counted = last - first + 1

                runs.append((first, last, match_type, counted))

            runs_by_source[source_index] = runs
            counted_by_source[source_index] = sum(run[3] for run in runs)

        source_results: list[SpanSourceResult] = []
        source_percents: dict[int, Decimal] = {}

        for source_index, counted in counted_by_source.items():
            percent = _percent(counted, countable_word_count)
            source_percents[source_index] = percent
            source_results.append(
                SpanSourceResult(
                    source=covered_sources[source_index],
                    word_count=counted,
                    percent=percent,
                )
            )

        source_results.sort(key=lambda item: item.word_count, reverse=True)

        matches: list[SpanMatch] = []

        for source_index, runs in runs_by_source.items():
            for first, last, match_type, counted in runs:
                start_offset = words[first][1]
                end_offset = words[last][2]

                matches.append(
                    SpanMatch(
                        source=covered_sources[source_index],
                        source_percent=source_percents[source_index],
                        start_offset=start_offset,
                        end_offset=end_offset,
                        word_count=counted,
                        text_excerpt=content[start_offset:end_offset][
                            : self.EXCERPT_MAX_CHARS
                        ],
                        source_excerpt=self._source_excerpt(
                            source=covered_sources[source_index],
                            tokens=source_tokens[source_index],
                            coverage=coverages[source_index],
                            first=first,
                            last=last,
                        ),
                        match_type=match_type,
                    )
                )

        matches.sort(key=lambda match: match.start_offset)

        total_assigned = sum(item.word_count for item in source_results)

        return SpanAnalysisResult(
            similarity_percent=_percent(total_assigned, countable_word_count),
            countable_word_count=countable_word_count,
            sources=source_results,
            matches=matches,
        )

    @property
    def fuzzy_enabled(self) -> bool:
        return self.min_short_run < self.min_match_words

    def _normalize_word(self, word: str) -> str:
        normalized = self._normalized_words.get(word)

        if normalized is None:
            normalized = "".join(
                char
                for char in unicodedata.normalize("NFKD", word.lower())
                if not unicodedata.combining(char)
            )
            self._normalized_words[word] = normalized

        return normalized

    def _document_words(
        self,
        content: str,
        excluded_ranges: Sequence[tuple[int, int]],
    ) -> list[tuple[str | None, int, int]]:
        """
        (palabra normalizada, inicio, fin). Las palabras dentro de una cita
        excluida quedan como None: no cuentan y cortan cualquier tramo.
        """
        excluded = merge_ranges(excluded_ranges)
        excluded_starts = [start for start, _ in excluded]
        excluded_ends = [end for _, end in excluded]
        words: list[tuple[str | None, int, int]] = []

        for match in WORD_PATTERN.finditer(content):
            start, end = match.start(), match.end()
            index = bisect_right(excluded_starts, start) - 1

            if index >= 0 and start < excluded_ends[index]:
                words.append((None, start, end))
            else:
                words.append((self._normalize_word(match.group(0)), start, end))

        return words

    def _source_tokens(
        self,
        source: SpanSource,
    ) -> list[tuple[str | None, int, int, int]]:
        """
        (palabra normalizada, tramo, inicio, fin), con un None entre tramos
        para que ningún k-grama una dos tramos no contiguos de la fuente.
        """
        tokens: list[tuple[str | None, int, int, int]] = []

        for segment_index, segment in enumerate(source.segments):
            if tokens and tokens[-1][0] is not None:
                tokens.append((None, segment_index, 0, 0))

            for match in WORD_PATTERN.finditer(segment):
                tokens.append(
                    (
                        self._normalize_word(match.group(0)),
                        segment_index,
                        match.start(),
                        match.end(),
                    )
                )

        return tokens

    def _kgram_positions(
        self,
        tokens: list[str | None],
        size: int,
    ) -> dict[tuple[str, ...], list[int]]:
        positions: dict[tuple[str, ...], list[int]] = {}
        # Cantidad de palabras válidas (no None) seguidas que terminan en i.
        valid_run = 0

        for index, token in enumerate(tokens):
            valid_run = valid_run + 1 if token is not None else 0

            if valid_run >= size:
                start = index - size + 1
                positions.setdefault(tuple(tokens[start : index + 1]), []).append(
                    start
                )

        return positions

    def _coverage(
        self,
        document_kgrams: dict[tuple[str, ...], list[int]],
        source_words: list[str | None],
    ) -> dict[int, int | None]:
        size = self.min_match_words
        coverage: dict[int, int | None] = {}
        valid_run = 0

        for index, token in enumerate(source_words):
            valid_run = valid_run + 1 if token is not None else 0

            if valid_run < size:
                continue

            source_start = index - size + 1
            document_starts = document_kgrams.get(
                tuple(source_words[source_start : index + 1])
            )

            if not document_starts:
                continue

            for document_start in document_starts:
                for offset in range(size):
                    coverage.setdefault(
                        document_start + offset,
                        source_start + offset,
                    )

        return coverage

    def _fuzzy_coverage(
        self,
        document_short_kgrams: dict[tuple[str, ...], list[int]],
        source_words: list[str | None],
        tokens: list[str | None],
    ) -> dict[int, int | None]:
        """
        Paráfrasis ligera: encadena tramos cortos idénticos en orden y
        devuelve la cobertura (palabra del documento -> palabra de la fuente,
        None en los huecos) de los bloques que cumplen largo y proporción.
        """
        seeds = self._short_seeds(document_short_kgrams, source_words)

        if not seeds:
            return {}

        # Cadena de mayor cantidad de palabras idénticas que termina en cada
        # semilla. Las semillas van ordenadas por inicio en el documento, así
        # que todo predecesor posible ya está calculado.
        seeds.sort()
        seeds_by_document_end: dict[int, list[int]] = {}
        chain_matched: list[int] = []
        chain_previous: list[int] = []

        for index, (document_start, source_start, length) in enumerate(seeds):
            best_matched, best_previous = length, -1

            for document_gap in range(self.max_gap_words + 1):
                previous_end = document_start - document_gap - 1

                if document_gap and any(
                    tokens[gap_index] is None
                    for gap_index in range(previous_end + 1, document_start)
                ):
                    continue

                for previous in seeds_by_document_end.get(previous_end, ()):
                    _, previous_source_start, previous_length = seeds[previous]
                    source_gap = (
                        source_start - (previous_source_start + previous_length)
                    )

                    if not 0 <= source_gap <= self.max_gap_words:
                        continue

                    if any(
                        source_words[gap_index] is None
                        for gap_index in range(
                            previous_source_start + previous_length,
                            source_start,
                        )
                    ):
                        continue

                    matched = chain_matched[previous] + length

                    if matched > best_matched:
                        best_matched, best_previous = matched, previous

            chain_matched.append(best_matched)
            chain_previous.append(best_previous)
            seeds_by_document_end.setdefault(
                document_start + length - 1, []
            ).append(index)

        coverage: dict[int, int | None] = {}
        is_predecessor = set(chain_previous)

        for index in range(len(seeds)):
            if index in is_predecessor:
                continue

            chain = [index]

            while chain_previous[chain[-1]] >= 0:
                chain.append(chain_previous[chain[-1]])

            chain.reverse()
            first_start = seeds[chain[0]][0]
            last_start, _, last_length = seeds[chain[-1]]
            span = last_start + last_length - first_start

            if (
                len(chain) < 2
                or span < self.min_fuzzy_span
                or chain_matched[index] < self.min_fuzzy_ratio * span
            ):
                continue

            for document_index in range(first_start, last_start + last_length):
                coverage.setdefault(document_index, None)

            for seed_index in chain:
                document_start, source_start, length = seeds[seed_index]

                for offset in range(length):
                    if coverage.get(document_start + offset) is None:
                        coverage[document_start + offset] = source_start + offset

        return coverage

    def _short_seeds(
        self,
        document_short_kgrams: dict[tuple[str, ...], list[int]],
        source_words: list[str | None],
    ) -> list[tuple[int, int, int]]:
        """
        Tramos maximales de >= `min_short_run` palabras idénticas como
        (inicio en el documento, inicio en la fuente, largo): los k-gramas
        cortos comunes que caen en la misma diagonal y se solapan forman un
        solo tramo.
        """
        size = self.min_short_run
        hits_by_diagonal: dict[int, list[int]] = {}
        valid_run = 0

        for index, token in enumerate(source_words):
            valid_run = valid_run + 1 if token is not None else 0

            if valid_run < size:
                continue

            source_start = index - size + 1
            document_starts = document_short_kgrams.get(
                tuple(source_words[source_start : index + 1])
            )

            if (
                not document_starts
                or len(document_starts) > self.SHORT_RUN_MAX_DOCUMENT_OCCURRENCES
            ):
                continue

            for document_start in document_starts:
                hits_by_diagonal.setdefault(
                    source_start - document_start, []
                ).append(document_start)

        seeds: list[tuple[int, int, int]] = []

        for diagonal, document_starts in hits_by_diagonal.items():
            document_starts.sort()
            run_start = run_end = document_starts[0]

            for document_start in document_starts[1:] + [None]:
                # Mismo diagonal y k-gramas que se tocan o solapan: todas
                # las palabras entre ellos son idénticas.
                if document_start is not None and document_start <= run_end + size:
                    run_end = document_start
                    continue

                seeds.append(
                    (run_start, run_start + diagonal, run_end - run_start + size)
                )

                if document_start is not None:
                    run_start = run_end = document_start

        return seeds

    def _fill_gaps(
        self,
        coverage: dict[int, int | None],
        tokens: list[str | None],
    ) -> None:
        if self.max_gap_words <= 0:
            return

        covered = sorted(coverage)

        for previous, current in zip(covered, covered[1:]):
            gap = current - previous - 1

            if gap <= 0 or gap > self.max_gap_words:
                continue

            # Un hueco que pisa una cita excluida no se une: esas palabras
            # no cuentan y la coincidencia no es continua en el documento.
            if any(tokens[index] is None for index in range(previous + 1, current)):
                continue

            for index in range(previous + 1, current):
                coverage[index] = None

    def _attribute(
        self,
        coverages: list[dict[int, int | None]],
        word_count: int,
    ) -> list[int]:
        """
        Voraz con evaluación perezosa: el conteo de palabras libres de una
        fuente solo puede bajar a medida que otras se asignan, así que basta
        con recalcular la fuente del tope del heap antes de aceptarla.
        """
        owner = [-1] * word_count
        heap = [
            (-len(coverage), source_index)
            for source_index, coverage in enumerate(coverages)
        ]
        heapq.heapify(heap)

        while heap:
            negative_count, source_index = heapq.heappop(heap)
            free_words = self._free_words(coverages[source_index], owner)

            if not free_words:
                continue

            if len(free_words) < -negative_count and heap and len(free_words) < -heap[0][0]:
                heapq.heappush(heap, (-len(free_words), source_index))
                continue

            for word_index in free_words:
                owner[word_index] = source_index

        return owner

    def _free_words(
        self,
        coverage: dict[int, int | None],
        owner: list[int],
    ) -> list[int]:
        """
        Palabras de la fuente que siguen libres, descartando los restos de
        menos de `min_match_words` que deja una fuente mayor al quedarse con
        parte del tramo: no se muestra una "coincidencia" de 2 palabras.
        """
        free = sorted(
            word_index for word_index in coverage if owner[word_index] == -1
        )

        return [
            word_index
            for first, last in self._runs(free)
            if last - first + 1 >= self.min_match_words
            for word_index in range(first, last + 1)
        ]

    def _runs(self, indices: list[int]) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []

        for index in indices:
            if runs and index == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], index)
            else:
                runs.append((index, index))

        return runs

    def _source_excerpt(
        self,
        source: SpanSource,
        tokens: list[tuple[str | None, int, int, int]],
        coverage: dict[int, int | None],
        first: int,
        last: int,
    ) -> str:
        source_positions = [
            coverage[index]
            for index in range(first, last + 1)
            if coverage.get(index) is not None
        ]

        if not source_positions:
            return ""

        _, first_segment, excerpt_start, _ = tokens[source_positions[0]]
        _, last_segment, _, excerpt_end = tokens[source_positions[-1]]
        segment = source.segments[first_segment]

        if last_segment != first_segment or excerpt_end <= excerpt_start:
            excerpt_end = excerpt_start + self.EXCERPT_MAX_CHARS

        return segment[excerpt_start:excerpt_end][: self.EXCERPT_MAX_CHARS]
