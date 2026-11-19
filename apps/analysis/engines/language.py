"""
Detección simple de idioma (español / inglés) por proporción de palabras
vacías, sin dependencias nuevas. Sirve para sacar del clasificador de IA los
segmentos en otro idioma (resúmenes en inglés, textos citados, etc.): el
modelo se entrenó con prosa en español y fuera de ella no es fiable.
"""

from __future__ import annotations

import re

REASON_OTHER_LANGUAGE = "otro-idioma"

# Palabras vacías frecuentes. Se omiten las que existen en ambos idiomas
# ("a", "no", "me", "he", "son", "con"...) para no confundir el recuento.
_SPANISH = frozenset(
    "de la que el en los las del se por un una para con es al lo como más "
    "pero sus le ya o fue este sí porque esta entre cuando muy sin sobre "
    "también me hasta hay donde quien desde todo nos durante todos uno les "
    "ni contra otros ese eso ante ellos e esto mí antes algunos qué unos "
    "yo otro otras otra él tanto esa estos mucho quienes nada muchos cual "
    "poco ella estar estas algunas algo nosotros mi mis tú te ti tu tus "
    "ellas nosotras vosotros vosotras os mío mía míos mías tuyo tuya suyo "
    "suya nuestro nuestra son ser fue han ha sido su".split()
)
_ENGLISH = frozenset(
    "the of and to in is that it for was on are as with his they at be this "
    "from or had by hot but some what there we can out other were all your "
    "when up use how said an each she which do their time if will way about "
    "many then them would these so has been its who more than into only "
    "between through also our such not have not those both".split()
)

# Con menos palabras no se decide: se deja pasar como español.
MIN_WORDS = 20
# Una proporción de palabras vacías en español por debajo de esto y menor
# que la del inglés marca el segmento como "otro idioma".
SPANISH_RATIO_CEILING = 0.15

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def stopword_ratios(text: str) -> tuple[float, float]:
    words = [word.lower() for word in _WORD_RE.findall(text)]
    if not words:
        return 0.0, 0.0
    spanish = sum(word in _SPANISH for word in words)
    english = sum(word in _ENGLISH for word in words)
    return spanish / len(words), english / len(words)


def is_other_language(text: str) -> bool:
    if len(_WORD_RE.findall(text)) < MIN_WORDS:
        return False
    spanish, english = stopword_ratios(text)
    return spanish < SPANISH_RATIO_CEILING and english > spanish
