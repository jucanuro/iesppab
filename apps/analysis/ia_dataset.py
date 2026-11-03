"""
Dataset para entrenar el clasificador de texto generado por IA
(AI_DETECTION_MODE=classifier). Todo lo de aquí corre en local: plan de
prompts con semilla fija, limpieza de los textos de IA, paso por el MISMO
extractor de PDF que las tesis humanas y segmentación en prosa evaluable
de 150-250 palabras.

La regla central es la simetría: humanos e IA pasan por
`normalize_pdf_pages` -> `AcademicTextFilter` -> `build_segments` ->
`normalize_segment_text`. Si la IA entrara como .txt limpio y los humanos
como PDF, el modelo aprendería "texto limpio = IA". Por eso los textos de
IA se maquetan como tesis en PDF (WeasyPrint, con guiones de corte y
número de página) y se vuelven a extraer.
"""

from __future__ import annotations

import io
import random
import re
import statistics
from dataclasses import dataclass

from apps.analysis.engines import ai_segments
from apps.analysis.engines.text_filters import AcademicTextFilter

# --- Plan de prompts -------------------------------------------------------

TOPICS = (
    "la comprensión lectora en estudiantes de primaria de zonas rurales",
    "la producción de textos narrativos en segundo grado de primaria",
    "la conciencia fonológica en niños de 5 años",
    "la psicomotricidad gruesa en educación inicial",
    "la motricidad fina mediante el modelado con plastilina en niños de 4 años",
    "el juego simbólico y el desarrollo del lenguaje en inicial",
    "la resolución de problemas aditivos en tercer grado de primaria",
    "el uso del ábaco para la enseñanza del sistema de numeración decimal",
    "las regletas de Cuisenaire en la enseñanza de fracciones",
    "el pensamiento crítico en estudiantes de secundaria",
    "la indagación científica en el área de Ciencia y Tecnología",
    "los huertos escolares como estrategia de educación ambiental",
    "la educación intercultural bilingüe en comunidades quechuahablantes",
    "la enseñanza del castellano como segunda lengua en la Amazonía peruana",
    "la convivencia escolar y la prevención del acoso entre pares",
    "la autoestima y el rendimiento académico en adolescentes",
    "la inteligencia emocional de los docentes de educación básica",
    "el síndrome de burnout en docentes de instituciones públicas",
    "el liderazgo pedagógico del director y el desempeño docente",
    "el acompañamiento pedagógico a docentes rurales multigrado",
    "las escuelas multigrado y la planificación curricular",
    "la evaluación formativa y la retroalimentación en el aula",
    "las competencias digitales de los docentes tras la pandemia",
    "el aprendizaje remoto mediante la estrategia Aprendo en Casa",
    "el uso de tablets y recursos digitales en primaria",
    "la gamificación en la enseñanza de la matemática",
    "el aprendizaje cooperativo en el área de Personal Social",
    "el aprendizaje basado en proyectos en secundaria",
    "la participación de los padres de familia en la escuela",
    "la anemia infantil y el desarrollo cognitivo en la sierra peruana",
    "la alimentación escolar Qali Warma y la atención en clase",
    "los hábitos de estudio de estudiantes de un instituto pedagógico",
    "la gestión del tiempo en estudiantes de formación inicial docente",
    "la práctica preprofesional de los futuros docentes",
    "las canciones infantiles en el aprendizaje del inglés en inicial",
    "los cuentos andinos y la identidad cultural en primaria",
    "las danzas folclóricas en el desarrollo de la expresión corporal",
    "los juegos tradicionales peruanos en educación física",
    "la educación inclusiva de estudiantes con discapacidad",
    "la atención a estudiantes con trastorno por déficit de atención",
    "la dislexia y las dificultades de lectura en los primeros grados",
    "el clima del aula y la motivación por aprender",
    "la disciplina positiva en el nivel inicial",
    "la lectura en voz alta y el vocabulario en primaria",
    "la biblioteca de aula y el hábito lector",
    "el uso excesivo del celular y la atención en estudiantes",
    "la educación sexual integral en secundaria",
    "la tutoría y la orientación educativa en secundaria",
    "la ciudadanía y la participación estudiantil en el municipio escolar",
    "la deserción escolar en estudiantes de secundaria rural",
    "las estrategias metacognitivas en la comprensión de textos",
    "los organizadores gráficos en el aprendizaje de Ciencias Sociales",
    "el material reciclado en la enseñanza de la geometría",
    "la expresión oral mediante títeres en niños de 3 años",
    "la creatividad a través de las artes plásticas en inicial",
    "el aprendizaje significativo de Ausubel en las ciencias naturales",
    "la educación financiera en estudiantes de secundaria",
    "la resiliencia de los estudiantes tras la emergencia sanitaria",
    "el trabajo colegiado docente en las comunidades de aprendizaje",
    "la escritura académica en estudiantes de educación superior pedagógica",
)

