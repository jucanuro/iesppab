from __future__ import annotations

import json
import random
import re
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.analysis import ia_dataset
from apps.analysis.management.commands import exportar_dataset_ia as v1
from apps.documents.extractors import DocumentTextExtractor

BASE = Path(settings.BASE_DIR)
EVAL_DIR = BASE / "eval_ia"
RAW_DIR = BASE / "dataset_v2" / "raw"
DEFAULT_OUTPUT = BASE / "dataset_v2"

MAX_HUMAN_SEGMENTS = 3500
WIKI_SEGMENTS_PER_DOC = 3
WIKI_RESERVED = 40
WIKI_WINDOW_WORDS = 2000
CONTROL_SEGMENTS_PER_DOC = 15
OJS_SEGMENTS_PER_DOC = 3
RECENT_SEGMENTS_PER_DOC = 5
SPLIT_SEED = 20261110

# Particiones: "train"/"val"/"test" (UPeU <=2021, Wikipedia y OJS de entrenamiento),
# "test_wiki" (Wikipedia reservada: nunca entra al entrenamiento),
# "control" (20 tesis UPeU <=2021 fuera del corpus), "test_reciente"
# (UPeU 2022-26 y los 6 artículos de Wikipedia del v1: solo diagnóstico),
# "test_chat" (IA de chat: solo test).
HUMAN_PARTITIONS = ("train", "val", "test", "test_wiki")
_HEADING_LINE_RE = re.compile(r"^=+.*=+\s*$", re.M)


