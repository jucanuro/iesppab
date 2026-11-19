"""
Clasificador de IA por segmentos (BETO afinado, ver
`notebooks/entrenar_clasificador_ia.ipynb`).

Con `AI_DETECTION_MODE=classifier` el resultado se guarda en
`ai_score_breakdown["classifier"]` y su % es también `ai_probability_percent`
(la heurística queda en `heuristic_ai_score`). NO cambia el nivel de riesgo
ni los certificados: dependen solo de la similitud.

Los segmentos salen de `ai_segments.build_segments` (mismos offsets que el
visor) y se normalizan igual que en el dataset de entrenamiento
(`ia_dataset.normalize_segment_text` + `_split_long` + descartes), para que
el modelo vea en la inferencia el mismo tipo de texto con el que se entrenó.

% IA = % de palabras evaluadas en trozos con probabilidad >= umbral (el umbral
de `umbral.json`, fijado en Colab para <= 5 % de falsos positivos en humanos
de validación).
"""

from __future__ import annotations

import gc
import json
import logging
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.conf import settings

from apps.analysis import ia_dataset
from apps.analysis.engines import ai_segments
from apps.analysis.engines.language import REASON_OTHER_LANGUAGE, is_other_language

logger = logging.getLogger(__name__)

# "No evaluable": si casi todo el documento queda fuera (listas, ruido,
# tablas, otro idioma...) o se evalúan muy pocas palabras, un 0,0 % sería
# engañoso: no se midió nada. Se guarda como no evaluable.
NOT_EVALUABLE_EXCLUDED_RATIO = 0.90
NOT_EVALUABLE_MIN_WORDS = 200
NOT_EVALUABLE_NOTE = (
    "IA: no evaluable. El documento tiene muy poca prosa corrida analizable "
    "(predominan listas, tablas, texto en otro idioma o texto con ruido de "
    "extracción), por lo que no se puede estimar un porcentaje fiable."
)
# Texto de cada pieza señalada que se guarda en el desglose.
EXCERPT_CHARS = 300
# Solo se LISTAN (texto en el desglose, página e informe) las piezas con al
# menos esta probabilidad. El umbral del modelo (muy bajo, para el % total)
# no decide qué se lista; el % total no cambia.
LISTING_MIN_PROBABILITY = 0.5

AI_LABEL_INDEX = 1
DEFAULT_MAX_LENGTH = 384
BATCH_SIZE = 2
NUM_THREADS = 2

_lock = threading.Lock()
_loaded: dict[str, tuple] = {}


class ClassifierUnavailableError(RuntimeError):
    """No hay modelo configurado o no se pudo cargar."""


@dataclass
class ClassifiedPiece:
    text: str
    word_count: int
    probability: float


@dataclass
class ClassifiedSegment:
    start: int
    end: int
    word_count: int
    excluded_reason: str
    pieces: list[ClassifiedPiece] = field(default_factory=list)

    @property
    def max_probability(self) -> float | None:
        return max((p.probability for p in self.pieces), default=None)


@dataclass
class ClassifierResult:
    ai_percent: Decimal
    threshold: float
    segments: list[ClassifiedSegment]
    evaluable: bool = True
    not_evaluable_reason: str = ""

    @property
    def pieces(self) -> list[ClassifiedPiece]:
        return [piece for segment in self.segments for piece in segment.pieces]

    @property
    def evaluated_words(self) -> int:
        return sum(piece.word_count for piece in self.pieces)

    def to_breakdown(self) -> dict:
        evaluated = self.evaluated_words if self.evaluable else 0
        data = {
            "indicative": True,
            "evaluable": self.evaluable,
            "ai_percent": str(self.ai_percent),
            "threshold": self.threshold,
            "evaluated_words": evaluated,
            "evaluated_pieces": len(self.pieces) if self.evaluable else 0,
            "flagged_pieces": (
                sum(p.probability >= self.threshold for p in self.pieces)
                if self.evaluable
                else 0
            ),
            "total_segments": len(self.segments),
            "excluded_segments": sum(bool(s.excluded_reason) for s in self.segments),
            "segments": [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "words": segment.word_count,
                    "excluded_reason": segment.excluded_reason,
                    "pieces": [
                        {
                            "words": p.word_count,
                            "probability": round(p.probability, 6),
                            # Solo las piezas listables (>= 50 %) llevan texto:
                            # es lo que listan la página y el informe.
                            **(
                                {"text": p.text[:EXCERPT_CHARS]}
                                if p.probability >= max(self.threshold, LISTING_MIN_PROBABILITY)
                                else {}
                            ),
                        }
                        for p in segment.pieces
                    ]
                    if self.evaluable
                    else [],
                }
                for segment in self.segments
            ],
        }
        if not self.evaluable:
            data["not_evaluable_reason"] = self.not_evaluable_reason
            data["candidate_words"] = self.evaluated_words
            data["note"] = NOT_EVALUABLE_NOTE
        return data


