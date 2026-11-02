"""
Utilidades de texto del banco de similitud: palabras, oraciones, pasajes
copiables y paráfrasis por reglas (sinónimos, conectores y orden de
oraciones). Todo es determinista dado un `random.Random`.
"""

from __future__ import annotations

import random
import re
import unicodedata

from apps.analysis.engines.span_similarity import WORD_PATTERN

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡“\"(])")

PASSAGE_MIN_WORDS = 60
PASSAGE_MAX_WORDS = 130

# Conectores: se reemplaza la primera forma por la segunda (respetando
# mayúscula inicial). Ordenados de más largo a más corto para que "Por lo
# tanto" no lo pise "Por".
CONNECTORS = [
    ("sin embargo", "no obstante"),
    ("no obstante", "sin embargo"),
    ("por lo tanto", "por consiguiente"),
    ("por consiguiente", "por lo tanto"),
    ("en este sentido", "en esta línea"),
    ("por otro lado", "en cambio"),
    ("por otra parte", "asimismo"),
    ("es decir", "o sea"),
    ("además", "asimismo"),
    ("asimismo", "del mismo modo"),
    ("con el fin de", "para"),
    ("a través de", "mediante"),
    ("mediante", "por medio de"),
    ("debido a", "a causa de"),
    ("de acuerdo con", "según"),
    ("según", "de acuerdo con"),
    ("por ejemplo", "a modo de ejemplo"),
    ("finalmente", "por último"),
    ("también", "igualmente"),
]

# Sinónimos de una palabra (forma exacta en minúscula -> reemplazo).
SYNONYMS = {
    "estudiantes": "alumnos",
    "estudiante": "alumno",
    "alumnos": "estudiantes",
    "docentes": "profesores",
    "docente": "profesor",
    "profesores": "docentes",
    "maestros": "educadores",
    "niños": "infantes",
    "desarrollo": "progreso",
    "importante": "relevante",
    "importantes": "relevantes",
    "investigación": "indagación",
    "estudio": "trabajo",
    "proceso": "procedimiento",
    "procesos": "procedimientos",
    "capacidad": "aptitud",
    "capacidades": "aptitudes",
    "habilidades": "destrezas",
    "habilidad": "destreza",
    "conocimiento": "saber",
    "conocimientos": "saberes",
    "manera": "forma",
    "forma": "manera",
    "permite": "posibilita",
    "permiten": "posibilitan",
    "utilizar": "emplear",
    "utiliza": "emplea",
    "utilizan": "emplean",
    "uso": "empleo",
    "objetivo": "propósito",
    "objetivos": "propósitos",
    "problema": "dificultad",
    "problemas": "dificultades",
    "mejorar": "optimizar",
    "aspectos": "elementos",
    "aspecto": "elemento",
    "fundamental": "esencial",
    "fundamentales": "esenciales",
    "principal": "primordial",
    "principales": "primordiales",
    "diferentes": "distintos",
    "diversas": "variadas",
    "diversos": "variados",
    "actividades": "tareas",
    "actividad": "tarea",
    "señala": "indica",
    "señalan": "indican",
    "menciona": "refiere",
    "afirma": "sostiene",
    "define": "concibe",
    "considera": "estima",
    "consideran": "estiman",
    "realizar": "efectuar",
    "realiza": "efectúa",
    "lograr": "alcanzar",
    "logro": "alcance",
    "nivel": "grado",
    "niveles": "grados",
    "ámbito": "campo",
    "entorno": "medio",
    "sociedad": "comunidad",
    "rápidamente": "velozmente",
    "constantemente": "continuamente",
    "generalmente": "habitualmente",
    "cada": "todo",
    "muy": "sumamente",
    "gran": "enorme",
    "grandes": "enormes",
    "nuevas": "novedosas",
    "nuevos": "novedosos",
    "propia": "particular",
    "propio": "particular",
}


SPANISH_MARKERS = set("de la el en que los las del por con para una se es al como su más".split())
ENGLISH_MARKERS = set("the and of to in is that for with are this as be by".split())


def looks_spanish(tokens: list[str]) -> bool:
    lowered = [token.lower() for token in tokens]
    spanish = sum(1 for token in lowered if token in SPANISH_MARKERS)
    english = sum(1 for token in lowered if token in ENGLISH_MARKERS)
    return spanish >= max(2, english * 2)


def words(text: str) -> list[re.Match]:
    return list(WORD_PATTERN.finditer(text))


def count_words(text: str) -> int:
    return sum(1 for _ in WORD_PATTERN.finditer(text))


def split_sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text).strip()

    if not flat:
        return []

    return [sentence.strip() for sentence in SENTENCE_SPLIT.split(flat) if sentence.strip()]


