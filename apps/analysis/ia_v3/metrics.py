"""
C3 · Métricas de evaluación de IA v3 (FPR con Clopper-Pearson bilateral;
MAE/p90 en mixtos; recall por familia con IC; curva de fiabilidad y ECE).

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
MAE_META = 10.0
RECALL_NO_VISTOS_META = 0.70
ECE_META = 0.05
N_TRAMOS = 10
NO_VISTOS_MIN_DOCS = 60
NO_VISTOS_MIN_FAMILIAS = 3
FAMILIA_N_BAJO = 15


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


# --- REQ-IA-05: error en documentos mixtos -------------------------------


@dataclass(frozen=True)
class ResultadoMAE:
    n: int
    errores: tuple[float, ...]
    mae: float
    p90: float
    meta: float
    cumple: bool

    @property
    def veredicto(self) -> str:
        return "CUMPLE" if self.cumple else "NO CUMPLE"


def _percentil(valores: list[float], q: float) -> float:
    """Percentil por interpolación lineal (método habitual de numpy)."""
    ordenados = sorted(valores)
    pos = (len(ordenados) - 1) * q
    bajo = math.floor(pos)
    alto = math.ceil(pos)
    return ordenados[bajo] + (ordenados[alto] - ordenados[bajo]) * (pos - bajo)


def evaluar_mae(
    estimados: list[float], reales: list[float], meta: float = MAE_META
) -> ResultadoMAE:
    """MAE y p90 del error absoluto |% estimado − % real|; CUMPLE si MAE ≤ meta.

    El p90 solo se reporta; la meta es el MAE (Decisión 3).
    """
    if len(estimados) != len(reales):
        raise IABankError("Estimados y reales deben tener la misma longitud.")
    if not estimados:
        raise IABankError("No hay documentos mixtos para evaluar.")
    errores = [abs(e - r) for e, r in zip(estimados, reales)]
    mae = sum(errores) / len(errores)
    return ResultadoMAE(
        n=len(errores),
        errores=tuple(errores),
        mae=mae,
        p90=_percentil(errores, 0.9),
        meta=meta,
        cumple=mae <= meta,
    )


# --- REQ-IA-07: recall por familia de LLM ---------------------------------


@dataclass(frozen=True)
class RecallFamilia:
    familia: str
    vista: bool
    n: int
    detectados: int
    recall: float
    ic_inferior: float
    ic_superior: float
    n_bajo: bool


@dataclass(frozen=True)
class ResultadoRecall:
    familias: tuple[RecallFamilia, ...]
    n_no_vistos: int
    familias_no_vistas: int
    recall_no_vistos: float | None
    ic_inferior: float | None
    ic_superior: float | None
    meta: float
    insuficiente: bool

    @property
    def cumple(self) -> bool:
        return (not self.insuficiente) and self.recall_no_vistos >= self.meta

    @property
    def veredicto(self) -> str:
        if self.insuficiente:
            return "INSUFICIENTE"
        return "CUMPLE" if self.cumple else "NO CUMPLE"


def evaluar_recall(
    documentos: list[tuple[str, bool, float]],
    umbral_pct: float = DOC_UMBRAL_PCT,
    meta: float = RECALL_NO_VISTOS_META,
) -> ResultadoRecall:
    """Recall por familia (IC 95 %) separando familias vistas y no vistas.

    `documentos`: tuplas (familia, vista, % de IA) de documentos IA puros.
    El veredicto se calcula solo sobre las familias no vistas; con menos de
    60 documentos o menos de 3 familias no vistas es "insuficiente".
    """
    por_familia: dict[tuple[str, bool], list[float]] = {}
    for familia, vista, pct in documentos:
        por_familia.setdefault((familia, bool(vista)), []).append(pct)

    familias = []
    for (familia, vista), pcts in sorted(por_familia.items()):
        n = len(pcts)
        det = sum(1 for p in pcts if p >= umbral_pct)
        inf, sup = clopper_pearson(det, n)
        familias.append(
            RecallFamilia(
                familia=familia,
                vista=vista,
                n=n,
                detectados=det,
                recall=det / n,
                ic_inferior=inf,
                ic_superior=sup,
                n_bajo=n < FAMILIA_N_BAJO,
            )
        )

    nv = [f for f in familias if not f.vista]
    n_nv = sum(f.n for f in nv)
    det_nv = sum(f.detectados for f in nv)
    insuficiente = n_nv < NO_VISTOS_MIN_DOCS or len(nv) < NO_VISTOS_MIN_FAMILIAS
    if n_nv:
        inf, sup = clopper_pearson(det_nv, n_nv)
        recall_nv: float | None = det_nv / n_nv
    else:
        inf = sup = recall_nv = None
    return ResultadoRecall(
        familias=tuple(familias),
        n_no_vistos=n_nv,
        familias_no_vistas=len(nv),
        recall_no_vistos=recall_nv,
        ic_inferior=inf,
        ic_superior=sup,
        meta=meta,
        insuficiente=insuficiente,
    )


# --- REQ-IA-08: curva de fiabilidad y ECE ---------------------------------


@dataclass(frozen=True)
class TramoFiabilidad:
    inferior: float
    superior: float
    n: int
    prob_media: float | None
    fraccion_ia: float | None


@dataclass(frozen=True)
class ResultadoCalibracion:
    n: int
    tramos: tuple[TramoFiabilidad, ...]
    ece: float
    meta: float
    cumple: bool

    @property
    def veredicto(self) -> str:
        return "CUMPLE" if self.cumple else "NO CUMPLE"


def curva_fiabilidad(
    probabilidades: list[float],
    etiquetas: list[int],
    n_tramos: int = N_TRAMOS,
    meta: float = ECE_META,
) -> ResultadoCalibracion:
    """Curva de fiabilidad (tramos de igual ancho) y ECE; CUMPLE si ECE ≤ meta.

    Se llama una vez por segmento. `etiquetas`: 1 = IA, 0 = humano.
    """
    if len(probabilidades) != len(etiquetas):
        raise IABankError("Probabilidades y etiquetas deben tener la misma longitud.")
    if not probabilidades:
        raise IABankError("No hay predicciones para calibrar.")
    if any(not 0.0 <= p <= 1.0 for p in probabilidades):
        raise IABankError("Las probabilidades deben estar entre 0 y 1.")

    cubos: list[list[tuple[float, int]]] = [[] for _ in range(n_tramos)]
    for p, y in zip(probabilidades, etiquetas):
        cubos[min(int(p * n_tramos), n_tramos - 1)].append((p, y))

    total = len(probabilidades)
    tramos = []
    ece = 0.0
    for i, cubo in enumerate(cubos):
        if cubo:
            pm = sum(p for p, _ in cubo) / len(cubo)
            fr = sum(y for _, y in cubo) / len(cubo)
            ece += len(cubo) / total * abs(pm - fr)
        else:
            pm = fr = None
        tramos.append(
            TramoFiabilidad(
                inferior=i / n_tramos,
                superior=(i + 1) / n_tramos,
                n=len(cubo),
                prob_media=pm,
                fraccion_ia=fr,
            )
        )
    return ResultadoCalibracion(
        n=total, tramos=tuple(tramos), ece=ece, meta=meta, cumple=ece <= meta
    )
