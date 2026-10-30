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

# Batching adaptativo por presupuesto de memoria para los forward pass de
# `_batch_sequence_perplexities` (burstiness del modo global y segmentos
# del modo segments). Un batch_size fijo (el diseño anterior usaba 16) es
# seguro solo mientras las secuencias de ese lote sean cortas: como se
# ordenan por longitud antes de agrupar (bucketing), las más largas
# terminan juntas en los últimos lotes y el pico depende del producto
# batch_size × longitud (activaciones) y batch_size × longitud² (atención),
# no solo de la cantidad de secuencias.
#
# Cada lote se arma justo antes de su forward pass (`_iter_batches_by_memory_budget`)
# para que su pico estimado (`_estimate_forward_pass_bytes`) quepa en el
# MENOR de: una fracción de la RAM libre en ese momento y un techo
# absoluto (`BATCH_MEMORY_MAX_BYTES`).
BURSTINESS_BATCH_MEMORY_FRACTION = 0.5

# Techo absoluto del pico de un lote, sin importar cuánta RAM libre
# reporte la máquina. El modelo cargado (con Django, torch y transformers)
# ocupa ~620-940MB de RSS según el entorno, así que con este techo un
# worker queda en ~1.5GB aunque analice una tesis de 30k palabras (servidor
# de 2GB). Sin techo, una máquina con 7GB libres armaba lotes de varios GB
# y el kernel mataba el proceso (OOM medido en WSL con una tesis de 28.8k
# palabras en modo segments: 7.2GB de RSS).
BATCH_MEMORY_MAX_BYTES = 512 * 1024 * 1024

# Margen sobre `_estimate_forward_pass_bytes`. El pico de RSS de un mismo
# lote varió hasta ±300MB entre corridas idénticas (el allocator no
# devuelve la memoria liberada de inmediato), así que la fórmula se ajustó
# para quedar por encima del MÁXIMO medido, no del promedio.
BURSTINESS_BATCH_MEMORY_SAFETY_FACTOR = 1.3

# Posiciones que se proyectan al vocabulario a la vez al calcular la
# pérdida. Los logits de un lote completo (batch × longitud × 50257 × 4
# bytes, más las copias de `shift_logits.contiguous()` y de `log_softmax`
# dentro de `cross_entropy`, ~3x) eran el pico real: ~550MB por cada
# secuencia de 900 tokens. Por trozos, ese costo queda fijo (~150MB) e
# independiente del tamaño del lote.
LOSS_CHUNK_TOKENS = 256

# Copias simultáneas, en float32, de cada tensor estimado. Valores
# calibrados midiendo el pico de RSS de `_mean_token_losses` en procesos
# limpios (batch × longitud de 1×100 a 128×30 y 8×900; DeepESP/gpt2-spanish
# en CPU, torch 2.13):
#  - activaciones (múltiplos de batch × longitud × n_embd): residual,
#    LayerNorm, qkv, MLP (4 × n_embd antes y después de GELU) y buffers
#    de los kernels; con lotes de muchas secuencias cortas (32×100,
#    128×30) es el término dominante.
#  - atención (batch × cabezas × longitud²): scores, máscara, softmax.
#    Con no_grad solo vive la de UNA capa a la vez, no la de las 12 como
#    suponía la fórmula anterior.
#  - logits de un trozo de pérdida (LOSS_CHUNK_TOKENS × vocab): logits,
#    log_softmax y el gather de `cross_entropy`.
#  - fijo: buffers de los hilos de torch (~70MB medidos con 1×100).
_ACTIVATION_COPIES = 32
_ATTENTION_COPIES = 4
_LOSS_LOGITS_COPIES = 3
_FIXED_OVERHEAD_BYTES = 64 * 1024 * 1024

# Techo de seguridad final: un lote de 1 secuencia SIEMPRE se procesa,
# exceda o no el presupuesto estimado, para que el documento nunca deje de
# analizarse por falta de RAM "de sobra" — ver `_iter_batches_by_memory_budget`.
# Si ni siquiera ese lote mínimo cabe en la memoria física real, el
# try/except alrededor del forward pass en `_batch_sequence_perplexities`
# lo detecta y degrada con gracia (se omite esa secuencia, no se revienta
# el proceso).
MIN_BURSTINESS_BATCH_SIZE = 1

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