SECTIONS = (
    "la introducción",
    "el planteamiento del problema",
    "la justificación teórica, práctica y metodológica",
    "los antecedentes internacionales y nacionales",
    "el marco teórico",
    "las bases teóricas",
    "la metodología",
    "la descripción de los resultados",
    "la discusión de resultados",
    "las conclusiones y recomendaciones",
)

LEVELS = (
    "una tesis de licenciatura en educación",
    "un trabajo de investigación de un instituto pedagógico",
    "una tesis de maestría en educación",
)

STYLE_NORMAL = "normal"
STYLE_STUDENT = "estudiante"
STYLE_HUMANIZED = "humanizado"
STYLE_WEIGHTS = ((STYLE_NORMAL, 0.40), (STYLE_STUDENT, 0.30), (STYLE_HUMANIZED, 0.30))

STYLE_INSTRUCTIONS = {
    STYLE_NORMAL: (
        "Escríbelo en prosa académica formal.",
        "Usa un registro académico, en párrafos.",
        "Redáctalo con estilo académico y cita autores en formato APA (Autor, año).",
    ),
    STYLE_STUDENT: (
        "Escríbelo como lo haría un estudiante peruano de último ciclo, con "
        "lenguaje sencillo y algunas repeticiones; que suene natural, no perfecto.",
        "Soy estudiante y no quiero que suene a robot: escribe con estilo "
        "sencillo, como si yo lo hubiera redactado.",
        "Redáctalo como una estudiante de un instituto pedagógico, no como un experto.",
    ),
    STYLE_HUMANIZED: (
        "Escríbelo de forma que no sea detectado como texto generado por IA: "
        "varía la longitud de las oraciones e incluye alguna imperfección natural.",
        "Hazlo indetectable para Turnitin y GPTZero: escribe como un humano.",
        "Redáctalo y luego reescríbelo con un estilo más humano y menos "
        "predecible; entrégame solo la versión reescrita.",
        "Humaniza el texto para que ningún detector de IA lo marque.",
        "Usa un estilo humano, con oraciones de distinta longitud y alguna "
        "expresión coloquial peruana.",
    ),
}

FORMAT_INSTRUCTIONS = (
    "Sin títulos ni viñetas.",
    "En párrafos corridos.",
    "",
)

PLAN_SEED_GEMINI = 20261102
PLAN_SEED_CLAUDE = 20261103


@dataclass(frozen=True)
class PromptSpec:
    id: str
    tema: str
    seccion: str
    estilo: str
    palabras: int
    prompt: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "tema": self.tema,
            "seccion": self.seccion,
            "estilo": self.estilo,
            "palabras": self.palabras,
            "prompt": self.prompt,
        }


def build_plan(count: int, *, seed: int, prefix: str) -> list[PromptSpec]:
    """Combinación al azar (reproducible) de tema × sección × estilo."""
    rng = random.Random(seed)
    styles = [style for style, _ in STYLE_WEIGHTS]
    weights = [weight for _, weight in STYLE_WEIGHTS]
    plan = []

    for index in range(1, count + 1):
        topic = rng.choice(TOPICS)
        section = rng.choice(SECTIONS)
        style = rng.choices(styles, weights=weights)[0]
        words = rng.randrange(800, 1201, 50)
        level = rng.choice(LEVELS)
        style_text = rng.choice(STYLE_INSTRUCTIONS[style])
        format_text = rng.choice(FORMAT_INSTRUCTIONS)
        prompt = (
            f"Redacta {section} de {level} sobre {topic}. "
            f"Unas {words} palabras. {style_text} {format_text}"
        ).strip()
        plan.append(
            PromptSpec(
                id=f"{prefix}_{index:03d}",
                tema=topic,
                seccion=section,
                estilo=style,
                palabras=words,
                prompt=prompt,
            )
        )

    return plan


