"""
Evalúa el clasificador de IA sobre tesis completas de `eval_ia/` (fuera de
git): % IA por documento, RAM pico y tiempo, y los trozos con su
probabilidad en un JSON para revisarlos a mano.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.analysis.engines.ai_classifier import SegmentAIClassifier
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.management.commands.exportar_dataset_ia import _file_document
from apps.documents.extractors import DocumentTextExtractor

EVAL_DIR = Path(__file__).resolve().parents[4] / "eval_ia"
MANTILLA_MARK = "Mantilla"


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _peak_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _reset_peak() -> None:
    try:
        Path("/proc/self/clear_refs").write_text("5")
    except OSError:
        pass


class Command(BaseCommand):
    help = "Evalúa el clasificador de IA sobre eval_ia/ (Mantilla, instituto, control)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--json", default="eval_ia/resultados_clasificador.json")

    def handle(self, *args, **options) -> None:
        groups = {
            "instituto": sorted((EVAL_DIR / "humanos").glob("*.docx")),
            "control UPeU pre-2022": sorted((EVAL_DIR / "humanos_pre2022").glob("*.pdf")),
            "IA (eval_ia/ia)": sorted((EVAL_DIR / "ia").glob("*.docx")),
        }
        if not any(groups.values()):
            raise CommandError("No hay archivos en eval_ia/.")

        extractor = DocumentTextExtractor()
        text_filter = AcademicTextFilter()
        classifier = SegmentAIClassifier()
        classifier.threshold  # carga el modelo antes de medir

        base_rss = _rss_mb()
        self.stdout.write(f"RSS con el modelo cargado: {base_rss:.0f} MB")
        rows = []

        for group, paths in groups.items():
            for path in paths:
                try:
                    raw = extractor.extract(_file_document(path)).content
                except Exception as exc:  # docx corruptos
                    self.stderr.write(f"Se omite '{path.name}': {exc}")
                    continue

                content = text_filter.filter_for_similarity(content=raw).content or raw
                _reset_peak()
                started = time.monotonic()
                result = classifier.analyze(
                    content, excluded_ranges=text_filter.find_quoted_ranges(content)
                )
                seconds = time.monotonic() - started
                rows.append(
                    {
                        "group": group,
                        "mantilla": MANTILLA_MARK.lower() in path.name.lower(),
                        "name": path.name,
                        "ai_percent": float(result.ai_percent),
                        "words": result.evaluated_words,
                        "pieces": len(result.pieces),
                        "seconds": round(seconds, 1),
                        "peak_mb": round(_peak_mb()),
                        "pieces_detail": [
                            {"text": p.text, "probability": p.probability}
                            for p in result.pieces
                        ],
                    }
                )
                self.stdout.write(
                    f"{group[:10]:10} {path.name[:50]:50} ia={result.ai_percent:>6} "
                    f"trozos={len(result.pieces):>3} {seconds:6.1f}s pico={rows[-1]['peak_mb']}MB"
                )

        Path(options["json"]).write_text(
            json.dumps({"threshold": classifier.threshold, "rows": rows}, ensure_ascii=False),
            encoding="utf-8",
        )
        self._summary(rows)

    def _summary(self, rows: list[dict]) -> None:
        self.stdout.write("\ngrupo | n | mediana %IA | docs>=20% | mediana s/tesis | s/1000 pal | pico MB")
        sets = [(g, [r for r in rows if r["group"] == g]) for g in dict.fromkeys(r["group"] for r in rows)]
        sets.append(("Mantilla", [r for r in rows if r["mantilla"]]))
        for name, subset in sets:
            if not subset:
                continue
            words = sum(r["words"] for r in subset) or 1
            self.stdout.write(
                f"{name} | {len(subset)} | {statistics.median(r['ai_percent'] for r in subset):.1f} | "
                f"{sum(r['ai_percent'] >= 20 for r in subset)}/{len(subset)} | "
                f"{statistics.median(r['seconds'] for r in subset):.1f} | "
                f"{1000 * sum(r['seconds'] for r in subset) / words:.1f} | "
                f"{max(r['peak_mb'] for r in subset)}"
            )
