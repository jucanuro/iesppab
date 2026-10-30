from __future__ import annotations

import json
import math
import random
import resource
import statistics
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.analysis.engines import ai_segments
from apps.analysis.engines import binoculars_detector as bd
from apps.analysis.engines import perplexity_detector as pd
from apps.analysis.management.commands.evaluate_ai_detection import (
    LABEL_AI,
    LABEL_HUMAN,
    load_file_samples,
)

# Grupos del banco privado (eval_ia/, fuera de git): nombre, subcarpeta,
# etiqueta esperada y extensiones (None = todas). Del instituto solo los
# DOCX: los 3 PDF de humanos/ son licenciaturas de otro origen.
GROUP_CALIBRATION = "upeu_pre2022"
GROUPS = (
    (GROUP_CALIBRATION, "humanos_pre2022", LABEL_HUMAN, None),
    ("upeu_2022plus", "humanos_upeu_2022plus", LABEL_HUMAN, None),
    ("instituto", "humanos", LABEL_HUMAN, {".docx"}),
    ("ia", "ia", LABEL_AI, None),
)

ENGINE_BINOCULARS = "binoculars"
ENGINE_GPT2 = "gpt2"
ENGINES = (ENGINE_BINOCULARS, ENGINE_GPT2)

DEFAULT_SEED = 2026
DEFAULT_TARGET_FPR = 0.01
DOC_THRESHOLD = 20.0
CACHE_NAME = "binoculars_segmentos.json"


