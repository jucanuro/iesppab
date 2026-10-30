from __future__ import annotations

import gc
import logging
import math
import threading
from dataclasses import dataclass, field
from decimal import Decimal

from apps.analysis.engines import ai_segments

logger = logging.getLogger(__name__)

# Detector experimental de IA con el método Binoculars (Hans et al. 2024,
# "Spotting LLMs With Binoculars: Zero-Shot Detection of Machine-Generated
# Text"). NO está conectado al análisis: solo lo usa el comando
# `evaluate_binoculars` para compararlo con la perplejidad GPT-2 actual.
#
# En vez de la perplejidad sola (que castiga la prosa formulaica humana),
# Binoculars la divide por la "cross-perplejidad": cuánto le sorprende a
# un modelo lo que OTRO modelo esperaba ver en cada posición. Un texto
# humano predecible también lo es para los dos modelos, y el cociente se
# mantiene; un texto de IA es mucho más predecible de lo que su "rareza
# esperada" justificaría, y el cociente baja.
#
#   score = log PPL_performer(texto) / log X-PPL(observer, performer)
#
# donde log X-PPL = media por token de  sum_v p_observer(v) * -log p_performer(v).
# Igual que en el código de los autores (performer = modelo instruct).
# Score bajo = más probable IA.
#
# Par de modelos con el mismo tokenizer (requisito del método), Apache 2.0.
OBSERVER_MODEL_NAME = "Qwen/Qwen2.5-0.5B"
PERFORMER_MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"

# Umbral de segmento (score < umbral = IA), calibrado con
# `evaluate_binoculars` sobre la mitad de calibración de las tesis UPeU
# pre-2022 (semilla 2026): el 1 % de sus segmentos de prosa evaluable
# queda por debajo. No es el umbral del paper (0.8536 para Falcon-7B):
# depende del par de modelos y del idioma.
SEGMENT_THRESHOLD = 0.8886

# Segmentos de hasta 250 palabras (ai_segments.SEGMENT_MAX_WORDS) dan
# ~400-450 tokens de Qwen en español; el tope solo protege de un
# segmento anómalo (p. ej. sin espacios).
MAX_TOKENS = 512

# Filas de la matriz de salida (vocabulario de 151.936 tokens) que se
# pasan a float32 a la vez. Los logits completos de un segmento serían
# 512 × 151.936 × 4 bytes ≈ 300MB por modelo, y la matriz de salida en
# float32 otros 545MB por modelo: por bloques de vocabulario cada uno
# queda en ~60MB y la entropía cruzada se acumula con un softmax "online".
VOCAB_CHUNK = 16384

# Los dos modelos NO se tienen cargados a la vez: con ambos residentes el
# pico medido fue de 2.8GB (2.0GB de pesos en bfloat16 + ~0.5GB de
# torch/Django + transitorios). `score_texts` carga el performer, guarda
# sus estados finales y una copia de su matriz de salida (272MB), lo
# libera y recién entonces carga el observer. Cargar un modelo en
# bfloat16 desde la caché de disco tarda ~1s, poco frente a los ~4s por
# segmento. Los estados guardados (posiciones × 896 × 4 bytes, ~1.3MB por
# segmento) se acotan procesando los textos de a este número.
TEXTS_PER_PASS = 64

_load_lock = threading.Lock()
_tokenizer = None


def _upcast_linear_forward(linear):
    """
    Pesos guardados en bfloat16 (~1GB por modelo en vez de ~2GB en
    float32) pero el cálculo en float32. Medido en un i7-8750H (AVX2 sin
    bf16 nativo): un forward de 300 tokens tarda 4.6s con matmul en
    bfloat16 y 1.1s así, igual que con el modelo entero en float32. Cada
    capa se pasa a float32 solo mientras se usa (~20MB a la vez).
    """
    from torch.nn import functional as F

    def forward(hidden):
        bias = linear.bias.float() if linear.bias is not None else None
        return F.linear(hidden, linear.weight.float(), bias)

    return forward