def chat_style(number: int) -> str:
    """Estilo de los 30 prompts de chat (eval_ia/ia_externa)."""
    if number <= 18:
        return STYLE_NORMAL
    if number <= 24:
        return STYLE_STUDENT
    return STYLE_HUMANIZED


# --- Limpieza de la salida de un chat/API ---------------------------------

_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"(\*\*|__)(.+?)\1|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)")
_MD_BULLET_RE = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_MD_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_PREAMBLE_RE = re.compile(
    r"^\s*(?:¡?claro|aqu[ií] tienes|por supuesto|con gusto|a continuaci[oó]n)[^\n]*\n",
    re.IGNORECASE,
)


def clean_generated_text(text: str) -> str:
    """Quita markdown y la frase de cortesía inicial; deja los párrafos."""
    text = text.replace("\r\n", "\n")
    text = _PREAMBLE_RE.sub("", text, count=1)
    text = _MD_RULE_RE.sub("", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("- ", text)
    text = _MD_EMPHASIS_RE.sub(lambda m: m.group(2) or m.group(3) or "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- Maquetación como tesis en PDF y re-extracción -------------------------

_PDF_CSS = """
@page { size: A4; margin: 2.5cm 2.5cm 2.5cm 3cm;
        @bottom-center { content: counter(page); font: 11pt 'Times New Roman', serif; } }
body { font: 12pt/1.5 'Times New Roman', 'Liberation Serif', serif; text-align: justify;
       hyphens: auto; }
p { margin: 0 0 0.6em 0; text-indent: 1.25cm; }
p.titulo { text-indent: 0; font-weight: bold; }
p.lista { text-indent: 0; margin-left: 0.8cm; }
"""


def _html_paragraphs(text: str) -> str:
    from html import escape

    parts = []
    for block in re.split(r"\n\s*\n", text):
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            words = len(line.split())
            if line.startswith("- ") or re.match(r"^\d+[.)]\s", line):
                css = "lista"
            elif words <= 12 and not line.endswith((".", ":", "?", "!", ";")):
                css = "titulo"
            else:
                css = ""
            parts.append(f'<p class="{css}">{escape(line)}</p>')
    return "\n".join(parts)


def render_as_thesis_pdf(text: str) -> bytes:
    from weasyprint import HTML

    html = (
        '<html lang="es"><head><meta charset="utf-8"><style>'
        f"{_PDF_CSS}</style></head><body>{_html_paragraphs(text)}</body></html>"
    )
    return HTML(string=html).write_pdf()


def pdf_bytes_to_text(pdf_bytes: bytes) -> str:
    """Igual que la cosecha OAI: `read_pdf_pages` + `normalize_pdf_pages`."""
    from apps.documents.extractors import normalize_pdf_pages, read_pdf_pages

    return normalize_pdf_pages(read_pdf_pages(io.BytesIO(pdf_bytes)))


def generated_text_via_pdf(text: str) -> str:
    return pdf_bytes_to_text(render_as_thesis_pdf(clean_generated_text(text)))


# --- Segmentos de prosa y normalización compartida -------------------------

MIN_SEGMENT_WORDS = 100

_CITATION_PAREN_RE = re.compile(
    r"\s?\((?=[^()]{0,200}\b(?:1[89]\d\d|20\d\d|s\.\s?f\.)\b)[^()]{1,200}\)"
)
_CITATION_BRACKET_RE = re.compile(r"\s?\[\d+(?:[,–-]\s?\d+)*\]")
_URL_RE = re.compile(r"(?:https?://|www\.|doi:\s?)\S+", re.IGNORECASE)
_HEADING_NUMBER_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.)\s+")
_SENTENCE_PUNCT = (".", ":", ";", "?", "!", "»", "”", '"', ")")
_PAGE_NUMBER_LINE_RE = re.compile(r"^\s*\d{1,3}\s*$")
_HYPHEN_SPLIT_RE = re.compile(r"(\w)-\s+([a-záéíóúñü])")
_PRIVATE_GLYPH_RE = re.compile(r"[\uf000-\uf8ff•▪●◦■□➢✓►]")
_STRAY_TOKEN_RE = re.compile(r"(?<!\S)(?:-|=|<|>|&|\*|/)(?!\S)")
_UPPER_RUN_RE = re.compile(r"(?:\b[A-ZÁÉÍÓÚÑÜ]{2,}\b[ ,:]*){3,}")
_SPLIT_LETTER_RE = re.compile(r"(?<!\S)[b-df-hj-np-tv-xzñ](?!\S)")
# Tolerancia de letras sueltas (palabras partidas por el PDF: "p ositiva",
# "e l") por cada 100 palabras: por encima, el segmento se descarta en
# ambas clases porque esa huella solo existe en PDFs, no en DOCX ni en IA.
MAX_SPLIT_LETTERS_PER_100 = 0.5
# Lo que la prueba de solo formato mostró que separa las clases por el
# origen del texto (PDF de tesis real vs. texto generado) y no por el
# estilo: cifras, siglas/mayúsculas, paréntesis, comillas sueltas y la
# puntuación de listas (";" y ":"). Se neutraliza igual en ambas clases y
# en la inferencia.
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,:/]\d+)*(?:\s?%|º|°|er|ro|do|to|vo|mo|no)?(?!\w)")
_ANY_PAREN_RE = re.compile(r"\s?\([^()]{0,300}\)")
_LIST_PUNCT_RE = re.compile(r"\s*[;:]\s*")
_QUOTES = str.maketrans({"“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'",
                         "–": "-", "—": "-", "…": "..."})


