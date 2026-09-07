from __future__ import annotations

import csv
import random
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.analysis.engines.ai_detector import SpanishAIDetector
from apps.analysis.engines.perplexity_detector import SpanishPerplexityDetector

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "evaluation" / "fixtures"
DEFAULT_AI_DIR = FIXTURES_DIR / "ai_samples"
DEFAULT_HUMAN_DIR = FIXTURES_DIR / "human_samples"
DEFAULT_CSV_PATH = FIXTURES_DIR.parent / "benchmark_results.csv"

# Umbral con el que producción ya marca un documento como riesgo MEDIO por IA
# (ver ReportRiskLevel._resolve_risk_level en apps/analysis/services.py).
DEFAULT_HEURISTIC_THRESHOLD = Decimal("35.00")
SWEEP_THRESHOLDS = [Decimal(str(value)) for value in range(5, 100, 5)]

ENGINE_CHOICES = ["heuristic", "perplexity", "both"]


@dataclass
class SampleResult:
    filename: str
    label: int  # 1 = IA, 0 = humano
    probability: Decimal
    detail: str  # info extra específica del motor, solo para el CSV

    def predicted(self, threshold: Decimal) -> int:
        return 1 if self.probability >= threshold else 0


class Command(BaseCommand):
    help = (
        "Evalúa detectores de IA (heurístico y/o perplejidad+burstiness) contra "
        "un banco de pruebas etiquetado "
        "(apps/analysis/evaluation/fixtures/{ai_samples,human_samples}) y "
        "reporta matriz de confusión, precisión, recall, F1 y un estadístico "
        "rank-based (P(score_IA > score_humano), equivalente a AUC)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--engine",
            choices=ENGINE_CHOICES,
            default="heuristic",
            help=(
                "Qué motor evaluar: 'heuristic' (SpanishAIDetector, default, "
                "compatible con el uso anterior de este comando), 'perplexity' "
                "(SpanishPerplexityDetector, instanciado directamente en este "
                "script sin pasar por el flag PERPLEXITY_AI_DETECTION_ENABLED "
                "de producción) o 'both'."
            ),
        )
        parser.add_argument(
            "--ai-dir",
            type=str,
            default=str(DEFAULT_AI_DIR),
            help="Carpeta con muestras .txt etiquetadas como IA.",
        )
        parser.add_argument(
            "--human-dir",
            type=str,
            default=str(DEFAULT_HUMAN_DIR),
            help="Carpeta con muestras .txt etiquetadas como humanas.",
        )
        parser.add_argument(
            "--threshold",
            type=str,
            default=str(DEFAULT_HEURISTIC_THRESHOLD),
            help="Umbral de ai_probability_percent para el motor heurístico.",
        )
        parser.add_argument(
            "--perplexity-threshold",
            type=str,
            default=None,
            help=(
                "Umbral de ai_probability_percent para el motor de perplejidad. "
                "Si no se indica, se calcula dinámicamente como el punto medio "
                "entre la media de score de las muestras IA y la media de las "
                "muestras humanas observadas en esta corrida (documentado en la "
                "salida)."
            ),
        )
        parser.add_argument(
            "--csv-path",
            type=str,
            default=str(DEFAULT_CSV_PATH),
            help="Ruta del CSV de detalle a generar.",
        )
        parser.add_argument(
            "--sweep",
            action="store_true",
            help=(
                "Además del umbral indicado, prueba una batería de umbrales "
                "(5..95, paso 5) como diagnóstico y muestra cuál maximiza F1. "
                "No modifica nada en producción."
            ),
        )
        parser.add_argument(
            "--split-eval",
            action="store_true",
            help=(
                "Divide las muestras en train/test (estratificado por "
                "etiqueta, semilla fija --split-seed), calibra el umbral "
                "óptimo (barrido de F1) SOLO con train, y reporta las "
                "métricas de ese umbral fijo aplicado a test. Corrige el "
                "sobreajuste de calcular el umbral y evaluarlo sobre la "
                "misma muestra."
            ),
        )
        parser.add_argument(
            "--split-seed",
            type=int,
            default=42,
            help="Semilla del split train/test (reproducibilidad).",
        )
        parser.add_argument(
            "--train-fraction",
            type=float,
            default=0.7,
            help="Fracción de cada grupo (IA/humano) que va a train.",
        )

    def handle(self, *args, **options) -> None:
        run_started = time.monotonic()
        ai_dir = Path(options["ai_dir"])
        human_dir = Path(options["human_dir"])
        heuristic_threshold = Decimal(options["threshold"])
        perplexity_threshold_override = (
            Decimal(options["perplexity_threshold"])
            if options["perplexity_threshold"] is not None
            else None
        )
        csv_path = Path(options["csv_path"])
        engine = options["engine"]

        ai_samples = self._load_samples(directory=ai_dir)
        human_samples = self._load_samples(directory=human_dir)

        if not ai_samples:
            self.stdout.write(self.style.ERROR(f"No se encontraron .txt en {ai_dir}"))
            return

        if not human_samples:
            self.stdout.write(self.style.ERROR(f"No se encontraron .txt en {human_dir}"))
            return

        engine_results: dict[str, list[SampleResult]] = {}
        engine_thresholds: dict[str, Decimal] = {}

        if engine in ("heuristic", "both"):
            self.stdout.write(self.style.MIGRATE_HEADING("\n### Motor: heurístico (SpanishAIDetector) ###"))
            results = self._run_heuristic(ai_samples, human_samples)
            engine_results["heuristic"] = results
            engine_thresholds["heuristic"] = heuristic_threshold
            self._report_engine(results, threshold=heuristic_threshold, sweep=options["sweep"])

            if options["split_eval"]:
                self._report_split_eval(
                    results,
                    seed=options["split_seed"],
                    train_fraction=options["train_fraction"],
                )

        if engine in ("perplexity", "both"):
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    "\n### Motor: perplejidad + burstiness (SpanishPerplexityDetector) ###"
                )
            )
            self.stdout.write(
                "Instanciado directamente en este script, sin pasar por "
                "PERPLEXITY_AI_DETECTION_ENABLED (motor evaluado en aislamiento)."
            )
            results = self._run_perplexity(ai_samples, human_samples)
            engine_results["perplexity"] = results

            if perplexity_threshold_override is not None:
                perplexity_threshold = perplexity_threshold_override
                self.stdout.write(
                    f"\nUmbral de perplejidad indicado explícitamente: {perplexity_threshold}"
                )
            else:
                perplexity_threshold = self._derive_midpoint_threshold(results)
                ai_mean = statistics.mean(
                    float(r.probability) for r in results if r.label == 1
                )
                human_mean = statistics.mean(
                    float(r.probability) for r in results if r.label == 0
                )
                self.stdout.write(
                    "\nUmbral de perplejidad calculado dinámicamente: "
                    f"{perplexity_threshold} (punto medio entre la media IA "
                    f"observada={ai_mean:.2f} y la media humana observada="
                    f"{human_mean:.2f} en esta corrida; no hay un umbral "
                    "calibrado de producción para este motor todavía)."
                )

            engine_thresholds["perplexity"] = perplexity_threshold
            self._report_engine(results, threshold=perplexity_threshold, sweep=options["sweep"])

            if options["split_eval"]:
                self._report_split_eval(
                    results,
                    seed=options["split_seed"],
                    train_fraction=options["train_fraction"],
                )

        self._write_csv(csv_path=csv_path, engine_results=engine_results, engine_thresholds=engine_thresholds)

        if engine == "both":
            self._print_comparison(engine_results, engine_thresholds)

        self.stdout.write(self.style.SUCCESS(f"\nDetalle por muestra guardado en: {csv_path}"))

        total_elapsed = time.monotonic() - run_started
        minutes, seconds = divmod(total_elapsed, 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"Tiempo total de la corrida: {int(minutes)}m {seconds:.1f}s "
                f"({total_elapsed:.1f}s, {len(ai_samples)} IA + {len(human_samples)} humanas)"
            )
        )

    # --- Carga y ejecución por motor ---

    def _load_samples(self, directory: Path) -> list[tuple[str, str]]:
        if not directory.exists():
            return []

        samples = []
        for path in sorted(directory.glob("*.txt")):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                samples.append((path.name, text))
        return samples

    def _run_heuristic(
        self,
        ai_samples: list[tuple[str, str]],
        human_samples: list[tuple[str, str]],
    ) -> list[SampleResult]:
        detector = SpanishAIDetector()
        results: list[SampleResult] = []

        for filename, text in ai_samples:
            results.append(self._evaluate_heuristic_sample(detector, filename, text, label=1))
        for filename, text in human_samples:
            results.append(self._evaluate_heuristic_sample(detector, filename, text, label=0))

        return results

    def _evaluate_heuristic_sample(
        self,
        detector: SpanishAIDetector,
        filename: str,
        text: str,
        label: int,
    ) -> SampleResult:
        result = detector.analyze(content=text)
        return SampleResult(
            filename=filename,
            label=label,
            probability=result.ai_probability_percent,
            detail=f"num_findings={len(result.findings)}",
        )

    def _run_perplexity(
        self,
        ai_samples: list[tuple[str, str]],
        human_samples: list[tuple[str, str]],
    ) -> list[SampleResult]:
        detector = SpanishPerplexityDetector()
        results: list[SampleResult] = []

        total = len(ai_samples) + len(human_samples)
        done = 0

        for filename, text in ai_samples:
            done += 1
            results.append(
                self._evaluate_perplexity_sample(detector, filename, text, label=1, index=done, total=total)
            )
        for filename, text in human_samples:
            done += 1
            results.append(
                self._evaluate_perplexity_sample(detector, filename, text, label=0, index=done, total=total)
            )

        return results

    def _evaluate_perplexity_sample(
        self,
        detector: SpanishPerplexityDetector,
        filename: str,
        text: str,
        label: int,
        index: int,
        total: int,
    ) -> SampleResult:
        start = time.monotonic()
        result = detector.analyze(content=text)
        elapsed = time.monotonic() - start

        self.stdout.write(
            f"[{index}/{total}] {filename}: score={result.ai_probability_percent} "
            f"perplexity={result.mean_perplexity} burstiness={result.burstiness} "
            f"({elapsed:.1f}s)"
        )

        return SampleResult(
            filename=filename,
            label=label,
            probability=result.ai_probability_percent,
            detail=(
                f"mean_perplexity={result.mean_perplexity} "
                f"burstiness={result.burstiness} "
                f"sentence_count={result.sentence_count}"
            ),
        )

    def _derive_midpoint_threshold(self, results: list[SampleResult]) -> Decimal:
        ai_scores = [float(r.probability) for r in results if r.label == 1]
        human_scores = [float(r.probability) for r in results if r.label == 0]
        midpoint = (statistics.mean(ai_scores) + statistics.mean(human_scores)) / 2
        return Decimal(str(round(midpoint, 2)))

    # --- Métricas compartidas entre motores ---

    def _report_engine(
        self,
        results: list[SampleResult],
        threshold: Decimal,
        sweep: bool,
    ) -> None:
        self._print_group_summary(results)
        self._print_confusion_and_metrics(results, threshold=threshold)
        self._print_rank_statistic(results)

        if sweep:
            self._print_threshold_sweep(results)

    def _print_group_summary(self, results: list[SampleResult]) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Score promedio por grupo ==="))

        for label, name in ((1, "IA"), (0, "Humano")):
            scores = [float(item.probability) for item in results if item.label == label]
            if not scores:
                continue

            self.stdout.write(
                f"{name:7s} n={len(scores):2d} "
                f"media={statistics.mean(scores):6.2f}  "
                f"mediana={statistics.median(scores):6.2f}  "
                f"min={min(scores):6.2f}  max={max(scores):6.2f}  "
                f"stdev={statistics.pstdev(scores):6.2f}"
            )

    def _print_confusion_and_metrics(
        self,
        results: list[SampleResult],
        threshold: Decimal,
    ) -> None:
        metrics = self._compute_metrics(results, threshold)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n=== Matriz de confusión (umbral={threshold}) ==="
            )
        )
        self.stdout.write(
            f"                 Predicho IA   Predicho humano\n"
            f"Real IA          {metrics['tp']:>10d}   {metrics['fn']:>15d}\n"
            f"Real humano      {metrics['fp']:>10d}   {metrics['tn']:>15d}"
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Métricas ==="))
        self.stdout.write(f"Precisión (precision): {metrics['precision']:.3f}")
        self.stdout.write(f"Sensibilidad (recall):  {metrics['recall']:.3f}")
        self.stdout.write(f"F1-score:               {metrics['f1']:.3f}")
        self.stdout.write(f"Exactitud (accuracy):   {metrics['accuracy']:.3f}")

    def _compute_metrics(
        self,
        results: list[SampleResult],
        threshold: Decimal,
    ) -> dict:
        tp = fp = tn = fn = 0

        for item in results:
            predicted = item.predicted(threshold)
            if item.label == 1 and predicted == 1:
                tp += 1
            elif item.label == 1 and predicted == 0:
                fn += 1
            elif item.label == 0 and predicted == 1:
                fp += 1
            else:
                tn += 1

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )
        accuracy = (tp + tn) / len(results) if results else 0.0

        return {
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "accuracy": accuracy,
        }

    def _rank_statistic(self, results: list[SampleResult]) -> float:
        """
        P(score_IA > score_humano) sobre todos los pares (muestra IA, muestra
        humana), con 0.5 de crédito en empates. Equivalente al estadístico de
        Mann-Whitney U normalizado / AUC: 0.5 = el motor no discrimina mejor
        que el azar, 1.0 = separación perfecta, <0.5 = el motor tiende a
        puntuar más alto a los textos humanos que a los de IA (peor que azar).
        """
        ai_scores = [float(r.probability) for r in results if r.label == 1]
        human_scores = [float(r.probability) for r in results if r.label == 0]

        if not ai_scores or not human_scores:
            return 0.0

        total_pairs = 0
        favorable = 0.0

        for ai_score in ai_scores:
            for human_score in human_scores:
                total_pairs += 1
                if ai_score > human_score:
                    favorable += 1.0
                elif ai_score == human_score:
                    favorable += 0.5

        return favorable / total_pairs if total_pairs else 0.0

    def _print_rank_statistic(self, results: list[SampleResult]) -> None:
        auc_like = self._rank_statistic(results)
        self.stdout.write(
            self.style.MIGRATE_HEADING("\n=== Estadístico rank-based (AUC-like) ===")
        )
        self.stdout.write(f"P(score_IA > score_humano) = {auc_like:.3f}  (0.5 = azar)")

    def _print_threshold_sweep(self, results: list[SampleResult]) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n=== Barrido de umbrales (solo diagnóstico) ==="
            )
        )
        self.stdout.write(
            f"{'umbral':>7s}  {'precision':>9s}  {'recall':>7s}  {'f1':>6s}  {'accuracy':>8s}"
        )

        best_threshold = None
        best_f1 = -1.0

        for threshold in SWEEP_THRESHOLDS:
            metrics = self._compute_metrics(results, threshold)
            self.stdout.write(
                f"{float(threshold):7.1f}  {metrics['precision']:9.3f}  "
                f"{metrics['recall']:7.3f}  {metrics['f1']:6.3f}  {metrics['accuracy']:8.3f}"
            )
            if metrics["f1"] > best_f1:
                best_f1 = metrics["f1"]
                best_threshold = threshold

        self.stdout.write(
            self.style.WARNING(
                f"\nMejor F1 en el barrido: umbral={best_threshold} -> F1={best_f1:.3f} "
                f"(solo diagnóstico, no cambia nada en producción)"
            )
        )

    # --- Split train/test (mitiga sobreajuste del umbral) ---

    def _stratified_split(
        self,
        results: list[SampleResult],
        seed: int,
        train_fraction: float,
    ) -> tuple[list[SampleResult], list[SampleResult]]:
        """
        Split estratificado por etiqueta: cada grupo (IA/humano) se baraja de
        forma independiente con `seed` y se corta al mismo `train_fraction`,
        para que train y test conserven proporciones similares de cada clase
        (evita, por ejemplo, que test quede con muy pocas muestras IA por
        azar). Determinístico dado el mismo seed.
        """
        rng = random.Random(seed)
        train: list[SampleResult] = []
        test: list[SampleResult] = []

        for label in (1, 0):
            group = [item for item in results if item.label == label]
            shuffled = group[:]
            rng.shuffle(shuffled)
            cut = round(len(shuffled) * train_fraction)
            train.extend(shuffled[:cut])
            test.extend(shuffled[cut:])

        return train, test

    def _best_f1_threshold(self, results: list[SampleResult]) -> tuple[Decimal, float]:
        best_threshold = SWEEP_THRESHOLDS[0]
        best_f1 = -1.0

        for threshold in SWEEP_THRESHOLDS:
            f1 = self._compute_metrics(results, threshold)["f1"]
            if f1 > best_f1:
                best_f1 = f1
                best_threshold = threshold

        return best_threshold, best_f1

    def _report_split_eval(
        self,
        results: list[SampleResult],
        seed: int,
        train_fraction: float,
    ) -> None:
        train, test = self._stratified_split(results, seed=seed, train_fraction=train_fraction)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n=== Split train/test (seed={seed}, train={train_fraction:.0%}) ==="
            )
        )
        train_ai = sum(1 for r in train if r.label == 1)
        train_human = sum(1 for r in train if r.label == 0)
        test_ai = sum(1 for r in test if r.label == 1)
        test_human = sum(1 for r in test if r.label == 0)
        self.stdout.write(
            f"Train: n={len(train)} ({train_ai} IA / {train_human} humanas)   "
            f"Test: n={len(test)} ({test_ai} IA / {test_human} humanas)"
        )

        threshold, train_best_f1 = self._best_f1_threshold(train)
        self.stdout.write(
            f"Umbral óptimo calibrado SOLO en train (barrido F1): {threshold} "
            f"(F1 train en ese punto = {train_best_f1:.3f})"
        )

        train_metrics = self._compute_metrics(train, threshold)
        test_metrics = self._compute_metrics(test, threshold)
        train_auc = self._rank_statistic(train)
        test_auc = self._rank_statistic(test)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n=== Train vs. test con umbral fijo ({threshold}) ==="
            )
        )
        header = f"{'set':6s}  {'n':>4s}  {'AUC-like':>8s}  {'precision':>9s}  {'recall':>7s}  {'f1':>6s}  {'accuracy':>8s}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        self.stdout.write(
            f"{'train':6s}  {len(train):4d}  {train_auc:8.3f}  "
            f"{train_metrics['precision']:9.3f}  {train_metrics['recall']:7.3f}  "
            f"{train_metrics['f1']:6.3f}  {train_metrics['accuracy']:8.3f}"
        )
        self.stdout.write(
            f"{'test':6s}  {len(test):4d}  {test_auc:8.3f}  "
            f"{test_metrics['precision']:9.3f}  {test_metrics['recall']:7.3f}  "
            f"{test_metrics['f1']:6.3f}  {test_metrics['accuracy']:8.3f}"
        )

        f1_drop = train_metrics["f1"] - test_metrics["f1"]
        auc_drop = train_auc - test_auc
        if f1_drop > 0.10 or auc_drop > 0.10:
            self.stdout.write(
                self.style.WARNING(
                    f"\nCaída train->test: ΔF1={f1_drop:+.3f}  ΔAUC-like={auc_drop:+.3f} "
                    "— señal de sobreajuste del umbral/motor a la muestra chica original."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nCaída train->test: ΔF1={f1_drop:+.3f}  ΔAUC-like={auc_drop:+.3f} "
                    "— sin caída grande, la separación se sostiene en datos no vistos."
                )
            )

    # --- Comparación final y CSV ---

    def _print_comparison(
        self,
        engine_results: dict[str, list[SampleResult]],
        engine_thresholds: dict[str, Decimal],
    ) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING("\n\n### Comparación heurístico vs. perplejidad ###")
        )

        header = f"{'motor':12s}  {'umbral':>7s}  {'AUC-like':>8s}  {'precision':>9s}  {'recall':>7s}  {'f1':>6s}  {'accuracy':>8s}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for name in ("heuristic", "perplexity"):
            if name not in engine_results:
                continue
            results = engine_results[name]
            threshold = engine_thresholds[name]
            metrics = self._compute_metrics(results, threshold)
            auc_like = self._rank_statistic(results)
            self.stdout.write(
                f"{name:12s}  {float(threshold):7.2f}  {auc_like:8.3f}  "
                f"{metrics['precision']:9.3f}  {metrics['recall']:7.3f}  "
                f"{metrics['f1']:6.3f}  {metrics['accuracy']:8.3f}"
            )

    def _write_csv(
        self,
        csv_path: Path,
        engine_results: dict[str, list[SampleResult]],
        engine_thresholds: dict[str, Decimal],
    ) -> None:
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "motor",
                    "archivo",
                    "etiqueta",
                    "ai_probability_percent",
                    "prediccion",
                    "acierto",
                    "detalle",
                ]
            )
            for engine_name, results in engine_results.items():
                threshold = engine_thresholds[engine_name]
                for item in results:
                    label_name = "IA" if item.label == 1 else "humano"
                    predicted = item.predicted(threshold)
                    predicted_name = "IA" if predicted == 1 else "humano"
                    writer.writerow(
                        [
                            engine_name,
                            item.filename,
                            label_name,
                            item.probability,
                            predicted_name,
                            predicted == item.label,
                            item.detail,
                        ]
                    )
