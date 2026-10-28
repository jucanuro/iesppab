from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from apps.analysis.engines.exclusions import (
    build_chunks,
    calculate_similarity_percent,
    shingles_without_boundary,
)


@dataclass(frozen=True)
class CandidateKnowledgeChunk:
    document_id: UUID
    title: str
    owner_name: str
    text_excerpt: str
    normalized_text: str


@dataclass(frozen=True)
class SimilarityMatch:
    source_document_id: UUID
    source_title: str
    source_owner_name: str
    matched_percent: Decimal
    text_excerpt: str
    source_excerpt: str
    start_offset: int
    end_offset: int
    # Solo lo llena el motor span: "exacta" o "aproximada" (paráfrasis).
    match_type: str = ""


@dataclass(frozen=True)
class SimilarityAnalysisResult:
    similarity_percent: Decimal
    matches: list[SimilarityMatch]
    # Rangos (start_offset, end_offset) de TODOS los fragmentos que
    # superaron el umbral, sin truncar por MAX_MATCHES. `matches` se recorta
    # para el detalle mostrado/persistido; el cálculo del % total (aquí y en
    # el número principal del reporte) debe usar este conjunto completo para
    # no subestimar el plagio en una tesis larga con muchas coincidencias.
    all_matched_ranges: list[tuple[int, int]] = field(default_factory=list)


