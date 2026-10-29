from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_search import WebCandidateCollector, WebSearchRun
from apps.documents.extractors import DocumentTextExtractor
from apps.documents.models import Document
from apps.reports.models import AnalysisReport

SEQUENCE_WORDS = 8

STRATEGY_PREVIOUS = "anterior"
STRATEGY_NEW = "nueva"
STRATEGY_BOTH = "ambas"


@dataclass
class StrategyResult:
    name: str
    phrases: list[str]
    queries: list[str]
    run: WebSearchRun | None = None
    # (secuencias compartidas, consulta, página), mayor primero.
    scored_pages: list[tuple[int, str, object]] | None = None

    def pages_with_at_least(self, sequences: int) -> int:
        return sum(1 for shared, _query, _page in self.scored_pages or [] if shared >= sequences)


class LegacyWebQueryStrategy:
    """
    Copia congelada de la estrategia de búsqueda web anterior (commit
    a37cac0): la oración más larga de cada tramo, con y sin comillas,
    luego `filetype:pdf` y `site:` de repositorios académicos. Solo existe
    para comparar contra la estrategia actual con `--estrategia anterior`.
    """

    def __init__(self, collector: WebCandidateCollector) -> None:
        self.collector = collector

    def phrases(self, content: str) -> list[str]:
        max_phrases = self.collector.max_query_phrases
        candidates = self._phrase_candidates(content)

        if not candidates or max_phrases <= 0:
            return []

        section_size = max(len(content), 1) / max_phrases
        best_by_section: dict[int, str] = {}

        for offset, phrase in candidates:
            section = min(int(offset / section_size), max_phrases - 1)
            current = best_by_section.get(section)

            if current is None or len(phrase) > len(current):
                best_by_section[section] = phrase

        return self.collector._dedupe_keep_order(
            [best_by_section[section] for section in sorted(best_by_section)]
        )

    def queries(self, phrases: list[str]) -> list[str]:
        selected_phrases = phrases[: self.collector.max_query_phrases]
        queries = [f'"{phrase}"' for phrase in selected_phrases]
        queries.extend(selected_phrases)
        queries.extend(f'"{phrase}" filetype:pdf' for phrase in selected_phrases[:3])

        for phrase in selected_phrases[:3]:
            for domain in WebCandidateCollector.ACADEMIC_DOMAINS:
                queries.append(f'"{phrase}" site:{domain}')

        return self.collector._dedupe_keep_order(queries)[: self.collector.max_queries]

    def _phrase_candidates(self, content: str) -> list[tuple[int, str]]:
        candidates: list[tuple[int, str]] = []
        paragraph_start = 0

        for separator in re.finditer(r"\n\s*\n|\Z", content):
            paragraph = content[paragraph_start : separator.start()]
            offset = paragraph_start
            paragraph_start = separator.end()

            clean = re.sub(r"\s+", " ", paragraph).strip()

            if len(clean) < 160 or self.collector._is_low_value_paragraph(clean):
                continue

            candidates.extend(
                (offset + sentence_offset, phrase)
                for sentence_offset, phrase in self._phrases_from_paragraph(paragraph)
            )

        return candidates

    def _phrases_from_paragraph(self, paragraph: str) -> list[tuple[int, str]]:
        phrases: list[tuple[int, str]] = []

        for match in re.finditer(r"\S.*?(?:[.!?](?=\s)|$)", paragraph, flags=re.DOTALL):
            clean = re.sub(r"\s+", " ", match.group(0)).strip()

            if 10 <= len(clean.split()) <= 34:
                phrases.append((match.start(), clean[:260]))

        if phrases:
            return phrases

        words = paragraph.split()

        if len(words) >= 18:
            return [(0, " ".join(words[:30])[:260])]

        return []


