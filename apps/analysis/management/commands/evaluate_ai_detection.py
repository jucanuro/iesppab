from __future__ import annotations

import csv
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from apps.analysis.engines import ai_segments
from apps.analysis.engines import perplexity_detector as pd
from apps.analysis.engines.ai_segments import SegmentedPerplexityDetector
from apps.analysis.engines.perplexity_detector import SpanishPerplexityDetector
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.documents.extractors import DocumentTextExtractor, TextExtractionError

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

# Modo --dir: banco privado de tesis completas (eval_ia/, fuera de git).
# Un documento cuenta como "marcado IA" con >= 20 % de IA, el umbral que
# un revisor tomaría como alerta en una tesis entera (no el 50 del banco).
DIR_HUMAN_SUBDIR = "humanos"
DIR_AI_SUBDIR = "ia"
DIR_DEFAULT_MIDPOINTS = "55,70,80,90"
DIR_DOC_THRESHOLD = Decimal("20.00")
DIR_CSV_NAME = "resultados_ia.csv"
DIR_MIME_TYPES = {
    ".pdf": DocumentTextExtractor.PDF_MIME,
    ".docx": DocumentTextExtractor.DOCX_MIME,
}
SNIPPET_CHARS = 220


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
    global_mean_perplexity: float | None = None
    global_burstiness: float | None = None

    @property
    def segments_percent(self) -> Decimal:
        return self.segments_result.ai_probability_percent

    def segments_percent_at(self, midpoint: float) -> Decimal:
        return ai_segments.aggregate_ai_percent(
            self.segments_result.segments,
            midpoint=midpoint,
        )

    def global_percent_at(self, midpoint: float) -> Decimal:
        """Como `SpanishPerplexityDetector.analyze`, con otro MIDPOINT."""
        if self.global_mean_perplexity is None:
            return Decimal("0.00")

        burstiness_score = (
            pd._logistic_score(
                self.global_burstiness,
                midpoint=pd.BURSTINESS_MIDPOINT,
                scale=pd.BURSTINESS_SCALE,
            )
            if self.global_burstiness is not None
            else None
        )
        return pd._combine_scores(
            pd._logistic_score(
                self.global_mean_perplexity,
                midpoint=midpoint,
                scale=pd.PERPLEXITY_SCALE,
            ),
            burstiness_score,
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
            default=None,
            help=(
                "Ruta del CSV de detalle a generar. Default: "
                f"{DEFAULT_CSV_PATH} (o <dir>/{DIR_CSV_NAME} con --dir)."
            ),
        )
        parser.add_argument(
            "--dir",
            type=str,
            default=None,
            help=(
                "Carpeta con tesis completas (PDF/DOCX/TXT) en "
                f"{DIR_HUMAN_SUBDIR}/ (esperado humano) e {DIR_AI_SUBDIR}/ "
                "(esperado IA). Reemplaza al banco, fixtures y --document-title. "
                f"MIDPOINT por defecto: {DIR_DEFAULT_MIDPOINTS}."
            ),
        )
        parser.add_argument(
            "--top-humans",
            type=int,
            default=5,
            help="Con --dir: cuántos humanos con mayor %% IA listar para revisión.",
        )

    def handle(self, *args, **options) -> None:
        if options["dir"]:
            self._handle_dir(Path(options["dir"]), options)
            return

        midpoints = [float(value) for value in options["midpoints"].split(",")]
        titles = options["document_title"] or ["Mantilla"]

        samples = self._document_samples(titles)
        samples += self._dir_samples(SEGMENTS_AI_DIR, LABEL_AI, GROUP_DOCUMENT)
        samples += self._dir_samples(SEGMENTS_HUMAN_DIR, LABEL_HUMAN, GROUP_DOCUMENT)

        if not options["skip_bank"]:
            limit = options["bank_limit"]
            samples += self._dir_samples(BANK_AI_DIR, LABEL_AI, GROUP_BANK)[:limit]
            samples += self._dir_samples(BANK_HUMAN_DIR, LABEL_HUMAN, GROUP_BANK)[:limit]

        results = self._analyze(samples)

        documents = [r for r in results if r.sample.group == GROUP_DOCUMENT]
        bank = [r for r in results if r.sample.group == GROUP_BANK]

        self._print_documents_table(documents)
        self._print_metrics("Documentos", documents)
        if bank:
            self._print_metrics("Banco de párrafos", bank)
            self._print_metrics("Todo", results)
        self._print_sweep(documents, bank, midpoints)
        self._write_csv(Path(options["csv_path"] or DEFAULT_CSV_PATH), results)

    def _analyze(self, samples: list[Sample]) -> list[SampleResult]:
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
                    global_mean_perplexity=global_result.mean_perplexity,
                    global_burstiness=global_result.burstiness,
                )
            )

            if sample.group == GROUP_DOCUMENT or index % 25 == 0:
                self.stderr.write(f"[{index}/{len(samples)}] {sample.name}")

        return results

    # --- Modo --dir (banco privado de tesis completas) ---

    def _handle_dir(self, directory: Path, options: dict) -> None:
        human_dir = directory / DIR_HUMAN_SUBDIR
        ai_dir = directory / DIR_AI_SUBDIR
        if not human_dir.is_dir() and not ai_dir.is_dir():
            raise CommandError(
                f"'{directory}' no tiene subcarpetas {DIR_HUMAN_SUBDIR}/ ni {DIR_AI_SUBDIR}/."
            )

        midpoints_option = options["midpoints"]
        if midpoints_option == DEFAULT_MIDPOINTS:
            midpoints_option = DIR_DEFAULT_MIDPOINTS
        midpoints = [float(value) for value in midpoints_option.split(",")]

        samples = self._file_samples(human_dir, LABEL_HUMAN)
        samples += self._file_samples(ai_dir, LABEL_AI)
        if not samples:
            raise CommandError(f"No hay archivos PDF/DOCX/TXT legibles en '{directory}'.")

        results = self._analyze(samples)
        humans = [r for r in results if r.sample.label == LABEL_HUMAN]
        ai_texts = [r for r in results if r.sample.label == LABEL_AI]

        self._print_dir_summary(humans, ai_texts, midpoints)
        self._print_top_humans(humans, midpoints[0], options["top_humans"])

        csv_path = Path(options["csv_path"] or directory / DIR_CSV_NAME)
        self._write_dir_csv(csv_path, results, midpoints)

    def _file_samples(self, directory: Path, label: str) -> list[Sample]:
        """
        Mismo texto que analiza el servicio: extracción con
        `DocumentTextExtractor`, `filter_for_similarity` (sin portada ni
        bibliografía) y las citas entre comillas como rangos excluidos.
        """
        if not directory.is_dir():
            return []

        extractor = DocumentTextExtractor()
        text_filter = AcademicTextFilter()
        samples = []

        for path in sorted(directory.iterdir()):
            suffix = path.suffix.lower()
            if not path.is_file() or suffix not in {*DIR_MIME_TYPES, ".txt"}:
                continue

            try:
                if suffix == ".txt":
                    raw = path.read_text(encoding="utf-8")
                else:
                    raw = extractor.extract(_FileDocument(path)).content
            except (TextExtractionError, UnicodeDecodeError) as exc:
                self.stderr.write(f"Se omite '{path.name}': {exc}")
                continue

            content = text_filter.filter_for_similarity(content=raw).content or raw
            samples.append(
                Sample(
                    name=path.name,
                    label=label,
                    group=GROUP_DOCUMENT,
                    content=content,
                    excluded_ranges=text_filter.find_quoted_ranges(content),
                )
            )

        return samples

    def _print_dir_summary(
        self,
        humans: list[SampleResult],
        ai_texts: list[SampleResult],
        midpoints: list[float],
    ) -> None:
        self.stdout.write(
            f"\n== Resumen por MIDPOINT (documento marcado si % IA >= "
            f"{DIR_DOC_THRESHOLD}; humanos={len(humans)}, IA={len(ai_texts)}) =="
        )
        header = (
            f"{'MIDPOINT':>8} {'modo':<9} {'FP humanos':>11} {'recall IA':>10} "
            f"{'mediana hum':>12} {'mediana IA':>11}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for midpoint in midpoints:
            for mode, percent_of in (
                ("global", lambda r: r.global_percent_at(midpoint)),
                ("segments", lambda r: r.segments_percent_at(midpoint)),
            ):
                human_values = [percent_of(r) for r in humans]
                ai_values = [percent_of(r) for r in ai_texts]
                self.stdout.write(
                    f"{midpoint:>8.0f} {mode:<9} "
                    f"{_rate(human_values):>11} {_rate(ai_values):>10} "
                    f"{_median(human_values):>12} {_median(ai_values):>11}"
                )

    def _print_top_humans(
        self,
        humans: list[SampleResult],
        midpoint: float,
        limit: int,
    ) -> None:
        if not humans or limit <= 0:
            return

        ranked = sorted(
            humans,
            key=lambda r: (r.segments_percent_at(midpoint), r.global_percent_at(midpoint)),
            reverse=True,
        )[:limit]

        self.stdout.write(
            f"\n== Humanos con mayor % IA (segments, MIDPOINT {midpoint:.0f}) para "
            "revisar a mano =="
        )
        for result in ranked:
            self.stdout.write(
                f"\n{result.sample.name}: segments={result.segments_percent_at(midpoint)} "
                f"global={result.global_percent_at(midpoint)}"
            )
            evaluable = [s for s in result.segments_result.segments if s.evaluable]
            for segment in sorted(evaluable, key=lambda s: s.perplexity)[:2]:
                score = pd._logistic_score(
                    segment.perplexity, midpoint=midpoint, scale=pd.PERPLEXITY_SCALE
                )
                self.stdout.write(
                    f"  - ppl={segment.perplexity:.1f} score={score}: "
                    f"\"{_snippet(segment.evaluable_text)}\""
                )

    def _write_dir_csv(
        self,
        path: Path,
        results: list[SampleResult],
        midpoints: list[float],
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "archivo", "esperado", "palabras", "segmentos",
                    "segmentos_evaluables", "ppl_global", "seg_global",
                    "seg_segments",
                ]
                + [
                    f"pct_{mode}_{midpoint:.0f}"
                    for midpoint in midpoints
                    for mode in ("global", "segments")
                ]
            )
            for result in results:
                segments = result.segments_result
                ppl = result.global_mean_perplexity
                writer.writerow(
                    [
                        result.sample.name,
                        result.sample.label,
                        len(result.sample.content.split()),
                        len(segments.segments),
                        len(segments.evaluable_segments),
                        f"{ppl:.1f}" if ppl is not None else "",
                        f"{result.global_seconds:.2f}",
                        f"{result.segments_seconds:.2f}",
                    ]
                    + [
                        value
                        for midpoint in midpoints
                        for value in (
                            result.global_percent_at(midpoint),
                            result.segments_percent_at(midpoint),
                        )
                    ]
                )

        self.stdout.write(f"\nDetalle por archivo: {path}")

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


def _rate(values: list[Decimal]) -> str:
    """Fracción de documentos marcados (% IA >= DIR_DOC_THRESHOLD)."""
    if not values:
        return "n/a"
    flagged = sum(1 for value in values if value >= DIR_DOC_THRESHOLD)
    return f"{flagged}/{len(values)}={flagged / len(values):.2f}"


def _median(values: list[Decimal]) -> str:
    return f"{statistics.median(values):.2f}" if values else "n/a"


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS].rstrip() + "…"


class _FileDocument(SimpleNamespace):
    """
    Lo mínimo de `Document` que usa `DocumentTextExtractor.extract`, para
    extraer archivos del disco sin crear registros en la base.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            id=path.name,
            mime_type=DIR_MIME_TYPES[path.suffix.lower()],
            # Sin archivo abierto: `File.open()` lo abre por nombre.
            original_file=File(None, name=str(path)),
        )