def is_prose_sentence(sentence: str) -> bool:
    """
    Oración de prosa copiable: sin líneas de puntos (índices), sin exceso de
    números (tablas) y con al menos 6 palabras.
    """
    if "...." in sentence or "…" in sentence or "|" in sentence:
        return False

    tokens = [match.group(0) for match in WORD_PATTERN.finditer(sentence)]

    if len(tokens) < 6:
        return False

    alphabetic = sum(1 for token in tokens if token.isalpha())
    lowercase = sum(1 for token in tokens if token.isalpha() and token.islower())

    return (
        alphabetic / len(tokens) >= 0.9
        and lowercase / len(tokens) >= 0.6
        and looks_spanish(tokens)
    )


def passage_candidates(
    text: str,
    min_words: int = PASSAGE_MIN_WORDS,
    max_words: int = PASSAGE_MAX_WORDS,
) -> list[str]:
    """
    Pasajes de oraciones de prosa seguidas con `min_words`-`max_words`
    palabras, sin solaparse entre sí.
    """
    sentences = split_sentences(text)
    passages: list[str] = []
    index = 0

    while index < len(sentences):
        chosen: list[str] = []
        total = 0
        cursor = index

        while cursor < len(sentences) and is_prose_sentence(sentences[cursor]):
            size = count_words(sentences[cursor])

            if total + size > max_words:
                break

            chosen.append(sentences[cursor])
            total += size
            cursor += 1

            if total >= min_words:
                break

        if total >= min_words and len(chosen) >= 2:
            passages.append(" ".join(chosen))
            index = cursor
        else:
            index += 1

    return passages


def pick_passage(text: str, rng: random.Random) -> str | None:
    candidates = passage_candidates(text)

    if not candidates:
        return None

    return rng.choice(candidates)


def pick_phrase(text: str, rng: random.Random, min_words: int, max_words: int) -> str | None:
    """Tramo de `min_words`-`max_words` palabras dentro de una oración de prosa."""
    sentences = [
        sentence
        for sentence in split_sentences(text)
        if is_prose_sentence(sentence) and count_words(sentence) >= max_words + 2
    ]

    if not sentences:
        return None

    sentence = rng.choice(sentences)
    tokens = words(sentence)
    length = rng.randint(min_words, max_words)
    start = rng.randrange(0, len(tokens) - length + 1)
    end = tokens[start + length - 1].end()

    # El patrón de palabras no incluye acentos combinantes (texto de PDF sin
    # normalizar): se extiende el corte hasta el final real de la palabra.
    while end < len(sentence) and (
        sentence[end].isalnum() or unicodedata.category(sentence[end]).startswith("M")
    ):
        end += 1

    return sentence[tokens[start].start() : end]


def _match_case(original: str, replacement: str) -> str:
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _replace_connectors(sentence: str) -> str:
    for source, target in CONNECTORS:
        pattern = re.compile(rf"\b{re.escape(source)}\b", flags=re.IGNORECASE)
        match = pattern.search(sentence)

        if match:
            return (
                sentence[: match.start()]
                + _match_case(match.group(0), target)
                + sentence[match.end() :]
            )

    return sentence


def _replace_synonyms(sentence: str) -> str:
    def replace(match: re.Match) -> str:
        word = match.group(0)
        target = SYNONYMS.get(word.lower())
        return _match_case(word, target) if target else word

    return WORD_PATTERN.sub(replace, sentence)


def paraphrase(passage: str) -> str:
    """
    Paráfrasis por reglas, sin IA: cambia un conector por oración,
    reemplaza las palabras del diccionario de sinónimos y mueve la primera
    oración al final.
    """
    sentences = [
        _replace_synonyms(_replace_connectors(sentence))
        for sentence in split_sentences(passage)
    ]

    if len(sentences) >= 2:
        sentences = sentences[1:] + sentences[:1]

    return " ".join(sentences)


def split_excerpts(text: str, excerpt_words: int, skip_ratio: float = 0.1) -> list[str]:
    """
    Divide el texto en fragmentos de párrafos seguidos de unas
    `excerpt_words` palabras, sin solaparse, saltando el primer
    `skip_ratio` del documento (portada, índice).
    """
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    total = sum(count_words(paragraph) for paragraph in paragraphs)
    skip = int(total * skip_ratio)

    excerpts: list[str] = []
    current: list[str] = []
    current_words = 0
    seen = 0

    for paragraph in paragraphs:
        size = count_words(paragraph)
        seen += size

        if seen <= skip:
            continue

        current.append(paragraph)
        current_words += size

        if current_words >= excerpt_words:
            excerpts.append("\n\n".join(current))
            current = []
            current_words = 0

    return excerpts
