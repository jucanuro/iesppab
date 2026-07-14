from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


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


@dataclass(frozen=True)
class SimilarityAnalysisResult:
    similarity_percent: Decimal
    matches: list[SimilarityMatch]


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
    ) -> SimilarityAnalysisResult:
        if not content.strip() or not candidates:
            return SimilarityAnalysisResult(
                similarity_percent=Decimal("0.00"),
                matches=[],
            )

        current_chunks = self._build_current_chunks(content=content)
        matches: list[SimilarityMatch] = []

        for chunk_text, normalized_chunk, start_offset, end_offset in current_chunks:
            best_match: SimilarityMatch | None = None

            for candidate in candidates:
                candidate_match = self._compare_chunk(
                    chunk_text=chunk_text,
                    normalized_chunk=normalized_chunk,
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
        )[: self.MAX_MATCHES]

        similarity_percent = self._calculate_total_similarity(
            content_length=len(content),
            matched_ranges=[
                (match.start_offset, match.end_offset)
                for match in matches
            ],
        )

        return SimilarityAnalysisResult(
            similarity_percent=similarity_percent,
            matches=matches,
        )

    def _compare_chunk(
        self,
        chunk_text: str,
        normalized_chunk: str,
        start_offset: int,
        end_offset: int,
        candidate: CandidateKnowledgeChunk,
    ) -> SimilarityMatch | None:
        if not normalized_chunk or not candidate.normalized_text:
            return None

        current_shingles = self._build_shingles(normalized_chunk)
        candidate_shingles = self._build_shingles(candidate.normalized_text)

        if not current_shingles or not candidate_shingles:
            return None

        intersection = current_shingles.intersection(candidate_shingles)

        if not intersection:
            return None

        coverage = Decimal(len(intersection)) / Decimal(len(current_shingles))

        dice = Decimal(2 * len(intersection)) / Decimal(
            len(current_shingles) + len(candidate_shingles)
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
    ) -> list[tuple[str, str, int, int]]:
        words_with_offsets = self._words_with_offsets(content=content)

        if len(words_with_offsets) < self.MIN_WORDS:
            return []

        chunks: list[tuple[str, str, int, int]] = []

        for start_index in range(0, len(words_with_offsets), self.CHUNK_STEP):
            selected_words = words_with_offsets[
                start_index : start_index + self.CHUNK_WORD_SIZE
            ]

            if len(selected_words) < self.MIN_WORDS:
                continue

            start_offset = selected_words[0][1]
            end_offset = selected_words[-1][2]
            chunk_text = content[start_offset:end_offset].strip()
            normalized_chunk = self._normalize(chunk_text)

            if normalized_chunk:
                chunks.append(
                    (
                        chunk_text,
                        normalized_chunk,
                        start_offset,
                        end_offset,
                    )
                )

        return chunks

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
        words = text.split()

        if len(words) < self.SHINGLE_SIZE:
            return set(words)

        return {
            " ".join(words[index : index + self.SHINGLE_SIZE])
            for index in range(len(words) - self.SHINGLE_SIZE + 1)
        }

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
    ) -> Decimal:
        if content_length <= 0 or not matched_ranges:
            return Decimal("0.00")

        merged_ranges = self._merge_ranges(ranges=matched_ranges)
        matched_chars = sum(end - start for start, end in merged_ranges)

        percent = (
            Decimal(matched_chars) / Decimal(content_length)
        ) * Decimal("100")

        return min(percent, Decimal("100.00")).quantize(Decimal("0.01"))

    def _merge_ranges(
        self,
        ranges: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        sorted_ranges = sorted(ranges)
        merged: list[tuple[int, int]] = []

        for start, end in sorted_ranges:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                previous_start, previous_end = merged[-1]
                merged[-1] = (previous_start, max(previous_end, end))

        return merged