"""
C3 · Métricas de evaluación de IA v3 (parte 1: FPR con Clopper-Pearson bilateral).

Python puro, sin scipy. El intervalo exacto se obtiene por bisección sobre la
CDF binomial. Constantes de meta: Decisión del Gerente (ver design.md §3, C3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .exceptions import IABankError

FPR_META = 0.02
DOC_UMBRAL_PCT = 20
CONFIANZA = 0.95
_ITERACIONES_BISECCION = 100


def _log_pmf(i: int, n: int, p: float) -> float:
    return (
        math.lgamma(n + 1)
        - math.lgamma(i + 1)
        - math.lgamma(n - i + 1)
        + i * math.log(p)
        + (n - i) * math.log1p(-p)
    )


def _cdf_binomial(k: int, n: int, p: float) -> float:
    """P(X <= k) para X ~ Binomial(n, p), con 0 < p < 1."""
    return min(1.0, sum(math.exp(_log_pmf(i, n, p)) for i in range(k + 1)))


def _validar(falsos_positivos: int, n: int) -> None:
    if n <= 0:
        raise IABankError("El tamaño de la muestra debe ser mayor que cero.")
    if not 0 <= falsos_positivos <= n:
        raise IABankError(
            f"Falsos positivos ({falsos_positivos}) fuera del rango 0..{n}."
        )


def clopper_pearson(
    falsos_positivos: int, n: int, confianza: float = CONFIANZA
) -> tuple[float, float]:
    """Intervalo de confianza exacto bilateral (límite inferior, límite superior)."""
    _validar(falsos_positivos, n)
    k = falsos_positivos
    cola = (1.0 - confianza) / 2.0

    if k == 0:
        inferior = 0.0
    else:
        # P(X >= k | p) = cola; P(X >= k) crece con p.
        lo, hi = 0.0, 1.0
        for _ in range(_ITERACIONES_BISECCION):
            medio = (lo + hi) / 2.0
            if 1.0 - _cdf_binomial(k - 1, n, medio) < cola:
                lo = medio
            else:
                hi = medio
        inferior = (lo + hi) / 2.0

    if k == n:
        superior = 1.0
    else:
        # P(X <= k | p) = cola; P(X <= k) decrece con p.
        lo, hi = 0.0, 1.0
        for _ in range(_ITERACIONES_BISECCION):
            medio = (lo + hi) / 2.0
            if _cdf_binomial(k, n, medio) > cola:
                lo = medio
            else:
                hi = medio
        superior = (lo + hi) / 2.0

    return inferior, superior


@dataclass(frozen=True)
class ResultadoFPR:
    n: int
    falsos_positivos: int
    fpr: float
    ic_inferior: float
    ic_superior: float
    meta: float
    cumple: bool

    @property
    def veredicto(self) -> str:
        return "CUMPLE" if self.cumple else "NO CUMPLE"


def evaluar_fpr(
    falsos_positivos: int, n: int, meta: float = FPR_META
) -> ResultadoFPR:
    """FPR de documento con IC 95 % bilateral; CUMPLE solo si la cota superior ≤ meta."""
    inferior, superior = clopper_pearson(falsos_positivos, n)
    return ResultadoFPR(
        n=n,
        falsos_positivos=falsos_positivos,
        fpr=falsos_positivos / n,
        ic_inferior=inferior,
        ic_superior=superior,
        meta=meta,
        cumple=superior <= meta,
    )


def contar_falsos_positivos(
    porcentajes_ia: list[float], umbral_pct: float = DOC_UMBRAL_PCT
) -> int:
    """Un documento humano es falso positivo si su % de IA ≥ el umbral de documento."""
    return sum(1 for p in porcentajes_ia if p >= umbral_pct)