class Command(v1.Command):
    help = (
        "Exporta dataset_v2/ (JSONL, fuera de git): humanos UPeU <=2021 + Wikipedia en español "
        "(+ revistas OJS si existen), IA igual que el v1. Corre la prueba de solo formato "
        "(AUROC <= 0,65) y separa por documento."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--salida", default=str(DEFAULT_OUTPUT))
        parser.add_argument("--max-humanos", type=int, default=MAX_HUMAN_SEGMENTS)
        parser.add_argument("--wikipedia", default=str(RAW_DIR / "wikipedia_articulos.jsonl"))
        parser.add_argument("--ojs", default=str(RAW_DIR / "ojs_articulos.jsonl"))

    def handle(self, *args, **options) -> None:
        wikipedia = Path(options["wikipedia"])
        if not wikipedia.exists():
            raise CommandError(f"Falta {wikipedia}: corre antes `preparar_wikipedia_v2`.")
        out_dir = Path(options["salida"])
        out_dir.mkdir(parents=True, exist_ok=True)
        cap = options["max_humanos"]

        control_handles = self._control_handles()
        wiki_rows = self._wikipedia_rows(wikipedia)
        ojs_rows = self._ojs_rows(Path(options["ojs"]))
        control_rows = self._capped(self._control_rows(), CONTROL_SEGMENTS_PER_DOC)
        recent_rows = self._recent_rows()
        fixed = len(wiki_rows) + len(ojs_rows) + len(control_rows) + len(recent_rows)
        corpus_cap = cap - fixed
        if corpus_cap <= 0:
            raise CommandError(f"Los humanos nuevos ya suman {fixed} (> tope {cap}).")
        corpus_rows = self._corpus_rows(control_handles, corpus_cap)

        rows = corpus_rows + control_rows + wiki_rows + ojs_rows + recent_rows
        rows += self._api_rows("gemini") + self._api_rows("claude") + self._chat_rows()

        humans = [r for r in rows if r["etiqueta"] == v1.LABEL_HUMAN]
        if len(humans) > cap:
            raise CommandError(f"{len(humans)} humanos superan el tope de {cap}.")

        path = out_dir / "segmentos.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        summary = self._summary(rows)
        summary["humanos_total"] = len(humans)
        summary["tope_humanos"] = cap
        summary["prueba_formato"] = v1.format_probe(rows, HUMAN_PARTITIONS + ("train", "val"))
        (out_dir / "resumen.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        with zipfile.ZipFile(out_dir / "dataset_v2.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, "segmentos.jsonl")
            zf.write(out_dir / "resumen.json", "resumen.json")
        self._print(summary)
        self.stdout.write(f"Humanos totales: {len(humans)} (tope {cap})")

    # --- Humanos nuevos -------------------------------------------------------

    def _wikipedia_rows(self, path: Path) -> list[dict]:
        articles = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        splits = split_wikipedia(articles, WIKI_RESERVED, SPLIT_SEED)
        rng = random.Random(SPLIT_SEED)
        rows = []
        for article, split in zip(articles, splits):
            text = _window(article["texto"], WIKI_WINDOW_WORDS, rng)
            segments = ia_dataset.prose_segments(ia_dataset.generated_text_via_pdf(text))
            rng.shuffle(segments)
            rows += v1._rows(segments[:WIKI_SEGMENTS_PER_DOC], v1.LABEL_HUMAN, "humano_wikipedia",
                             "wiki_" + _slug(article["titulo"]), split, article["tematica"])
        return rows

    def _ojs_rows(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        articles = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        splits = _split_by_ratio(len(articles), v1.HUMAN_SPLITS, SPLIT_SEED)
        rng = random.Random(SPLIT_SEED)
        rows = []
        for article, split in zip(articles, splits):
            segments = ia_dataset.prose_segments(article["texto"])
            rng.shuffle(segments)
            rows += v1._rows(segments[:OJS_SEGMENTS_PER_DOC], v1.LABEL_HUMAN, "humano_ojs",
                             "ojs_" + _slug(article["titulo"]), split, article.get("tematica", ""))
        return rows

    def _capped(self, rows: list[dict], per_document: int) -> list[dict]:
        rng = random.Random(SPLIT_SEED)
        by_doc = defaultdict(list)
        for row in rows:
            by_doc[row["documento"]].append(row)
        kept = []
        for doc in sorted(by_doc):
            group = by_doc[doc]
            rng.shuffle(group)
            kept += group[:per_document]
        return kept

    def _recent_rows(self) -> list[dict]:
        """Solo diagnóstico: UPeU 2022-26 y los artículos de Wikipedia con los que se vio el FP del v1."""
        extractor = DocumentTextExtractor()
        rows = []
        for pdf in sorted((EVAL_DIR / "humanos_upeu_2022plus").glob("*.pdf")):
            raw = extractor.extract(v1._file_document(pdf)).content
            rows += v1._rows(ia_dataset.prose_segments(raw), v1.LABEL_HUMAN, "humano_reciente",
                             pdf.stem, "test_reciente", "")
        for txt in sorted((EVAL_DIR / "humanos_recientes").glob("wp_*.txt")):
            raw = _HEADING_LINE_RE.sub("", txt.read_text(encoding="utf-8"))
            rows += v1._rows(ia_dataset.prose_segments(ia_dataset.generated_text_via_pdf(raw)),
                             v1.LABEL_HUMAN, "wikipedia_v1", txt.stem, "test_reciente", "")
        return self._capped(rows, RECENT_SEGMENTS_PER_DOC)


def split_wikipedia(articles: list[dict], reserved: int, seed: int) -> list[str]:
    """'test_wiki' para `reserved` artículos (estratificado por temática); el resto, 'train'."""
    rng = random.Random(seed)
    by_theme = defaultdict(list)
    for index, article in enumerate(articles):
        by_theme[article["tematica"]].append(index)
    reserved_idx: list[int] = []
    for theme in sorted(by_theme):
        indexes = by_theme[theme][:]
        rng.shuffle(indexes)
        take = max(1, round(reserved * len(indexes) / max(len(articles), 1)))
        reserved_idx += indexes[:take]
    rng.shuffle(reserved_idx)
    chosen = set(reserved_idx[:reserved])
    return ["test_wiki" if i in chosen else "train" for i in range(len(articles))]


def _split_by_ratio(count: int, splits, seed: int) -> list[str]:
    ids = list(range(count))
    random.Random(seed).shuffle(ids)
    assigned = v1._assign_splits([str(i) for i in ids], splits)
    return [assigned[str(i)] for i in range(count)]


def _window(text: str, max_words: int, rng: random.Random) -> str:
    """Ventana contigua de párrafos de ~max_words (para no renderizar artículos enormes)."""
    paragraphs = text.split("\n\n")
    if len(text.split()) <= max_words:
        return text
    start = rng.randrange(len(paragraphs))
    window, words = [], 0
    for paragraph in paragraphs[start:] + paragraphs[:start]:
        window.append(paragraph)
        words += len(paragraph.split())
        if words >= max_words:
            break
    return "\n\n".join(window)


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_value.lower()).strip("_")[:60]
