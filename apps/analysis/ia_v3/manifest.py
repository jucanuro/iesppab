"""
C1 · Cargador y validador del manifiesto del banco de evaluación de IA v3.

El manifiesto (JSON) lista un texto fuente por entrada. Este módulo no lee el
contenido de los textos salvo para verificar su SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .exceptions import IABankError

IA_V3_SEED = 20261113  # semilla fija de las particiones (design §4)

PARTICIONES = {"train", "val", "test", "sellado", "no_vistos", "diagnostico"}
ETIQUETAS = {"humano", "ia"}

MIN_TEST_FPR = 183
MIN_INSTITUCIONES = 3
MAX_FRACCION_INSTITUCION = 0.5
MIN_NO_VISTOS_DOCS = 60
MIN_NO_VISTOS_FAMILIAS = 3
CAMPOS_OBLIGATORIOS = ("id", "ruta", "sha256", "etiqueta", "particion")

INSUFICIENTE = "insuficiente"
OK = "OK"


@dataclass(frozen=True)
class Evaluacion:
    estado: str  # OK | insuficiente
    motivos: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.estado == OK


def sha256_archivo(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def cargar_manifiesto(path: str | Path) -> list[dict]:
    """Lee el JSON y valida la estructura mínima de cada entrada."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IABankError(f"No existe el manifiesto: {path}") from exc
    except json.JSONDecodeError as exc:
        raise IABankError(f"Manifiesto con JSON inválido: {exc}") from exc
    entradas = data.get("entradas") if isinstance(data, dict) else data
    if not isinstance(entradas, list):
        raise IABankError("El manifiesto debe ser una lista de entradas (o {'entradas': [...]}).")
    vistos: set[str] = set()
    for i, e in enumerate(entradas):
        if not isinstance(e, dict):
            raise IABankError(f"Entrada {i} del manifiesto no es un objeto.")
        eid = e.get("id", f"#{i}")
        for campo in CAMPOS_OBLIGATORIOS:
            if not e.get(campo):
                raise IABankError(f"Entrada {eid}: falta el campo '{campo}'.")
        if e["particion"] not in PARTICIONES:
            raise IABankError(f"Entrada {eid}: partición inválida '{e['particion']}'.")
        if e["etiqueta"] not in ETIQUETAS:
            raise IABankError(f"Entrada {eid}: etiqueta inválida '{e['etiqueta']}'.")
        if eid in vistos:
            raise IABankError(f"Id duplicado en el manifiesto: {eid}.")
        vistos.add(eid)
    return entradas


def verificar_hashes(entradas: list[dict], raiz: str | Path) -> None:
    """SHA-256 ausente, archivo faltante o hash distinto → IABankError con el id."""
    raiz = Path(raiz)
    for e in entradas:
        archivo = raiz / e["ruta"]
        if not archivo.is_file():
            raise IABankError(f"Texto {e['id']}: no existe el archivo {e['ruta']}.")
        real = sha256_archivo(archivo)
        if real != str(e["sha256"]).lower():
            raise IABankError(f"Texto {e['id']}: el SHA-256 no coincide con el manifiesto.")


def verificar_sin_fuga(entradas: list[dict]) -> None:
    """Un texto fuente (mismo sha256 o misma ruta) no puede estar en dos particiones."""
    for clave in ("sha256", "ruta"):
        por_clave: dict[str, dict] = {}
        for e in entradas:
            k = str(e[clave]).lower()
            previa = por_clave.get(k)
            if previa is None:
                por_clave[k] = e
            elif previa["particion"] != e["particion"]:
                raise IABankError(
                    f"Fuente en dos particiones: {previa['id']} ({previa['particion']}) "
                    f"y {e['id']} ({e['particion']})."
                )


def humanos_test_fpr(entradas: list[dict]) -> list[dict]:
    """Humanos de la partición test (conjunto de la meta de FPR)."""
    return [e for e in entradas if e["etiqueta"] == "humano" and e["particion"] == "test"]


def evaluar_test_fpr(entradas: list[dict]) -> Evaluacion:
    """≥183 humanos, ≥3 instituciones, ninguna >50 % → OK; si no, 'insuficiente'."""
    humanos = humanos_test_fpr(entradas)
    motivos: list[str] = []
    if len(humanos) < MIN_TEST_FPR:
        motivos.append(f"test_fpr tiene {len(humanos)} documentos (mínimo {MIN_TEST_FPR}).")
    por_inst = Counter(str(e.get("institucion") or "").strip() for e in humanos)
    por_inst.pop("", None)
    if len(por_inst) < MIN_INSTITUCIONES:
        motivos.append(f"test_fpr tiene {len(por_inst)} instituciones (mínimo {MIN_INSTITUCIONES}).")
    if humanos:
        for inst, n in por_inst.items():
            if n / len(humanos) > MAX_FRACCION_INSTITUCION:
                motivos.append(f"La institución '{inst}' aporta {n}/{len(humanos)} (>50 %).")
    return Evaluacion(INSUFICIENTE if motivos else OK, tuple(motivos))


def evaluar_no_vistos(entradas: list[dict]) -> Evaluacion:
    """≥60 documentos IA y ≥3 familias en no_vistos → OK; si no, 'insuficiente'."""
    docs = [e for e in entradas if e["particion"] == "no_vistos" and e["etiqueta"] == "ia"]
    familias = {e.get("familia") for e in docs if e.get("familia")}
    motivos: list[str] = []
    if len(docs) < MIN_NO_VISTOS_DOCS:
        motivos.append(f"no_vistos tiene {len(docs)} documentos (mínimo {MIN_NO_VISTOS_DOCS}).")
    if len(familias) < MIN_NO_VISTOS_FAMILIAS:
        motivos.append(f"no_vistos tiene {len(familias)} familias (mínimo {MIN_NO_VISTOS_FAMILIAS}).")
    return Evaluacion(INSUFICIENTE if motivos else OK, tuple(motivos))


def cargar_y_validar(path: str | Path, raiz: str | Path | None = None) -> list[dict]:
    """Carga, comprueba ausencia de fuga y (si se da `raiz`) los SHA-256."""
    entradas = cargar_manifiesto(path)
    verificar_sin_fuga(entradas)
    if raiz is not None:
        verificar_hashes(entradas, raiz)
    return entradas
