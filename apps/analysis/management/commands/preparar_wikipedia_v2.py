from __future__ import annotations

import bz2
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.analysis import wikipedia_corpus

BASE = Path(settings.BASE_DIR)
RAW_DIR = BASE / "dataset_v2" / "raw"
DEFAULT_DUMP = RAW_DIR / "eswiki-latest-pages-articles.xml.bz2"
DEFAULT_OUTPUT = RAW_DIR / "wikipedia_articulos.jsonl"
SELECTION_SEED = 20261110


class Command(BaseCommand):
    help = (
        "Selecciona artículos largos de Wikipedia en español (educación, pedagogía, "
        "psicología, ciencias sociales, historia y ciencia) desde el dump oficial de "
        "Wikimedia y guarda su prosa en dataset_v2/raw/ (fuera de git). Primero baja el "
        "dump con --descargar (una sola conexión; el robots.txt prohíbe /w/api.php)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dump", default=str(DEFAULT_DUMP))
        parser.add_argument("--salida", default=str(DEFAULT_OUTPUT))
        parser.add_argument("--articulos", type=int, default=340, help="entrenamiento + validación")
        parser.add_argument("--por-tematica", type=int, default=140, help="tamaño del reservorio")
        parser.add_argument("--descargar", action="store_true", help="baja el dump (reanuda si está a medias)")
        parser.add_argument("--excluir-titulos-de", default=str(BASE / "eval_ia" / "humanos_recientes"),
                            help="carpeta con wp_*.txt de evaluación que no deben entrar al dataset")

    def handle(self, *args, **options) -> None:
        dump = Path(options["dump"])
        if options["descargar"]:
            self._download(dump)
        if not dump.exists():
            raise CommandError(f"No existe el dump {dump}. Usa --descargar.")

        excluded = self._excluded_titles(Path(options["excluir_titulos_de"]))
        self.stdout.write(f"Títulos excluidos (evaluación): {len(excluded)}")

        def progress(count, reservoir):
            seen = dict(reservoir.seen)
            self.stdout.write(f"  {count:,} páginas leídas; candidatos por temática: {seen}")

        with bz2.BZ2File(dump) as handle:
            reservoir = wikipedia_corpus.scan_dump(
                handle, size_per_theme=options["por_tematica"], seed=SELECTION_SEED,
                exclude_titles=excluded, progress=progress,
            )
        articles = wikipedia_corpus.select_balanced(
            reservoir, options["articulos"], SELECTION_SEED
        )
        out = Path(options["salida"])
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for article in articles:
                fh.write(json.dumps(article, ensure_ascii=False) + "\n")
        by_theme: dict[str, int] = {}
        for article in articles:
            by_theme[article["tematica"]] = by_theme.get(article["tematica"], 0) + 1
        self.stdout.write(f"{len(articles)} artículos en {out}: {by_theme}")

    def _excluded_titles(self, folder: Path) -> set[str]:
        titles = set()
        if folder.exists():
            for path in folder.glob("wp_*.txt"):
                titles.add(path.stem[3:].replace("_", " "))
        return titles

    def _download(self, dump: Path) -> None:
        import requests

        dump.parent.mkdir(parents=True, exist_ok=True)
        start = dump.stat().st_size if dump.exists() else 0
        headers = {"User-Agent": wikipedia_corpus.USER_AGENT}
        if start:
            headers["Range"] = f"bytes={start}-"
        with requests.get(wikipedia_corpus.DUMP_URL, headers=headers, stream=True, timeout=60) as resp:
            if resp.status_code == 416:
                return
            resp.raise_for_status()
            mode = "ab" if resp.status_code == 206 else "wb"
            with dump.open(mode) as fh:
                for chunk in resp.iter_content(1 << 20):
                    fh.write(chunk)
