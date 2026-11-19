"""
Construcción del banco IA v3 (T09): manifiesto (C1) + mixtos (C2) → JSONL por partición.

Servicio puro (sin BD ni red). Determinista dada la semilla: mismos textos, mismo
manifiesto y misma semilla producen archivos idénticos byte a byte.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from apps.analysis.engines.text_filters import AcademicTextFilter

from .exceptions import IABankError
from .manifest import cargar_y_validar, sha256_archivo
from .mixtos import PARTICIONES_MEZCLABLES, construir_mixto

# Colab solo recibe train y val (design §2 C5: entrena en train, calibra en val; la
# evaluación con test corre en la PC, §3 y §8). Nada más sale hacia Colab (REQ-IA-16).
PARTICIONES_COLAB = {"train", "val"}
OBJETIVOS_MIXTOS = tuple(range(10, 100, 10))  # los extremos 0/100 son los textos puros
MIXTOS_POR_OBJETIVO = 10


@dataclass
class ResultadoBanco:
    archivos: dict[str, Path] = field(default_factory=dict)  # partición → JSONL
    hashes: dict[str, str] = field(default_factory=dict)  # partición → sha256 del JSONL
    conteos: dict[str, dict[str, int]] = field(default_factory=dict)
    omitidos: list[str] = field(default_factory=list)
    hash_banco: str = ""
    sha_manifiesto: str = ""


def _linea(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False) + "\n"


def seleccionar_entradas(entradas: list[dict], para_colab: bool) -> list[dict]:
    """Con `para_colab` deja solo train y val y aborta ante `publico` != True."""
    if not para_colab:
        return list(entradas)
    elegidas = [e for e in entradas if e["particion"] in PARTICIONES_COLAB]
    privados = sorted(e["id"] for e in elegidas if e.get("publico") is not True)
    if privados:
        raise IABankError(
            "--para-colab aborta: textos no públicos (publico=false) en el banco: "
            + ", ".join(privados[:10])
            + ("…" if len(privados) > 10 else "")
            + "."
        )
    return elegidas


def _leer(raiz: Path, entrada: dict) -> str:
    return (raiz / entrada["ruta"]).read_text(encoding="utf-8")


def _fila_pura(entrada: dict, texto: str) -> dict:
    return {
        "id": entrada["id"],
        "tipo": "puro",
        "etiqueta": entrada["etiqueta"],
        "familia": entrada.get("familia"),
        "particion": entrada["particion"],
        "pct_real_ia": 100.0 if entrada["etiqueta"] == "ia" else 0.0,
        "publico": entrada.get("publico"),
        "sha256": entrada["sha256"],
        "texto": texto,
    }


def _filas_mixtas(
    particion: str,
    humanos: list[dict],
    ias: list[dict],
    textos: dict[str, str],
    semilla: int,
    por_objetivo: int,
    filtro: AcademicTextFilter,
    omitidos: list[str],
) -> list[dict]:
    filas = []
    orden_h = sorted(humanos, key=lambda e: (hashlib.sha256(f"{semilla}:{e['id']}".encode()).hexdigest(), e["id"]))
    for objetivo in OBJETIVOS_MIXTOS:
        rng = random.Random(f"{semilla}:pares:{particion}:{objetivo}")
        for k in range(por_objetivo):
            h = orden_h[k % len(orden_h)]
            a = ias[rng.randrange(len(ias))]
            try:
                m = construir_mixto(h, a, textos[h["id"]], textos[a["id"]], objetivo, semilla, filtro)
            except IABankError as exc:
                omitidos.append(f"{particion}/{objetivo}/{h['id']}+{a['id']}: {exc}")
                continue
            filas.append(
                {
                    "id": f"mix_{particion}_{objetivo:03d}_{h['id']}__{a['id']}",
                    "tipo": "mixto",
                    "etiqueta": "mixto",
                    "familia": a.get("familia"),
                    "particion": particion,
                    "pct_real_ia": m["pct_real_ia"],
                    "objetivo_pct": m["objetivo_pct"],
                    "tramos": m["tramos"],
                    "semilla": m["semilla"],
                    "origen_humano": h["id"],
                    "origen_ia": a["id"],
                    "publico": bool(h.get("publico") is True and a.get("publico") is True),
                    "sha256": m["sha256"],
                    "texto": m["texto"],
                }
            )
    return filas


def construir_banco(
    manifiesto: str | Path,
    raiz: str | Path,
    salida: str | Path,
    semilla: int,
    para_colab: bool = False,
    por_objetivo: int = MIXTOS_POR_OBJETIVO,
) -> ResultadoBanco:
    """Valida el manifiesto (hashes y fuga), genera mixtos y escribe `<partición>.jsonl`."""
    raiz, salida = Path(raiz), Path(salida)
    entradas = cargar_y_validar(manifiesto, raiz)
    entradas = seleccionar_entradas(entradas, para_colab)
    if not entradas:
        raise IABankError("El manifiesto no tiene textos exportables.")

    textos = {e["id"]: _leer(raiz, e) for e in entradas}
    filtro = AcademicTextFilter()
    res = ResultadoBanco(sha_manifiesto=sha256_archivo(Path(manifiesto)))
    salida.mkdir(parents=True, exist_ok=True)

    for particion in sorted({e["particion"] for e in entradas}):
        de_part = sorted((e for e in entradas if e["particion"] == particion), key=lambda e: e["id"])
        filas = [_fila_pura(e, textos[e["id"]]) for e in de_part]
        if particion in PARTICIONES_MEZCLABLES:
            humanos = [e for e in de_part if e["etiqueta"] == "humano"]
            ias = [e for e in de_part if e["etiqueta"] == "ia"]
            if humanos and ias:
                filas += _filas_mixtas(
                    particion, humanos, ias, textos, semilla, por_objetivo, filtro, res.omitidos
                )
        filas.sort(key=lambda f: f["id"])
        ruta = salida / f"{particion}.jsonl"
        with open(ruta, "w", encoding="utf-8", newline="\n") as fh:
            for f in filas:
                fh.write(_linea(f))
        res.archivos[particion] = ruta
        res.hashes[particion] = sha256_archivo(ruta)
        res.conteos[particion] = {
            "puros": sum(1 for f in filas if f["tipo"] == "puro"),
            "mixtos": sum(1 for f in filas if f["tipo"] == "mixto"),
        }

    # hash del banco: manifiesto + semilla + modo + hash de cada partición (orden fijo)
    canon = json.dumps(
        {"manifiesto": res.sha_manifiesto, "semilla": semilla, "para_colab": para_colab,
         "particiones": res.hashes},
        sort_keys=True,
    )
    res.hash_banco = hashlib.sha256(canon.encode()).hexdigest()
    (salida / "banco.json").write_text(
        json.dumps({"hash_banco": res.hash_banco, "semilla": semilla, "para_colab": para_colab,
                    "sha256_manifiesto": res.sha_manifiesto, "particiones": res.hashes,
                    "conteos": res.conteos}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return res
