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

    No prueba uso de IA. Solo estima patrones como:
    - baja variación sintáctica
    - exceso de conectores formales
    - estilo demasiado uniforme
    - baja presencia de marcas personales
    - estructura académica genérica
    """

    MIN_PARAGRAPH_LENGTH = 120
    FINDING_THRESHOLD = Decimal("38.00")

    FORMAL_CONNECTORS = {
        "además",
        "asimismo",
        "por lo tanto",
        "por consiguiente",
        "en consecuencia",
        "sin embargo",
        "no obstante",
        "cabe destacar",
        "es importante mencionar",
        "en este sentido",
        "de esta manera",
        "por otro lado",
        "en conclusión",
        "finalmente",
        "en primer lugar",
        "en segundo lugar",
        "desde esta perspectiva",
        "por ende",
    }

    GENERIC_ACADEMIC_PHRASES = {
        "el presente trabajo",
        "la presente investigación",
        "es importante señalar",
        "cabe resaltar",
        "en la actualidad",
        "en el ámbito educativo",
        "se puede evidenciar",
        "resulta fundamental",
        "de manera significativa",
        "contribuye al desarrollo",
        "permite fortalecer",
    }

    PERSONAL_MARKERS = {
        "yo",
        "nosotros",
        "considero",
        "observé",
        "realicé",
        "apliqué",
        "mi experiencia",
        "nuestra experiencia",
        "entrevisté",
        "visité",
    }

    def analyze(self, content: str) -> AIAnalysisResult:
        paragraphs = self._split_paragraphs(content=content)

        if not paragraphs:
            return AIAnalysisResult(
                ai_probability_percent=Decimal("0.00"),
                findings=[],
            )

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
                        text_excerpt=paragraph[:900],
                        confidence_percent=score,
                    )
                )

        probability = (
            weighted_score / total_weight
        ).quantize(Decimal("0.01"))

        return AIAnalysisResult(
            ai_probability_percent=probability,
            findings=sorted(
                findings,
                key=lambda item: item.confidence_percent,
                reverse=True,
            )[:20],
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

        if len(words) < 45 or len(sentences) < 2:
            return Decimal("0.00")

        sentence_lengths = [
            len(self._words(sentence))
            for sentence in sentences
            if self._words(sentence)
        ]

        if not sentence_lengths:
            return Decimal("0.00")

        average_length = sum(sentence_lengths) / len(sentence_lengths)
        std_dev = self._standard_deviation(sentence_lengths)
        lexical_diversity = len(set(words)) / len(words)
        connector_ratio = self._phrase_ratio(paragraph, self.FORMAL_CONNECTORS)
        generic_ratio = self._phrase_ratio(paragraph, self.GENERIC_ACADEMIC_PHRASES)
        personal_ratio = self._phrase_ratio(paragraph, self.PERSONAL_MARKERS)
        punctuation_ratio = self._punctuation_ratio(paragraph)
        repeated_words_ratio = self._repeated_words_ratio(words)

        score = 0.0

        # Oraciones demasiado uniformes.
        if std_dev <= 5:
            score += 20
        elif std_dev <= 8:
            score += 14
        elif std_dev <= 12:
            score += 8

        # Longitud académica demasiado estable.
        if 16 <= average_length <= 28:
            score += 13
        elif 12 <= average_length <= 34:
            score += 7

        # Diversidad léxica media y poco natural.
        if 0.42 <= lexical_diversity <= 0.66:
            score += 14
        elif 0.35 <= lexical_diversity <= 0.75:
            score += 7

        # Conectores formales excesivos.
        if connector_ratio >= 0.030:
            score += 18
        elif connector_ratio >= 0.015:
            score += 10

        # Frases académicas genéricas.
        if generic_ratio >= 0.020:
            score += 18
        elif generic_ratio >= 0.010:
            score += 10

        # Puntuación muy regular.
        if 0.018 <= punctuation_ratio <= 0.065:
            score += 8

        # Repetición moderada.
        if repeated_words_ratio >= 0.16:
            score += 8
        elif repeated_words_ratio >= 0.10:
            score += 4

        # Si hay marcas personales, reduce sospecha.
        if personal_ratio > 0:
            score -= 18

        score = max(0.0, min(score, 95.0))

        return Decimal(str(round(score, 2))).quantize(Decimal("0.01"))

    def _words(self, text: str) -> list[str]:
        return re.findall(r"\b[a-záéíóúñü]{3,}\b", text.lower())

    def _sentences(self, text: str) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        return [sentence.strip() for sentence in sentences if sentence.strip()]

    def _standard_deviation(self, values: list[int]) -> float:
        if not values:
            return 0.0

        average = sum(values) / len(values)
        variance = sum((value - average) ** 2 for value in values) / len(values)

        return math.sqrt(variance)

    def _phrase_ratio(self, paragraph: str, phrases: set[str]) -> float:
        paragraph_lower = paragraph.lower()
        count = sum(paragraph_lower.count(phrase) for phrase in phrases)
        words_count = max(len(self._words(paragraph)), 1)

        return count / words_count

    def _punctuation_ratio(self, paragraph: str) -> float:
        punctuation_count = len(re.findall(r"[.,;:]", paragraph))
        return punctuation_count / max(len(paragraph), 1)

    def _repeated_words_ratio(self, words: list[str]) -> float:
        if not words:
            return 0.0

        repeated = len(words) - len(set(words))
        return repeated / len(words)