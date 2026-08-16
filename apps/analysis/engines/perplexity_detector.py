from __future__ import annotations

import logging
import math
import re
import statistics
import threading
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

# Modelo elegido: DeepESP/gpt2-spanish (GPT-2 "small", 12 capas, 124M parámetros,
# ~250MB en fp32, tokenizer BPE entrenado sobre corpus en español —no reutiliza
# el vocabulario en inglés de GPT-2 original, lo que da una perplejidad más
# representativa del idioma). Es de los modelos causales en español más chicos
# con soporte estable en `transformers`; alternativas más grandes (p.ej. GPT-2
# entrenado por flax-community, ~500MB+) dan mejor calidad de lenguaje pero no
# se justifican todavía en un servidor de 2GB sin medir antes el impacto de
# este modelo, que ya es el piso razonable para la técnica.
MODEL_NAME = "DeepESP/gpt2-spanish"

# n_ctx del modelo es 1024 tokens. Se trunca a 900 para dejar margen y evitar
# documentos larguísimos disparando el tiempo de inferencia sin aportar señal
# adicional relevante (la perplejidad promedio se estabiliza mucho antes).
MAX_TOKENS = 900

# Oraciones más cortas que esto dan perplejidad muy ruidosa/inestable (pocos
# tokens = varianza artificialmente alta o baja) y se excluyen del cálculo de
# burstiness.
MIN_SENTENCE_TOKENS = 6

# Perplejidades absurdamente altas (oraciones raras, símbolos, texto mal
# extraído) se recortan para que no distorsionen la desviación estándar.
MAX_CLAMPED_PERPLEXITY = 10_000.0

_load_lock = threading.Lock()
_tokenizer = None
_model = None


def _get_model_and_tokenizer():
    """
    Carga perezosa y única (singleton) del modelo/tokenizer. Recargarlo en
    cada análisis sería carísimo en tiempo (segundos por carga) y memoria
    (duplicaría ~250MB por instancia).
    """
    global _tokenizer, _model

    if _model is not None:
        return _tokenizer, _model

    with _load_lock:
        if _model is not None:
            return _tokenizer, _model

        import torch
        from transformers import GPT2LMHeadModel, GPT2TokenizerFast

        logger.info(
            "Cargando modelo de perplejidad '%s' (primera vez en este proceso)...",
            MODEL_NAME,
        )

        tokenizer = GPT2TokenizerFast.from_pretrained(MODEL_NAME)
        model = GPT2LMHeadModel.from_pretrained(MODEL_NAME)
        model.eval()
        torch.set_grad_enabled(False)

        _tokenizer = tokenizer
        _model = model

        logger.info("Modelo de perplejidad '%s' cargado.", MODEL_NAME)

        return _tokenizer, _model


def _sentence_split(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def _sequence_perplexity(text: str, *, max_tokens: int = MAX_TOKENS) -> float | None:
    import torch

    tokenizer, model = _get_model_and_tokenizer()

    encoding = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=max_tokens,
    )
    input_ids = encoding["input_ids"]

    if input_ids.shape[1] < 2:
        return None

    with torch.no_grad():
        output = model(input_ids, labels=input_ids)

    loss = output.loss.item()

    if math.isnan(loss) or math.isinf(loss):
        return None

    return min(math.exp(loss), MAX_CLAMPED_PERPLEXITY)


# --- Calibración de la señal combinada (perplejidad + burstiness -> score 0-100) ---
#
# IMPORTANTE: estos umbrales NO están entrenados ni calibrados con un dataset
# propio de textos IA-vs-humano en español académico. Son estimaciones
# razonables basadas en el comportamiento observado del modelo sobre texto
# claramente generado por IA (perplejidad baja, ~15-35) frente a texto
# académico humano en español (perplejidad más alta y variable, ~45-100+).
# Deben revisarse con casos reales antes de confiar ciegamente en ellos.
#
# Se usa una curva logística en vez de umbrales duros para evitar saltos
# artificiales cerca de los límites: el score cae suavemente a medida que la
# perplejidad/burstiness se aleja del punto medio.
PERPLEXITY_MIDPOINT = 55.0
PERPLEXITY_SCALE = 15.0

BURSTINESS_MIDPOINT = 12.0
BURSTINESS_SCALE = 5.0

