"""
FASE 2 — Medición de costo de generar embeddings semánticos (NO se procesa
el corpus real de tesis, NO toca la base de datos; solo mide tiempo/RAM con
un documento sintético representativo).

Ejecutar (con el venv activado):

    python apps/analysis/experiments/paraphrase_cost_benchmark.py

Mide:
1. Tiempo de generar embeddings de UN documento típico (~10,000 palabras),
   fragmentado igual que el resto del pipeline (85 palabras/fragmento, ver
   InternalSimilarityEngine.CHUNK_WORD_SIZE).
2. RAM que ocupa el modelo cargado (RSS delta, no incluye el intérprete
   Python base).
3. Proyección aritmética (NO ejecutada) del costo de escalar a 195,000 tesis:
   tiempo total estimado y espacio en disco de los vectores resultantes.
"""

from __future__ import annotations

import resource
import time

from apps.analysis.engines.similarity import InternalSimilarityEngine
from apps.analysis.experiments.paraphrase_poc import TEST_CASES, MODEL_NAME

CORPUS_SIZE = 195_000  # tesis en el corpus objetivo, según el pedido original
TYPICAL_DOC_WORDS = 10_000
EMBEDDING_DIM = 384  # dimensión de salida de paraphrase-multilingual-MiniLM-L12-v2
FLOAT32_BYTES = 4


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _build_synthetic_document(target_words: int) -> str:
    """Concatena los párrafos del POC (original+parafraseado+no_relacionado)
    en bucle hasta alcanzar ~target_words. Sirve solo para medir throughput
    del modelo, no para medir calidad semántica (eso ya lo hace el POC)."""
    pool = []
    for case in TEST_CASES:
        pool.extend([case["original"], case["parafraseado"], case["no_relacionado"]])
    words: list[str] = []
    i = 0
    while len(words) < target_words:
        words.extend(pool[i % len(pool)].split())
        i += 1
    return " ".join(words[:target_words])


def _chunk_by_words(text: str, chunk_size: int) -> list[str]:
    words = text.split()
    return [
        " ".join(words[start : start + chunk_size])
        for start in range(0, len(words), chunk_size)
    ]


def main() -> None:
    rss_before_model = _rss_mb()

    t0 = time.time()
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)
    load_time = time.time() - t0
    rss_after_model = _rss_mb()

    doc = _build_synthetic_document(TYPICAL_DOC_WORDS)
    fragments = _chunk_by_words(doc, InternalSimilarityEngine.CHUNK_WORD_SIZE)

    t1 = time.time()
    embeddings = model.encode(fragments, show_progress_bar=False, batch_size=32)
    encode_time = time.time() - t1
    rss_after_encode = _rss_mb()

    print(f"Modelo: {MODEL_NAME}")
    print(f"Tiempo de carga del modelo: {load_time:.1f}s")
    print(
        f"RAM: antes de cargar={rss_before_model:.0f}MB, "
        f"tras cargar={rss_after_model:.0f}MB "
        f"(delta modelo ≈ {rss_after_model - rss_before_model:.0f}MB), "
        f"tras codificar 1 doc={rss_after_encode:.0f}MB"
    )
    print()
    print(f"Documento sintético: {TYPICAL_DOC_WORDS} palabras -> {len(fragments)} fragmentos "
          f"de {InternalSimilarityEngine.CHUNK_WORD_SIZE} palabras (mismo tamaño que "
          f"InternalSimilarityEngine)")
    print(f"Tiempo de embeddings para 1 documento: {encode_time:.2f}s "
          f"({encode_time / len(fragments) * 1000:.1f}ms/fragmento)")
    print(f"Forma de los embeddings: {embeddings.shape}")

    # --- Proyección aritmética a 195k tesis (NO se ejecuta el corpus) ---
    total_seconds = encode_time * CORPUS_SIZE
    total_hours = total_seconds / 3600
    total_days = total_hours / 24

    vector_bytes_per_doc = len(fragments) * EMBEDDING_DIM * FLOAT32_BYTES
    total_disk_bytes = vector_bytes_per_doc * CORPUS_SIZE
    total_disk_gb = total_disk_bytes / (1024**3)

    print()
    print(f"--- Proyección a {CORPUS_SIZE:,} tesis (aritmética, NO ejecutada) ---")
    print(
        f"Tiempo total estimado (CPU, secuencial, 1 proceso): "
        f"{total_hours:.1f}h (~{total_days:.1f} días)"
    )
    print(
        f"Espacio en disco de los vectores (float32, sin comprimir, "
        f"~{len(fragments)} fragmentos/doc x {EMBEDDING_DIM}d): {total_disk_gb:.1f} GB"
    )


if __name__ == "__main__":
    main()
