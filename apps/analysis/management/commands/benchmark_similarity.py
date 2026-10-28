from __future__ import annotations

import csv
import random
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User, UserRole
from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.span_similarity import WORD_PATTERN, SpanSimilarityEngine
from apps.analysis.engines.web_search import WebPageContent
from apps.analysis.indexers import DocumentKnowledgeIndexer
from apps.analysis.models import DocumentFingerprint, OaiRecord
from apps.analysis.services import DocumentAnalysisService
from apps.core.models import Institution
from apps.documents.models import Document, DocumentKind, DocumentText

EVALUATION_DIR = Path(__file__).resolve().parent.parent.parent / "evaluation"
DEFAULT_SAMPLES_DIR = EVALUATION_DIR / "fixtures" / "ai_samples"
DEFAULT_CSV_PATH = EVALUATION_DIR / "similarity_benchmark_results.csv"

# (motor, recuperación, etiqueta de columna)
CONFIGURATIONS = [
    ("chunk", "vieja", "chunk+vieja"),
    ("chunk", "nueva", "chunk+nueva"),
    ("span", "nueva", "span"),
]

SAMPLES_PER_SOURCE = 4
# Largo de tramo idéntico que define "copia" para armar el relleno: las
# muestras comparten fórmulas metodológicas ("enfoque cuantitativo de diseño
# no experimental corte transversal...") y ninguna muestra que comparta un
# tramo así con una fuente puede usarse como texto "original".
CLEAN_MATCH_WORDS = 8
SAME_TOPIC_SAMPLES = 5
PHRASE_WORDS = 10
PHRASES_PER_SOURCE = 4
PARAPHRASE_EVERY = 6

# Criterio de aceptación (puntos porcentuales).
SYNTHETIC_TOLERANCE = Decimal("3.00")
ORIGINAL_TOLERANCE = Decimal("1.00")

# Reemplazos para la paráfrasis: palabras que no aparecen como tales en las
# muestras, así el cambio siempre rompe la coincidencia.
PARAPHRASE_WORDS = ["xilofono", "zarandaja", "quimera", "albedrio", "trebol", "yunque"]

STOPWORDS = set(
    "a al ante bajo como con contra de del desde donde durante e el en entre "
    "es esta este esto estos estas la las le les lo los mas mediante mismo no "
    "o para pero por que se ser si sin sobre son su sus tambien u un una unas "
    "uno unos y ya sus cual cuales cuando".split()
)


@dataclass(frozen=True)
class BenchmarkSource:
    label: str
    kind: str  # internal | oai | web
    samples: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(self.samples)


@dataclass
class BenchmarkCase:
    name: str
    description: str
    content: str
    expected_percent: Decimal
    expected_sources: set[str]
    # "sintetico" (tolerancia 3 pts), "original" (tolerancia 1 pt) o
    # "informativo" (sin criterio: ver la nota de paráfrasis).
    category: str
    note: str = ""


@dataclass
class CaseResult:
    percent: Decimal
    internal_percent: Decimal
    web_percent: Decimal
    detected_sources: set[str]
    seconds: float
    extra_sources: int = 0
    detail: dict = field(default_factory=dict)


def count_words(text: str) -> int:
    return sum(1 for _ in WORD_PATTERN.finditer(text))


