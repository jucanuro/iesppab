from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.analysis.engines import ai_segments
from apps.analysis.engines.ai_segments import SegmentedPerplexityDetector
from apps.analysis.engines.perplexity_detector import SpanishPerplexityDetector
from apps.analysis.engines.text_filters import AcademicTextFilter

EVALUATION_DIR = Path(__file__).resolve().parent.parent.parent / "evaluation"
FIXTURES_DIR = EVALUATION_DIR / "fixtures"
BANK_AI_DIR = FIXTURES_DIR / "ai_samples"
BANK_HUMAN_DIR = FIXTURES_DIR / "human_samples"
SEGMENTS_AI_DIR = FIXTURES_DIR / "segments" / "ia"
SEGMENTS_HUMAN_DIR = FIXTURES_DIR / "segments" / "humano"
DEFAULT_CSV_PATH = EVALUATION_DIR / "ai_detection_modes_results.csv"

THRESHOLD = Decimal("50.00")
DEFAULT_MIDPOINTS = "55,70,80,90,100"

LABEL_AI = "IA"
LABEL_HUMAN = "humano"

GROUP_BANK = "banco"
GROUP_DOCUMENT = "documento"


@dataclass
class Sample:
    name: str
    label: str
    group: str
    content: str
    excluded_ranges: list[tuple[int, int]]
    from_database: bool = False


@dataclass
class SampleResult:
    sample: Sample
    global_percent: Decimal
    global_seconds: float
    segments_result: ai_segments.SegmentedAIAnalysisResult
    segments_seconds: float

    @property
    def segments_percent(self) -> Decimal:
        return self.segments_result.ai_probability_percent

    def segments_percent_at(self, midpoint: float) -> Decimal:
        return ai_segments.aggregate_ai_percent(
            self.segments_result.segments,
            midpoint=midpoint,
        )