def _load_model(name: str):
    import torch
    from torch import nn
    from transformers import AutoModelForCausalLM

    logger.debug("Cargando modelo de Binoculars '%s'...", name)
    model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.bfloat16)
    model.eval()

    for module in model.modules():
        if isinstance(module, nn.Linear):
            # La cabeza de lenguaje la aplica `_segment_statistics` por
            # bloques; nunca pasa por su forward.
            if module is not model.lm_head:
                module.forward = _upcast_linear_forward(module)
        elif not isinstance(module, nn.Embedding):
            # RMSNorm, rotary: pocos parámetros, directamente en float32.
            for parameter in module.parameters(recurse=False):
                parameter.data = parameter.data.float()
            for buffer_name, buffer in module.named_buffers(recurse=False):
                if buffer.is_floating_point():
                    setattr(module, buffer_name, buffer.float())

    return model


def _get_tokenizer():
    """Carga perezosa y única del tokenizer (compartido por los dos modelos)."""
    global _tokenizer

    if _tokenizer is not None:
        return _tokenizer

    with _load_lock:
        if _tokenizer is None:
            from transformers import AutoTokenizer

            _tokenizer = AutoTokenizer.from_pretrained(OBSERVER_MODEL_NAME)

    return _tokenizer


def _final_hidden_states(model, input_ids):
    """Estados del último bloque (ya normalizados), sin la cabeza de lenguaje."""
    embeddings = model.get_input_embeddings()(input_ids).float()
    return model.model(inputs_embeds=embeddings).last_hidden_state


def _segment_statistics(
    observer_hidden,
    performer_hidden,
    target_ids,
    observer_head,
    performer_head,
    *,
    vocab_chunk: int = VOCAB_CHUNK,
) -> tuple[float, float]:
    """
    (log-perplejidad bajo el performer, log-cross-perplejidad), medias por
    token, recorriendo el vocabulario por bloques de `vocab_chunk` filas.

    Por posición:
      - log-ppl  = LSE(z_p) - z_p[objetivo]
      - x-ent    = sum_v softmax(z_o)_v * (LSE(z_p) - z_p[v])
                 = LSE(z_p) - E_{p_observer}[z_p]
    LSE(z_p) se acumula con un máximo móvil; E_{p_observer}[z_p] con el
    softmax online del observer (máximo, suma y suma ponderada se
    reescalan cada vez que aparece un logit mayor).

    `*_hidden`: (posiciones, d) en float32; `target_ids`: (posiciones,);
    `*_head`: matrices (vocab, d) en cualquier dtype.
    """
    import torch

    positions = target_ids.shape[0]
    vocab_size = performer_head.shape[0]
    negative_infinity = torch.full((positions,), float("-inf"))

    perf_max = negative_infinity.clone()
    perf_sum = torch.zeros(positions)
    obs_max = negative_infinity.clone()
    obs_sum = torch.zeros(positions)
    obs_weighted = torch.zeros(positions)
    target_logits = torch.zeros(positions)

    for start in range(0, vocab_size, vocab_chunk):
        end = min(start + vocab_chunk, vocab_size)
        perf_logits = performer_hidden @ performer_head[start:end].float().T
        obs_logits = observer_hidden @ observer_head[start:end].float().T

        new_max = torch.maximum(perf_max, perf_logits.max(dim=1).values)
        perf_sum = perf_sum * torch.exp(perf_max - new_max) + torch.exp(
            perf_logits - new_max[:, None]
        ).sum(dim=1)
        perf_max = new_max

        new_max = torch.maximum(obs_max, obs_logits.max(dim=1).values)
        rescale = torch.exp(obs_max - new_max)
        weights = torch.exp(obs_logits - new_max[:, None])
        obs_sum = obs_sum * rescale + weights.sum(dim=1)
        obs_weighted = obs_weighted * rescale + (weights * perf_logits).sum(dim=1)
        obs_max = new_max

        in_chunk = (target_ids >= start) & (target_ids < end)
        if in_chunk.any():
            rows = in_chunk.nonzero(as_tuple=True)[0]
            target_logits[rows] = perf_logits[rows, target_ids[rows] - start]

        del perf_logits, obs_logits, weights

    perf_lse = perf_max + torch.log(perf_sum)
    log_ppl = (perf_lse - target_logits).mean().item()
    log_x_ppl = (perf_lse - obs_weighted / obs_sum).mean().item()

    return log_ppl, log_x_ppl


@dataclass
class BinocularsScore:
    score: float
    log_ppl: float
    log_x_ppl: float
    tokens: int


