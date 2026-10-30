from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from apps.analysis.engines import perplexity_detector as pd

# Detección de IA por segmentos (AI_DETECTION_MODE=segments), estilo
# Turnitin: en vez de UNA perplejidad para todo el documento (que además
# `compute_perplexity` trunca a los primeros MAX_TOKENS=900 tokens, ~640
# palabras), el texto completo se parte en segmentos de ~150-250 palabras
# respetando párrafos, cada segmento recibe su propio score y el % del
# documento es el % de palabras evaluables que caen en segmentos con
# score >= SEGMENT_AI_THRESHOLD.
#
# El score de cada segmento usa la perplejidad del segmento COMPLETO (con
# contexto), no la mediana por oración: la auditoría sobre la tesis
# Mantilla mostró que una oración evaluada aislada da ~1.5x la perplejidad
# contextual, y PERPLEXITY_MIDPOINT/SCALE se calibraron sobre párrafos
# completos de ~160 palabras (banco de pruebas: IA mediana 56, humano 143).
# La mediana por oración y el burstiness se guardan solo como diagnóstico.
SEGMENT_MIN_WORDS = 150
SEGMENT_TARGET_WORDS = 200
SEGMENT_MAX_WORDS = 250

SEGMENT_AI_THRESHOLD = Decimal("50.00")

# Un segmento con más de esta fracción de palabras en inglés o "ruido
# técnico" (siglas, números, operadores booleanos) no se evalúa: la
# perplejidad de un modelo en español no dice nada útil sobre ese texto.
MAX_NOISE_RATIO = 0.30

# Segmentos que, tras descontar citas y títulos de tablas, quedan con muy
# pocas palabras dan una perplejidad ruidosa: tampoco se evalúan.
MIN_EVALUABLE_WORDS = 40

# Oraciones más cortas que esto no entran en la mediana por oración ni en
# el burstiness (equivalente en palabras a MIN_SENTENCE_TOKENS).
MIN_SENTENCE_WORDS = 5

REASON_NOISE = "ruido"
REASON_FEW_WORDS = "pocas-palabras"
REASON_NO_PERPLEXITY = "sin-perplejidad"
REASON_REPEATED = "repetido"
REASON_SESSION_PLAN = "plan-de-sesion"
REASON_TABLE = "tabla"
REASON_LIST = "lista"

# --- Solo cuenta la prosa corrida ---
#
# En las tesis del instituto, los segmentos con más "IA" eran sobre todo
# texto que no es prosa: objetivos reformulados casi idénticos, filas de
# tablas de DOCX con celdas combinadas repetidas ("COMPETENCIA COMPETENCIA
# ..."), competencias del MINEDU copiadas en mayúsculas y planes de
# sesión. Ese texto es predecible para GPT-2 (perplejidad baja) sin que
# nadie lo haya generado con IA, así que no entra ni en el numerador ni
# en el denominador.

# Un segmento con más de esta fracción de sus 8-gramas ya vistos antes en
# el documento (o antes dentro del mismo segmento) es repetición.
REPEATED_NGRAM_SIZE = 8
MAX_REPEATED_NGRAM_RATIO = 0.50

# Lista/enumeración: al menos esta fracción de las palabras del segmento
# está en líneas con forma de ítem (ver `_is_list_line`).
MIN_LIST_WORD_RATIO = 0.50

# Tabla: esta fracción de las palabras en filas cortas sin puntuación final
# o con muchos números, si el segmento trae un rótulo "Tabla N"; o, sin
# rótulo, esta otra fracción en filas con muchos números.
MIN_TABLE_WORD_RATIO_WITH_LABEL = 0.50
MIN_TABLE_WORD_RATIO = 0.40
TABLE_ROW_MIN_NUMERIC_RATIO = 0.15

# Plan de sesión: al menos esta cantidad de rótulos distintos de
# `_SESSION_PLAN_MARKERS` (en mayúsculas o al inicio de línea).
MIN_SESSION_PLAN_MARKERS = 3

# Líneas de menos palabras que esto y sin puntuación final son títulos o
# celdas, no prosa.
SHORT_LINE_WORDS = 8

