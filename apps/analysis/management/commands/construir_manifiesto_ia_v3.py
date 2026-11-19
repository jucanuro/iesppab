"""
T03 · Construye el manifiesto del banco de evaluación de IA v3 (design §1.2).

Entradas: carpetas de `eval_ia/` (IA ya generada, chats, no vistos) y un CSV de
metadatos de los humanos. Las particiones se asignan por texto fuente y de forma
determinista con la semilla `IA_V3_SEED`. No lee el contenido salvo para el SHA-256.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.manifest import (
    IA_V3_SEED,
    cargar_y_validar,
    evaluar_no_vistos,
    evaluar_test_fpr,
    sha256_archivo,
)

DEFAULT_RAIZ = Path(settings.BASE_DIR) / "eval_ia"
DEFAULT_SALIDA = Path(settings.BASE_DIR) / "apps/analysis/evaluation/ia_v3/manifiesto.json"
ANIO_MAX_META = 2021
FUENTES_API = {"claude": "claude_api", "gemini": "gemini_api"}
CHATS = {"chatgpt": "chatgpt_chat", "gemini": "gemini_chat"}
# Proporciones (train, val, test) [Propuesto]; los humanos reservan más a test_fpr.
FRACCIONES_HUMANOS = (0.50, 0.15, 0.35)
FRACCIONES_IA = (0.70, 0.15, 0.15)
FRACCIONES_WIKIPEDIA = (0.85, 0.15, 0.0)  # Wikipedia nunca en test (design §1.2)
COLUMNAS_CSV = ("ruta", "institucion", "anio", "origen", "publico", "verificado",
                "criterio_verificacion", "fuente_url", "licencia")


def _orden(semilla: int, clave: str) -> str:
    return hashlib.sha256(f"{semilla}:{clave}".encode()).hexdigest()


def asignar_particiones(ids: list[str], fracciones: tuple[float, float, float], semilla: int) -> dict[str, str]:
    """Reparte `ids` en train/val/test por orden pseudoaleatorio fijo por semilla."""
    ordenados = sorted(ids, key=lambda i: (_orden(semilla, i), i))
    n = len(ordenados)
    n_val = round(n * fracciones[1])
    n_test = round(n * fracciones[2])
    res = {}
    for k, i in enumerate(ordenados):
        res[i] = "test" if k < n_test else "val" if k < n_test + n_val else "train"
    return res


def _bool(valor: str, campo: str, fila: int) -> bool:
    v = (valor or "").strip().lower()
    if v in ("true", "1", "si", "sí", "s"):
        return True
    if v in ("false", "0", "no", "n"):
        return False
    raise IABankError(f"CSV fila {fila}: '{campo}' debe ser verdadero/falso (recibido '{valor}').")


class Command(BaseCommand):
    help = (
        "Construye el manifiesto del banco IA v3 desde carpetas de eval_ia/ y un CSV de "
        "metadatos de humanos; particiones deterministas por IA_V3_SEED. No hace commit."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--raiz", default=str(DEFAULT_RAIZ), help="Carpeta eval_ia/.")
        parser.add_argument("--metadatos", required=True, help="CSV de metadatos de humanos.")
        parser.add_argument("--salida", default=str(DEFAULT_SALIDA))
        parser.add_argument("--semilla", type=int, default=IA_V3_SEED)
        parser.add_argument("--chats-no-visto", action="store_true",
                            help="Marca los 60 chats con visto=false (solo si T01/V-01 concluyó 'no usados').")

    def handle(self, *args, **options) -> None:
        raiz = Path(options["raiz"])
        try:
            entradas = construir_entradas(
                raiz, Path(options["metadatos"]), options["semilla"],
                chats_vistos=not options["chats_no_visto"],
            )
            salida = Path(options["salida"])
            escribir_manifiesto(entradas, salida)
            cargar_y_validar(salida, raiz)
        except IABankError as exc:
            raise CommandError(str(exc)) from exc

        por_part = defaultdict(int)
        for e in entradas:
            por_part[(e["etiqueta"], e["particion"])] += 1
        self.stdout.write(f"Manifiesto: {salida} ({len(entradas)} textos, semilla {options['semilla']})")
        for (etq, part), n in sorted(por_part.items()):
            self.stdout.write(f"  {etq:7s} {part:11s} {n}")
        self.stdout.write(f"SHA-256 del manifiesto: {sha256_archivo(salida)}")
        for nombre, ev in (("test_fpr", evaluar_test_fpr(entradas)), ("no_vistos", evaluar_no_vistos(entradas))):
            self.stdout.write(f"{nombre}: {ev.estado}")
            for m in ev.motivos:
                self.stdout.write(f"  - {m}")


def _entrada(raiz: Path, archivo: Path, **campos) -> dict:
    if not archivo.is_file():
        raise IABankError(f"No existe el archivo {archivo}.")
    ruta = archivo.relative_to(raiz).as_posix()
    return {"id": ruta.rsplit(".", 1)[0].replace("/", "__"), "ruta": ruta,
            "sha256": sha256_archivo(archivo), **campos}


def _leer_metadatos(csv_path: Path) -> list[dict]:
    if not csv_path.is_file():
        raise IABankError(f"No existe el CSV de metadatos: {csv_path}.")
    with open(csv_path, encoding="utf-8", newline="") as fh:
        lector = csv.DictReader(fh)
        faltan = [c for c in COLUMNAS_CSV if c not in (lector.fieldnames or [])]
        if faltan:
            raise IABankError(f"El CSV no tiene las columnas: {', '.join(faltan)}.")
        return [dict(f, _fila=i) for i, f in enumerate(lector, start=2)]


def _humanos(raiz: Path, csv_path: Path, semilla: int) -> list[dict]:
    humanos = []
    for f in _leer_metadatos(csv_path):
        fila = f["_fila"]
        try:
            anio = int(str(f["anio"]).strip())
        except ValueError as exc:
            raise IABankError(f"CSV fila {fila}: 'anio' inválido '{f['anio']}'.") from exc
        institucion = (f["institucion"] or "").strip()
        if not institucion:
            raise IABankError(f"CSV fila {fila}: falta 'institucion'.")
        wiki = (f["origen"] or "").strip().lower() == "wikipedia"
        verificado = _bool(f["verificado"], "verificado", fila)
        e = _entrada(
            raiz, raiz / f["ruta"].strip(), etiqueta="humano", familia="humano", anio=anio,
            institucion=institucion, publico=_bool(f["publico"], "publico", fila),
            verificado={"valor": verificado, "criterio": (f["criterio_verificacion"] or "").strip()},
            fuente_url=(f["fuente_url"] or "").strip(), licencia=(f["licencia"] or "").strip(),
        )
        e["_wiki"] = wiki
        humanos.append(e)

    grupos: dict[tuple, list[dict]] = defaultdict(list)
    for e in humanos:
        if e["anio"] > ANIO_MAX_META:
            e["particion"] = "diagnostico"  # REQ-IA-02
        elif e["_wiki"]:
            grupos[("wiki", e["institucion"])].append(e)
        elif not e["verificado"]["valor"]:
            grupos[("sinverificar", e["institucion"])].append(e)  # nunca en test_fpr
        else:
            grupos[("meta", e["institucion"])].append(e)
    fracciones = {"wiki": FRACCIONES_WIKIPEDIA, "sinverificar": (0.85, 0.15, 0.0), "meta": FRACCIONES_HUMANOS}
    for (tipo, _inst), lista in grupos.items():  # estratificado por institución
        asign = asignar_particiones([e["id"] for e in lista], fracciones[tipo], semilla)
        for e in lista:
            e["particion"] = asign[e["id"]]
    for e in humanos:
        del e["_wiki"]
    return humanos


def _ia_api(raiz: Path, semilla: int) -> list[dict]:
    res = []
    for carpeta, familia in FUENTES_API.items():
        for archivo in sorted((raiz / "ia_api" / carpeta).glob("*.txt")):
            res.append(_entrada(raiz, archivo, etiqueta="ia", familia=familia, publico=True,
                                verificado={"valor": True, "criterio": "generado con API (RN-IA-11)"}))
    asign = asignar_particiones([e["id"] for e in res], FRACCIONES_IA, semilla)
    for e in res:
        e["particion"] = asign[e["id"]]
    return res


def _chats(raiz: Path, vistos: bool) -> list[dict]:
    res = []
    for prefijo, familia in CHATS.items():
        for archivo in sorted((raiz / "ia_externa").glob(f"{prefijo}_*.txt")):
            res.append(_entrada(raiz, archivo, etiqueta="ia", familia=familia, publico=True,
                                particion="sellado", visto=vistos,
                                verificado={"valor": True, "criterio": "chat gratuito (60 existentes)"}))
    return res


def _no_vistos(raiz: Path) -> list[dict]:
    res = []
    base = raiz / "no_vistos_v3"
    if base.is_dir():
        for carpeta in sorted(p for p in base.iterdir() if p.is_dir()):
            for archivo in sorted(carpeta.glob("*.txt")):
                res.append(_entrada(raiz, archivo, etiqueta="ia", familia=carpeta.name, publico=True,
                                    particion="no_vistos", visto=False,
                                    verificado={"valor": True, "criterio": "chat gratuito a mano (§1.3)"}))
    return res


def construir_entradas(raiz: Path, csv_path: Path, semilla: int, chats_vistos: bool = True) -> list[dict]:
    entradas = (_humanos(raiz, csv_path, semilla) + _ia_api(raiz, semilla)
                + _chats(raiz, chats_vistos) + _no_vistos(raiz))
    ids = [e["id"] for e in entradas]
    if len(ids) != len(set(ids)):
        raise IABankError("Hay ids duplicados al construir el manifiesto.")
    return sorted(entradas, key=lambda e: e["id"])


def escribir_manifiesto(entradas: list[dict], salida: Path) -> None:
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps({"entradas": entradas}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