_OOM_RUNTIME_ERROR_MARKERS = ("alloc", "memory", "bad_alloc")


def _is_out_of_memory_error(exc: Exception) -> bool:
    """
    PyTorch en CPU no siempre lanza `MemoryError` ante una asignación que no
    cabe en RAM: a menudo es un `RuntimeError` cuyo mensaje viene del
    allocator de C++ (p.ej. "[enforce fail at alloc_cpu.cpp...] std::bad_alloc"
    o "DefaultCPUAllocator: not enough memory"). Se distingue de otros
    `RuntimeError` (por ejemplo, un error de forma de tensor) buscando esas
    señales en el mensaje, para no silenciar errores que sí deberían
    propagarse.
    """
    if isinstance(exc, MemoryError):
        return True

    if isinstance(exc, RuntimeError):
        message = str(exc).lower()
        return any(marker in message for marker in _OOM_RUNTIME_ERROR_MARKERS)

    return False


def _estimate_forward_pass_bytes(batch_size: int, seq_len: int, model) -> int:
    """
    Estimación conservadora del pico de memoria (bytes, por encima del
    modelo cargado) de `_batch_sequence_perplexities` para un lote: las
    activaciones de una capa del transformer, las matrices de atención de
    UNA capa (con no_grad cada capa libera las suyas antes de la
    siguiente) y los logits de un trozo de `LOSS_CHUNK_TOKENS` posiciones
    (ver `_mean_token_losses`). Ver `_ACTIVATION_COPIES` y compañía para
    el detalle de las copias y la calibración.
    """
    config = model.config
    float_bytes = 4

    activation_bytes = (
        _ACTIVATION_COPIES * batch_size * seq_len * config.n_embd * float_bytes
    )
    attention_bytes = (
        _ATTENTION_COPIES * batch_size * config.n_head * seq_len * seq_len * float_bytes
    )
    loss_tokens = min(LOSS_CHUNK_TOKENS, batch_size * seq_len)
    loss_bytes = _LOSS_LOGITS_COPIES * loss_tokens * config.vocab_size * float_bytes

    return int(
        (activation_bytes + attention_bytes + loss_bytes + _FIXED_OVERHEAD_BYTES)
        * BURSTINESS_BATCH_MEMORY_SAFETY_FACTOR
    )


def _cgroup_memory_limit_and_usage_bytes() -> tuple[int, int] | None:
    """
    Límite y uso actual de memoria del cgroup del proceso, si corre dentro
    de uno con límite configurado (p. ej. un contenedor Docker con
    `--memory`). `psutil.virtual_memory()` reporta la RAM del HOST, no la
    del cgroup — dentro de un contenedor con límite más estricto que el
    host, eso hace que el presupuesto adaptativo sobreestime enormemente lo
    disponible (confirmado en la verificación de FASE 3: un contenedor con
    `--memory=2g` en un host con ~6.7GB libres fue OOM-killed porque el
    presupuesto se calculó sobre esos 6.7GB, no sobre el límite real de
    2GB). Devuelve `None` si no hay límite de cgroup aplicable (proceso
    corriendo directo en el host, o límite "unlimited").
    """
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            raw_limit = f.read().strip()
        if raw_limit != "max":
            with open("/sys/fs/cgroup/memory.current") as f:
                usage = int(f.read().strip())
            return int(raw_limit), usage
    except OSError:
        pass

    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            limit = int(f.read().strip())
        # cgroup v1 representa "sin límite" con un número enorme (cercano a
        # 2^63), no con la palabra "max" como cgroup v2.
        if limit < (1 << 62):
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
                usage = int(f.read().strip())
            return limit, usage
    except OSError:
        pass

    return None