# Un ítem (objetivo, pregunta, viñeta) no pasa de este largo: un párrafo de
# prosa rotulado "A. Dimensión física: ..." con 100+ palabras sigue
# siendo prosa.
LIST_ITEM_MAX_WORDS = 60

# Abreviaturas tras las cuales un punto NO cierra la oración. Sin esto,
# "García Flores et al. (2022) señalan..." se cortaba en "et al." y daba
# oraciones sueltas con perplejidad altísima.
_ABBREVIATIONS = (
    "et al.", "p.", "pp.", "ej.", "vol.", "núm.", "no.", "ed.", "eds.",
    "dr.", "dra.", "sr.", "sra.", "aprox.", "fig.", "cf.", "vs.", "i.e.",
    "e.g.", "op. cit.", "ibíd.", "cap.",
)

_PARAGRAPH_RE = re.compile(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", re.DOTALL)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\S+")

# "Tabla 3" / "Figura 2." en su propia línea (el título viene en la línea
# siguiente), o "Tabla 3. Título" en una sola línea.
_FIGURE_LABEL_RE = re.compile(
    r"^[ \t]*(?:Tabla|Figura|Cuadro|Gráfico)\s+\d+[.:]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_FIGURE_INLINE_RE = re.compile(
    r"^[ \t]*(?:Tabla|Figura|Cuadro|Gráfico)\s+\d+[.:][^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Palabras funcionales del inglés que no existen como palabra en español
# (se omiten "a", "no", "son", etc.): sirven para reconocer oraciones en
# inglés (abstract, keywords, títulos citados).
_ENGLISH_WORDS = frozenset(
    "the of and in to for with on by from is are was were this that these "
    "those an as at be been or not which their its it using based study "
    "studies into than such has have had can between through also they "
    "we our results findings keywords abstract".split()
)
_ENGLISH_SENTENCE_RATIO = 0.20

# Verbos con los que empiezan los objetivos ("Determinar la influencia...").
_OBJECTIVE_VERBS = frozenset(
    "determinar identificar analizar establecer describir evaluar conocer "
    "comparar explicar demostrar proponer diseñar aplicar mejorar desarrollar "
    "fortalecer promover verificar medir relacionar precisar valorar elaborar "
    "implementar reconocer fomentar contribuir estimar examinar comprobar "
    "caracterizar indagar".split()
)
_LIST_MARKER_RE = re.compile(
    r"^(?:[•●▪◦■□➢►✓✔*\-–—\uf000-\uf8ff]|\d+(?:\.\d+)*[.)]|[a-zA-Z][.)]\s|"
    r"[IVX]+[.)]\s|¿)"
)
_HYPOTHESIS_RE = re.compile(
    r"^(?:H[oaie0-9]{1,2}|Hipótesis\s+(?:nula|alterna|general|específica)\w*)\s*[:.]",
    re.IGNORECASE,
)
_UPPERCASE_WORD_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ]{2,}[.,:;]?$")
_LINE_END_PUNCTUATION = (".", ":", ";", "?", "!", "»", "”", '"')

# Rótulos de un plan/sesión de aprendizaje (formato MINEDU).
_SESSION_PLAN_MARKERS = (
    "inicio", "desarrollo", "cierre", "momentos", "materiales", "recursos",
    "competencia", "competencias", "capacidades", "desempeño", "desempeños",
    "criterios de evaluación", "evidencia", "instrumento de evaluación",
    "enfoque transversal", "propósito", "sesión de aprendizaje",
    "retroalimentación", "acciones observables", "secuencia didáctica",
    "título de la sesión", "área curricular",
)
_SESSION_PLAN_MARKER_RE = re.compile(
    r"(?:^|\n)[ \t]*(" + "|".join(re.escape(m) for m in _SESSION_PLAN_MARKERS) + r")\b",
    re.IGNORECASE,
)
_SESSION_PLAN_UPPER_RE = re.compile(
    r"\b(" + "|".join(re.escape(m.upper()) for m in _SESSION_PLAN_MARKERS) + r")\b"
)
_SESSION_TIME_RE = re.compile(r"\(\s*\d+\s*(?:min|minutos)\b", re.IGNORECASE)
_NGRAM_WORD_RE = re.compile(r"\w+")

