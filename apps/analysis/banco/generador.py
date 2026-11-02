"""
Generador del banco de similitud.

Cada caso es un fragmento de una tesis base (no está en el corpus ni en
la base interna) con pasajes insertados cuyo origen se guarda en el JSON
de respuestas. Con la misma semilla y las mismas entradas el banco sale
idéntico.

Categorías (10 casos cada una):
- original: sin inserciones (mide falsos positivos).
- corpus: 3-5 pasajes literales de tesis UPeU del corpus.
- web: 3-5 pasajes literales de páginas web reales.
- instituto: 3-5 pasajes de tesis del instituto indexadas como envíos de
  otros alumnos dentro del banco.
- parafrasis: los mismos pasajes de "instituto", parafraseados por reglas.
- frases: 8-12 frases sueltas de 5-10 palabras de fuentes distintas.

Aparte, 5 casos "web_completa": la tesis base entera (12.000-20.000
palabras, como una real) con 6-10 pasajes web. Usan su propio generador
aleatorio, así que no cambian los 60 casos anteriores.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from apps.analysis.banco.textos import (
    SENTENCE_SPLIT,
    count_words,
    paraphrase,
    pick_passage,
    pick_phrase,
    split_excerpts,
)

CATEGORIES = ["original", "corpus", "web", "instituto", "parafrasis", "frases"]
CASES_PER_CATEGORY = 10
EXCERPT_WORDS = 2500
INSTITUTE_SOURCES = 6
PASSAGES_MIN, PASSAGES_MAX = 3, 5
PHRASES_MIN, PHRASES_MAX = 8, 12
PHRASE_WORDS_MIN, PHRASE_WORDS_MAX = 5, 10
# Mismo tope de páginas que el análisis real (WEB_ANALYSIS_MAX_PAGES_TOTAL).
WEB_PAGES_PER_CASE = 16
# Por categoría: el caso 1 también se genera como DOCX y el 2 como PDF.
FILE_VARIANTS = {1: "docx", 2: "pdf"}
FULL_WEB_CATEGORY = "web_completa"
FULL_WEB_CASES = 5
FULL_PASSAGES_MIN, FULL_PASSAGES_MAX = 6, 10
FULL_MIN_WORDS, FULL_MAX_WORDS = 12000, 20000

SOURCE_CORPUS = "corpus"
SOURCE_WEB = "web"
SOURCE_INSTITUTE = "instituto"


class BankGenerationError(Exception):
    """No hay material suficiente para armar el banco."""


@dataclass(frozen=True)
class BaseDocument:
    id: str
    origin: str  # "upeu" | "instituto"
    title: str
    text: str


@dataclass(frozen=True)
class SourceDocument:
    key: str  # "oai:<uuid>" | "web:<url>" | "instituto:<id de base>"
    kind: str  # SOURCE_CORPUS | SOURCE_WEB | SOURCE_INSTITUTE
    title: str
    text: str


@dataclass
class Insertion:
    source_kind: str
    source_key: str
    source_title: str
    text: str
    # Texto literal de la fuente cuando `text` es una paráfrasis.
    original: str = ""


@dataclass
class Segment:
    start: int
    end: int
    kind: str  # "base" | "insercion" | "separador"
    insertion: int | None = None


@dataclass
class BankCase:
    id: str
    category: str
    base_id: str
    excerpt: int
    base_text: str
    content: str
    segments: list[Segment] = field(default_factory=list)
    insertions: list[Insertion] = field(default_factory=list)
    web_pages: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "categoria": self.category,
            "base_id": self.base_id,
            "fragmento": self.excerpt,
            "texto_base": self.base_text,
            "contenido": self.content,
            "segmentos": [
                {"inicio": s.start, "fin": s.end, "tipo": s.kind, "insercion": s.insertion}
                for s in self.segments
            ],
            "inserciones": [
                {
                    "fuente_tipo": i.source_kind,
                    "fuente_clave": i.source_key,
                    "fuente_titulo": i.source_title,
                    "texto": i.text,
                    "original": i.original,
                }
                for i in self.insertions
            ],
            "paginas_web": list(self.web_pages),
            "archivos": list(self.files),
        }

    @classmethod
    def from_json(cls, data: dict) -> "BankCase":
        return cls(
            id=data["id"],
            category=data["categoria"],
            base_id=data["base_id"],
            excerpt=data["fragmento"],
            base_text=data["texto_base"],
            content=data["contenido"],
            segments=[
                Segment(start=s["inicio"], end=s["fin"], kind=s["tipo"], insertion=s["insercion"])
                for s in data["segmentos"]
            ],
            insertions=[
                Insertion(
                    source_kind=i["fuente_tipo"],
                    source_key=i["fuente_clave"],
                    source_title=i["fuente_titulo"],
                    text=i["texto"],
                    original=i.get("original", ""),
                )
                for i in data["inserciones"]
            ],
            web_pages=list(data.get("paginas_web", [])),
            files=list(data.get("archivos", [])),
        )


@dataclass
class Bank:
    seed: int
    excerpt_words: int
    institute_sources: list[BaseDocument]
    cases: list[BankCase]

    def to_json(self) -> dict:
        return {
            "semilla": self.seed,
            "palabras_fragmento": self.excerpt_words,
            "fuentes_instituto": [
                {"id": base.id, "titulo": base.title, "clave": institute_key(base.id)}
                for base in self.institute_sources
            ],
            "casos": [case.to_json() for case in self.cases],
        }


def institute_key(base_id: str) -> str:
    return f"{SOURCE_INSTITUTE}:{base_id}"


def _boundaries(text: str, paragraphs: bool) -> list[int]:
    """Posiciones donde se puede insertar sin partir palabras ni oraciones."""
    pattern = re.compile(r"\n\s*\n") if paragraphs else SENTENCE_SPLIT
    return [match.start() for match in pattern.finditer(text)]


def _spread(points: list[int], count: int, rng: random.Random) -> list[int]:
    """Elige `count` puntos repartidos a lo largo del texto (uno por tramo)."""
    if len(points) < count:
        raise BankGenerationError(
            f"El fragmento base no tiene {count} lugares para insertar."
        )

    chosen = []
    for index in range(count):
        low = len(points) * index // count
        high = max(len(points) * (index + 1) // count, low + 1)
        chosen.append(points[rng.randrange(low, high)])
    return sorted(chosen)


def splice(
    base_text: str,
    insertions: list[Insertion],
    rng: random.Random,
    as_phrases: bool = False,
) -> tuple[str, list[Segment]]:
    """
    Inserta los textos en el fragmento base: pasajes como párrafos propios
    entre párrafos (o entre oraciones si faltan párrafos), frases como
    oración propia entre oraciones. Devuelve el contenido y sus segmentos.
    """
    if not insertions:
        return base_text, [Segment(0, len(base_text), "base")]

    paragraph_points = _boundaries(base_text, paragraphs=True)
    use_paragraphs = not as_phrases and len(paragraph_points) >= len(insertions)
    points = paragraph_points if use_paragraphs else _boundaries(base_text, paragraphs=False)
    positions = _spread(points, len(insertions), rng)

    if as_phrases:
        before, after = " ", "."
    else:
        before, after = "\n\n", ""

    content = ""
    segments: list[Segment] = []
    cursor = 0

    def add(text: str, kind: str, insertion: int | None = None) -> None:
        nonlocal content
        if text:
            segments.append(Segment(len(content), len(content) + len(text), kind, insertion))
            content += text

    for index, position in enumerate(positions):
        add(base_text[cursor:position], "base")
        add(before, "separador")
        add(insertions[index].text, "insercion", index)
        add(after, "separador")
        cursor = position

    add(base_text[cursor:], "base")
    return content, segments


class BankGenerator:
    def __init__(
        self,
        seed: int,
        excerpt_words: int = EXCERPT_WORDS,
        cases_per_category: int = CASES_PER_CATEGORY,
        institute_sources: int = INSTITUTE_SOURCES,
    ) -> None:
        self.seed = seed
        self.excerpt_words = excerpt_words
        self.cases_per_category = cases_per_category
        self.institute_sources = institute_sources

    def generate(
        self,
        bases: list[BaseDocument],
        corpus: list[SourceDocument],
        web: list[SourceDocument],
    ) -> Bank:
        rng = random.Random(self.seed)
        bases = sorted(bases, key=lambda base: base.id)
        corpus = sorted(corpus, key=lambda source: source.key)
        web = sorted(web, key=lambda source: source.key)

        institute = [base for base in bases if base.origin == SOURCE_INSTITUTE]
        if len(institute) < self.institute_sources + 1:
            raise BankGenerationError("Faltan tesis del instituto para usar como fuentes.")

        source_bases = rng.sample(institute, self.institute_sources)
        source_ids = {base.id for base in source_bases}
        institute_pool = [
            SourceDocument(
                key=institute_key(base.id),
                kind=SOURCE_INSTITUTE,
                title=base.title,
                text=base.text,
            )
            for base in source_bases
        ]

        excerpts = self._excerpts([base for base in bases if base.id not in source_ids], rng)
        needed = len(CATEGORIES) * self.cases_per_category

        if len(excerpts) < needed:
            raise BankGenerationError(
                f"Solo hay {len(excerpts)} fragmentos base; hacen falta {needed}."
            )

        excerpt_iter = iter(excerpts)
        cases: list[BankCase] = []
        institute_insertions: list[list[Insertion]] = []

        for category in CATEGORIES:
            for number in range(1, self.cases_per_category + 1):
                base_id, excerpt_index, base_text = next(excerpt_iter)

                if category == "original":
                    insertions: list[Insertion] = []
                elif category == "corpus":
                    insertions = self._passages(corpus, rng)
                elif category == "web":
                    insertions = self._passages(web, rng)
                elif category == "instituto":
                    insertions = self._passages(institute_pool, rng)
                    institute_insertions.append(insertions)
                elif category == "parafrasis":
                    insertions = [
                        Insertion(
                            source_kind=item.source_kind,
                            source_key=item.source_key,
                            source_title=item.source_title,
                            text=paraphrase(item.text),
                            original=item.text,
                        )
                        for item in institute_insertions[number - 1]
                    ]
                else:
                    insertions = self._phrases(corpus, web, institute_pool, rng)

                content, segments = splice(
                    base_text,
                    insertions,
                    rng,
                    as_phrases=category == "frases",
                )

                case = BankCase(
                    id=f"{category}_{number:02d}",
                    category=category,
                    base_id=base_id,
                    excerpt=excerpt_index,
                    base_text=base_text,
                    content=content,
                    segments=segments,
                    insertions=insertions,
                    files=[FILE_VARIANTS[number]] if number in FILE_VARIANTS else [],
                )
                case.web_pages = self._web_pages(case, web, rng)
                cases.append(case)

        return Bank(
            seed=self.seed,
            excerpt_words=self.excerpt_words,
            institute_sources=source_bases,
            cases=cases,
        )

    def _excerpts(
        self,
        bases: list[BaseDocument],
        rng: random.Random,
    ) -> list[tuple[str, int, str]]:
        """Fragmentos repartidos por rondas entre las tesis (barajadas)."""
        per_base = []
        for base in bases:
            chunks = split_excerpts(base.text, self.excerpt_words)
            per_base.append([(base.id, index, chunk) for index, chunk in enumerate(chunks)])

        rng.shuffle(per_base)
        ordered = []
        round_index = 0

        while any(round_index < len(chunks) for chunks in per_base):
            for chunks in per_base:
                if round_index < len(chunks):
                    ordered.append(chunks[round_index])
            round_index += 1

        return ordered

    def generate_full_web(
        self,
        bases: list[BaseDocument],
        web: list[SourceDocument],
        excluded_ids: set[str],
    ) -> list[BankCase]:
        """
        Tesis completas con pasajes web. `excluded_ids`: las tesis que el
        banco indexa como fuentes del instituto (darían 100 % internas).
        """
        rng = random.Random(f"{self.seed}-{FULL_WEB_CATEGORY}")
        web = sorted(web, key=lambda source: source.key)
        pool = sorted(
            (
                base for base in bases
                if base.id not in excluded_ids
                and FULL_MIN_WORDS <= count_words(base.text) <= FULL_MAX_WORDS
            ),
            key=lambda base: base.id,
        )

        if len(pool) < FULL_WEB_CASES:
            raise BankGenerationError(
                f"Solo hay {len(pool)} tesis de {FULL_MIN_WORDS}-{FULL_MAX_WORDS} palabras."
            )

        cases = []
        for number, base in enumerate(rng.sample(pool, FULL_WEB_CASES), start=1):
            insertions = self._passages(
                web, rng, count=rng.randint(FULL_PASSAGES_MIN, FULL_PASSAGES_MAX)
            )
            content, segments = splice(base.text, insertions, rng)
            case = BankCase(
                id=f"{FULL_WEB_CATEGORY}_{number:02d}",
                category=FULL_WEB_CATEGORY,
                base_id=base.id,
                excerpt=-1,
                base_text=base.text,
                content=content,
                segments=segments,
                insertions=insertions,
            )
            case.web_pages = self._web_pages(case, web, rng)
            cases.append(case)

        return cases

    def _passages(
        self,
        pool: list[SourceDocument],
        rng: random.Random,
        count: int | None = None,
    ) -> list[Insertion]:
        count = count or rng.randint(PASSAGES_MIN, PASSAGES_MAX)
        candidates = list(pool)
        rng.shuffle(candidates)
        insertions = []

        for source in candidates:
            passage = pick_passage(source.text, rng)

            if passage is None:
                continue

            insertions.append(
                Insertion(
                    source_kind=source.kind,
                    source_key=source.key,
                    source_title=source.title,
                    text=passage,
                )
            )

            if len(insertions) == count:
                return insertions

        raise BankGenerationError("No hay suficientes fuentes con pasajes copiables.")

    def _phrases(
        self,
        corpus: list[SourceDocument],
        web: list[SourceDocument],
        institute: list[SourceDocument],
        rng: random.Random,
    ) -> list[Insertion]:
        count = rng.randint(PHRASES_MIN, PHRASES_MAX)
        # Mezcla de los tres orígenes, cada frase de una fuente distinta.
        candidates = (
            rng.sample(corpus, min(len(corpus), count))
            + rng.sample(web, min(len(web), count // 2))
            + rng.sample(institute, min(len(institute), count // 3))
        )
        rng.shuffle(candidates)
        insertions = []

        for source in candidates:
            phrase = pick_phrase(source.text, rng, PHRASE_WORDS_MIN, PHRASE_WORDS_MAX)

            if phrase is None:
                continue

            insertions.append(
                Insertion(
                    source_kind=source.kind,
                    source_key=source.key,
                    source_title=source.title,
                    text=phrase,
                )
            )

            if len(insertions) == count:
                return insertions

        raise BankGenerationError("No hay suficientes fuentes para las frases sueltas.")

    def _web_pages(
        self,
        case: BankCase,
        web: list[SourceDocument],
        rng: random.Random,
    ) -> list[str]:
        """
        Páginas que se le dan al caso como resultado de la búsqueda web: las
        de sus inserciones más otras del banco hasta el tope del análisis real.
        """
        own = []
        for insertion in case.insertions:
            if insertion.source_kind == SOURCE_WEB and insertion.source_key not in own:
                own.append(insertion.source_key)

        others = [source.key for source in web if source.key not in own]
        fill = max(WEB_PAGES_PER_CASE - len(own), 0)
        return own + rng.sample(others, min(fill, len(others)))


def bank_summary(bank: Bank) -> dict[str, dict]:
    """Palabras insertadas y fuentes por categoría (para revisar el banco)."""
    summary: dict[str, dict] = {}

    for case in bank.cases:
        item = summary.setdefault(case.category, {"casos": 0, "palabras": 0, "insertadas": 0})
        item["casos"] += 1
        item["palabras"] += count_words(case.content)
        item["insertadas"] += sum(count_words(i.text) for i in case.insertions)

    return summary