def _available_batch_memory_budget_bytes() -> int:
    """
    Fracción de la memoria libre AHORA MISMO (no un valor fijo de arranque)
    que un solo lote puede usar, con techo `BATCH_MEMORY_MAX_BYTES`. Se
    recalcula en cada lote nuevo porque la memoria disponible fluctúa entre lotes (otras
    peticiones, otros procesos del servidor). Toma el MÍNIMO entre la RAM
    libre del host (`psutil`) y la memoria libre dentro del cgroup actual,
    si el proceso corre bajo un límite de contenedor — ver
    `_cgroup_memory_limit_and_usage_bytes`.
    """
    import psutil

    available = psutil.virtual_memory().available

    cgroup_info = _cgroup_memory_limit_and_usage_bytes()
    if cgroup_info is not None:
        limit, usage = cgroup_info
        available = min(available, max(0, limit - usage))

    return min(
        int(available * BURSTINESS_BATCH_MEMORY_FRACTION),
        BATCH_MEMORY_MAX_BYTES,
    )


def _iter_batches_by_memory_budget(
    order: list[int],
    lengths: list[int],
    model,
):
    """
    Agrupa índices (ya ordenados por longitud en `order`) en lotes cuyo
    forward pass estimado no exceda el presupuesto de memoria. Es un
    generador a propósito: cada lote se arma recién cuando el consumidor
    pide el siguiente, es decir, DESPUÉS de procesar (y liberar) el
    anterior, así que el presupuesto refleja la RAM libre de ese momento.
    Antes los lotes se armaban todos de una vez al principio, con la misma
    lectura de RAM libre para todos.

    Techo de seguridad: un lote de tamaño 1 SIEMPRE se acepta así exceda el
    presupuesto estimado (`MIN_BURSTINESS_BATCH_SIZE`), para que el
    documento completo siempre termine de procesarse pase lo que pase con
    la estimación. Si ni siquiera ese lote mínimo cabe en la memoria física
    real, el try/except alrededor del forward pass en
    `_batch_sequence_perplexities` es quien degrada con gracia.
    """
    position = 0

    while position < len(order):
        budget = _available_batch_memory_budget_bytes()
        current = [order[position]]
        current_max_len = lengths[order[position]]
        position += 1

        while position < len(order):
            idx = order[position]
            candidate_max_len = max(current_max_len, lengths[idx])

            if (
                _estimate_forward_pass_bytes(len(current) + 1, candidate_max_len, model)
                > budget
            ):
                break

            current.append(idx)
            current_max_len = candidate_max_len
            position += 1

        yield current


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

    try:
        with torch.no_grad():
            output = model(input_ids, labels=input_ids)
    except (MemoryError, RuntimeError) as exc:
        if not _is_out_of_memory_error(exc):
            raise
        logger.error(
            "Cálculo de perplejidad omitido por falta de memoria (%d tokens): %s",
            input_ids.shape[1],
            exc,
        )
        return None

    loss = output.loss.item()

    if math.isnan(loss) or math.isinf(loss):
        return None

    return min(math.exp(loss), MAX_CLAMPED_PERPLEXITY)


