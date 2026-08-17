from __future__ import annotations

import logging
import math
import re
import statistics
import threading
from dataclasses import dataclass
from decimal import Decimal

from apps.analysis.engines.similarity import InternalSimilarityEngine

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

# Fragmentación de compute_burstiness(): calcular la desviación estándar
# sobre TODAS las oraciones de un documento completo (cientos) permite que
# un puñado de oraciones mal extraídas (tablas, fórmulas, citas rotas) por
# el parser de PDF/DOCX disparen la varianza muy por fuera del rango
# calibrado en el banco de pruebas (párrafos cortos, AUC 0.952). En su
# lugar, el documento se parte en fragmentos del mismo tamaño de palabras
# que ya usa el motor de similitud (`InternalSimilarityEngine`) —esa es la
# escala que sí se validó— y se calcula un burstiness por fragmento,
# agregando con la MEDIANA entre fragmentos para que uno o dos fragmentos
# anómalos no dominen el resultado (a diferencia del promedio).
FRAGMENT_WORD_SIZE = InternalSimilarityEngine.CHUNK_WORD_SIZE

# Clip adicional (independiente de la fragmentación) sobre la perplejidad de
# oraciones individuales antes de entrar al cálculo de burstiness: incluso
# dentro de un fragmento corto, una sola oración con texto corrupto puede
# tener una perplejidad varios órdenes de magnitud por encima del resto y
# dominar la desviación estándar. El valor es una estimación conservadora
# sin calibrar contra datos reales (muy por encima del rango humano
# académico esperado, ~45-100+, ver comentario de PERPLEXITY_MIDPOINT más
# abajo) — debe revisarse si aparecen documentos reales que aún disparen
# burstiness fuera de rango tras este clip.
MAX_SENTENCE_PERPLEXITY_FOR_BURSTINESS = 400.0

# Tamaño de lote para el forward pass batched de compute_burstiness. 16 es
# conservador para CPU con ~6GB libres y GPT-2 small (~250MB en fp32): deja
# margen amplio incluso si varias oraciones cercanas a MAX_TOKENS caen en el
# mismo lote. Bajar este valor si se despliega en un entorno con menos
# memoria disponible.
BURSTINESS_BATCH_SIZE = 16

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

        # El checkpoint de este modelo se sirve en float16. En GPU eso sería
        # deseable, pero en CPU `torch.addmm`/`matmul` en fp16 no tiene ruta
        # vectorizada por MKL y cae a un kernel de referencia sin SIMD: cada
        # forward pass midió ~2.9s por secuencia corta (contra ~0.07s en
        # fp32), sin ninguna economía de escala al agrupar en lotes — este
        # era el cuello de botella dominante, no la falta de batching en sí.
        # Forzar fp32 (este modelo es pequeño, ~500MB en fp32, sin problema
        # de memoria) recupera la ruta rápida de MKL y además hace que el
        # batching de compute_burstiness sí aporte la mejora esperada.
        model = model.to(torch.float32)
        model.eval()
        torch.set_grad_enabled(False)

        # GPT-2 no trae pad_token por defecto (solo eos). Hace falta uno para
        # poder paddear lotes de oraciones de distinto largo en
        # compute_burstiness; reusar eos_token es la convención estándar de
        # `transformers` para modelos causales sin pad_token propio. Padding
        # a la derecha es seguro con atención causal: un token de relleno
        # después de una oración nunca puede ser atendido por los tokens
        # reales que lo preceden, así que no contamina su pérdida.
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        tokenizer.padding_side = "right"

        _tokenizer = tokenizer
        _model = model

        logger.info("Modelo de perplejidad '%s' cargado.", MODEL_NAME)

        return _tokenizer, _model