class InternalSimilarityEngine:
    """
    Motor de similitud interna basado en fragmentos aprendidos.

    Ya no compara documento completo contra documento completo.
    Ahora compara fragmentos del documento actual contra el banco interno
    de fragmentos aprendidos.
    """

    CHUNK_WORD_SIZE = 85
    CHUNK_STEP = 45
    MIN_WORDS = 30
    SHINGLE_SIZE = 4
    MATCH_THRESHOLD = Decimal("0.16")
    MAX_MATCHES = 35

    def analyze(
        self,
        content: str,
        candidates: list[CandidateKnowledgeChunk],
        excluded_ranges: Sequence[tuple[int, int]] = (),
    ) -> SimilarityAnalysisResult:
        if not content.strip() or not candidates:
            return SimilarityAnalysisResult(
                similarity_percent=Decimal("0.00"),
                matches=[],
                all_matched_ranges=[],
            )

        current_chunks = self._build_current_chunks(
            content=content,
            excluded_ranges=excluded_ranges,
        )

        # Los shingles de cada candidato no cambian entre fragmentos del
        # documento actual, así que se construyen una sola vez aquí en vez
        # de reconstruirlos en cada una de las N comparaciones (uno por
        # fragmento) que recibe ese mismo candidato.
        candidate_shingles = [
            self._build_shingles(candidate.normalized_text)
            for candidate in candidates
        ]

        matches: list[SimilarityMatch] = []

        for chunk_text, normalized_chunk, start_offset, end_offset in current_chunks:
            current_shingles = self._build_shingles(normalized_chunk)

            if not current_shingles:
                continue

            best_match: SimilarityMatch | None = None

            for candidate, candidate_shingle_set in zip(
                candidates,
                candidate_shingles,
            ):
                candidate_match = self._compare_chunk(
                    chunk_text=chunk_text,
                    normalized_chunk=normalized_chunk,
                    current_shingles=current_shingles,
                    candidate_shingle_set=candidate_shingle_set,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    candidate=candidate,
                )

                if candidate_match is None:
                    continue

                if (
                    best_match is None
                    or candidate_match.matched_percent
                    > best_match.matched_percent
                ):
                    best_match = candidate_match

            if best_match is not None:
                matches.append(best_match)

        matches = sorted(
            matches,
            key=lambda item: item.matched_percent,
            reverse=True,
        )

        # El % total se calcula sobre TODOS los fragmentos que superaron el
        # umbral. Truncar antes subestima el plagio justo en la tesis larga
        # con más de MAX_MATCHES coincidencias. El límite solo recorta el
        # detalle que se persiste/muestra, nunca el cálculo del score.
        all_matched_ranges = [
            (match.start_offset, match.end_offset)
            for match in matches
        ]

        similarity_percent = self._calculate_total_similarity(
            content_length=len(content),
            matched_ranges=all_matched_ranges,
            excluded_ranges=excluded_ranges,
        )

        return SimilarityAnalysisResult(
            similarity_percent=similarity_percent,
            matches=matches[: self.MAX_MATCHES],
            all_matched_ranges=all_matched_ranges,
        )

    def _compare_chunk(
        self,
        chunk_text: str,
        normalized_chunk: str,
        current_shingles: set[str],
        candidate_shingle_set: set[str],
        start_offset: int,
        end_offset: int,
        candidate: CandidateKnowledgeChunk,
    ) -> SimilarityMatch | None:
        if not normalized_chunk or not candidate.normalized_text:
            return None

        if not current_shingles or not candidate_shingle_set:
            return None

        intersection = current_shingles.intersection(candidate_shingle_set)

        if not intersection:
            return None

        coverage = Decimal(len(intersection)) / Decimal(len(current_shingles))

        dice = Decimal(2 * len(intersection)) / Decimal(
            len(current_shingles) + len(candidate_shingle_set)
        )

        score = (coverage * Decimal("0.70")) + (dice * Decimal("0.30"))

        exact_bonus = self._exact_phrase_bonus(
            normalized_chunk=normalized_chunk,
            normalized_candidate=candidate.normalized_text,
        )

        score = min(score + exact_bonus, Decimal("1.00"))

        if score < self.MATCH_THRESHOLD:
            return None

        matched_percent = (score * Decimal("100")).quantize(Decimal("0.01"))

        return SimilarityMatch(
            source_document_id=candidate.document_id,
            source_title=candidate.title,
            source_owner_name=candidate.owner_name,
            matched_percent=matched_percent,
            text_excerpt=chunk_text[:900],
            source_excerpt=candidate.text_excerpt[:900],
            start_offset=start_offset,
            end_offset=end_offset,
        )

    def _build_current_chunks(
        self,
        content: str,
        excluded_ranges: Sequence[tuple[int, int]] = (),
    ) -> list[tuple[str, str, int, int]]:
        # Las palabras de una cita excluida no participan de los shingles
        # del fragmento (ver `exclusions.build_chunks`).
        return build_chunks(
            content=content,
            words_with_offsets=self._words_with_offsets(content=content),
            excluded_ranges=excluded_ranges,
            chunk_word_size=self.CHUNK_WORD_SIZE,
            chunk_step=self.CHUNK_STEP,
            min_words=self.MIN_WORDS,
            normalize=self._normalize,
        )

    def _words_with_offsets(self, content: str) -> list[tuple[str, int, int]]:
        matches = re.finditer(
            r"\b[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9]+\b",
            content,
            flags=re.UNICODE,
        )

        return [
            (match.group(0), match.start(), match.end())
            for match in matches
        ]

    def _build_shingles(self, text: str) -> set[str]:
        # Ningún shingle cruza el borde de una cita excluida.
        return shingles_without_boundary(text.split(), self.SHINGLE_SIZE)

    def _build_shingle_sequence(self, text: str) -> list[str]:
        """
        Igual que _build_shingles pero conservando orden de aparición y
        duplicados. Lo usa el motor de fingerprinting (Winnowing), que
        necesita la secuencia posicional de shingles, no solo el conjunto.
        """
        words = text.split()

        if len(words) < self.SHINGLE_SIZE:
            return words

        return [
            " ".join(words[index : index + self.SHINGLE_SIZE])
            for index in range(len(words) - self.SHINGLE_SIZE + 1)
        ]

    def _exact_phrase_bonus(
        self,
        normalized_chunk: str,
        normalized_candidate: str,
    ) -> Decimal:
        words = normalized_chunk.split()

        if len(words) < 12:
            return Decimal("0.00")

        for size in (18, 14, 10):
            for index in range(0, max(len(words) - size + 1, 1), 5):
                phrase = " ".join(words[index : index + size])

                if phrase and phrase in normalized_candidate:
                    if size >= 18:
                        return Decimal("0.18")
                    if size >= 14:
                        return Decimal("0.12")
                    return Decimal("0.08")

        return Decimal("0.00")

    def _normalize(self, text: str) -> str:
        text = text.lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(
            char for char in text if not unicodedata.combining(char)
        )
        text = re.sub(r"[^a-z0-9ñü\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _calculate_total_similarity(
        self,
        content_length: int,
        matched_ranges: list[tuple[int, int]],
        excluded_ranges: Sequence[tuple[int, int]] = (),
    ) -> Decimal:
        return calculate_similarity_percent(
            content_length=content_length,
            matched_ranges=matched_ranges,
            excluded_ranges=excluded_ranges,
        )