def classifier_pieces(segment_text: str) -> list[str]:
    """Igual que `ia_dataset.prose_segments`, para un segmento ya evaluable."""
    normalized = ia_dataset.normalize_segment_text(segment_text)
    pieces = []
    for piece in ia_dataset._split_long(normalized):
        if len(piece.split()) >= ia_dataset.MIN_SEGMENT_WORDS and not ia_dataset.has_pdf_artifacts(piece):
            pieces.append(ia_dataset._SPLIT_LETTER_RE.sub("", piece).replace("  ", " "))
    return pieces


def _read_config(model_dir: str) -> dict:
    """Lee `umbral.json` sin cargar el modelo (barato)."""
    path = Path(model_dir)
    if not model_dir or not (path / "config.json").is_file():
        raise ClassifierUnavailableError(
            f"No hay modelo del clasificador en '{model_dir}' (AI_CLASSIFIER_MODEL_DIR)."
        )
    return json.loads((path / "umbral.json").read_text(encoding="utf-8"))


def unload(model_dir: str | None = None) -> None:
    """Libera el modelo de memoria (se vuelve a cargar solo al usarlo)."""
    with _lock:
        if model_dir is None:
            _loaded.clear()
        else:
            _loaded.pop(model_dir, None)
    gc.collect()


def _load(model_dir: str):
    with _lock:
        if model_dir in _loaded:
            return _loaded[model_dir]

        path = Path(model_dir)
        if not model_dir or not (path / "config.json").is_file():
            raise ClassifierUnavailableError(
                f"No hay modelo del clasificador en '{model_dir}' (AI_CLASSIFIER_MODEL_DIR)."
            )

        import torch
        torch.set_num_threads(NUM_THREADS)
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        config = _read_config(model_dir)
        tokenizer = AutoTokenizer.from_pretrained(path)
        model = AutoModelForSequenceClassification.from_pretrained(path, low_cpu_mem_usage=True).eval()
        _loaded[model_dir] = (
            tokenizer,
            model,
            float(config["umbral"]),
            int(config.get("max_length", DEFAULT_MAX_LENGTH)),
            torch,
        )
        return _loaded[model_dir]


class SegmentAIClassifier:
    def __init__(self, model_dir: str | None = None) -> None:
        self.model_dir = model_dir if model_dir is not None else settings.AI_CLASSIFIER_MODEL_DIR

    def predict(self, texts: list[str]) -> list[float]:
        tokenizer, model, _, max_length, torch = _load(self.model_dir)
        probabilities: list[float] = []
        with torch.inference_mode():
            for i in range(0, len(texts), BATCH_SIZE):
                batch = tokenizer(
                    texts[i:i + BATCH_SIZE],
                    truncation=True,
                    max_length=max_length,
                    padding=True,
                    return_tensors="pt",
                )
                logits = model(**batch).logits
                probabilities += torch.softmax(logits, dim=-1)[:, AI_LABEL_INDEX].tolist()
                del batch, logits
                gc.collect()
        return probabilities

    @property
    def threshold(self) -> float:
        return float(_read_config(self.model_dir)["umbral"])

    def analyze(
        self,
        content: str,
        *,
        excluded_ranges: list[tuple[int, int]] | None = None,
    ) -> ClassifierResult:
        segments = []
        for segment in ai_segments.build_segments(content, excluded_ranges=excluded_ranges):
            classified = ClassifiedSegment(
                start=segment.start,
                end=segment.end,
                word_count=segment.word_count,
                excluded_reason=segment.excluded_reason,
            )
            if segment.evaluable and is_other_language(segment.evaluable_text):
                classified.excluded_reason = REASON_OTHER_LANGUAGE
            elif segment.evaluable:
                classified.pieces = [
                    ClassifiedPiece(text=text, word_count=len(text.split()), probability=0.0)
                    for text in classifier_pieces(segment.evaluable_text)
                ]
            segments.append(classified)

        pieces = [piece for segment in segments for piece in segment.pieces]
        threshold = self.threshold
        reason = self._not_evaluable_reason(segments, pieces)

        if reason:
            # No se usa el modelo: no hay prosa suficiente que medir.
            unload(self.model_dir)
            return ClassifierResult(
                ai_percent=Decimal("0.00"),
                threshold=threshold,
                segments=segments,
                evaluable=False,
                not_evaluable_reason=reason,
            )

        try:
            probabilities = self.predict([p.text for p in pieces])
        finally:
            unload(self.model_dir)
        for piece, probability in zip(pieces, probabilities):
            piece.probability = probability

        total = sum(p.word_count for p in pieces)
        flagged = sum(p.word_count for p in pieces if p.probability >= threshold)
        percent = (
            (Decimal(flagged) * 100 / Decimal(total)).quantize(Decimal("0.01"))
            if total
            else Decimal("0.00")
        )
        return ClassifierResult(ai_percent=percent, threshold=threshold, segments=segments)

    @staticmethod
    def _not_evaluable_reason(
        segments: list[ClassifiedSegment], pieces: list[ClassifiedPiece]
    ) -> str:
        if not segments:
            return "sin-segmentos"
        excluded = sum(bool(segment.excluded_reason) for segment in segments)
        if excluded / len(segments) >= NOT_EVALUABLE_EXCLUDED_RATIO:
            return "segmentos-excluidos"
        if sum(piece.word_count for piece in pieces) < NOT_EVALUABLE_MIN_WORDS:
            return "pocas-palabras-evaluadas"
        return ""
