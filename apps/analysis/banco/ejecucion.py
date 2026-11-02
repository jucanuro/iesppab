"""
Pasos del banco de similitud que tocan BD, archivos y red. Los usa
`benchmark_similarity --banco ...`.

1. preparar-web: consultas de educación en Brave (con caché) y foto de las
   páginas en `<banco>/web/paginas.json`. Es el único paso con red.
2. evaluar-bases: extrae las tesis candidatas de eval_ia, descarta las que
   están en el corpus, las versiones repetidas y las que dan > 10 % solas.
3. generar: arma los casos con semilla fija (`<banco>/respuestas.json`,
   `<banco>/casos/`), incluidos algunos DOCX y PDF.
4. correr: indexa las fuentes del instituto, analiza cada base sola (línea
   base) y cada caso con el motor span, califica y guarda los resultados.

Todo lo que se indexa ocurre dentro de una transacción que se revierte: nada
del banco queda como fuente real.
"""

from __future__ import annotations

import html
import json
import random
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from apps.analysis.banco.calificador import (
    Baseline,
    CaseScore,
    EngineOutput,
    baseline_from_output,
    score_case,
    summarize,
)
from apps.analysis.banco.generador import (
    SOURCE_CORPUS,
    SOURCE_INSTITUTE,
    SOURCE_WEB,
    Bank,
    BankCase,
    BankGenerator,
    BaseDocument,
    SourceDocument,
    bank_summary,
    institute_key,
)
from apps.analysis.banco.textos import count_words
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_search import WebCandidateCollector, WebPageContent
from apps.analysis.models import OaiFullText, OaiFullTextChunk, OaiFullTextStatus
from apps.documents.extractors import DocumentTextExtractor

EVAL_DIR = Path(settings.BASE_DIR) / "eval_ia"
DEFAULT_BANK_DIR = EVAL_DIR / "banco"
UPEU_DIRS = ["humanos_pre2022", "humanos_upeu_2022plus"]
INSTITUTE_DIR = "humanos"

MAX_BASE_PERCENT = Decimal("10.00")
# Dos tesis son versiones de la misma si comparten esta fracción de sus
# tramos de 8 palabras (las versiones medidas comparten 0,57-1,00; las
# distintas, menos de 0,05).
VERSION_CONTAINMENT = 0.5
VERSION_KGRAM = 8
CORPUS_POOL = 200
MIN_WEB_PAGE_WORDS = 300

# Temas de educación para la foto web: consultas de tema, no frases
# copiadas, como haría un alumno buscando material.
WEB_QUERIES = [
    "comprensión lectora en estudiantes de primaria estrategias",
    "importancia del juego en el aprendizaje de niños de educación inicial",
    "expresión oral en niños de educación inicial",
    "psicomotricidad en educación inicial definición",
    "aprendizaje basado en proyectos en educación básica",
    "evaluación formativa y retroalimentación en el aula",
    "convivencia escolar y clima del aula",
    "materiales didácticos en el aprendizaje de la matemática",
    "producción de textos escritos en estudiantes de primaria",
    "estrategias lúdicas para el aprendizaje",
    "desempeño docente en instituciones educativas",
    "autoestima y rendimiento académico en estudiantes",
    "conciencia fonológica en niños de cinco años",
    "motricidad fina y preescritura en niños",
    "educación intercultural bilingüe en el Perú",
    "competencias digitales docentes",
    "habilidades sociales en estudiantes de primaria",
    "inteligencia emocional en el aula",
    "lectura de cuentos infantiles y desarrollo del lenguaje",
    "trabajo colaborativo en el aprendizaje escolar",
]


# --- utilidades -------------------------------------------------------------


def _extract(path: Path) -> str:
    extractor = DocumentTextExtractor()
    mime = extractor.PDF_MIME if path.suffix.lower() == ".pdf" else extractor.DOCX_MIME

    with path.open("rb") as handle:
        document = SimpleNamespace(id=path.name, mime_type=mime, original_file=File(handle))
        return extractor.extract(document).content


def _filtered(text: str) -> str:
    return AcademicTextFilter().filter_for_similarity(content=text).content or text


