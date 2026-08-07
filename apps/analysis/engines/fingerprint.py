from __future__ import annotations

import hashlib

from apps.analysis.engines.similarity import InternalSimilarityEngine

_SIGNED_64BIT_MAX = (2**63) - 1

_shingle_engine = InternalSimilarityEngine()


def hash_shingle(shingle: str) -> int:
    """
    Hash estable de un shingle, independiente del proceso.

    No se usa hash() nativo de Python porque su semilla varía entre
    procesos (PYTHONHASHSEED aleatorio), lo que haría que el mismo texto
    produjera huellas distintas en cada ejecución. blake2b es determinista
    y rápido para cadenas cortas como los shingles.

    El resultado se acota a 63 bits (% (2**63 - 1)) para que siempre quepa
    en un BigIntegerField de Postgres, que es signed 64-bit.
    """
    digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % _SIGNED_64BIT_MAX


def compute_fingerprints(normalized_text: str, window_size: int = 4) -> set[int]:
    """
    Calcula las huellas digitales de un texto mediante Winnowing
    (Schleimer, Wilkerson & Aiken, 2003 — "Winnowing: Local Algorithms
    for Document Fingerprinting"), el mismo enfoque usado por MOSS y por
    el origen académico de Turnitin.

    A diferencia de usar todos los shingles como huellas (costoso e
    innecesario), Winnowing selecciona solo un subconjunto representativo:
    por cada ventana deslizante de `window_size` shingles consecutivos, se
    conserva únicamente el hash mínimo de la ventana. Esto garantiza que:

    - Cualquier coincidencia de `window_size` shingles seguidos entre dos
      documentos comparte al menos una huella (garantía de detección).
    - El número total de huellas guardadas se reduce drásticamente
      respecto al número de shingles, sin perder la capacidad de detectar
      coincidencias largas.

    Pasos:
    1. Genera la secuencia ORDENADA de shingles del texto (reutilizando
       InternalSimilarityEngine._build_shingle_sequence, no se duplica esa
       lógica).
    2. Hashea cada shingle con hash_shingle().
    3. Desliza una ventana de `window_size` hashes consecutivos y selecciona
       el hash mínimo de cada ventana. Si hay empate, se selecciona el de
       posición más a la derecha (regla estándar de Winnowing, evita
       preferir sistemáticamente huellas de la izquierda de la ventana).
    4. Devuelve el conjunto de hashes únicos seleccionados: las huellas
       finales del documento.
    """
    shingles = _shingle_engine._build_shingle_sequence(normalized_text)
    hashes = [hash_shingle(shingle) for shingle in shingles]

    if len(hashes) < window_size:
        return set(hashes)

    selected: set[int] = set()

    for start in range(len(hashes) - window_size + 1):
        window = hashes[start : start + window_size]
        min_value = min(window)
        rightmost_index = max(
            index for index, value in enumerate(window) if value == min_value
        )
        selected.add(window[rightmost_index])

    return selected
