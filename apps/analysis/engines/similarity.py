from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True)
class CandidateDocumentText:
    document_id: UUID
    title: str
    owner_name: str
    content: str


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
    Motor MVP de similitud interna.

    Técnica:
    - Divide el documento en segmentos.
    - Normaliza texto.
    - Crea shingles de palabras.
    - Compara con documentos internos mediante Jaccard.
    """

    MIN_SEGMENT_LENGTH = 160
    SHINGLE_SIZE = 8
    MATCH_THRESHOLD = Decimal("0.34")
    MAX_MATCHES = 15

    def analyze(
        self,
        content: str,
        candidates: list[CandidateDocumentText],
    ) -> SimilarityAnalysisResult:
        if not content.strip() or not candidates:
            return SimilarityAnalysisResult(
                similarity_percent=Decimal("0.00"),
                matches=[],
            )

        matches: list[SimilarityMatch] = []
        matched_ranges: list[tuple[int, int]] = []

        segments = self._split_segments(content=content)

        for segment, start_offset, end_offset in segments:
            best_match: SimilarityMatch | None = None

            for candidate in candidates:
                candidate_match = self._compare_segment_with_candidate(
                    segment=segment,
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
                matched_ranges.append((start_offset, end_offset))

        matches = sorted(
            matches,
            key=lambda item: item.matched_percent,
            reverse=True,
        )[: self.MAX_MATCHES]

        similarity_percent = self._calculate_total_similarity(
            content_length=len(content),
            matched_ranges=matched_ranges,
        )

        return SimilarityAnalysisResult(
            similarity_percent=similarity_percent,
            matches=matches,
        )

    def _compare_segment_with_candidate(
        self,
        segment: str,
        start_offset: int,
        end_offset: int,
        candidate: CandidateDocumentText,
    ) -> SimilarityMatch | None:
        normalized_segment = self._normalize(segment)
        normalized_candidate = self._normalize(candidate.content)

        segment_shingles = self._build_shingles(normalized_segment)
        candidate_shingles = self._build_shingles(normalized_candidate)

        if not segment_shingles or not candidate_shingles:
            return None

        intersection = segment_shingles.intersection(candidate_shingles)
        union = segment_shingles.union(candidate_shingles)

        if not union:
            return None

        score = Decimal(len(intersection)) / Decimal(len(union))

        if score < self.MATCH_THRESHOLD:
            return None

        matched_percent = (score * Decimal("100")).quantize(Decimal("0.01"))

        return SimilarityMatch(
            source_document_id=candidate.document_id,
            source_title=candidate.title,
            source_owner_name=candidate.owner_name,
            matched_percent=matched_percent,
            text_excerpt=segment.strip()[:700],
            source_excerpt=self._find_source_excerpt(
                segment=segment,
                candidate_content=candidate.content,
            ),
            start_offset=start_offset,
            end_offset=end_offset,
        )

    def _split_segments(self, content: str) -> list[tuple[str, int, int]]:
        paragraphs = re.split(r"\n\s*\n", content)
        segments: list[tuple[str, int, int]] = []
        cursor = 0

        for paragraph in paragraphs:
            clean = paragraph.strip()

            if len(clean) < self.MIN_SEGMENT_LENGTH:
                cursor += len(paragraph) + 2
                continue

            start = content.find(clean, cursor)

            if start == -1:
                start = content.find(clean)

            if start == -1:
                cursor += len(paragraph) + 2
                continue

            end = start + len(clean)
            segments.append((clean, start, end))
            cursor = end

        return segments

    def _build_shingles(self, text: str) -> set[str]:
        words = text.split()

        if len(words) < self.SHINGLE_SIZE:
            return set(words)

        return {
            " ".join(words[index : index + self.SHINGLE_SIZE])
            for index in range(len(words) - self.SHINGLE_SIZE + 1)
        }

    def _normalize(self, text: str) -> str:
        text = text.lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(
            char for char in text if not unicodedata.combining(char)
        )
        text = re.sub(r"[^a-záéíóúñü0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _find_source_excerpt(
        self,
        segment: str,
        candidate_content: str,
    ) -> str:
        segment_words = self._normalize(segment).split()

        if not segment_words:
            return candidate_content[:700]

        anchor = " ".join(segment_words[: min(6, len(segment_words))])
        normalized_candidate = self._normalize(candidate_content)
        index = normalized_candidate.find(anchor)

        if index == -1:
            return candidate_content[:700]

        return candidate_content[max(0, index - 120) : index + 580]

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