# Peso de burstiness dentro de la señal combinada de perplejidad. Perplejidad
# es la señal más estable (se calcula sobre el documento completo); burstiness
# depende de tener suficientes oraciones válidas y es más ruidosa en textos
# cortos, por eso pesa menos. Si no hay suficientes oraciones para calcular
# burstiness, el score combinado usa 100% perplejidad (ver `_combine_scores`).
BURSTINESS_WEIGHT = Decimal("0.40")
PERPLEXITY_WEIGHT = Decimal("0.60")


def _logistic_score(value: float, *, midpoint: float, scale: float) -> Decimal:
    """
    Convierte un valor (perplejidad o burstiness, donde MENOR = más IA) en un
    score 0-100 de "probabilidad de IA" usando una logística centrada en
    `midpoint`. Valores muy por debajo del punto medio tienden a 100, muy por
    encima tienden a 0.
    """
    raw = 100.0 / (1.0 + math.exp((value - midpoint) / scale))
    clamped = max(0.0, min(raw, 100.0))

    return Decimal(str(round(clamped, 2))).quantize(Decimal("0.01"))


def _combine_scores(
    perplexity_score: Decimal | None,
    burstiness_score: Decimal | None,
) -> Decimal:
    if perplexity_score is None:
        return Decimal("0.00")

    if burstiness_score is None:
        return perplexity_score

    combined = (
        perplexity_score * PERPLEXITY_WEIGHT + burstiness_score * BURSTINESS_WEIGHT
    )

    return combined.quantize(Decimal("0.01"))


@dataclass(frozen=True)
class PerplexityAnalysisResult:
    ai_probability_percent: Decimal
    mean_perplexity: float | None
    burstiness: float | None
    sentence_count: int


class SpanishPerplexityDetector:
    """
    Estimador de texto generado por IA basado en perplejidad + "burstiness",
    la técnica popularizada por GPTZero (2023): el texto generado por modelos
    de lenguaje tiende a ser más "predecible" para otro modelo de lenguaje
    (perplejidad baja) y más uniforme oración a oración (burstiness baja),
    mientras que el texto humano es más impredecible y variable.

    Usa un modelo de lenguaje en español YA ENTRENADO (`DeepESP/gpt2-spanish`)
    solo para inferencia — no se entrena ni ajusta ningún modelo aquí. Es una
    ESTIMACIÓN estadística, no un clasificador entrenado ni validado con un
    dataset propio de textos IA-vs-humano en este dominio académico. Los
    resultados deben interpretarse como una señal adicional, no como prueba
    concluyente.
    """

    def compute_perplexity(self, text: str) -> float | None:
        """Perplejidad promedio del texto completo (truncado a MAX_TOKENS)."""
        return _sequence_perplexity(text)

    def compute_burstiness(self, text: str) -> float | None:
        """
        Desviación estándar de la perplejidad oración por oración. Cada
        oración se evalúa de forma independiente (sin el contexto de las
        oraciones anteriores), lo cual es una simplificación razonable para
        medir variabilidad de estilo sin pagar el costo de recomputar
        contexto acumulado token a token.

        Texto humano = alta variación entre oraciones (bursty).
        Texto de IA = variación baja (uniforme).
        """
        sentences = _sentence_split(text)

        perplexities: list[float] = []

        for sentence in sentences:
            tokenizer, _ = _get_model_and_tokenizer()
            token_count = len(tokenizer.encode(sentence))

            if token_count < MIN_SENTENCE_TOKENS:
                continue

            perplexity = _sequence_perplexity(sentence, max_tokens=MAX_TOKENS)

            if perplexity is not None:
                perplexities.append(perplexity)

        if len(perplexities) < 2:
            return None

        return statistics.stdev(perplexities)

    def analyze(self, content: str) -> PerplexityAnalysisResult:
        sentences = _sentence_split(content)

        mean_perplexity = self.compute_perplexity(content)
        burstiness = self.compute_burstiness(content)

        perplexity_score = (
            _logistic_score(
                mean_perplexity,
                midpoint=PERPLEXITY_MIDPOINT,
                scale=PERPLEXITY_SCALE,
            )
            if mean_perplexity is not None
            else None
        )

        burstiness_score = (
            _logistic_score(
                burstiness,
                midpoint=BURSTINESS_MIDPOINT,
                scale=BURSTINESS_SCALE,
            )
            if burstiness is not None
            else None
        )

        ai_probability_percent = _combine_scores(perplexity_score, burstiness_score)

        return PerplexityAnalysisResult(
            ai_probability_percent=ai_probability_percent,
            mean_perplexity=mean_perplexity,
            burstiness=burstiness,
            sentence_count=len(sentences),
        )