class Command(BaseCommand):
    help = (
        "Compara la detección de IA por perplejidad en modo 'global' (una "
        "perplejidad para los primeros 900 tokens) y 'segments' (todo el "
        "documento por segmentos) sobre el banco de pruebas, la tesis "
        "indicada con --document-title y los fixtures de segments/{ia,humano}. "
        "Incluye un barrido de PERPLEXITY_MIDPOINT solo para 'segments'. No "
        "modifica nada en producción."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--document-title",
            action="append",
            default=None,
            help=(
                "Título (parcial) de un documento de la base cuyo último reporte "
                "se evalúa como IA. Repetible. Default: 'Mantilla'."
            ),
        )
        parser.add_argument(
            "--skip-bank",
            action="store_true",
            help="No evaluar el banco de párrafos cortos (ai_samples/human_samples).",
        )
        parser.add_argument(
            "--bank-limit",
            type=int,
            default=None,
            help="Máximo de muestras por etiqueta del banco (para corridas rápidas).",
        )
        parser.add_argument(
            "--midpoints",
            type=str,
            default=DEFAULT_MIDPOINTS,
            help="Valores de MIDPOINT a barrer en modo segments, separados por coma.",
        )
        parser.add_argument(
            "--csv-path",
            type=str,
            default=str(DEFAULT_CSV_PATH),
            help="Ruta del CSV de detalle a generar.",
        )

    def handle(self, *args, **options) -> None:
        midpoints = [float(value) for value in options["midpoints"].split(",")]
        titles = options["document_title"] or ["Mantilla"]

        samples = self._document_samples(titles)
        samples += self._dir_samples(SEGMENTS_AI_DIR, LABEL_AI, GROUP_DOCUMENT)
        samples += self._dir_samples(SEGMENTS_HUMAN_DIR, LABEL_HUMAN, GROUP_DOCUMENT)

        if not options["skip_bank"]:
            limit = options["bank_limit"]
            samples += self._dir_samples(BANK_AI_DIR, LABEL_AI, GROUP_BANK)[:limit]
            samples += self._dir_samples(BANK_HUMAN_DIR, LABEL_HUMAN, GROUP_BANK)[:limit]

        global_detector = SpanishPerplexityDetector()
        segments_detector = SegmentedPerplexityDetector()
        results = []

        for index, sample in enumerate(samples, start=1):
            started = time.monotonic()
            global_result = global_detector.analyze(sample.content)
            global_seconds = time.monotonic() - started

            started = time.monotonic()
            segments_result = segments_detector.analyze(
                sample.content,
                excluded_ranges=sample.excluded_ranges,
            )
            segments_seconds = time.monotonic() - started

            results.append(
                SampleResult(
                    sample=sample,
                    global_percent=global_result.ai_probability_percent,
                    global_seconds=global_seconds,
                    segments_result=segments_result,
                    segments_seconds=segments_seconds,
                )
            )

            if sample.group == GROUP_DOCUMENT or index % 25 == 0:
                self.stderr.write(f"[{index}/{len(samples)}] {sample.name}")

        documents = [r for r in results if r.sample.group == GROUP_DOCUMENT]
        bank = [r for r in results if r.sample.group == GROUP_BANK]

        self._print_documents_table(documents)
        self._print_metrics("Documentos", documents)
        if bank:
            self._print_metrics("Banco de párrafos", bank)
            self._print_metrics("Todo", results)
        self._print_sweep(documents, bank, midpoints)
        self._write_csv(Path(options["csv_path"]), results)

    # --- Muestras ---

    def _document_samples(self, titles: list[str]) -> list[Sample]:
        from apps.documents.models import Document
        from apps.reports.models import AnalysisReport

        text_filter = AcademicTextFilter()
        samples = []

        for title in titles:
            document = (
                Document.objects.filter(title__icontains=title)
                .order_by("-created_at")
                .values("id", "title")
                .first()
            )
            if document is None:
                raise CommandError(f"No se encontró un documento con título '{title}'.")

            # `.values()` en vez del modelo: así el comando funciona aunque la
            # base local tenga migraciones de reports sin aplicar.
            report = (
                AnalysisReport.objects.filter(document_id=document["id"])
                .order_by("-created_at")
                .values("analyzed_content")
                .first()
            )
            if not report or not report["analyzed_content"]:
                raise CommandError(
                    f"El documento '{document['title']}' no tiene texto analizado."
                )

            content = report["analyzed_content"]
            samples.append(
                Sample(
                    name=document["title"],
                    label=LABEL_AI,
                    group=GROUP_DOCUMENT,
                    content=content,
                    excluded_ranges=text_filter.find_quoted_ranges(content),
                    from_database=True,
                )
            )

        return samples

    def _dir_samples(self, directory: Path, label: str, group: str) -> list[Sample]:
        return [
            Sample(
                name=path.name,
                label=label,
                group=group,
                content=path.read_text(encoding="utf-8"),
                excluded_ranges=[],
            )
            for path in sorted(directory.glob("*.txt"))
        ]

    # --- Reportes ---

    def _print_documents_table(self, results: list[SampleResult]) -> None:
        self.stdout.write("\n== Documentos: global vs segments (MIDPOINT actual) ==")
        header = (
            f"{'documento':<26} {'esperado':<8} {'palabras':>8} {'% global':>9} "
            f"{'% segm.':>8} {'evaluables':>11} {'t global':>9} {'t segm.':>8}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for result in results:
            segments = result.segments_result
            evaluable = f"{len(segments.evaluable_segments)}/{len(segments.segments)}"
            self.stdout.write(
                f"{result.sample.name[:26]:<26} {result.sample.label:<8} "
                f"{len(result.sample.content.split()):>8} "
                f"{result.global_percent:>9} {result.segments_percent:>8} "
                f"{evaluable:>11} {result.global_seconds:>8.1f}s "
                f"{result.segments_seconds:>7.1f}s"
            )

    def _print_metrics(self, title: str, results: list[SampleResult]) -> None:
        self.stdout.write(f"\n== Precisión/recall con umbral {THRESHOLD}: {title} (n={len(results)}) ==")

        for mode, percent_of in (
            ("global", lambda r: r.global_percent),
            ("segments", lambda r: r.segments_percent),
        ):
            precision, recall, false_positive_rate = _metrics(results, percent_of)
            self.stdout.write(
                f"{mode:<9} precisión={_fmt(precision)} recall={_fmt(recall)} "
                f"falsos_positivos_humanos={_fmt(false_positive_rate)}"
            )

        total_global = sum(r.global_seconds for r in results)
        total_segments = sum(r.segments_seconds for r in results)
        self.stdout.write(
            f"tiempo total: global={total_global:.1f}s segments={total_segments:.1f}s"
        )

    def _print_sweep(
        self,
        documents: list[SampleResult],
        bank: list[SampleResult],
        midpoints: list[float],
    ) -> None:
        theses = [r for r in documents if r.sample.from_database]
        fixtures = [r for r in documents if not r.sample.from_database]
        ai_texts = [r for r in fixtures if r.sample.label == LABEL_AI]
        humans = [r for r in fixtures if r.sample.label == LABEL_HUMAN]

        self.stdout.write("\n== Barrido de MIDPOINT (solo segments, umbral de segmento 50) ==")
        columns = (
            [r.sample.name[:12] for r in theses]
            + [r.sample.name.removesuffix(".txt") for r in ai_texts]
            + [r.sample.name.removesuffix(".txt").replace("humano_tesis", "hum") for r in humans]
            + ["FP hum(3)"]
        )
        if bank:
            columns += ["FP banco", "recall banco"]

        self.stdout.write(f"{'MIDPOINT':>8} " + " ".join(f"{c:>12}" for c in columns))

        for midpoint in midpoints:
            values = [r.segments_percent_at(midpoint) for r in theses + ai_texts + humans]
            cells = [f"{value:>12}" for value in values]
            human_fp = sum(1 for r in humans if r.segments_percent_at(midpoint) >= THRESHOLD)
            cells.append(f"{human_fp}/{len(humans)}".rjust(12))

            if bank:
                _, recall, fpr = _metrics(bank, lambda r: r.segments_percent_at(midpoint))
                cells += [f"{_fmt(fpr):>12}", f"{_fmt(recall):>12}"]

            self.stdout.write(f"{midpoint:>8.0f} " + " ".join(cells))

    def _write_csv(self, path: Path, results: list[SampleResult]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "grupo", "documento", "esperado", "palabras", "pct_global",
                    "pct_segments", "segmentos", "segmentos_evaluables",
                    "seg_global", "seg_segments", "ppl_segmentos",
                ]
            )
            for result in results:
                segments = result.segments_result
                writer.writerow(
                    [
                        result.sample.group,
                        result.sample.name,
                        result.sample.label,
                        len(result.sample.content.split()),
                        result.global_percent,
                        result.segments_percent,
                        len(segments.segments),
                        len(segments.evaluable_segments),
                        f"{result.global_seconds:.2f}",
                        f"{result.segments_seconds:.2f}",
                        " ".join(
                            f"{s.perplexity:.1f}" if s.perplexity else s.excluded_reason
                            for s in segments.segments
                        ),
                    ]
                )

        self.stdout.write(f"\nDetalle por muestra: {path}")


def _metrics(results, percent_of) -> tuple[float | None, float | None, float | None]:
    true_positive = false_positive = false_negative = negatives = 0

    for result in results:
        predicted_ai = percent_of(result) >= THRESHOLD
        is_ai = result.sample.label == LABEL_AI

        if is_ai and predicted_ai:
            true_positive += 1
        elif is_ai:
            false_negative += 1
        else:
            negatives += 1
            if predicted_ai:
                false_positive += 1

    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else None
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else None
    )
    false_positive_rate = false_positive / negatives if negatives else None

    return precision, recall, false_positive_rate


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"