def _kgrams(text: str) -> set[tuple[str, ...]]:
    tokens = re.findall(r"\w+", text.lower())
    return {
        tuple(tokens[index : index + VERSION_KGRAM])
        for index in range(len(tokens) - VERSION_KGRAM + 1)
    }


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_json(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Falta {path}: corre antes el paso que lo genera.")
    return json.loads(path.read_text(encoding="utf-8"))


def git_commit() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, cwd=settings.BASE_DIR,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True, cwd=settings.BASE_DIR,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "desconocido"
    return f"{commit}-sucio" if dirty else commit


def _fulltext_handles() -> set[str]:
    """Handles UPeU (p. ej. '367') con texto completo en el corpus."""
    identifiers = OaiFullText.objects.filter(status=OaiFullTextStatus.OK).values_list(
        "record__oai_identifier", flat=True
    )
    return {
        match.group(1)
        for identifier in identifiers
        if (match := re.search(r"20\.500\.12840/(\d+)$", identifier))
    }


def corpus_text(record_id) -> str:
    return "".join(
        OaiFullTextChunk.objects.filter(record_id=record_id)
        .order_by("position")
        .values_list("text", flat=True)
    )


# --- 1. foto web ------------------------------------------------------------


def prepare_web(bank_dir: Path, write: Callable[[str], None]) -> list[dict]:
    collector = WebCandidateCollector()
    collector.max_pages_total = len(WEB_QUERIES) * 5
    collector.max_pages_per_query = 5
    run = collector.run_queries(WEB_QUERIES)

    pages = []
    seen = set()
    for query, page in run.pages:
        if page.url in seen or count_words(page.text) < MIN_WEB_PAGE_WORDS:
            continue
        seen.add(page.url)
        pages.append(
            {
                "clave": f"web:{page.url}",
                "url": page.url,
                "titulo": page.title,
                "dominio": page.domain,
                "formato": page.source_format,
                "texto": page.text,
                "consulta": query,
            }
        )

    _write_json(bank_dir / "web" / "paginas.json", pages)
    write(
        f"Consultas: {run.executed_queries}; páginas leídas: {run.fetched_pages}; "
        f"guardadas (>= {MIN_WEB_PAGE_WORDS} palabras): {len(pages)}."
    )
    return pages


def load_web_pages(bank_dir: Path) -> dict[str, WebPageContent]:
    return {
        item["clave"]: WebPageContent(
            title=item["titulo"],
            url=item["url"],
            domain=item["dominio"],
            text=item["texto"],
            source_format=item["formato"],
        )
        for item in _read_json(bank_dir / "web" / "paginas.json")
    }


# --- 2. evaluar bases -------------------------------------------------------


@dataclass
class BaseCandidate:
    id: str
    origin: str
    path: Path
    status: str = "pendiente"
    text: str = ""
    percent: Decimal | None = None
    version_of: str = ""
    detail: str = ""


def _base_candidates(eval_dir: Path) -> list[BaseCandidate]:
    candidates = []
    for folder in UPEU_DIRS:
        for path in sorted((eval_dir / folder).glob("*.pdf")):
            handle = path.stem.split("_")[-1]
            candidates.append(BaseCandidate(id=f"upeu_{handle}", origin="upeu", path=path))

    for path in sorted((eval_dir / INSTITUTE_DIR).iterdir()):
        if path.suffix.lower() in {".pdf", ".docx"}:
            slug = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_")
            while any(c.id == f"inst_{slug}" for c in candidates):
                slug += "_2"
            candidates.append(BaseCandidate(id=f"inst_{slug}", origin=SOURCE_INSTITUTE, path=path))

    return candidates


def evaluate_bases(
    bank_dir: Path,
    run_alone: Callable[[str], EngineOutput],
    write: Callable[[str], None],
    eval_dir: Path = EVAL_DIR,
) -> list[BaseCandidate]:
    """
    `run_alone(texto)` analiza un texto con el motor (sin web) dentro de la
    transacción del llamador.
    """
    in_corpus = _fulltext_handles()
    candidates = _base_candidates(eval_dir)
    kept: list[tuple[BaseCandidate, set]] = []

    for candidate in candidates:
        if candidate.origin == "upeu" and candidate.id.split("_")[1] in in_corpus:
            candidate.status = "en_corpus"
            continue

        try:
            candidate.text = _filtered(_extract(candidate.path))
        except Exception as exc:  # noqa: BLE001 - se informa y se sigue
            candidate.status = "extraccion_fallida"
            candidate.detail = str(exc)[:200]
            continue

        if count_words(candidate.text) < 2000:
            candidate.status = "texto_corto"
            continue

        grams = _kgrams(candidate.text)
        duplicate = next(
            (
                other for other, other_grams in kept
                if len(grams & other_grams) / max(min(len(grams), len(other_grams)), 1)
                > VERSION_CONTAINMENT
            ),
            None,
        )
        if duplicate is not None:
            candidate.status = "version"
            candidate.version_of = duplicate.id
            continue

        kept.append((candidate, grams))

    for candidate, _ in kept:
        output = run_alone(candidate.text)
        candidate.percent = output.percent
        candidate.status = "ok" if output.percent <= MAX_BASE_PERCENT else "mas_de_10"
        write(f"  {candidate.id[:60]:<60} {output.percent:>6}%  {candidate.status}")

    texts_dir = bank_dir / "bases"
    texts_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        if candidate.status == "ok":
            (texts_dir / f"{candidate.id}.txt").write_text(candidate.text, encoding="utf-8")

    _write_json(
        bank_dir / "bases.json",
        [
            {
                "id": c.id,
                "origen": c.origin,
                "archivo": str(c.path.relative_to(eval_dir)),
                "palabras": count_words(c.text) if c.text else 0,
                "porcentaje": None if c.percent is None else str(c.percent),
                "estado": c.status,
                "version_de": c.version_of,
                "detalle": c.detail,
            }
            for c in candidates
        ],
    )
    return candidates


def load_bases(bank_dir: Path) -> list[BaseDocument]:
    bases = []
    for item in _read_json(bank_dir / "bases.json"):
        if item["estado"] != "ok":
            continue
        bases.append(
            BaseDocument(
                id=item["id"],
                origin=item["origen"],
                title=Path(item["archivo"]).stem,
                text=(bank_dir / "bases" / f"{item['id']}.txt").read_text(encoding="utf-8"),
            )
        )
    return bases


# --- 3. generar -------------------------------------------------------------


def corpus_pool(seed: int, size: int = CORPUS_POOL) -> list[SourceDocument]:
    records = list(
        OaiFullText.objects.filter(status=OaiFullTextStatus.OK)
        .order_by("record__oai_identifier")
        .values_list("record_id", "record__title")
    )
    sample = random.Random(seed).sample(records, min(size, len(records)))
    return [
        SourceDocument(key=f"oai:{record_id}", kind=SOURCE_CORPUS, title=title, text=corpus_text(record_id))
        for record_id, title in sample
    ]


def generate(bank_dir: Path, seed: int, write: Callable[[str], None]) -> Bank:
    bases = load_bases(bank_dir)
    web = [
        SourceDocument(key=key, kind=SOURCE_WEB, title=page.title, text=page.text)
        for key, page in sorted(load_web_pages(bank_dir).items())
    ]
    bank = BankGenerator(seed=seed).generate(bases=bases, corpus=corpus_pool(seed), web=web)

    cases_dir = bank_dir / "casos"
    cases_dir.mkdir(parents=True, exist_ok=True)
    for old in cases_dir.iterdir():
        old.unlink()

    for case in bank.cases:
        (cases_dir / f"{case.id}.txt").write_text(case.content, encoding="utf-8")
        for extension in case.files:
            write_case_file(case, cases_dir / f"{case.id}.{extension}")

    data = bank.to_json()
    data["generado"] = timezone.now().isoformat()
    data["commit"] = git_commit()
    _write_json(bank_dir / "respuestas.json", data)

    write(
        f"Bases: {len(bases)} ({len(bank.institute_sources)} del instituto quedan como "
        f"envíos de otros alumnos). Páginas web: {len(web)}."
    )
    for category, item in bank_summary(bank).items():
        write(
            f"  {category:<11} casos={item['casos']} palabras={item['palabras']} "
            f"insertadas={item['insertadas']}"
        )
    return bank


def write_case_file(case: BankCase, path: Path) -> None:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", case.content) if p.strip()]

    if path.suffix == ".docx":
        from docx import Document as DocxDocument

        document = DocxDocument()
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
        document.save(str(path))
        return

    from weasyprint import HTML

    body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
    HTML(
        string=(
            "<html><head><meta charset='utf-8'><style>"
            "body{font-family:serif;font-size:12pt;line-height:1.5}"
            "p{text-align:justify;margin:0 0 10pt}"
            f"</style></head><body>{body}</body></html>"
        )
    ).write_pdf(str(path))


def load_bank(bank_dir: Path) -> tuple[dict, list[BankCase]]:
    data = _read_json(bank_dir / "respuestas.json")
    return data, [BankCase.from_json(item) for item in data["casos"]]


# --- 4. correr --------------------------------------------------------------


class BankRunner:
    """
    Corre el banco dentro de una transacción revertida con el motor span y
    el resto de la configuración vigente (N, tope de candidatos, etc.).
    """

    def __init__(self, bank_dir: Path, write: Callable[[str], None], web_real: bool = False):
        self.bank_dir = bank_dir
        self.write = write
        self.web_real = web_real
        self.text_filter = AcademicTextFilter()

    def run(self, case_ids: set[str] | None = None) -> tuple[dict, list[CaseScore]]:
        meta, cases = load_bank(self.bank_dir)
        if case_ids:
            cases = [case for case in cases if case.id in case_ids]

        snapshot = load_web_pages(self.bank_dir)
        pages_by_case = {
            case.id: [snapshot[key] for key in case.web_pages if key in snapshot]
            for case in cases
        }

        if self.web_real:
            # Fuera de la transacción: así la caché web sí queda guardada.
            collector = WebCandidateCollector()
            for case in cases:
                if case.category == "web":
                    pages_by_case[case.id] = collector.collect(content=case.content)

        with override_settings(SIMILARITY_ENGINE="span"):
            with transaction.atomic():
                scores = self._run_cases(meta, cases, pages_by_case)
                transaction.set_rollback(True)

        return self._config(meta), scores

    def _config(self, meta: dict) -> dict:
        return {
            "motor": "span",
            "texto_comun_min_docs": getattr(settings, "SIMILARITY_COMMON_TEXT_MIN_DOCS", None),
            "tope_candidatos": getattr(settings, "SIMILARITY_MAX_CANDIDATES", None),
            "min_palabras_tramo": getattr(settings, "SIMILARITY_MIN_MATCH_WORDS", None),
            "min_tramo_corto": getattr(settings, "SIMILARITY_MIN_SHORT_RUN", None),
            "web": "real (casos web)" if self.web_real else "foto sin red",
            "semilla_banco": meta.get("semilla"),
            "commit_banco": meta.get("commit"),
        }

    def _run_cases(self, meta, cases, pages_by_case) -> list[CaseScore]:
        # Import diferido: el servicio lee el motor en __init__ y tiene que
        # crearse dentro de override_settings.
        from apps.accounts.models import User, UserRole
        from apps.analysis.indexers import DocumentKnowledgeIndexer
        from apps.analysis.services import DocumentAnalysisService
        from apps.core.models import Institution
        from apps.documents.models import Document, DocumentKind, DocumentText

        suffix = uuid4().hex[:8]
        institution = Institution.objects.create(
            name=f"Banco similitud {suffix}", slug=f"banco-similitud-{suffix}"
        )

        def user(name: str) -> User:
            return User.objects.create_user(
                username=f"{name}-{suffix}", email=f"{name}-{suffix}@banco.local",
                password=uuid4().hex, role=UserRole.STUDENT, institution=institution,
            )

        def document(owner: User, title: str) -> Document:
            item = Document(
                institution=institution, owner=owner, uploaded_by=owner, title=title,
                kind=DocumentKind.OTHER, original_filename="banco.txt",
                mime_type="text/plain", file_size_bytes=1, sha256_hash=uuid4().hex,
            )
            item.original_file.name = f"private/banco/{uuid4().hex}.txt"
            item.save()
            return item

        indexer = DocumentKnowledgeIndexer()
        institute_keys: dict[str, str] = {}
        for index, source in enumerate(meta["fuentes_instituto"], start=1):
            text = (self.bank_dir / "bases" / f"{source['id']}.txt").read_text(encoding="utf-8")
            owner = user(f"banco-alumno-fuente-{index}")
            item = document(owner, source["titulo"])
            indexer.index(DocumentText(document=item), content=text)
            institute_keys[f"internal:{item.id}"] = institute_key(source["id"])

        case_owner = user("banco-alumno-caso")
        case_document = document(case_owner, "Caso banco")
        service = DocumentAnalysisService(requested_by=case_owner)

        def analyze(text: str, pages: list[WebPageContent], extra_seconds: float = 0.0) -> EngineOutput:
            quoted = self.text_filter.find_quoted_ranges(text)
            started = time.perf_counter()
            result = service._run_similarity(
                document=case_document, analysis_content=text, quoted_ranges=quoted,
                collect_web_pages=lambda: list(pages), engine="span",
            )
            elapsed = time.perf_counter() - started + extra_seconds
            span = result.span_result
            return EngineOutput(
                analyzed_text=text,
                marked_ranges=[(m.start_offset, m.end_offset) for m in span.matches],
                quoted_ranges=quoted,
                source_keys={item.source.key for item in span.sources if item.word_count > 0},
                percent=span.similarity_percent,
                seconds=elapsed,
            )

        scores: list[CaseScore] = []
        for position, case in enumerate(cases, start=1):
            pages = pages_by_case[case.id]
            case_output = analyze(case.content, pages)
            baseline: Baseline = (
                baseline_from_output(case_output)
                if case.category == "original"
                else baseline_from_output(analyze(case.base_text, pages))
            )
            scores.append(score_case(case, case_output, baseline, institute_keys))

            for extension in case.files:
                path = self.bank_dir / "casos" / f"{case.id}.{extension}"
                started = time.perf_counter()
                text = _filtered(_extract(path))
                extraction = time.perf_counter() - started
                output = analyze(text, pages, extra_seconds=extraction)
                scores.append(score_case(case, output, baseline, institute_keys, variant=extension))

            last = scores[-1 - len(case.files)]
            self.write(
                f"  [{position}/{len(cases)}] {case.id:<14} {last.percent:>6}% "
                f"recall={last.word_recall} fp_nuevas={last.new_false_positive_words} "
                f"{last.seconds:.1f}s"
            )

        return scores


def save_results(bank_dir: Path, config: dict, scores: list[CaseScore]) -> Path:
    now = timezone.localtime()
    commit = git_commit()
    path = bank_dir / "resultados" / f"{now:%Y%m%d-%H%M%S}_{commit}.json"
    _write_json(
        path,
        {
            "fecha": now.isoformat(),
            "commit": commit,
            "configuracion": config,
            "resumen": summarize(scores),
            "casos": [score.to_json() for score in scores],
        },
    )
    return path


CATEGORY_LABELS = {
    "original": "Originales",
    "corpus": "Corpus UPeU",
    "web": "Web",
    "instituto": "Instituto",
    "parafrasis": "Paráfrasis",
    "frases": "Frases sueltas",
}


def format_table(summary: dict[str, dict]) -> list[str]:
    header = (
        f"{'categoría':<22} {'casos':>5} {'rec.pal':>8} {'rec.fte':>8} {'% medio':>8} "
        f"{'% máx':>7} {'base %':>7} {'FP nuevas':>10} {'FP % base':>9} {'f.extra':>7} {'s/doc':>6}"
    )
    lines = [header, "-" * len(header)]

    def label(key: str) -> str:
        category, _, variant = key.partition(".")
        name = CATEGORY_LABELS.get(category, category)
        return f"{name} ({variant.upper()})" if variant else name

    order = [c for c in CATEGORY_LABELS] + sorted(k for k in summary if "." in k)
    for key in order:
        if key not in summary:
            continue
        item = summary[key]
        lines.append(
            f"{label(key):<22} {item['casos']:>5} {item['recall_palabras'] or '-':>8} "
            f"{item['recall_fuentes'] or '-':>8} {item['porcentaje_medio']:>8} "
            f"{item['porcentaje_max']:>7} {item['linea_base_media']:>7} "
            f"{item['fp_nuevas_palabras']:>10} {item['fp_nuevas_pct_base'] or '-':>9} "
            f"{item['fuentes_extra']:>7} {item['segundos_medio']:>6}"
        )
    return lines