def percent(part: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0.00")
    return (Decimal(part) / Decimal(total) * Decimal("100")).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = (
        "Banco de pruebas de similitud sin red: arma casos sintéticos con % "
        "conocido a partir de apps/analysis/evaluation/fixtures/ai_samples, "
        "indexa las fuentes (interno, OAI y web simulada) en una transacción "
        "que se revierte al final, y compara chunk+recuperación vieja, "
        "chunk+recuperación nueva y el motor span."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--seed", type=int, default=20261028)
        parser.add_argument("--samples-dir", type=str, default=str(DEFAULT_SAMPLES_DIR))
        parser.add_argument("--csv", type=str, default=str(DEFAULT_CSV_PATH))
        parser.add_argument(
            "--no-csv",
            action="store_true",
            help="No escribe el CSV de resultados.",
        )

    def handle(self, *args, **options) -> None:
        rng = random.Random(options["seed"])
        samples = self._load_samples(Path(options["samples_dir"]))
        sources, cases = self._build_cases(samples=samples, rng=rng)

        with transaction.atomic():
            results = self._run(sources=sources, cases=cases)
            # Nada de lo indexado para el banco de pruebas queda en la base.
            transaction.set_rollback(True)

        self._print_table(cases=cases, results=results)

        if not options["no_csv"]:
            self._write_csv(Path(options["csv"]), cases=cases, results=results)

        self.results = results
        self.cases = cases

    # --- construcción de casos -------------------------------------------

    def _load_samples(self, samples_dir: Path) -> list[str]:
        paths = sorted(
            path
            for path in samples_dir.glob("*.txt")
            if path.is_file()
        )
        return [path.read_text(encoding="utf-8").strip() for path in paths]

    def _build_cases(
        self,
        samples: list[str],
        rng: random.Random,
    ) -> tuple[list[BenchmarkSource], list[BenchmarkCase]]:
        pool = list(samples)
        rng.shuffle(pool)

        source_kinds = ["internal", "internal", "oai", "oai", "web"]
        sources: list[BenchmarkSource] = []

        for index, kind in enumerate(source_kinds, start=1):
            chosen = tuple(pool.pop() for _ in range(SAMPLES_PER_SOURCE))
            sources.append(
                BenchmarkSource(label=f"Fuente {index} ({kind})", kind=kind, samples=chosen)
            )

        source_kgrams = set()

        for source in sources:
            source_kgrams |= self._kgrams(source.text)

        clean_pool = [
            sample for sample in pool if not (self._kgrams(sample) & source_kgrams)
        ]
        self.discarded_samples = len(pool) - len(clean_pool)
        pool = clean_pool

        # "Misma temática": las muestras no usadas con más vocabulario en
        # común con alguna muestra de las fuentes.
        source_vocabularies = [
            self._vocabulary(sample) for source in sources for sample in source.samples
        ]
        pool.sort(
            key=lambda sample: max(
                self._jaccard(self._vocabulary(sample), vocabulary)
                for vocabulary in source_vocabularies
            ),
            reverse=True,
        )
        same_topic = [pool.pop(0) for _ in range(SAME_TOPIC_SAMPLES)]
        rng.shuffle(pool)
        fillers = iter(pool)

        def take(count: int) -> list[str]:
            return [next(fillers) for _ in range(count)]

        s1, s2, s3, s4, s5 = sources
        cases: list[BenchmarkCase] = []

        # 1. Copia literal 100 %.
        cases.append(
            BenchmarkCase(
                name="copia_100",
                description="Copia literal completa de una fuente interna",
                content=s1.text,
                expected_percent=Decimal("100.00"),
                expected_sources={s1.label},
                category="sintetico",
            )
        )

        # 2. Copia 50 %: dos muestras de una fuente OAI intercaladas con dos
        # originales.
        filler_a, filler_b = take(2)
        parts = [(s3.samples[0], True), (filler_a, False), (s3.samples[1], True), (filler_b, False)]
        cases.append(self._composed_case(
            name="copia_50",
            description="Mitad copiada de una fuente OAI, mitad original",
            parts=parts,
            expected_sources={s3.label},
        ))

        # 3. Copia 10 %: una muestra de la fuente web entre nueve originales.
        originals = take(9)
        parts = [(text, False) for text in originals[:5]]
        parts.append((s5.samples[0], True))
        parts.extend((text, False) for text in originals[5:])
        cases.append(self._composed_case(
            name="copia_10",
            description="Un pasaje de una página web entre texto original",
            parts=parts,
            expected_sources={s5.label},
        ))

        # 4. 20 frases sueltas de 10 palabras (4 de cada una de las 5 fuentes)
        # insertadas entre oraciones originales.
        phrases = [
            self._random_phrase(rng.choice(source.samples), rng)
            for source in sources
            for _ in range(PHRASES_PER_SOURCE)
        ]
        rng.shuffle(phrases)
        sentences = re.split(r"(?<=\.)\s+", " ".join(take(10)))
        step = max(len(sentences) // (len(phrases) + 1), 1)
        parts = []
        phrase_iter = iter(phrases)

        for index, sentence in enumerate(sentences, start=1):
            parts.append((sentence, False))

            if index % step == 0:
                phrase = next(phrase_iter, None)

                if phrase is not None:
                    parts.append((phrase, True))
                    parts.append((".", False))

        cases.append(self._composed_case(
            name="frases_20",
            description="20 frases de 10 palabras de 5 fuentes entre oraciones originales",
            parts=parts,
            expected_sources={source.label for source in sources},
            separator=" ",
        ))

        # 5. Paráfrasis ligera: 1 de cada 6 palabras cambiada. El tramo
        # idéntico más largo queda en 5 palabras; con la regla "excluir
        # coincidencias < 8 palabras" lo esperado es 0 %, aunque el texto
        # sigue siendo copia en un ~83 %.
        paraphrased = self._paraphrase(s4.text)
        cases.append(
            BenchmarkCase(
                name="parafrasis_1de6",
                description="Fuente OAI con 1 de cada 6 palabras cambiada",
                content=paraphrased,
                expected_percent=Decimal("0.00"),
                expected_sources=set(),
                category="informativo",
                note=(
                    "Regla Turnitin con mínimo 8 palabras: 0 %. Copia real "
                    "≈83 % (5 de cada 6 palabras); fuera del criterio."
                ),
            )
        )

        # 6. Texto original.
        cases.append(
            BenchmarkCase(
                name="original",
                description="Texto original sin copia",
                content="\n\n".join(take(5)),
                expected_percent=Decimal("0.00"),
                expected_sources=set(),
                category="original",
            )
        )

        # 7. Misma temática sin copia.
        cases.append(
            BenchmarkCase(
                name="misma_tematica",
                description="Misma temática que las fuentes, sin copia",
                content="\n\n".join(same_topic),
                expected_percent=Decimal("0.00"),
                expected_sources=set(),
                category="original",
            )
        )

        return sources, cases

    def _composed_case(
        self,
        name: str,
        description: str,
        parts: list[tuple[str, bool]],
        expected_sources: set[str],
        separator: str = "\n\n",
    ) -> BenchmarkCase:
        content = separator.join(text for text, _ in parts)
        copied = sum(count_words(text) for text, is_copy in parts if is_copy)

        return BenchmarkCase(
            name=name,
            description=description,
            content=content,
            expected_percent=percent(copied, count_words(content)),
            expected_sources=expected_sources,
            category="sintetico",
        )

    def _random_phrase(self, sample: str, rng: random.Random) -> str:
        words = list(WORD_PATTERN.finditer(sample))
        start = rng.randrange(0, len(words) - PHRASE_WORDS)
        return sample[words[start].start() : words[start + PHRASE_WORDS - 1].end()]

    def _paraphrase(self, text: str) -> str:
        pieces: list[str] = []
        cursor = 0

        for index, match in enumerate(WORD_PATTERN.finditer(text)):
            if index % PARAPHRASE_EVERY == PARAPHRASE_EVERY - 1:
                pieces.append(text[cursor : match.start()])
                pieces.append(PARAPHRASE_WORDS[(index // PARAPHRASE_EVERY) % len(PARAPHRASE_WORDS)])
                cursor = match.end()

        pieces.append(text[cursor:])
        return "".join(pieces)

    def _kgrams(self, text: str) -> set[tuple[str, ...]]:
        tokens = [word for word, _, _ in SpanSimilarityEngine()._document_words(text, [])]
        return {
            tuple(tokens[index : index + CLEAN_MATCH_WORDS])
            for index in range(len(tokens) - CLEAN_MATCH_WORDS + 1)
        }

    def _vocabulary(self, text: str) -> set[str]:
        return {
            word
            for word in (match.group(0).lower() for match in WORD_PATTERN.finditer(text))
            if len(word) > 3 and word not in STOPWORDS
        }

    def _jaccard(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    # --- ejecución ---------------------------------------------------------

    def _run(
        self,
        sources: list[BenchmarkSource],
        cases: list[BenchmarkCase],
    ) -> dict[tuple[str, str], CaseResult]:
        suffix = uuid4().hex[:8]
        institution = Institution.objects.create(
            name=f"Benchmark similitud {suffix}",
            slug=f"benchmark-similitud-{suffix}",
        )
        source_owner = self._user(institution, f"bench-fuente-{suffix}")
        case_owner = self._user(institution, f"bench-alumno-{suffix}")

        label_by_title: dict[str, str] = {}
        web_pages: list[WebPageContent] = []
        indexer = DocumentKnowledgeIndexer()

        for source in sources:
            title = f"{source.label} [{suffix}]"
            label_by_title[title] = source.label

            if source.kind == "internal":
                document = self._document(institution, source_owner, title)
                indexer.index(DocumentText(document=document), content=source.text)
            elif source.kind == "oai":
                record = OaiRecord.objects.create(
                    repository_name="Repositorio benchmark",
                    repository_domain="benchmark.local",
                    oai_identifier=f"oai:benchmark:{suffix}:{source.label}",
                    title=title,
                    text_excerpt=source.text,
                    normalized_text=indexer._normalize(source.text),
                    harvested_at=timezone.now(),
                )
                DocumentFingerprint.objects.bulk_create(
                    [
                        DocumentFingerprint(hash=value, source_type="oai", source_id=record.id)
                        for value in compute_fingerprints(record.normalized_text)
                    ],
                    ignore_conflicts=True,
                )
            else:
                web_pages.append(
                    WebPageContent(
                        title=title,
                        url=f"https://benchmark.local/{suffix}/{len(web_pages)}",
                        domain="benchmark.local",
                        text=source.text,
                        source_format="html",
                    )
                )

        case_document = self._document(institution, case_owner, f"Caso benchmark [{suffix}]")
        service = DocumentAnalysisService(requested_by=case_owner)
        results: dict[tuple[str, str], CaseResult] = {}

        for case in cases:
            for engine, retrieval, column in CONFIGURATIONS:
                started = time.perf_counter()
                similarity = service._run_similarity(
                    document=case_document,
                    analysis_content=case.content,
                    quoted_ranges=[],
                    collect_web_pages=lambda: list(web_pages),
                    engine=engine,
                    retrieval=retrieval,
                )
                elapsed = time.perf_counter() - started

                titles = {match.source_title for match in similarity.internal_result.matches}
                titles |= {match.title for match in similarity.web_result.matches}
                detected = {label_by_title[title] for title in titles if title in label_by_title}

                results[(case.name, column)] = CaseResult(
                    percent=similarity.total_similarity_percent,
                    internal_percent=similarity.internal_result.similarity_percent,
                    web_percent=similarity.web_result.web_similarity_percent,
                    detected_sources=detected,
                    extra_sources=len(titles - set(label_by_title)),
                    seconds=elapsed,
                )

        return results

    def _user(self, institution: Institution, username: str) -> User:
        return User.objects.create_user(
            username=username,
            email=f"{username}@benchmark.local",
            password=uuid4().hex,
            role=UserRole.STUDENT,
            institution=institution,
        )

    def _document(self, institution: Institution, owner: User, title: str) -> Document:
        document = Document(
            institution=institution,
            owner=owner,
            uploaded_by=owner,
            title=title,
            kind=DocumentKind.OTHER,
            original_filename="benchmark.txt",
            mime_type="text/plain",
            file_size_bytes=1,
            sha256_hash=uuid4().hex,
        )
        # Solo el nombre: el banco de pruebas no escribe archivos en disco.
        document.original_file.name = f"private/benchmark/{uuid4().hex}.txt"
        document.save()
        return document

    # --- salida -----------------------------------------------------------

    def _passes(self, case: BenchmarkCase, result: CaseResult) -> str:
        error = abs(result.percent - case.expected_percent)

        if case.category == "sintetico":
            return "OK" if error <= SYNTHETIC_TOLERANCE else "FALLA"
        if case.category == "original":
            return "OK" if error <= ORIGINAL_TOLERANCE else "FALLA"
        return "-"

    def _sources_cell(self, case: BenchmarkCase, result: CaseResult) -> str:
        found = len(case.expected_sources & result.detected_sources)
        wrong = len(result.detected_sources - case.expected_sources) + result.extra_sources
        cell = f"{found}/{len(case.expected_sources)}"
        return f"{cell} +{wrong}" if wrong else cell

    def _print_table(
        self,
        cases: list[BenchmarkCase],
        results: dict[tuple[str, str], CaseResult],
    ) -> None:
        columns = [column for _, _, column in CONFIGURATIONS]
        header = f"{'caso':<16} {'palabras':>8} {'esperado':>9} " + " ".join(
            f"{column:>24}" for column in columns
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for case in cases:
            cells = []

            for column in columns:
                result = results[(case.name, column)]
                cells.append(
                    f"{result.percent:>6}% {self._passes(case, result):>5} "
                    f"f:{self._sources_cell(case, result):<7} {result.seconds:>4.1f}s"
                )

            self.stdout.write(
                f"{case.name:<16} {count_words(case.content):>8} "
                f"{case.expected_percent:>8}% " + " ".join(f"{cell:>24}" for cell in cells)
            )

        self.stdout.write("")
        self.stdout.write(
            "f: fuentes esperadas detectadas / esperadas; +N = fuentes no esperadas. "
            f"Criterio: sintéticos ±{SYNTHETIC_TOLERANCE} pts, originales ±{ORIGINAL_TOLERANCE} pts."
        )

        self.stdout.write(
            f"Muestras descartadas como relleno por compartir tramos de "
            f"{CLEAN_MATCH_WORDS}+ palabras con las fuentes: {self.discarded_samples}."
        )

        for case in cases:
            if case.note:
                self.stdout.write(f"Nota {case.name}: {case.note}")

    def _write_csv(
        self,
        path: Path,
        cases: list[BenchmarkCase],
        results: dict[tuple[str, str], CaseResult],
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "caso", "categoria", "palabras", "esperado", "configuracion",
                "total", "interno_oai", "web", "criterio", "fuentes", "segundos",
            ])

            for case in cases:
                for _, _, column in CONFIGURATIONS:
                    result = results[(case.name, column)]
                    writer.writerow([
                        case.name,
                        case.category,
                        count_words(case.content),
                        case.expected_percent,
                        column,
                        result.percent,
                        result.internal_percent,
                        result.web_percent,
                        self._passes(case, result),
                        self._sources_cell(case, result),
                        f"{result.seconds:.2f}",
                    ])

        self.stdout.write(f"Resultados guardados en {path}")