def _batch_sequence_perplexities(
    texts: list[str],
    *,
    max_tokens: int = MAX_TOKENS,
) -> list[float | None]:
    """
    Perplejidad de cada texto en `texts`, procesados en lotes en vez de un
    forward pass por texto. Devuelve una lista alineada por índice con
    `texts` (None donde no se pudo calcular, misma semántica que
    `_sequence_perplexity`).

    Las oraciones se ordenan por longitud antes de agruparlas en lotes
    (bucketing) para que el padding dentro de cada lote sea mínimo: mezclar
    una oración de 8 tokens con una de 300 en el mismo lote forzaría
    paddear la corta a 300, desperdiciando memoria y cómputo. Los lotes se
    arman por PRESUPUESTO DE MEMORIA, no por una cantidad fija de oraciones
    (ver `_iter_batches_by_memory_budget`): el pico de RAM depende del
    producto batch_size × longitud², así que un lote con oraciones largas
    debe ser más chico que uno con oraciones cortas para que el pico total
    se mantenga acotado sin importar el tamaño del documento.
    """
    import torch

    if not texts:
        return []

    tokenizer, model = _get_model_and_tokenizer()

    lengths = [
        len(tokenizer.encode(text, truncation=True, max_length=max_tokens))
        for text in texts
    ]
    order = sorted(range(len(texts)), key=lambda i: lengths[i])
    results: list[float | None] = [None] * len(texts)

    for chunk_indices in _iter_batches_by_memory_budget(order, lengths, model):
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

        try:
            with torch.no_grad():
                losses, token_counts = _mean_token_losses(
                    model, input_ids, attention_mask
                )
        except (MemoryError, RuntimeError) as exc:
            if not _is_out_of_memory_error(exc):
                raise
            logger.error(
                "Lote de perplejidad omitido por falta de memoria (%d secuencias, "
                "máx %d tokens en el lote): %s",
                len(chunk_indices),
                max(lengths[i] for i in chunk_indices),
                exc,
            )
            continue
        finally:
            # El generador arma el siguiente lote con la RAM libre de ese
            # momento: los tensores de este lote no deben seguir vivos.
            del encoding, input_ids, attention_mask

        for row, idx in enumerate(chunk_indices):
            if token_counts[row].item() < 1:
                continue

            mean_loss = losses[row].item()

            if math.isnan(mean_loss) or math.isinf(mean_loss):
                continue

            results[idx] = min(math.exp(mean_loss), MAX_CLAMPED_PERPLEXITY)

    return results


def _mean_token_losses(model, input_ids, attention_mask):
    """
    Pérdida media por secuencia (sobre sus tokens reales) y cantidad de
    tokens predichos de cada una, SIN materializar los logits del lote
    completo: se corre el transformer sin la cabeza de lenguaje y
    `lm_head` se aplica por trozos de `LOSS_CHUNK_TOKENS` posiciones
    reales (el padding ni se proyecta).

    Sin `labels=` en la llamada al modelo porque la pérdida interna de
    `transformers` promedia sobre TODAS las posiciones desplazadas,
    incluyendo el padding — eso contaminaría la perplejidad de las
    secuencias cortas del lote. Cada secuencia se evalúa solo sobre sus
    propios tokens reales, igual que `_sequence_perplexity`.
    """
    import torch
    from torch.nn import functional as F

    hidden = model.transformer(
        input_ids,
        attention_mask=attention_mask,
    ).last_hidden_state

    # La posición t predice el token t + 1.
    shift_mask = attention_mask[:, 1:].bool()
    shift_hidden = hidden[:, :-1, :][shift_mask]
    shift_labels = input_ids[:, 1:][shift_mask]
    rows = torch.arange(input_ids.shape[0]).unsqueeze(1).expand_as(shift_mask)[shift_mask]
    del hidden

    loss_sums = torch.zeros(input_ids.shape[0], dtype=torch.float32)

    for start in range(0, shift_labels.shape[0], LOSS_CHUNK_TOKENS):
        end = start + LOSS_CHUNK_TOKENS
        logits = model.lm_head(shift_hidden[start:end])
        token_losses = F.cross_entropy(logits, shift_labels[start:end], reduction="none")
        loss_sums.index_add_(0, rows[start:end], token_losses)
        del logits, token_losses

    token_counts = shift_mask.sum(dim=1)

    return loss_sums / token_counts.clamp(min=1), token_counts


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
BURSTINESS_WEIGHT = Decimal("0.00")
PERPLEXITY_WEIGHT = Decimal("1.00")


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
        dominante de todo el análisis. El tamaño de cada lote se decide de
        forma adaptativa según la RAM libre en ese momento (ver
        `_iter_batches_by_memory_budget`), no un conteo fijo de oraciones, para
        que el pico de memoria se mantenga acotado sin importar el tamaño del
        documento ni cuánta RAM tenga el servidor. El batching se hace sobre
        todas las oraciones del documento a la vez (no por fragmento) para no
        perder ese beneficio de rendimiento; el agrupamiento en fragmentos
        ocurre después, sobre los resultados ya calculados.

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
