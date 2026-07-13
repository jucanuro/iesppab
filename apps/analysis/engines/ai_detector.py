from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AIFinding:
    start_offset: int
    end_offset: int
    text_excerpt: str
    confidence_percent: Decimal


@dataclass(frozen=True)
class AIAnalysisResult:
    ai_probability_percent: Decimal
    findings: list[AIFinding]


class SpanishAIDetector:
    """
    Detector heurístico MVP para español.

    No acusa ni prueba uso de IA.
    Solo entrega una estimación técnica basada en regularidad,
    diversidad léxica, conectores y variabilidad sintáctica.
    """

    MIN_PARAGRAPH_LENGTH = 220
    FINDING_THRESHOLD = Decimal("62.00")

    CONNECTORS = {
        "además",
        "asimismo",
        "por lo tanto",
        "por consiguiente",
        "en consecuencia",
        "sin embargo",
        "no obstante",
        "cabe destacar",
        "en este sentido",
        "de esta manera",
        "por otro lado",
        "en conclusión",
        "finalmente",
        "en primer lugar",
        "en segundo lugar",
    }

    def analyze(self, content: str) -> AIAnalysisResult:
        paragraphs = self._split_paragraphs(content=content)

        findings: list[AIFinding] = []
        weighted_score = Decimal("0.00")
        total_weight = Decimal("0.00")

        for paragraph, start_offset, end_offset in paragraphs:
            score = self._score_paragraph(paragraph=paragraph)
            weight = Decimal(len(paragraph))

            weighted_score += score * weight
            total_weight += weight

            if score >= self.FINDING_THRESHOLD:
                findings.append(
                    AIFinding(
                        start_offset=start_offset,
                        end_offset=end_offset,
                        text_excerpt=paragraph[:700],
                        confidence_percent=score,
                    )
                )

        if total_weight == 0:
            return AIAnalysisResult(
                ai_probability_percent=Decimal("0.00"),
                findings=[],
            )

        probability = (weighted_score / total_weight).quantize(
            Decimal("0.01")
        )

        return AIAnalysisResult(
            ai_probability_percent=probability,
            findings=sorted(
                findings,
                key=lambda item: item.confidence_percent,
                reverse=True,
            )[:12],
        )

    def _split_paragraphs(self, content: str) -> list[tuple[str, int, int]]:
        raw_paragraphs = re.split(r"\n\s*\n", content)
        paragraphs: list[tuple[str, int, int]] = []
        cursor = 0

        for raw in raw_paragraphs:
            paragraph = raw.strip()

            if len(paragraph) < self.MIN_PARAGRAPH_LENGTH:
                cursor += len(raw) + 2
                continue

            start = content.find(paragraph, cursor)

            if start == -1:
                start = content.find(paragraph)

            if start == -1:
                cursor += len(raw) + 2
                continue

            end = start + len(paragraph)
            paragraphs.append((paragraph, start, end))
            cursor = end

        return paragraphs

    def _score_paragraph(self, paragraph: str) -> Decimal:
        words = self._words(paragraph)
        sentences = self._sentences(paragraph)

        if len(words) < 80 or len(sentences) < 3:
            return Decimal("0.00")

        sentence_lengths = [len(self._words(sentence)) for sentence in sentences]
        average_length = sum(sentence_lengths) / len(sentence_lengths)
        std_dev = self._standard_deviation(sentence_lengths)
        lexical_diversity = len(set(words)) / len(words)
        connector_ratio = self._connector_ratio(paragraph=paragraph)
        punctuation_ratio = self._punctuation_ratio(paragraph=paragraph)

        score = Decimal("0.00")

        if 14 <= average_length <= 30:
            score += Decimal("20.00")

        if std_dev <= 8:
            score += Decimal("20.00")
        elif std_dev <= 12:
            score += Decimal("10.00")

        if 0.45 <= lexical_diversity <= 0.72:
            score += Decimal("18.00")

        if connector_ratio >= 0.018:
            score += Decimal("20.00")
        elif connector_ratio >= 0.010:
            score += Decimal("10.00")

        if 0.025 <= punctuation_ratio <= 0.075:
            score += Decimal("12.00")

        if self._has_balanced_sentence_structure(sentence_lengths):
            score += Decimal("10.00")

        return min(score, Decimal("95.00")).quantize(Decimal("0.01"))

    def _words(self, text: str) -> list[str]:
        return re.findall(r"\b[a-záéíóúñü]+\b", text.lower())

    def _sentences(self, text: str) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        return [sentence for sentence in sentences if sentence.strip()]

    def _standard_deviation(self, values: list[int]) -> float:
        if not values:
            return 0.0

        average = sum(values) / len(values)
        variance = sum((value - average) ** 2 for value in values) / len(values)

        return math.sqrt(variance)

    def _connector_ratio(self, paragraph: str) -> float:
        paragraph_lower = paragraph.lower()
        count = sum(
            paragraph_lower.count(connector)
            for connector in self.CONNECTORS
        )
        words_count = max(len(self._words(paragraph)), 1)

        return count / words_count

    def _punctuation_ratio(self, paragraph: str) -> float:
        punctuation_count = len(re.findall(r"[.,;:]", paragraph))
        return punctuation_count / max(len(paragraph), 1)

    def _has_balanced_sentence_structure(
        self,
        sentence_lengths: list[int],
    ) -> bool:
        if len(sentence_lengths) < 4:
            return False

        min_length = min(sentence_lengths)
        max_length = max(sentence_lengths)

        return min_length >= 8 and max_length <= 36