def _sentence_split(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def _split_sentences_into_fragments(
    sentences: list[str],
    *,
    fragment_word_size: int = FRAGMENT_WORD_SIZE,
) -> list[list[str]]:
    """
    Agrupa oraciones consecutivas en fragmentos de ~`fragment_word_size`
    palabras cada uno, sin cortar ninguna oración a la mitad. A diferencia
    del motor de similitud (que usa una ventana deslizante con overlap para
    maximizar cobertura de coincidencias), aquí no hay overlap: el objetivo
    es obtener muestras independientes del documento para agregar su
    burstiness con la mediana, y solapar fragmentos solo duplicaría el peso
    de las mismas oraciones sin aportar independencia real.

    El fragmento final, si queda más corto que `fragment_word_size` porque
    el documento no es múltiplo exacto, se fusiona con el anterior en vez de
    quedar como un fragmento diminuto y ruidoso.
    """
    fragments: list[list[str]] = []
    current: list[str] = []
    current_words = 0

    for sentence in sentences:
        current.append(sentence)
        current_words += len(sentence.split())

        if current_words >= fragment_word_size:
            fragments.append(current)
            current = []
            current_words = 0

    if current:
        if fragments:
            fragments[-1].extend(current)
        else:
            fragments.append(current)

    return fragments


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


def _batch_sequence_perplexities(
    texts: list[str],
    *,
    max_tokens: int = MAX_TOKENS,
    batch_size: int = BURSTINESS_BATCH_SIZE,
) -> list[float | None]:
    """
    Perplejidad de cada texto en `texts`, procesados en lotes en vez de un
    forward pass por texto. Devuelve una lista alineada por índice con
    `texts` (None donde no se pudo calcular, misma semántica que
    `_sequence_perplexity`).

    Las oraciones se ordenan por longitud antes de agruparlas en lotes
    (bucketing) para que el padding dentro de cada lote sea mínimo: mezclar
    una oración de 8 tokens con una de 300 en el mismo lote forzaría
    paddear la corta a 300, desperdiciando memoria y cómputo.
    """
    import torch
    from torch.nn import functional as F

    if not texts:
        return []

    tokenizer, model = _get_model_and_tokenizer()

    order = sorted(range(len(texts)), key=lambda i: len(tokenizer.encode(texts[i])))
    results: list[float | None] = [None] * len(texts)

    for start in range(0, len(order), batch_size):
        chunk_indices = order[start : start + batch_size]
        chunk_texts = [texts[i] for i in chunk_indices]

        encoding = tokenizer(
            chunk_texts,
            return_tensors="pt",
            truncation=True,
            max_length=max_tokens,
            padding=True,
        )
        input_ids = encoding["input_ids"]
        attention_mask = encoding["attention_mask"]

        with torch.no_grad():
            output = model(input_ids, attention_mask=attention_mask)

        # Sin `labels=` en la llamada al modelo porque la pérdida interna de
        # `transformers` promedia sobre TODAS las posiciones desplazadas,
        # incluyendo el padding — eso contaminaría la perplejidad de las
        # oraciones cortas del lote. En su lugar se calcula la pérdida por
        # token manualmente y se enmascara con `attention_mask` antes de
        # promediar, para que cada oración se evalúe solo sobre sus propios
        # tokens reales (igual que hacía `_sequence_perplexity` sin batching).
        shift_logits = output.logits[..., :-1, :].contiguous()
        shift_labels = input_ids[..., 1:].contiguous()
        shift_mask = attention_mask[..., 1:].contiguous().float()

        per_token_loss = F.cross_entropy(
            shift_logits.view(-1, shift_logits.size(-1)),
            shift_labels.view(-1),
            reduction="none",
        ).view(shift_labels.shape)

        masked_loss = per_token_loss * shift_mask
        token_counts = shift_mask.sum(dim=1)

        for row, idx in enumerate(chunk_indices):
            real_token_count = token_counts[row].item()

            if real_token_count < 1:
                continue

            mean_loss = (masked_loss[row].sum() / token_counts[row]).item()

            if math.isnan(mean_loss) or math.isinf(mean_loss):
                continue

            results[idx] = min(math.exp(mean_loss), MAX_CLAMPED_PERPLEXITY)

    return results


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
    # Se acota el exponente antes de exp(): burstiness no tiene un tope
    # natural (a diferencia de la perplejidad, recortada a
    # MAX_CLAMPED_PERPLEXITY) y algunos textos humanos producen valores
    # extremos (>1000) que, sin este recorte, disparan un OverflowError en
    # math.exp para |exponente| > ~709. El resultado sin recortar ya
    # tendería a 0/100 en esos casos, así que acotar el exponente no cambia
    # el score, solo evita el crash.
    exponent = max(-700.0, min(700.0, (value - midpoint) / scale))
    raw = 100.0 / (1.0 + math.exp(exponent))
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
        Mediana de la desviación estándar de perplejidad oración-a-oración,
        calculada POR FRAGMENTO (no sobre el documento completo). Cada
        oración se evalúa de forma independiente (sin el contexto de las
        oraciones anteriores), lo cual es una simplificación razonable para
        medir variabilidad de estilo sin pagar el costo de recomputar
        contexto acumulado token a token.

        Calcular la desviación estándar sobre TODAS las oraciones de un
        documento largo (cientos) deja que un puñado de oraciones mal
        extraídas (tablas, fórmulas, citas rotas) saturen la varianza muy
        por fuera del rango calibrado en el banco de pruebas, que se hizo
        sobre párrafos cortos. Fragmentar a esa misma escala
        (`FRAGMENT_WORD_SIZE`, ver comentario junto a la constante) y agregar
        con la MEDIANA entre fragmentos —no el promedio— hace que uno o dos
        fragmentos anómalos no dominen el resultado final.

        Las oraciones válidas se evalúan en lotes (`_batch_sequence_perplexities`)
        en vez de un forward pass por oración — con documentos de varias
        decenas de oraciones, procesarlas una por una es el cuello de botella
        dominante de todo el análisis. El batching se hace sobre todas las
        oraciones del documento a la vez (no por fragmento) para no perder
        ese beneficio de rendimiento; el agrupamiento en fragmentos ocurre
        después, sobre los resultados ya calculados.

        Texto humano = alta variación entre oraciones (bursty).
        Texto de IA = variación baja (uniforme).
        """
        sentences = _sentence_split(text)
        tokenizer, _ = _get_model_and_tokenizer()

        valid_sentences = [
            sentence
            for sentence in sentences
            if len(tokenizer.encode(sentence)) >= MIN_SENTENCE_TOKENS
        ]

        if len(valid_sentences) < 2:
            return None

        fragments = _split_sentences_into_fragments(valid_sentences)
        raw_perplexities = _batch_sequence_perplexities(valid_sentences)

        fragment_burstiness_values: list[float] = []
        sentence_index = 0

        for fragment in fragments:
            fragment_perplexities = []

            for _ in fragment:
                perplexity = raw_perplexities[sentence_index]
                sentence_index += 1

                if perplexity is None:
                    continue

                fragment_perplexities.append(
                    min(perplexity, MAX_SENTENCE_PERPLEXITY_FOR_BURSTINESS)
                )

            if len(fragment_perplexities) >= 2:
                fragment_burstiness_values.append(
                    statistics.stdev(fragment_perplexities)
                )

        if not fragment_burstiness_values:
            return None

        return statistics.median(fragment_burstiness_values)

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
