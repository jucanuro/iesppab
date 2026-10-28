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
    word_count: int
    text_excerpt: str
    source_excerpt: str


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
       excluidas no son contables ni pueden coincidir).
    """

    EXCERPT_MAX_CHARS = 900

    def __init__(
        self,
        min_match_words: int | None = None,
        max_gap_words: int | None = None,
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

        document_kgrams = self._kgram_positions(tokens)

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

        for source in sources:
            tokens_of_source = self._source_tokens(source)
            coverage = self._coverage(
                document_kgrams=document_kgrams,
                source_words=[token[0] for token in tokens_of_source],
            )

            if not coverage:
                continue

            self._fill_gaps(coverage=coverage, tokens=tokens)
            coverages.append(coverage)
            source_tokens.append(tokens_of_source)
            covered_sources.append(source)

        owner = self._attribute(coverages=coverages, word_count=len(tokens))

        assigned_by_source: dict[int, list[int]] = {}

        for word_index, source_index in enumerate(owner):
            if source_index >= 0:
                assigned_by_source.setdefault(source_index, []).append(word_index)

        source_results: list[SpanSourceResult] = []
        source_percents: dict[int, Decimal] = {}

        for source_index, assigned in assigned_by_source.items():
            percent = _percent(len(assigned), countable_word_count)
            source_percents[source_index] = percent
            source_results.append(
                SpanSourceResult(
                    source=covered_sources[source_index],
                    word_count=len(assigned),
                    percent=percent,
                )
            )

        source_results.sort(key=lambda item: item.word_count, reverse=True)

        matches: list[SpanMatch] = []

        for source_index, assigned in assigned_by_source.items():
            for first, last in self._runs(assigned):
                start_offset = words[first][1]
                end_offset = words[last][2]

                matches.append(
                    SpanMatch(
                        source=covered_sources[source_index],
                        source_percent=source_percents[source_index],
                        start_offset=start_offset,
                        end_offset=end_offset,
                        word_count=last - first + 1,
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
    ) -> dict[tuple[str, ...], list[int]]:
        size = self.min_match_words
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