class Command(BaseCommand):
    help = (
        "Compara Binoculars (Qwen2.5-0.5B / -Instruct) con la perplejidad "
        "GPT-2 actual sobre los MISMOS segmentos de prosa evaluable del banco "
        "eval_ia/. Calibra el umbral de segmento con la mitad de las tesis "
        "UPeU pre-2022 y reporta con la otra mitad. Los scores por segmento se "
        "guardan en un JSON y cada motor se puede correr en un proceso aparte "
        "(--engine) para medir su RAM pico por separado."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dir", type=str, required=True)
        parser.add_argument(
            "--engine",
            choices=[*ENGINES, "all", "none"],
            default="all",
            help="Motor a calcular (lo ya guardado en el JSON no se recalcula). "
            "'none' solo reporta.",
        )
        parser.add_argument(
            "--cache",
            type=str,
            default=None,
            help=f"JSON de scores por segmento. Default: <dir>/{CACHE_NAME}.",
        )
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        parser.add_argument(
            "--target-fpr",
            type=float,
            default=DEFAULT_TARGET_FPR,
            help="Fracción máxima de segmentos humanos de calibración marcados.",
        )
        parser.add_argument(
            "--batch-memory-fraction",
            type=float,
            default=None,
            help="Fracción de RAM libre por lote de GPT-2 (baja memoria: 0.2).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Máximo de documentos por grupo (corridas rápidas).",
        )

    def handle(self, *args, **options) -> None:
        directory = Path(options["dir"])
        if not directory.is_dir():
            raise CommandError(f"'{directory}' no es una carpeta.")

        cache_path = Path(options["cache"] or directory / CACHE_NAME)
        cache = _load_cache(cache_path)

        engine = options["engine"]
        engines = ENGINES if engine == "all" else () if engine == "none" else (engine,)
        if options["batch_memory_fraction"] is not None:
            pd.BURSTINESS_BATCH_MEMORY_FRACTION = options["batch_memory_fraction"]

        for name in engines:
            self._compute(name, directory, cache, cache_path, options["limit"])

        documents = list(cache["documents"].values())
        if not documents:
            raise CommandError("No hay documentos evaluados.")

        self._report(documents, cache, options["seed"], options["target_fpr"])

    # --- Cálculo ---

    def _compute(self, engine, directory, cache, cache_path, limit) -> None:
        done = 0

        # Primero lo que hace falta para calibrar y para el AUROC.
        compute_order = sorted(GROUPS, key=lambda g: g[0] not in (GROUP_CALIBRATION, "ia"))

        for group, subdir, label, suffixes in compute_order:
            samples = load_file_samples(directory / subdir, label, stderr=self.stderr)
            if suffixes is not None:
                samples = [s for s in samples if Path(s.name).suffix.lower() in suffixes]

            for sample in samples[:limit]:
                key = f"{group}/{sample.name}"
                entry = cache["documents"].get(key)
                if entry is not None and engine in entry["scores"]:
                    continue

                segments = ai_segments.build_segments(
                    sample.content,
                    excluded_ranges=sample.excluded_ranges,
                )
                if entry is None:
                    entry = cache["documents"][key] = _new_entry(group, sample, segments)
                elif len(entry["segments"]) != len(segments):
                    raise CommandError(f"'{key}': cambió la segmentación desde el JSON.")

                started = time.monotonic()
                entry["scores"][engine] = _score_segments(engine, segments)
                entry["seconds"][engine] = round(time.monotonic() - started, 2)

                peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                cache["peak_rss_mb"][engine] = round(
                    max(peak_mb, cache["peak_rss_mb"].get(engine, 0)), 1
                )
                _save_cache(cache_path, cache)

                done += 1
                self.stderr.write(
                    f"[{engine}] {key}: {entry['seconds'][engine]:.0f}s, "
                    f"pico {peak_mb:.0f}MB"
                )

        if done:
            self.stderr.write(f"[{engine}] {done} documentos nuevos en {cache_path}")

    # --- Reporte ---

    def _report(self, documents, cache, seed, target_fpr) -> None:
        calibration_names, test_names = split_calibration(
            [d["name"] for d in documents if d["group"] == GROUP_CALIBRATION],
            seed=seed,
        )
        calibration = [
            d for d in documents
            if d["group"] == GROUP_CALIBRATION and d["name"] in calibration_names
        ]
        test = [
            d for d in documents
            if d["group"] == GROUP_CALIBRATION and d["name"] in test_names
        ]
        engines = [e for e in ENGINES if all(e in d["scores"] for d in documents)]
        if not engines:
            raise CommandError("Ningún motor tiene scores para todos los documentos.")

        thresholds = {}
        for engine in engines:
            values = [
                value
                for d in calibration
                for value in _evaluable_values(d, engine)
            ]
            thresholds[engine] = calibrate_threshold(values, target_fpr)

        self.stdout.write(
            f"\n== Calibración (semilla {seed}): {len(calibration_names)} tesis UPeU "
            f"pre-2022 de calibración, {len(test_names)} de prueba; umbral de "
            f"segmento con <= {target_fpr:.0%} de segmentos humanos marcados =="
        )
        for engine in engines:
            label = "score <" if engine == ENGINE_BINOCULARS else "ppl <"
            self.stdout.write(f"{engine:<11} {label} {thresholds[engine]:.4f}")

        rows = [
            ("UPeU pre-2022 prueba", test),
            ("UPeU pre-2022 calib.", calibration),
        ] + [
            (group, [d for d in documents if d["group"] == group])
            for group, *_ in GROUPS
            if group != GROUP_CALIBRATION
        ]

        header = (
            f"{'grupo':<21} {'n':>3} {'motor':<11} {'mediana %IA':>11} "
            f"{'docs>=20%':>9} {'seg. marc.':>10} {'% evaluable':>11} "
            f"{'s/doc med':>9}"
        )
        self.stdout.write(f"\n== Por grupo (documento marcado si % IA >= {DOC_THRESHOLD:.0f}) ==")
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for name, group_documents in rows:
            if not group_documents:
                continue
            for engine in engines:
                percents = [
                    document_ai_percent(d, engine, thresholds[engine])
                    for d in group_documents
                ]
                values = [v for d in group_documents for v in _evaluable_values(d, engine)]
                flagged_segments = sum(1 for v in values if v < thresholds[engine])
                seconds = [d["seconds"][engine] for d in group_documents]
                self.stdout.write(
                    f"{name:<21} {len(group_documents):>3} {engine:<11} "
                    f"{statistics.median(percents):>11.1f} "
                    f"{_share(percents, DOC_THRESHOLD):>9} "
                    f"{flagged_segments / len(values) if values else 0:>10.1%} "
                    f"{evaluable_word_share(group_documents, engine):>11.0%} "
                    f"{statistics.median(seconds):>9.0f}"
                )

        ai_documents = [d for d in documents if d["group"] == "ia"]
        self.stdout.write(
            "\n== AUROC por segmento (UPeU pre-2022 prueba vs IA, mismos segmentos) =="
        )
        for label, engine, field in (
            ("Binoculars", ENGINE_BINOCULARS, "score"),
            ("Qwen-Instruct ppl sola", ENGINE_BINOCULARS, "log_ppl"),
            ("GPT-2 ppl", ENGINE_GPT2, "score"),
        ):
            if engine not in engines:
                continue
            humans, ai_values = _paired_values(test, ai_documents, engines, engine, field)
            self.stdout.write(
                f"{label:<23} {auroc(humans, ai_values):.3f} "
                f"(humanos={len(humans)}, IA={len(ai_values)})"
            )

        self.stdout.write("\n== Tiempo y RAM ==")
        for engine in engines:
            seconds = [d["seconds"][engine] for d in documents]
            words = sum(_total_words(d) for d in documents)
            self.stdout.write(
                f"{engine:<11} mediana {statistics.median(seconds):.0f}s/doc, "
                f"total {sum(seconds) / 60:.0f} min, "
                f"{sum(seconds) / words * 1000:.1f}s por 1000 palabras, "
                f"pico RSS {cache['peak_rss_mb'].get(engine, 0):.0f}MB"
            )


def _new_entry(group: str, sample, segments: list[ai_segments.TextSegment]) -> dict:
    return {
        "group": group,
        "name": sample.name,
        "segments": [
            {
                "words": segment.word_count,
                "evaluable_words": segment.evaluable_word_count,
                "reason": segment.excluded_reason,
            }
            for segment in segments
        ],
        "scores": {},
        "seconds": {},
    }


def _score_segments(engine: str, segments: list[ai_segments.TextSegment]) -> list:
    """Un valor por segmento (None en los no evaluables)."""
    evaluable = [i for i, s in enumerate(segments) if s.evaluable]
    values: list = [None] * len(segments)

    if engine == ENGINE_GPT2:
        perplexities = pd._batch_sequence_perplexities(
            [segments[i].evaluable_text for i in evaluable]
        )
        for index, perplexity in zip(evaluable, perplexities):
            values[index] = {"score": perplexity} if perplexity is not None else None
        return values

    results = bd.score_texts([segments[i].evaluable_text for i in evaluable])
    for index, result in zip(evaluable, results):
        if result is not None:
            values[index] = {
                "score": result.score,
                "log_ppl": result.log_ppl,
                "log_x_ppl": result.log_x_ppl,
                "tokens": result.tokens,
            }

    return values


def split_calibration(names: list[str], *, seed: int) -> tuple[set[str], set[str]]:
    """Mitad calibración / mitad prueba, al azar con semilla fija."""
    shuffled = sorted(names)
    random.Random(seed).shuffle(shuffled)
    half = len(shuffled) // 2
    return set(shuffled[:half]), set(shuffled[half:])


def calibrate_threshold(values: list[float], target_fpr: float) -> float:
    """
    Umbral tal que a lo sumo `target_fpr` de `values` (segmentos humanos)
    queda estrictamente por debajo (score bajo = IA).
    """
    if not values:
        raise CommandError("No hay segmentos de calibración.")

    ordered = sorted(values)
    return ordered[min(math.floor(target_fpr * len(ordered)), len(ordered) - 1)]


def _evaluable_values(document: dict, engine: str, field: str = "score") -> list[float]:
    return [v[field] for v in document["scores"][engine] if v is not None]


def document_ai_percent(document: dict, engine: str, threshold: float) -> float:
    evaluable = ai = 0

    for segment, value in zip(document["segments"], document["scores"][engine]):
        if value is None:
            continue
        evaluable += segment["evaluable_words"]
        if value["score"] < threshold:
            ai += segment["evaluable_words"]

    return 100 * ai / evaluable if evaluable else 0.0


def _total_words(document: dict) -> int:
    return sum(segment["words"] for segment in document["segments"])


def evaluable_word_share(documents: list[dict], engine: str) -> float:
    """Palabras evaluables (con score) / palabras totales del grupo."""
    total = sum(_total_words(d) for d in documents)
    evaluable = sum(
        segment["evaluable_words"]
        for d in documents
        for segment, value in zip(d["segments"], d["scores"][engine])
        if value is not None
    )
    return evaluable / total if total else 0.0


def _paired_values(humans, ai_documents, engines, engine, field):
    """Valores de `engine` solo en segmentos que tienen score en TODOS los motores."""

    def collect(documents):
        values = []
        for d in documents:
            for index, value in enumerate(d["scores"][engine]):
                if value is not None and all(
                    d["scores"][other][index] is not None for other in engines
                ):
                    values.append(value[field])
        return values

    return collect(humans), collect(ai_documents)


def auroc(human_values: list[float], ai_values: list[float]) -> float:
    """
    P(un segmento de IA tenga valor menor que uno humano), empates a la
    mitad (Mann-Whitney). Valor bajo = IA en los tres motores.
    """
    if not human_values or not ai_values:
        return float("nan")

    pooled = sorted(
        [(v, 0) for v in human_values] + [(v, 1) for v in ai_values],
        key=lambda item: item[0],
    )
    # Rangos promedio (1-based) con empates.
    rank_sum_ai = 0.0
    index = 0
    while index < len(pooled):
        end = index
        while end + 1 < len(pooled) and pooled[end + 1][0] == pooled[index][0]:
            end += 1
        average_rank = (index + end) / 2 + 1
        rank_sum_ai += average_rank * sum(1 for i in range(index, end + 1) if pooled[i][1])
        index = end + 1

    n_ai, n_human = len(ai_values), len(human_values)
    u_ai_lower = n_ai * n_human + n_ai * (n_ai + 1) / 2 - rank_sum_ai
    return u_ai_lower / (n_ai * n_human)


def _share(percents: list[float], threshold: float) -> str:
    if not percents:
        return "n/a"
    return f"{sum(1 for p in percents if p >= threshold) / len(percents):.0%}"


def _load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"documents": {}, "peak_rss_mb": {}}


def _save_cache(path: Path, cache: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
