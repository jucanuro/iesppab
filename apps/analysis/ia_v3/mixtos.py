"""
C2 · Constructor de documentos mixtos por bloques de párrafos (ORI-IA v3).

Mezcla párrafos de un texto humano y uno de IA de la MISMA partición, en bloques
intercalados, hasta acercarse a un % objetivo de IA. El % real se mide con el
mismo filtro que producción (`AcademicTextFilter`): palabras IA / palabras
evaluables del documento mezclado. Determinista dada la semilla.
"""

from __future__ import annotations

import hashlib
import json
import random
import re

from apps.analysis.engines.text_filters import AcademicTextFilter

from .exceptions import IABankError

MAX_PARRAFOS_BLOQUE = 3
SEPARADOR = "\n\n"
PARTICIONES_MEZCLABLES = {"train", "val", "test"}


def _parrafos(texto: str, filtro: AcademicTextFilter) -> list[str]:
    limpio = filtro.filter_for_similarity(content=texto).content
    return [p.strip() for p in re.split(r"\n\s*\n", limpio) if p.strip()]


def _palabras(texto: str) -> int:
    return len(texto.split())


def _validar_fuentes(humano: dict, ia: dict) -> str:
    if humano.get("etiqueta") != "humano" or ia.get("etiqueta") != "ia":
        raise IABankError(
            f"Se esperaba (humano, ia); llegó ({humano.get('etiqueta')}, {ia.get('etiqueta')})."
        )
    if humano.get("particion") != ia.get("particion"):
        raise IABankError(
            f"No se mezclan particiones distintas: {humano['id']} ({humano.get('particion')}) "
            f"con {ia['id']} ({ia.get('particion')})."
        )
    particion = humano["particion"]
    if particion not in PARTICIONES_MEZCLABLES:
        raise IABankError(
            f"La partición '{particion}' no admite mixtos (solo train/val/test): "
            f"{humano['id']}, {ia['id']}."
        )
    return particion


def _elegir_bloques(
    humanos: list[str], ias: list[str], objetivo_pct: int, rng: random.Random
) -> list[tuple[str, str]]:
    """Lista de (etiqueta, párrafo) intercalando bloques hasta acercarse al objetivo."""
    if objetivo_pct <= 0:
        return [("humano", p) for p in humanos]
    if objetivo_pct >= 100:
        return [("ia", p) for p in ias]

    objetivo = objetivo_pct / 100
    i_h = i_ia = 0
    w_h = w_ia = 0
    salida: list[tuple[str, str]] = []
    while True:
        quedan_h = i_h < len(humanos)
        quedan_ia = i_ia < len(ias)
        if not (quedan_h and quedan_ia):
            break
        total = w_h + w_ia
        # Con el documento vacío arranca el origen que el azar decida; luego se
        # corrige hacia el objetivo.
        if total == 0:
            etiqueta = "ia" if rng.random() < objetivo else "humano"
        else:
            etiqueta = "ia" if (w_ia / total) < objetivo else "humano"
        n = rng.randint(1, MAX_PARRAFOS_BLOQUE)
        if etiqueta == "ia":
            bloque = ias[i_ia : i_ia + n]
            i_ia += len(bloque)
            w_ia += sum(_palabras(p) for p in bloque)
        else:
            bloque = humanos[i_h : i_h + n]
            i_h += len(bloque)
            w_h += sum(_palabras(p) for p in bloque)
        salida.extend((etiqueta, p) for p in bloque)
    return salida


def _atribuir(
    piezas: list[tuple[str, str, str]], filtro: AcademicTextFilter
) -> tuple[str, list[dict], float]:
    """
    piezas: (etiqueta, origen_id, párrafo). Arma el texto, lo pasa por el filtro de
    producción y atribuye las palabras evaluables que sobreviven a cada tramo.
    """
    texto = SEPARADOR.join(p for _, _, p in piezas)
    filtrado = filtro.filter_for_similarity(content=texto).content

    tramos: list[dict] = []
    cursor_filtrado = 0
    cursor_texto = 0
    palabras_ia = 0
    palabras_total = 0
    for etiqueta, origen_id, parrafo in piezas:
        inicio = texto.index(parrafo, cursor_texto)
        fin = inicio + len(parrafo)
        cursor_texto = fin
        pos = filtrado.find(parrafo, cursor_filtrado)
        if pos >= 0:
            cursor_filtrado = pos + len(parrafo)
            evaluables = _palabras(parrafo)
        else:
            evaluables = 0  # el filtro lo descartó o lo alteró: no cuenta
        palabras_total += evaluables
        if etiqueta == "ia":
            palabras_ia += evaluables
        previo = tramos[-1] if tramos else None
        if previo and previo["origen_id"] == origen_id and previo["etiqueta"] == etiqueta:
            previo["fin"] = fin
            previo["palabras_evaluables"] += evaluables
        else:
            tramos.append(
                {
                    "inicio": inicio,
                    "fin": fin,
                    "origen_id": origen_id,
                    "etiqueta": etiqueta,
                    "palabras_evaluables": evaluables,
                }
            )
    pct = round(100 * palabras_ia / palabras_total, 2) if palabras_total else 0.0
    return texto, tramos, pct


def construir_mixto(
    entrada_humano: dict,
    entrada_ia: dict,
    texto_humano: str,
    texto_ia: str,
    objetivo_pct: int,
    semilla: int,
    filtro: AcademicTextFilter | None = None,
) -> dict:
    """Construye un mixto {texto, pct_real_ia, tramos, semilla, objetivo_pct, sha256, ...}."""
    if not 0 <= objetivo_pct <= 100:
        raise IABankError(f"objetivo_pct fuera de 0–100: {objetivo_pct}.")
    particion = _validar_fuentes(entrada_humano, entrada_ia)
    filtro = filtro or AcademicTextFilter()

    humanos = _parrafos(texto_humano, filtro)
    ias = _parrafos(texto_ia, filtro)
    if not humanos or not ias:
        raise IABankError(
            f"Sin párrafos evaluables en {entrada_humano['id']} o {entrada_ia['id']}."
        )

    rng = random.Random(f"{semilla}:{entrada_humano['id']}:{entrada_ia['id']}:{objetivo_pct}")
    bloques = _elegir_bloques(humanos, ias, objetivo_pct, rng)
    piezas = [
        (etiqueta, entrada_ia["id"] if etiqueta == "ia" else entrada_humano["id"], parrafo)
        for etiqueta, parrafo in bloques
    ]
    texto, tramos, pct = _atribuir(piezas, filtro)

    doc = {
        "texto": texto,
        "pct_real_ia": pct,
        "objetivo_pct": objetivo_pct,
        "particion": particion,
        "tramos": tramos,
        "semilla": semilla,
    }
    canon = json.dumps(doc, sort_keys=True, ensure_ascii=False)
    doc["sha256"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return doc
