from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable
from decimal import Decimal

# Marca que separa, dentro del texto normalizado de un fragmento, los
# tramos de prosa que quedan a cada lado de una cita excluida. No puede
# aparecer en un texto normalizado (solo [a-z0-9ñü ]), así que ningún
# shingle ni frase exacta que la contenga puede coincidir con una fuente:
# las palabras de antes y después de la cita nunca forman un shingle
# artificial que no existe en el documento.
EXCLUSION_BOUNDARY = "|"


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []

    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))

    return merged


def subtract_ranges(
    ranges: Iterable[tuple[int, int]],
    excluded_ranges: Iterable[tuple[int, int]],
) -> list[tuple[int, int]]:
    """
    Une `ranges` y les quita lo que cae dentro de `excluded_ranges`.
    """
    excluded = merge_ranges(excluded_ranges)
    result: list[tuple[int, int]] = []

    for start, end in merge_ranges(ranges):
        cursor = start

        for excluded_start, excluded_end in excluded:
            if excluded_end <= cursor:
                continue

            if excluded_start >= end:
                break

            if excluded_start > cursor:
                result.append((cursor, excluded_start))

            cursor = max(cursor, excluded_end)

        if cursor < end:
            result.append((cursor, end))

    return result


def calculate_similarity_percent(
    content_length: int,
    matched_ranges: Iterable[tuple[int, int]],
    excluded_ranges: Iterable[tuple[int, int]] = (),
) -> Decimal:
    """
    % de caracteres con coincidencia sobre el texto que SÍ cuenta: las
    citas excluidas no suman al numerador ni al denominador.
    """
    excluded = [
        (max(start, 0), min(end, content_length))
        for start, end in merge_ranges(excluded_ranges)
        if start < content_length
    ]
    countable_length = content_length - sum(end - start for start, end in excluded)

    if countable_length <= 0:
        return Decimal("0.00")

    counted_ranges = subtract_ranges(matched_ranges, excluded)

    if not counted_ranges:
        return Decimal("0.00")

    matched_chars = sum(end - start for start, end in counted_ranges)
    percent = (Decimal(matched_chars) / Decimal(countable_length)) * Decimal("100")

    return min(percent, Decimal("100.00")).quantize(Decimal("0.01"))


def build_chunks(
    content: str,
    words_with_offsets: list[tuple[str, int, int]],
    excluded_ranges: Iterable[tuple[int, int]],
    chunk_word_size: int,
    chunk_step: int,
    min_words: int,
    normalize: Callable[[str], str],
) -> list[tuple[str, str, int, int]]:
    """
    Fragmenta `content` en ventanas de palabras con offsets reales,
    salteando las palabras que caen dentro de `excluded_ranges`.

    El texto normalizado de cada fragmento se arma por tramos contiguos de
    prosa no excluida, unidos con `EXCLUSION_BOUNDARY`. Sin exclusiones hay
    un solo tramo y el resultado es idéntico a normalizar el fragmento
    entero, como se hacía antes.
    """
    excluded = merge_ranges(excluded_ranges)
    excluded_starts = [start for start, _ in excluded]
    excluded_ends = [end for _, end in excluded]

    # (inicio, fin, tramo): dos palabras conservadas pertenecen al mismo
    # tramo si no hay ninguna exclusión entre ellas, es decir, si la
    # cantidad de exclusiones que empiezan antes de cada una es la misma.
    kept_words: list[tuple[int, int, int]] = []

    for _, word_start, word_end in words_with_offsets:
        index = bisect_right(excluded_starts, word_start) - 1

        if index >= 0 and word_start < excluded_ends[index]:
            continue

        kept_words.append((word_start, word_end, index + 1))

    if len(kept_words) < min_words:
        return []

    chunks: list[tuple[str, str, int, int]] = []

    for start_index in range(0, len(kept_words), chunk_step):
        selected_words = kept_words[start_index : start_index + chunk_word_size]

        if len(selected_words) < min_words:
            continue

        start_offset = selected_words[0][0]
        end_offset = selected_words[-1][1]
        chunk_text = content[start_offset:end_offset].strip()

        segments: list[str] = []
        segment_start = selected_words[0]
        previous_word = selected_words[0]

        for word in selected_words[1:]:
            if word[2] != previous_word[2]:
                segments.append(
                    normalize(content[segment_start[0] : previous_word[1]])
                )
                segment_start = word

            previous_word = word

        segments.append(normalize(content[segment_start[0] : previous_word[1]]))

        normalized_chunk = f" {EXCLUSION_BOUNDARY} ".join(
            segment for segment in segments if segment
        )

        if normalized_chunk:
            chunks.append(
                (
                    chunk_text,
                    normalized_chunk,
                    start_offset,
                    end_offset,
                )
            )

    return chunks


def shingles_without_boundary(words: list[str], size: int) -> set[str]:
    if len(words) < size:
        return {word for word in words if word != EXCLUSION_BOUNDARY}

    return {
        " ".join(words[index : index + size])
        for index in range(len(words) - size + 1)
        if EXCLUSION_BOUNDARY not in words[index : index + size]
    }
