"""
Calificador del banco de similitud.

Por caso:
- Recall de palabras: palabras insertadas que quedaron marcadas / palabras
  insertadas (las que caen en citas excluidas no cuentan).
- Recall de fuentes: fuentes de las inserciones que el motor nombró.
- Falsos positivos nuevos: palabras del texto base marcadas en el caso
  que NO estaban marcadas al analizar la base sola (línea base).

Si el texto analizado no es idéntico al del caso (DOCX/PDF extraídos), las
palabras se emparejan por alineación de secuencias; las insertadas que se
pierden en la extracción cuentan como no detectadas.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from apps.analysis.banco.generador import BankCase
from apps.analysis.banco.textos import words


@dataclass(frozen=True)
class EngineOutput:
    """Lo que devolvió el motor para un texto."""

    analyzed_text: str
    marked_ranges: list[tuple[int, int]]
    quoted_ranges: list[tuple[int, int]]
    # Claves de fuente con palabras atribuidas, en el formato del banco
    # ("oai:<uuid>", "web:<url>", "instituto:<id>"); otras van tal cual.
    source_keys: set[str]
    percent: Decimal
    seconds: float


@dataclass(frozen=True)
class Baseline:
    """Lo que marcó el motor en el fragmento base analizado solo."""

    marked_words: frozenset[int]
    source_keys: frozenset[str]
    percent: Decimal


@dataclass
class CaseScore:
    case_id: str
    category: str
    variant: str  # "txt" | "docx" | "pdf"
    percent: Decimal
    baseline_percent: Decimal
    seconds: float
    inserted_words: int
    detected_words: int
    expected_sources: int
    detected_sources: int
    base_words: int
    new_false_positive_words: int
    baseline_words_marked_again: int
    extra_sources: list[str] = field(default_factory=list)
    missed_sources: list[str] = field(default_factory=list)

    @property
    def word_recall(self) -> Decimal | None:
        return _ratio(self.detected_words, self.inserted_words)

    @property
    def source_recall(self) -> Decimal | None:
        return _ratio(self.detected_sources, self.expected_sources)

    def to_json(self) -> dict:
        return {
            "caso": self.case_id,
            "categoria": self.category,
            "variante": self.variant,
            "porcentaje": str(self.percent),
            "porcentaje_linea_base": str(self.baseline_percent),
            "segundos": round(self.seconds, 2),
            "palabras_insertadas": self.inserted_words,
            "palabras_detectadas": self.detected_words,
            "recall_palabras": _str(self.word_recall),
            "fuentes_esperadas": self.expected_sources,
            "fuentes_detectadas": self.detected_sources,
            "recall_fuentes": _str(self.source_recall),
            "palabras_base": self.base_words,
            "fp_nuevas": self.new_false_positive_words,
            "base_marcada_otra_vez": self.baseline_words_marked_again,
            "fuentes_extra": self.extra_sources,
            "fuentes_no_detectadas": self.missed_sources,
        }


def _ratio(part: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(part) / Decimal(total) * 100).quantize(Decimal("0.1"))


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _overlaps(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    """`ranges` ordenados y sin solaparse."""
    index = bisect.bisect_right(ranges, (start, float("inf"))) - 1
    for candidate in ranges[max(index, 0) : index + 2]:
        if candidate[0] < end and start < candidate[1]:
            return True
    return False


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def word_flags(text: str, marked: list[tuple[int, int]], quoted: list[tuple[int, int]]):
    """(marcada, en_cita) por cada palabra del texto."""
    marked = _merge(marked)
    quoted = _merge(quoted)
    return [
        (_overlaps(marked, m.start(), m.end()), _overlaps(quoted, m.start(), m.end()))
        for m in words(text)
    ]


def baseline_from_output(output: EngineOutput) -> Baseline:
    flags = word_flags(output.analyzed_text, output.marked_ranges, output.quoted_ranges)
    return Baseline(
        marked_words=frozenset(i for i, (marked, quoted) in enumerate(flags) if marked and not quoted),
        source_keys=frozenset(output.source_keys),
        percent=output.percent,
    )


def case_word_labels(case: BankCase) -> list[tuple[str, int] | None]:
    """
    Etiqueta de cada palabra del contenido: ("insercion", n) o
    ("base", índice de la palabra en el fragmento base).
    """
    labels: list[tuple[str, int] | None] = []
    starts = [segment.start for segment in case.segments]
    base_index = 0

    for match in words(case.content):
        segment = case.segments[bisect.bisect_right(starts, match.start()) - 1]

        if segment.kind == "insercion":
            labels.append(("insercion", segment.insertion))
        elif segment.kind == "base":
            labels.append(("base", base_index))
            base_index += 1
        else:
            labels.append(None)

    return labels


def align_words(reference: str, analyzed: str) -> dict[int, int]:
    """Índice de palabra analizada -> índice de palabra del caso."""
    if reference == analyzed:
        return {index: index for index in range(len(words(reference)))}

    left = [m.group(0).lower() for m in words(reference)]
    right = [m.group(0).lower() for m in words(analyzed)]
    mapping: dict[int, int] = {}

    for block in SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            mapping[block.b + offset] = block.a + offset

    return mapping


def score_case(
    case: BankCase,
    output: EngineOutput,
    baseline: Baseline,
    institute_keys: dict[str, str] | None = None,
    variant: str = "txt",
) -> CaseScore:
    labels = case_word_labels(case)
    flags = word_flags(output.analyzed_text, output.marked_ranges, output.quoted_ranges)
    mapping = align_words(case.content, output.analyzed_text)

    inserted_total = sum(1 for label in labels if label and label[0] == "insercion")
    base_total = sum(1 for label in labels if label and label[0] == "base")
    detected = 0
    excluded_inserted = 0
    new_fp = 0
    again = 0

    for analyzed_index, (marked, quoted) in enumerate(flags):
        case_index = mapping.get(analyzed_index)
        label = labels[case_index] if case_index is not None else None

        if label and label[0] == "insercion":
            if quoted:
                excluded_inserted += 1
            elif marked:
                detected += 1
            continue

        if not marked or quoted:
            continue

        if label and label[0] == "base" and label[1] in baseline.marked_words:
            again += 1
        else:
            # Base marcada que no estaba en la línea base, o texto que la
            # extracción cambió y ya no se puede emparejar.
            new_fp += 1

    expected = {insertion.source_key for insertion in case.insertions}
    found = {_bank_key(key, institute_keys) for key in output.source_keys}
    baseline_found = {_bank_key(key, institute_keys) for key in baseline.source_keys}

    return CaseScore(
        case_id=case.id,
        category=case.category,
        variant=variant,
        percent=output.percent,
        baseline_percent=baseline.percent,
        seconds=output.seconds,
        inserted_words=inserted_total - excluded_inserted,
        detected_words=detected,
        expected_sources=len(expected),
        detected_sources=len(expected & found),
        base_words=base_total,
        new_false_positive_words=new_fp,
        baseline_words_marked_again=again,
        extra_sources=sorted(found - expected - baseline_found),
        missed_sources=sorted(expected - found),
    )


def _bank_key(key: str, institute_keys: dict[str, str] | None) -> str:
    if institute_keys and key in institute_keys:
        return institute_keys[key]
    return key


def summarize(scores: list[CaseScore]) -> dict[str, dict]:
    """Métricas agregadas por categoría (micro: suma de palabras/fuentes)."""
    groups: dict[str, list[CaseScore]] = {}
    for score in scores:
        key = score.category if score.variant == "txt" else f"{score.category}.{score.variant}"
        groups.setdefault(key, []).append(score)

    summary = {}
    for key, items in groups.items():
        inserted = sum(item.inserted_words for item in items)
        base = sum(item.base_words for item in items)
        percents = [item.percent for item in items]
        summary[key] = {
            "casos": len(items),
            "recall_palabras": _str(_ratio(sum(i.detected_words for i in items), inserted)),
            "recall_fuentes": _str(
                _ratio(sum(i.detected_sources for i in items), sum(i.expected_sources for i in items))
            ),
            "porcentaje_medio": str((sum(percents) / len(percents)).quantize(Decimal("0.01"))),
            "porcentaje_max": str(max(percents)),
            "linea_base_media": str(
                (sum(i.baseline_percent for i in items) / len(items)).quantize(Decimal("0.01"))
            ),
            "fp_nuevas_palabras": sum(i.new_false_positive_words for i in items),
            "fp_nuevas_pct_base": _str(
                None if base <= 0 else (Decimal(sum(i.new_false_positive_words for i in items)) / base * 100).quantize(Decimal("0.01"))
            ),
            "fuentes_extra": sum(len(i.extra_sources) for i in items),
            "segundos_medio": round(sum(i.seconds for i in items) / len(items), 2),
            "segundos_max": round(max(i.seconds for i in items), 2),
        }
    return summary