_SIGLA_RE = re.compile(r"^[A-ZÁÉÍÓÚÑ]{2,10}[0-9]*s?$")
_BOOLEAN_WORDS = frozenset({"AND", "OR", "NOT"})
_LETTER_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]")
_STRIP_PUNCT = "\"'“”‘’«»()[]{}.,;:¿?¡!"


@dataclass
class TextSegment:
    start: int
    end: int
    word_count: int
    evaluable_text: str
    evaluable_word_count: int
    noise_ratio: float
    sentences: list[str]
    perplexity: float | None = None
    sentence_median_perplexity: float | None = None
    burstiness: float | None = None
    repeated_ratio: float = 0.0
    excluded_reason: str = ""

    @property
    def evaluable(self) -> bool:
        return not self.excluded_reason


@dataclass
class SegmentedAIAnalysisResult:
    ai_probability_percent: Decimal
    segments: list[TextSegment] = field(default_factory=list)
    midpoint: float = pd.PERPLEXITY_MIDPOINT
    scale: float = pd.PERPLEXITY_SCALE

    @property
    def evaluable_segments(self) -> list[TextSegment]:
        return [segment for segment in self.segments if segment.evaluable]

    @property
    def evaluable_word_count(self) -> int:
        return sum(s.evaluable_word_count for s in self.evaluable_segments)

    def segment_score(self, segment: TextSegment) -> Decimal | None:
        if not segment.evaluable:
            return None
        return pd._logistic_score(
            segment.perplexity,
            midpoint=self.midpoint,
            scale=self.scale,
        )

    def to_breakdown(self) -> dict:
        """Forma serializable para `ai_score_breakdown` (visor/depuración)."""
        segments = []

        for segment in self.segments:
            score = self.segment_score(segment)
            segments.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "words": segment.word_count,
                    "evaluable_words": segment.evaluable_word_count,
                    "perplexity": _round(segment.perplexity),
                    "sentence_median_perplexity": _round(
                        segment.sentence_median_perplexity
                    ),
                    "burstiness": _round(segment.burstiness),
                    "noise_ratio": round(segment.noise_ratio, 3),
                    "repeated_ratio": round(segment.repeated_ratio, 3),
                    "score": str(score) if score is not None else None,
                    "evaluable": segment.evaluable,
                    "excluded_reason": segment.excluded_reason,
                }
            )

        excluded_words: dict[str, int] = {}
        for segment in self.segments:
            if not segment.evaluable:
                excluded_words[segment.excluded_reason] = (
                    excluded_words.get(segment.excluded_reason, 0) + segment.word_count
                )

        return {
            "segment_count": len(self.segments),
            "evaluable_segment_count": len(self.evaluable_segments),
            "evaluable_words": self.evaluable_word_count,
            "total_words": sum(segment.word_count for segment in self.segments),
            "excluded_words_by_reason": excluded_words,
            "midpoint": self.midpoint,
            "scale": self.scale,
            "threshold": str(SEGMENT_AI_THRESHOLD),
            "segments": segments,
        }


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """
    Offsets de cada oración dentro de `text`. Como `_sentence_split` del
    modo global, pero sin cortar tras abreviaturas académicas ("et al.",
    "pp.", ...); el modo global no se toca para no cambiar su
    comportamiento.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    boundaries = [(m.start(), m.end()) for m in _SENTENCE_END_RE.finditer(text)]

    for boundary_start, boundary_end in boundaries + [(len(text), len(text))]:
        piece = text[cursor:boundary_start]

        if piece.strip():
            if spans and text[spans[-1][0]:spans[-1][1]].lower().endswith(_ABBREVIATIONS):
                spans[-1] = (spans[-1][0], boundary_start)
            else:
                spans.append((cursor, boundary_start))

        cursor = boundary_end

    return spans


def split_sentences(text: str) -> list[str]:
    return [text[start:end].strip() for start, end in _sentence_spans(text)]


def _units(content: str) -> list[tuple[int, int, int]]:
    """
    Unidades mínimas (inicio, fin, palabras) a agrupar en segmentos: cada
    párrafo, salvo los que solos superan SEGMENT_MAX_WORDS, que se parten
    por oraciones (texto extraído sin saltos de párrafo, p. ej. resúmenes
    OAI o PDFs mal maquetados).
    """
    units = []

    for match in _PARAGRAPH_RE.finditer(content):
        paragraph = match.group(0)
        words = len(paragraph.split())

        if words <= SEGMENT_MAX_WORDS:
            units.append((match.start(), match.end(), words))
            continue

        for start, end in _sentence_spans(paragraph):
            units.append(
                (
                    match.start() + start,
                    match.start() + end,
                    len(paragraph[start:end].split()),
                )
            )

    return units


def _group_units(units: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    groups: list[list[tuple[int, int, int]]] = []
    current: list[tuple[int, int, int]] = []
    current_words = 0

    for unit in units:
        if (
            current
            and current_words + unit[2] > SEGMENT_MAX_WORDS
            and current_words >= SEGMENT_MIN_WORDS
        ):
            groups.append(current)
            current, current_words = [], 0

        current.append(unit)
        current_words += unit[2]

        if current_words >= SEGMENT_TARGET_WORDS:
            groups.append(current)
            current, current_words = [], 0

    if current:
        # Solo un resto muy corto se fusiona con el segmento anterior; uno
        # mediano queda solo para no crear segmentos de 300+ palabras.
        if groups and current_words < SEGMENT_MIN_WORDS // 2:
            groups[-1].extend(current)
        else:
            groups.append(current)

    return [
        (group[0][0], group[-1][1], sum(unit[2] for unit in group))
        for group in groups
    ]


def _figure_title_ranges(content: str) -> list[tuple[int, int]]:
    ranges = []

    for match in _FIGURE_LABEL_RE.finditer(content):
        # "Tabla 3" solo en su línea: el título es la línea siguiente.
        next_line_end = content.find("\n", match.end() + 1)
        if next_line_end == -1:
            next_line_end = len(content)
        ranges.append((match.start(), next_line_end))

    for match in _FIGURE_INLINE_RE.finditer(content):
        ranges.append((match.start(), match.end()))

    return ranges


def _evaluable_text(
    content: str,
    start: int,
    end: int,
    excluded_ranges: list[tuple[int, int]],
) -> str:
    """Texto del segmento sin citas ni títulos de tablas/figuras."""
    pieces = []
    cursor = start

    for range_start, range_end in sorted(excluded_ranges):
        if range_end <= cursor or range_start >= end:
            continue
        if range_start > cursor:
            pieces.append(content[cursor:range_start])
        cursor = max(cursor, range_end)

    if cursor < end:
        pieces.append(content[cursor:end])

    return re.sub(r"[ \t]+", " ", " ".join(pieces)).strip()


def _is_noise_token(word: str) -> bool:
    if word in _BOOLEAN_WORDS:
        return True

    stripped = word.strip(_STRIP_PUNCT)

    if not stripped:
        return False
    if not _LETTER_RE.search(stripped):
        return True  # números, porcentajes, símbolos
    if any(char.isdigit() for char in stripped):
        return True
    return bool(_SIGLA_RE.match(stripped))


def noise_ratio(sentences: list[str]) -> float:
    """
    Fracción de palabras en oraciones en inglés + palabras de ruido técnico
    (siglas, números, AND/OR/NOT) en el resto.
    """
    total = noisy = 0

    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue

        total += len(words)
        lowered = [word.strip(_STRIP_PUNCT).lower() for word in words]
        english = sum(1 for word in lowered if word in _ENGLISH_WORDS)

        if english / len(words) >= _ENGLISH_SENTENCE_RATIO:
            noisy += len(words)
        else:
            noisy += sum(1 for word in words if _is_noise_token(word))

    return noisy / total if total else 0.0


def repeated_ngram_ratio(text: str, seen: set[tuple[str, ...]]) -> float:
    """
    Fracción de los 8-gramas de `text` que ya estaban en `seen` (texto
    anterior del documento) o que aparecieron antes en el mismo `text`.
    Agrega los 8-gramas de `text` a `seen`.
    """
    words = _NGRAM_WORD_RE.findall(text.lower())
    size = REPEATED_NGRAM_SIZE
    total = repeated = 0

    for index in range(len(words) - size + 1):
        ngram = tuple(words[index:index + size])
        total += 1
        if ngram in seen:
            repeated += 1
        else:
            seen.add(ngram)

    return repeated / total if total else 0.0


def _ends_like_prose(line: str) -> bool:
    return line.endswith(_LINE_END_PUNCTUATION)


def _is_list_line(line: str) -> bool:
    """
    Línea con forma de ítem: viñeta o numeración, pregunta, objetivo
    ("Determinar..."), hipótesis ("Ho:", "H1:"), rótulo en MAYÚSCULAS
    (competencias del MINEDU) o línea corta sin puntuación final.
    """
    words = line.split()
    if not words or len(words) > LIST_ITEM_MAX_WORDS:
        return False

    if _LIST_MARKER_RE.match(line) or _HYPOTHESIS_RE.match(line):
        return True
    if words[0].strip(_STRIP_PUNCT).lower() in _OBJECTIVE_VERBS:
        return True
    if len(words) >= 3 and all(_UPPERCASE_WORD_RE.match(w) for w in words[:3]):
        return True
    return len(words) < SHORT_LINE_WORDS and not _ends_like_prose(line)


def _is_table_row(line: str) -> bool:
    words = line.split()
    if not words:
        return False

    numeric = sum(1 for word in words if any(char.isdigit() for char in word))
    return numeric / len(words) >= TABLE_ROW_MIN_NUMERIC_RATIO


def _share_of_words(lines: list[str], predicate) -> float:
    total = sum(len(line.split()) for line in lines)
    if not total:
        return 0.0
    return sum(len(line.split()) for line in lines if predicate(line)) / total


def structure_reason(raw_text: str) -> str:
    """
    Motivo por el que un segmento no es prosa corrida (plan de sesión,
    tabla o lista), o "" si lo es. `raw_text` es el texto del segmento con
    sus saltos de línea originales.
    """
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    if not lines:
        return ""

    markers = {m.group(1).lower() for m in _SESSION_PLAN_MARKER_RE.finditer(raw_text)}
    markers |= {m.group(1).lower() for m in _SESSION_PLAN_UPPER_RE.finditer(raw_text)}
    if _SESSION_TIME_RE.search(raw_text):
        markers.add("tiempo")
    if len(markers) >= MIN_SESSION_PLAN_MARKERS:
        return REASON_SESSION_PLAN

    has_table_label = bool(
        _FIGURE_LABEL_RE.search(raw_text) or _FIGURE_INLINE_RE.search(raw_text)
    )
    if has_table_label and _share_of_words(
        lines,
        lambda line: _is_table_row(line) or not _ends_like_prose(line),
    ) >= MIN_TABLE_WORD_RATIO_WITH_LABEL:
        return REASON_TABLE
    if _share_of_words(lines, _is_table_row) >= MIN_TABLE_WORD_RATIO:
        return REASON_TABLE

    if _share_of_words(lines, _is_list_line) >= MIN_LIST_WORD_RATIO:
        return REASON_LIST

    return ""


def build_segments(
    content: str,
    *,
    excluded_ranges: list[tuple[int, int]] | None = None,
) -> list[TextSegment]:
    """
    Parte `content` (el texto ya filtrado del análisis: sin portada ni
    bibliografía) en segmentos con offsets sobre ese mismo texto, para que
    el visor pueda pintarlos. `excluded_ranges` (las citas entre comillas)
    y los títulos de tablas/figuras se descuentan del texto evaluable.
    """
    excluded = list(excluded_ranges or []) + _figure_title_ranges(content)
    seen_ngrams: set[tuple[str, ...]] = set()
    segments = []

    for start, end, words in _group_units(_units(content)):
        text = _evaluable_text(content, start, end, excluded)
        sentences = split_sentences(text)
        evaluable_words = len(text.split())
        ratio = noise_ratio(sentences)
        # Siempre se calcula, aunque el segmento ya quede excluido por
        # otro motivo: su texto cuenta como "ya visto" para los siguientes.
        repeated = repeated_ngram_ratio(text, seen_ngrams)

        if ratio > MAX_NOISE_RATIO:
            reason = REASON_NOISE
        elif repeated > MAX_REPEATED_NGRAM_RATIO:
            reason = REASON_REPEATED
        else:
            reason = structure_reason(content[start:end])

        if not reason and evaluable_words < MIN_EVALUABLE_WORDS:
            reason = REASON_FEW_WORDS

        segments.append(
            TextSegment(
                start=start,
                end=end,
                word_count=words,
                evaluable_text=text,
                evaluable_word_count=evaluable_words,
                noise_ratio=ratio,
                sentences=sentences,
                repeated_ratio=repeated,
                excluded_reason=reason,
            )
        )

    return segments


def aggregate_ai_percent(
    segments: list[TextSegment],
    *,
    midpoint: float = pd.PERPLEXITY_MIDPOINT,
    scale: float = pd.PERPLEXITY_SCALE,
) -> Decimal:
    """
    % de palabras evaluables que caen en segmentos con score >= umbral. Los
    segmentos no evaluables no cuentan ni en el numerador ni en el
    denominador. Separado del cálculo de perplejidad para poder barrer
    MIDPOINT sin volver a correr el modelo.
    """
    evaluable_words = ai_words = 0

    for segment in segments:
        if not segment.evaluable:
            continue

        evaluable_words += segment.evaluable_word_count
        score = pd._logistic_score(segment.perplexity, midpoint=midpoint, scale=scale)

        if score >= SEGMENT_AI_THRESHOLD:
            ai_words += segment.evaluable_word_count

    if not evaluable_words:
        return Decimal("0.00")

    return (Decimal(ai_words) * 100 / Decimal(evaluable_words)).quantize(
        Decimal("0.01")
    )


class SegmentedPerplexityDetector:
    """
    Evalúa TODO el documento por segmentos (sin el tope de 900 tokens del
    modo global). Reutiliza el modelo y el batching por presupuesto de
    memoria de `perplexity_detector`.
    """

    def analyze(
        self,
        content: str,
        *,
        excluded_ranges: list[tuple[int, int]] | None = None,
        midpoint: float = pd.PERPLEXITY_MIDPOINT,
        scale: float = pd.PERPLEXITY_SCALE,
    ) -> SegmentedAIAnalysisResult:
        segments = build_segments(content, excluded_ranges=excluded_ranges)
        evaluable = [segment for segment in segments if segment.evaluable]

        segment_perplexities = pd._batch_sequence_perplexities(
            [segment.evaluable_text for segment in evaluable]
        )

        sentence_owner: list[TextSegment] = []
        sentence_texts: list[str] = []
        for segment in evaluable:
            for sentence in segment.sentences:
                if len(sentence.split()) >= MIN_SENTENCE_WORDS:
                    sentence_owner.append(segment)
                    sentence_texts.append(sentence)

        sentence_perplexities = pd._batch_sequence_perplexities(sentence_texts)

        per_segment: dict[int, list[float]] = {}
        for segment, perplexity in zip(sentence_owner, sentence_perplexities):
            if perplexity is not None:
                per_segment.setdefault(id(segment), []).append(perplexity)

        for segment, perplexity in zip(evaluable, segment_perplexities):
            if perplexity is None:
                segment.excluded_reason = REASON_NO_PERPLEXITY
                continue

            segment.perplexity = perplexity
            values = per_segment.get(id(segment), [])

            if values:
                segment.sentence_median_perplexity = statistics.median(values)
            if len(values) >= 2:
                segment.burstiness = statistics.stdev(
                    min(value, pd.MAX_SENTENCE_PERPLEXITY_FOR_BURSTINESS)
                    for value in values
                )

        return SegmentedAIAnalysisResult(
            ai_probability_percent=aggregate_ai_percent(
                segments,
                midpoint=midpoint,
                scale=scale,
            ),
            segments=segments,
            midpoint=midpoint,
            scale=scale,
        )
