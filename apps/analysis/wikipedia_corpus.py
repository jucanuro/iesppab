"""
Humanos de Wikipedia en español para el clasificador de IA v2.

La fuente es el dump oficial de Wikimedia (`dumps.wikimedia.org`), no la API:
el robots.txt de es.wikipedia.org prohíbe `/w/` y `/api/` a agentes
genéricos. Todo lo de aquí es local y sin red salvo la descarga del dump,
que hace el comando `preparar_wikipedia_v2` (un solo archivo, una conexión).

Solo se usan artículos de contenido (espacio de nombres 0, sin redirecciones
ni biografías). Del wikitexto se conserva únicamente la prosa: sin listas,
tablas, plantillas, referencias, imágenes ni secciones de cierre ("Véase
también", "Referencias"...).
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

DUMP_URL = "https://dumps.wikimedia.org/eswiki/latest/eswiki-latest-pages-articles.xml.bz2"
USER_AGENT = "IESPP-Originalidad/1.0 (investigación interna)"

MIN_WIKITEXT_CHARS = 15_000
MIN_PROSE_WORDS = 1_200
MIN_PARAGRAPH_WORDS = 20

# Orden = prioridad: la primera temática cuyas categorías coincidan gana. Los
# patrones van anclados al inicio del nombre de la categoría ("Educación en
# Perú" sí; "Alumnado de la Universidad de Harvard" no) para no arrastrar
# biografías ni obras que solo mencionan el tema.
THEMES: tuple[tuple[str, re.Pattern], ...] = (
    ("pedagogia", re.compile(
        r"^(?:pedagog|did[aá]ctic|curr[ií]cul|alfabetizaci|m[eé]todos? (?:pedag|de ense)|"
        r"teor[ií]as? (?:de|del) (?:la )?(?:educaci|aprendizaje)|psicolog[ií]a educ)", re.I)),
    ("educacion", re.compile(
        r"^(?:educaci[oó]n|sistemas? educativ|historia de la educaci|ense[ñn]anza|aprendizaje|"
        r"terminolog[ií]a educativa|evaluaci[oó]n educativa|tecnolog[ií]a educativa)", re.I)),
    ("psicologia", re.compile(
        r"^(?:psicolog|neuropsicolog|psicoan[aá]l|cognici|conducta|terapias? |procesos? cognitiv|"
        r"emoci|personalidad|motivaci|inteligencia)", re.I)),
    ("ciencias_sociales", re.compile(
        r"^(?:sociolog|antropolog|econom[ií]a|ciencias? pol[ií]tic|ciencia pol[ií]tic|teor[ií]a (?:pol|econ|soc)|"
        r"geograf[ií]a humana|demograf|ciencias sociales|ling[uü][ií]stica|filosof|metodolog[ií]a|[eé]tica|"
        r"cultura|sociedad|religi[oó]n|derecho (?:civil|penal|constitucional|internacional))", re.I)),
    ("ciencia", re.compile(
        r"^(?:f[ií]sica|qu[ií]mica|biolog|matem[aá]tic|astronom|geolog|ecolog|gen[eé]tica|"
        r"ciencias? de la|ciencia\b|ingenier[ií]a|neurociencia|evoluci[oó]n|clima)", re.I)),
    ("historia", re.compile(
        r"^(?:historia|edad (?:media|moderna|antigua)|siglo|guerras?\b|imperio|antig[uü]edad|revoluci[oó]n)",
        re.I)),
)
EXCLUDED_CATEGORY_RE = re.compile(
    r"nacidos|fallecidos|biograf|^personas|desambiguaci|^wikipedia|^anexo|^art[ií]culos|"
    r"^p[aá]ginas|^plantillas|^obras? de|^pel[ií]culas|^videojuegos|^series? de|"
    r"^(?:alumnado|miembros|presidentes|pol[ií]ticos|escritores|profesores|economistas|fil[oó]sofos|"
    r"psic[oó]logos|historiadores|cient[ií]ficos|f[ií]sicos|qu[ií]micos|bi[oó]logos|matem[aá]ticos|"
    r"soci[oó]logos|antrop[oó]logos|pedagogos|educadores|reyes|emperadores|militares|santos|pintores|"
    r"m[uú]sicos|actores|empresarios|abogados|periodistas|ingenieros|astr[oó]nomos|exploradores|"
    r"inventores|ling[uü]istas|gen[eé]ticos|doctores honoris)\b",
    re.I,
)
CUT_SECTIONS = frozenset({
    "véase también", "referencias", "notas", "notas y referencias", "enlaces externos",
    "bibliografía", "fuentes", "lecturas adicionales", "galería", "notas al pie",
    "referencias y notas", "bibliografía adicional", "otros proyectos",
})

_CATEGORY_RE = re.compile(r"\[\[\s*(?:Categor[ií]a|Category)\s*:\s*([^\]|]+)", re.I)
_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
_TABLE_RE = re.compile(r"\{\|(?:(?!\{\|).)*?\|\}", re.S)
_FILE_NS = {"archivo", "file", "imagen", "image", "categoría", "categoria", "category", "media"}
_LEFTOVER_RE = re.compile(r"(?:thumb|miniaturadeimagen|miniatura|\bupright\b|\bcentro\b)\s*\|[^\n]*", re.I)
_LIST_START = ("*", "#", ";", ":", "|", "!", "{", "=", "-", "•")


def extract_categories(wikitext: str) -> list[str]:
    return [match.strip() for match in _CATEGORY_RE.findall(wikitext)]


def classify_theme(categories: list[str]) -> str | None:
    """Temática del artículo o None si no es de ninguna (o es biografía/lista)."""
    if not categories or any(EXCLUDED_CATEGORY_RE.search(c) for c in categories):
        return None
    for name, pattern in THEMES:
        if any(pattern.search(category) for category in categories):
            return name
    return None


def cut_closing_sections(wikitext: str) -> str:
    for match in _HEADING_RE.finditer(wikitext):
        if match.group(2).strip().strip("'").lower() in CUT_SECTIONS:
            return wikitext[: match.start()]
    return wikitext


_INLINE_TEMPLATES = {"lang", "idioma", "nowrap", "small", "sc", "esd", "texto", "cita", "formatnum",
                     "unidad", "nobr", "mayúscula", "may"}


def _inline_template_text(template) -> str | None:
    """Plantillas que en la página se ven como texto corrido ({{lang|en|word}})."""
    name = str(template.name).strip().lower().split(":")[0]
    if name == "siglo" and template.params:
        return f"siglo {str(template.params[0].value).strip()}"
    if name in _INLINE_TEMPLATES and template.params:
        return str(template.params[-1].value).strip()
    return None


def wikitext_to_prose(wikitext: str) -> str:
    """Prosa de un artículo: párrafos separados por línea en blanco."""
    import mwparserfromhell

    text = cut_closing_sections(wikitext)
    previous = None
    while previous != text:  # tablas anidadas
        previous = text
        text = _TABLE_RE.sub("", text)
    code = mwparserfromhell.parse(text)
    for link in list(code.ifilter_wikilinks()):
        title = str(link.title).split(":", 1)
        if len(title) == 2 and title[0].strip().lower() in _FILE_NS:
            try:
                code.remove(link)
            except ValueError:
                pass
    for template in list(code.ifilter_templates(recursive=False)):
        replacement = _inline_template_text(template)
        if replacement is not None:
            code.replace(template, replacement)
    plain = code.strip_code(normalize=True, collapse=True)
    plain = _LEFTOVER_RE.sub("", plain)
    paragraphs = []
    for block in re.split(r"\n\s*\n|\n", plain):
        line = re.sub(r"\s+", " ", block).strip()
        if not line or line.startswith(_LIST_START):
            continue
        if len(line.split()) < MIN_PARAGRAPH_WORDS:
            continue
        paragraphs.append(line)
    return "\n\n".join(paragraphs)


def iter_dump_pages(fileobj) -> Iterator[tuple[str, str]]:
    """(título, wikitexto) de cada artículo del dump XML (ya descomprimido)."""
    import xml.etree.ElementTree as ET

    title = ns = text = None
    redirect = False
    for event, element in ET.iterparse(fileobj, events=("end",)):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "title":
            title = element.text
        elif tag == "ns":
            ns = element.text
        elif tag == "redirect":
            redirect = True
        elif tag == "text":
            text = element.text or ""
        elif tag == "page":
            if ns == "0" and not redirect and title and ":" not in title and text:
                yield title, text
            title = ns = text = None
            redirect = False
            element.clear()


@dataclass
class Candidate:
    title: str
    theme: str
    wikitext: str


class ThemeReservoir:
    """Muestreo por reservorio por temática (semilla fija): una pasada y memoria acotada."""

    def __init__(self, size_per_theme: int, seed: int) -> None:
        self.size = size_per_theme
        self.rng = random.Random(seed)
        self.items: dict[str, list[Candidate]] = defaultdict(list)
        self.seen: dict[str, int] = defaultdict(int)

    def offer(self, candidate: Candidate) -> None:
        bucket = self.items[candidate.theme]
        self.seen[candidate.theme] += 1
        if len(bucket) < self.size:
            bucket.append(candidate)
            return
        slot = self.rng.randrange(self.seen[candidate.theme])
        if slot < self.size:
            bucket[slot] = candidate


def scan_dump(fileobj, *, size_per_theme: int, seed: int, exclude_titles=frozenset(),
              progress=None) -> ThemeReservoir:
    reservoir = ThemeReservoir(size_per_theme, seed)
    excluded = {t.casefold() for t in exclude_titles}
    for index, (title, wikitext) in enumerate(iter_dump_pages(fileobj), 1):
        if progress and index % 200_000 == 0:
            progress(index, reservoir)
        if len(wikitext) < MIN_WIKITEXT_CHARS or title.casefold() in excluded:
            continue
        if wikitext.lstrip()[:9].lower() == "#redirec":
            continue
        theme = classify_theme(extract_categories(wikitext))
        if theme:
            reservoir.offer(Candidate(title, theme, wikitext))
    return reservoir


def select_balanced(reservoir: ThemeReservoir, total: int, seed: int) -> list[dict]:
    """Artículos con prosa suficiente, repartidos por temática y mezclados."""
    rng = random.Random(seed)
    per_theme: dict[str, list[dict]] = {}
    for theme, candidates in reservoir.items.items():
        ready = []
        for cand in candidates:
            prose = wikitext_to_prose(cand.wikitext)
            if len(prose.split()) >= MIN_PROSE_WORDS:
                ready.append({"titulo": cand.title, "tematica": theme, "texto": prose})
        rng.shuffle(ready)
        per_theme[theme] = ready
    chosen: list[dict] = []
    while len(chosen) < total and any(per_theme.values()):
        for theme in sorted(per_theme):
            if per_theme[theme] and len(chosen) < total:
                chosen.append(per_theme[theme].pop())
    rng.shuffle(chosen)
    return chosen