def normalized_words(text: str) -> list[str]:
    """
    Minúsculas, sin tildes ni puntuación: las páginas web y los PDF
    extraídos suelen diferir del documento en esos detalles.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.findall(r"\w+", without_accents)


def word_sequences(text: str, size: int = SEQUENCE_WORDS) -> set[tuple[str, ...]]:
    words = normalized_words(text)
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


class Command(BaseCommand):
    help = (
        "Diagnóstico de la búsqueda web de un documento: muestra las frases y "
        "consultas que se generan y, por cada página descargada, cuántas "
        f"secuencias de {SEQUENCE_WORDS} palabras comparte con el documento. "
        "Permite comparar la estrategia anterior con la nueva sobre el mismo "
        "texto. Sin --dry-run consume consultas reales de Brave Search."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("document_id", help="UUID del documento.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo muestra frases y consultas; no llama a Brave ni descarga páginas.",
        )
        parser.add_argument(
            "--estrategia",
            choices=[STRATEGY_NEW, STRATEGY_PREVIOUS, STRATEGY_BOTH],
            default=STRATEGY_NEW,
            help="Estrategia de consultas a probar (por defecto: nueva).",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        strategy_option = options["estrategia"]

        document = self._get_document(options["document_id"])
        content, source_label = self._analysis_content(document)

        if not content.strip():
            raise CommandError("El documento no tiene texto analizable.")

        if not dry_run and not getattr(settings, "BRAVE_SEARCH_API_KEY", ""):
            raise CommandError(
                "BRAVE_SEARCH_API_KEY no configurada. Usa --dry-run para ver solo las consultas."
            )

        self.stdout.write(f"Documento: {document.id} ({document.original_filename})")
        self.stdout.write(f"Texto: {source_label} ({len(content)} caracteres)")

        names = (
            [STRATEGY_PREVIOUS, STRATEGY_NEW]
            if strategy_option == STRATEGY_BOTH
            else [strategy_option]
        )
        document_sequences = word_sequences(content)
        results: list[StrategyResult] = []

        for name in names:
            result = self._build_strategy(name=name, content=content)

            if not dry_run:
                self._run_strategy(result=result, document_sequences=document_sequences)

            self._write_strategy(result)
            results.append(result)

        self._write_comparison(results)

    def _get_document(self, document_id: str) -> Document:
        try:
            return Document.objects.get(pk=document_id)
        except (Document.DoesNotExist, ValidationError, ValueError) as exc:
            raise CommandError(f"Documento no encontrado: {document_id}") from exc

    def _analysis_content(self, document: Document) -> tuple[str, str]:
        """
        El mismo texto que recibió la búsqueda web en el último análisis
        (`analyzed_content`); sin reporte, se extrae y filtra como lo haría
        el análisis, sin guardar nada.
        """
        report = AnalysisReport.objects.filter(document=document).first()

        if report is not None and report.analyzed_content:
            return report.analyzed_content, "texto analizado del último reporte"

        extracted = getattr(document, "extracted_text", None)
        raw_content = (
            extracted.content
            if extracted is not None
            else DocumentTextExtractor().extract(document=document).content
        )
        filtered = AcademicTextFilter().filter_for_similarity(content=raw_content)

        return filtered.content or raw_content, "texto extraído y filtrado (sin reporte)"

    def _build_strategy(self, name: str, content: str) -> StrategyResult:
        collector = WebCandidateCollector()

        if name == STRATEGY_PREVIOUS:
            legacy = LegacyWebQueryStrategy(collector)
            phrases = legacy.phrases(content)
            return StrategyResult(name=name, phrases=phrases, queries=legacy.queries(phrases))

        phrase_groups = collector._extract_search_phrases(content=content)

        return StrategyResult(
            name=name,
            phrases=[phrase for group in phrase_groups for phrase in group],
            queries=collector._build_queries(phrase_groups=phrase_groups),
        )

    def _run_strategy(
        self,
        result: StrategyResult,
        document_sequences: set[tuple[str, ...]],
    ) -> None:
        result.run = self._collector().run_queries(queries=result.queries)
        result.scored_pages = sorted(
            (
                (len(document_sequences & word_sequences(page.text)), query, page)
                for query, page in result.run.pages
            ),
            key=lambda item: -item[0],
        )

    def _collector(self) -> WebCandidateCollector:
        # Punto de extensión para los tests (Brave y descargas simulados).
        return WebCandidateCollector()

    def _write_strategy(self, result: StrategyResult) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== Estrategia {result.name} ==="))
        self.stdout.write(f"Frases ({len(result.phrases)}):")

        for index, phrase in enumerate(result.phrases, start=1):
            self.stdout.write(f"  {index:2}. [{len(phrase.split())} palabras] {phrase}")

        self.stdout.write(f"Consultas ({len(result.queries)}):")

        for index, query in enumerate(result.queries, start=1):
            self.stdout.write(f"  {index:2}. {query}")

        if result.run is None:
            return

        self.stdout.write(
            f"Páginas con contenido ({len(result.scored_pages)}), "
            f"consultas ejecutadas {result.run.executed_queries}, "
            f"páginas consultadas {result.run.fetched_pages}:"
        )

        for shared, query, page in result.scored_pages:
            self.stdout.write(f"  [{shared:4} secuencias] {page.url}")
            self.stdout.write(f"      consulta: {query}")

    def _write_comparison(self, results: list[StrategyResult]) -> None:
        names = [result.name for result in results]
        rows: list[tuple[str, list[object]]] = [
            ("Frases", [len(result.phrases) for result in results]),
            ("Consultas generadas", [len(result.queries) for result in results]),
        ]

        if all(result.run is not None for result in results):
            rows.extend(
                [
                    ("Consultas ejecutadas", [result.run.executed_queries for result in results]),
                    ("Páginas con contenido", [len(result.scored_pages) for result in results]),
                    (
                        f"Páginas con ≥1 secuencia de {SEQUENCE_WORDS}",
                        [result.pages_with_at_least(1) for result in results],
                    ),
                    (
                        f"Páginas con ≥3 secuencias de {SEQUENCE_WORDS}",
                        [result.pages_with_at_least(3) for result in results],
                    ),
                ]
            )

        label_width = max(len(label) for label, _values in rows)
        column_width = 12

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("=== Resumen ==="))
        self.stdout.write(
            " " * label_width + "".join(f"{name:>{column_width}}" for name in names)
        )

        for label, values in rows:
            self.stdout.write(
                f"{label:<{label_width}}"
                + "".join(f"{value:>{column_width}}" for value in values)
            )

        if len(results) < 2 or any(result.run is None for result in results):
            return

        # Páginas de ambas estrategias, una al lado de la otra, de mayor a
        # menor cantidad de secuencias compartidas.
        page_width = 50
        self.stdout.write("")
        self.stdout.write(
            "".join(f"{f'Páginas ({name})':<{page_width}}" for name in names).rstrip()
        )

        for index in range(max(len(result.scored_pages) for result in results)):
            cells = []

            for result in results:
                if index < len(result.scored_pages):
                    shared, _query, page = result.scored_pages[index]
                    cell = f"{shared:4} · {page.domain}"
                else:
                    cell = ""

                cells.append(f"{cell[: page_width - 2]:<{page_width}}")

            self.stdout.write("".join(cells).rstrip())