def normalize_segment_text(text: str) -> str:
    """
    Normalización final, idéntica para humanos e IA (y para la inferencia):
    sin títulos sueltos ni números de página, guiones de corte unidos, sin
    citas (Autor, año), sin URLs, comillas y rayas uniformes y todo en una
    sola línea. El modelo no ve saltos de línea.
    """
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or _PAGE_NUMBER_LINE_RE.match(stripped):
            continue
        if len(stripped.split()) <= 12 and not stripped.endswith(_SENTENCE_PUNCT):
            continue
        lines.append(_HEADING_NUMBER_RE.sub("", stripped))

    text = " ".join(lines).translate(_QUOTES)
    text = _PRIVATE_GLYPH_RE.sub(" ", text)
    text = _UPPER_RUN_RE.sub(" ", text)
    text = _STRAY_TOKEN_RE.sub(" ", text)
    text = _HYPHEN_SPLIT_RE.sub(r"\1\2", text)
    text = _URL_RE.sub("", text)
    text = _CITATION_PAREN_RE.sub("", text)
    text = _CITATION_BRACKET_RE.sub("", text)
    text = _ANY_PAREN_RE.sub("", text).replace("(", " ").replace(")", " ")
    text = _NUMBER_RE.sub(" ", text)
    text = text.replace('"', " ").replace("'", " ")
    text = _LIST_PUNCT_RE.sub(" ", text)
    text = re.sub(r"\s+([.,])", r"\1", text)
    text = re.sub(r",(\s*,)+", ",", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def prose_segments(raw_text: str) -> list[str]:
    """
    Segmentos de prosa evaluable de 150-250 palabras (`build_segments`, sin
    portada, índice, bibliografía, tablas, listas ni texto repetido), ya
    normalizados. Es lo que se usa tanto para el dataset como para puntuar.
    """
    text_filter = AcademicTextFilter()
    content = text_filter.filter_for_similarity(content=raw_text).content or raw_text
    segments = ai_segments.build_segments(
        content,
        excluded_ranges=text_filter.find_quoted_ranges(content),
    )
    result = []
    for segment in segments:
        if not segment.evaluable:
            continue
        normalized = normalize_segment_text(segment.evaluable_text)
        for piece in _split_long(normalized):
            if len(piece.split()) >= MIN_SEGMENT_WORDS and not has_pdf_artifacts(piece):
                result.append(_SPLIT_LETTER_RE.sub("", piece).replace("  ", " "))
    return result


def split_letter_rate(text: str) -> float:
    """Letras sueltas (consonantes) por cada 100 palabras."""
    return len(_SPLIT_LETTER_RE.findall(text)) * 100 / max(len(text.split()), 1)


def has_pdf_artifacts(text: str) -> bool:
    return split_letter_rate(text) > MAX_SPLIT_LETTERS_PER_100


def _split_long(text: str) -> list[str]:
    """
    `build_segments` puede dejar grupos de hasta ~320 palabras (un resto de
    150 + un párrafo largo). Se parten por oraciones en trozos parejos para
    que todo quede en 150-250 palabras, igual en ambas clases.
    """
    words = len(text.split())
    if words <= ai_segments.SEGMENT_MAX_WORDS:
        return [text]

    parts = -(-words // ai_segments.SEGMENT_MAX_WORDS)
    target = words / parts
    pieces: list[str] = []
    current: list[str] = []
    current_words = 0

    for sentence in ai_segments.split_sentences(text):
        current.append(sentence)
        current_words += len(sentence.split())
        if current_words >= target and len(pieces) < parts - 1:
            pieces.append(" ".join(current))
            current, current_words = [], 0

    if current:
        pieces.append(" ".join(current))
    return pieces


def overlapping_windows(
    segments: list[str],
    *,
    size: int = 200,
    stride: int = 100,
) -> list[str]:
    """
    Ventanas solapadas (por oraciones) sobre los segmentos de un mismo
    documento, para aumentar el train de IA. Nunca se usan en test.
    """
    sentences = [
        sentence
        for segment in segments
        for sentence in ai_segments.split_sentences(segment)
        if sentence
    ]
    lengths = [len(sentence.split()) for sentence in sentences]
    windows: list[str] = []
    start = 0

    while start < len(sentences):
        words = 0
        end = start
        while end < len(sentences) and words < size:
            words += lengths[end]
            end += 1
        if words >= MIN_SEGMENT_WORDS:
            windows.append(" ".join(sentences[start:end]))
        if end >= len(sentences):
            break
        advanced = 0
        while start < end - 1 and advanced < stride:
            advanced += lengths[start]
            start += 1
        if advanced == 0:
            start += 1

    return windows


# --- Prueba de "solo formato" ----------------------------------------------

# Rasgos de FORMATO (lo que no debería distinguir las clases) y de ESTILO
# (señal legítima: longitud de palabra y variación entre oraciones, el
# "burstiness" que usan los detectores). La prueba de solo formato usa los
# primeros; los de estilo se reportan aparte, como referencia.
STYLE_FEATURE_NAMES = (
    "letras_por_palabra",
    "palabras_por_oracion",
    "desv_palabras_por_oracion",
)
FORMAT_FEATURE_NAMES = (
    "palabras",
    "comas",
    "punto_y_coma",
    "dos_puntos",
    "parentesis",
    "comillas",
    "guiones",
    "digitos",
    "mayusculas",
    "tokens_de_una_letra",
)


def style_features(text: str) -> list[float]:
    words = text.split()
    sentence_lengths = [len(s.split()) for s in ai_segments.split_sentences(text)] or [0]
    return [
        sum(ch.isalpha() for ch in text) / max(len(words), 1),
        statistics.mean(sentence_lengths),
        statistics.pstdev(sentence_lengths),
    ]


def format_features(text: str) -> list[float]:
    chars = max(len(text), 1)
    words = text.split()
    n_words = max(len(words), 1)
    single = sum(1 for w in words if len(w.strip(".,;:()\"'¿?¡!")) == 1
                 and w.strip(".,;:()\"'¿?¡!").lower() not in {"a", "y", "o", "e", "u"})
    return [
        float(len(words)),
        text.count(",") / chars * 100,
        text.count(";") / chars * 100,
        text.count(":") / chars * 100,
        (text.count("(") + text.count(")")) / chars * 100,
        text.count('"') / chars * 100,
        text.count("-") / chars * 100,
        sum(ch.isdigit() for ch in text) / chars * 100,
        sum(ch.isupper() for ch in text) / chars * 100,
        single / n_words * 100,
    ]