def score_texts(
    texts: list[str],
    *,
    max_tokens: int = MAX_TOKENS,
) -> list[BinocularsScore | None]:
    """
    Score Binoculars de cada texto (un segmento), alineado con `texts`;
    None en los demasiado cortos. Un modelo a la vez (ver `TEXTS_PER_PASS`).
    """
    import torch

    tokenizer = _get_tokenizer()
    encoded = [
        tokenizer(text, return_tensors="pt", truncation=True, max_length=max_tokens)[
            "input_ids"
        ]
        for text in texts
    ]
    valid = [index for index, ids in enumerate(encoded) if ids.shape[1] >= 2]
    results: list[BinocularsScore | None] = [None] * len(texts)

    for start in range(0, len(valid), TEXTS_PER_PASS):
        batch = valid[start:start + TEXTS_PER_PASS]

        with torch.no_grad():
            performer = _load_model(PERFORMER_MODEL_NAME)
            performer_hidden = {
                index: _final_hidden_states(performer, encoded[index])[0, :-1]
                for index in batch
            }
            # Copia propia: la del modelo vive en el mmap del archivo, que
            # se liberaría junto con el modelo.
            performer_head = performer.lm_head.weight.detach().clone()
            del performer
            gc.collect()

            observer = _load_model(OBSERVER_MODEL_NAME)
            for index in batch:
                log_ppl, log_x_ppl = _segment_statistics(
                    _final_hidden_states(observer, encoded[index])[0, :-1],
                    performer_hidden.pop(index),
                    encoded[index][0, 1:],
                    observer.lm_head.weight,
                    performer_head,
                )
                if math.isfinite(log_ppl) and math.isfinite(log_x_ppl) and log_x_ppl > 0:
                    results[index] = BinocularsScore(
                        score=log_ppl / log_x_ppl,
                        log_ppl=log_ppl,
                        log_x_ppl=log_x_ppl,
                        tokens=int(encoded[index].shape[1]),
                    )
            del observer, performer_head
            gc.collect()

    return results


@dataclass
class BinocularsAnalysisResult:
    ai_probability_percent: Decimal
    segments: list[ai_segments.TextSegment] = field(default_factory=list)
    # Alineado con `segments`; None en los no evaluables.
    scores: list[BinocularsScore | None] = field(default_factory=list)
    threshold: float = SEGMENT_THRESHOLD


def aggregate_ai_percent(
    segments: list[ai_segments.TextSegment],
    scores: list[float | None],
    *,
    threshold: float = SEGMENT_THRESHOLD,
) -> Decimal:
    """
    % de palabras evaluables en segmentos con score < `threshold`. Como
    `ai_segments.aggregate_ai_percent`: los no evaluables (o sin score) no
    cuentan ni en el numerador ni en el denominador.
    """
    evaluable_words = ai_words = 0

    for segment, score in zip(segments, scores):
        if not segment.evaluable or score is None:
            continue

        evaluable_words += segment.evaluable_word_count
        if score < threshold:
            ai_words += segment.evaluable_word_count

    if not evaluable_words:
        return Decimal("0.00")

    return (Decimal(ai_words) * 100 / Decimal(evaluable_words)).quantize(
        Decimal("0.01")
    )


class BinocularsDetector:
    """
    Evalúa el documento con los mismos segmentos y el mismo filtro de
    prosa evaluable que `SegmentedPerplexityDetector` (sin repetidos,
    listas, tablas ni planes de sesión).
    """

    def analyze(
        self,
        content: str,
        *,
        excluded_ranges: list[tuple[int, int]] | None = None,
        threshold: float = SEGMENT_THRESHOLD,
    ) -> BinocularsAnalysisResult:
        segments = ai_segments.build_segments(content, excluded_ranges=excluded_ranges)
        evaluable = [index for index, s in enumerate(segments) if s.evaluable]
        scores: list[BinocularsScore | None] = [None] * len(segments)

        evaluable_scores = score_texts([segments[i].evaluable_text for i in evaluable])
        for index, score in zip(evaluable, evaluable_scores):
            if score is None:
                segments[index].excluded_reason = ai_segments.REASON_NO_PERPLEXITY
            scores[index] = score

        return BinocularsAnalysisResult(
            ai_probability_percent=aggregate_ai_percent(
                segments,
                [score.score if score else None for score in scores],
                threshold=threshold,
            ),
            segments=segments,
            scores=scores,
            threshold=threshold,
        )
