"""Tests de C1 (manifiesto del banco IA v3). Sin red ni BD."""

import csv
import hashlib
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.manifest import (
    cargar_manifiesto,
    cargar_y_validar,
    evaluar_no_vistos,
    evaluar_test_fpr,
    humanos_test_fpr,
    sha256_archivo,
    verificar_hashes,
    verificar_sin_fuga,
)


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _humano(i, inst, particion="test"):
    return {
        "id": f"h{i}", "ruta": f"h/{i}.txt", "sha256": _h(f"h{i}"),
        "etiqueta": "humano", "particion": particion, "institucion": inst,
    }


def _test_fpr(n, instituciones):
    return [_humano(i, instituciones[i % len(instituciones)]) for i in range(n)]


def _ia(i, familia, particion="no_vistos"):
    return {
        "id": f"i{i}", "ruta": f"ia/{i}.txt", "sha256": _h(f"i{i}"),
        "etiqueta": "ia", "particion": particion, "familia": familia,
    }


class HashesTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        (self.raiz / "a.txt").write_text("hola", encoding="utf-8")

    def _e(self, sha):
        return [{"id": "x1", "ruta": "a.txt", "sha256": sha, "etiqueta": "humano", "particion": "train"}]

    def test_hash_correcto(self):
        verificar_hashes(self._e(_h("hola")), self.raiz)

    def test_hash_distinto_indica_id(self):
        with self.assertRaisesMessage(IABankError, "x1"):
            verificar_hashes(self._e(_h("otro")), self.raiz)

    def test_archivo_ausente_indica_id(self):
        e = self._e(_h("hola"))
        e[0]["ruta"] = "no_existe.txt"
        with self.assertRaisesMessage(IABankError, "x1"):
            verificar_hashes(e, self.raiz)

    def test_sha256_ausente_en_manifiesto(self):
        e = self._e("")
        path = self.raiz / "m.json"
        path.write_text(json.dumps(e), encoding="utf-8")
        with self.assertRaisesMessage(IABankError, "sha256"):
            cargar_manifiesto(path)

    def test_cargar_y_validar_con_raiz(self):
        path = self.raiz / "m.json"
        path.write_text(json.dumps(self._e(_h("hola"))), encoding="utf-8")
        self.assertEqual(len(cargar_y_validar(path, self.raiz)), 1)


class FugaTests(SimpleTestCase):
    def test_fuente_en_dos_particiones_aborta(self):
        a = _humano(1, "A", "train")
        b = dict(_humano(2, "A", "test"), sha256=a["sha256"])
        with self.assertRaisesMessage(IABankError, "h2"):
            verificar_sin_fuga([a, b])

    def test_misma_ruta_en_dos_particiones_aborta(self):
        a = _humano(1, "A", "train")
        b = dict(_humano(2, "A", "val"), ruta=a["ruta"])
        with self.assertRaises(IABankError):
            verificar_sin_fuga([a, b])

    def test_sin_fuga(self):
        verificar_sin_fuga([_humano(1, "A", "train"), _humano(2, "B", "test")])


class TestFprTests(SimpleTestCase):
    INST = ["A", "B", "C"]

    def test_149_insuficiente(self):
        self.assertEqual(evaluar_test_fpr(_test_fpr(149, self.INST)).estado, "insuficiente")

    def test_150_insuficiente(self):
        self.assertFalse(evaluar_test_fpr(_test_fpr(150, self.INST)).ok)

    def test_183_ok(self):
        self.assertTrue(evaluar_test_fpr(_test_fpr(183, self.INST)).ok)

    def test_dos_instituciones_insuficiente(self):
        r = evaluar_test_fpr(_test_fpr(183, ["A", "B"]))
        self.assertFalse(r.ok)
        self.assertTrue(any("instituciones" in m for m in r.motivos))

    def test_tres_instituciones_ok(self):
        self.assertTrue(evaluar_test_fpr(_test_fpr(183, ["A", "B", "C"])).ok)

    def test_institucion_mayor_50_pct(self):
        e = _test_fpr(100, ["A"]) + [_humano(1000 + i, "B") for i in range(42)] + [
            _humano(2000 + i, "C") for i in range(41)
        ]
        r = evaluar_test_fpr(e)
        self.assertFalse(r.ok)
        self.assertTrue(any(">50" in m for m in r.motivos))

    def test_wikipedia_y_otras_particiones_no_cuentan(self):
        e = _test_fpr(183, self.INST)
        extra = [_humano(5000 + i, "D", "train") for i in range(10)]
        self.assertEqual(evaluar_test_fpr(e + extra).estado, "OK")
        self.assertFalse(evaluar_test_fpr(_test_fpr(182, self.INST) + extra).ok)


class NoVistosTests(SimpleTestCase):
    def _docs(self, n, familias):
        return [_ia(i, familias[i % len(familias)]) for i in range(n)]

    def test_menos_de_60_insuficiente(self):
        self.assertFalse(evaluar_no_vistos(self._docs(59, ["a", "b", "c"])).ok)

    def test_menos_de_3_familias_insuficiente(self):
        self.assertFalse(evaluar_no_vistos(self._docs(60, ["a", "b"])).ok)

    def test_60_y_3_familias_ok(self):
        self.assertTrue(evaluar_no_vistos(self._docs(60, ["a", "b", "c"])).ok)

    def test_otras_particiones_no_cuentan(self):
        docs = [_ia(i, "a", "sellado") for i in range(60)]
        self.assertFalse(evaluar_no_vistos(docs).ok)


class ConstruirManifiestoTests(SimpleTestCase):
    """T03 · comando construir_manifiesto_ia_v3 (sobre carpetas temporales)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name) / "eval_ia"
        self.csv = Path(self.tmp.name) / "meta.csv"
        filas = []

        def txt(ruta, contenido):
            p = self.raiz / ruta
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contenido, encoding="utf-8")

        for i in range(40):
            inst = ("A", "B", "C", "D")[i % 4]
            txt(f"humanos_v3/{inst}/h{i}.txt", f"humano {i}")
            filas.append((f"humanos_v3/{inst}/h{i}.txt", inst, 2019, "", "true", "true"))
        for i in range(10):
            txt(f"humanos_v3/W/w{i}.txt", f"wiki {i}")
            filas.append((f"humanos_v3/W/w{i}.txt", "Wikipedia", 2020, "wikipedia", "true", "true"))
        for i in range(5):
            txt(f"humanos_v3/A/r{i}.txt", f"reciente {i}")
            filas.append((f"humanos_v3/A/r{i}.txt", "A", 2023, "", "true", "true"))
        for i in range(20):
            txt(f"ia_api/claude/c{i}.txt", f"claude {i}")
            txt(f"ia_api/gemini/g{i}.txt", f"gemini {i}")
        for i in range(3):
            txt(f"ia_externa/chatgpt_{i}.txt", f"chatgpt {i}")
            txt(f"ia_externa/gemini_{i}.txt", f"gem {i}")
            txt(f"no_vistos_v3/llama/n{i}.txt", f"llama {i}")
        with open(self.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ruta", "institucion", "anio", "origen", "publico", "verificado",
                        "criterio_verificacion", "fuente_url", "licencia"])
            for r in filas:
                w.writerow([*r, "depósito ≤2021", "https://x", "CC-BY"])

    def _correr(self, salida, **extra):
        out = StringIO()
        call_command("construir_manifiesto_ia_v3", raiz=str(self.raiz), metadatos=str(self.csv),
                     salida=str(salida), stdout=out, **extra)
        return json.loads(Path(salida).read_text(encoding="utf-8"))["entradas"]

    def test_misma_semilla_mismo_hash(self):
        a, b = Path(self.tmp.name) / "a.json", Path(self.tmp.name) / "b.json"
        self._correr(a)
        self._correr(b)
        self.assertEqual(sha256_archivo(a), sha256_archivo(b))

    def test_otra_semilla_cambia_particiones(self):
        a, b = Path(self.tmp.name) / "a.json", Path(self.tmp.name) / "b.json"
        self._correr(a)
        self._correr(b, semilla=1)
        self.assertNotEqual(sha256_archivo(a), sha256_archivo(b))

    def test_wikipedia_nunca_en_test(self):
        e = self._correr(Path(self.tmp.name) / "m.json")
        wiki = [x for x in e if x.get("institucion") == "Wikipedia"]
        self.assertEqual(len(wiki), 10)
        self.assertTrue(all(x["particion"] in ("train", "val") for x in wiki))

    def test_2022_va_a_diagnostico_y_no_cuenta_en_test_fpr(self):
        e = self._correr(Path(self.tmp.name) / "m.json")
        rec = [x for x in e if x.get("anio") == 2023]
        self.assertTrue(rec and all(x["particion"] == "diagnostico" for x in rec))
        self.assertTrue(all(x["anio"] <= 2021 for x in humanos_test_fpr(e)))

    def test_chats_sellados_vistos_y_no_vistos(self):
        e = self._correr(Path(self.tmp.name) / "m.json")
        chats = [x for x in e if x["familia"] in ("chatgpt_chat", "gemini_chat")]
        self.assertEqual(len(chats), 6)
        self.assertTrue(all(x["particion"] == "sellado" and x["visto"] is True for x in chats))
        nv = [x for x in e if x["particion"] == "no_vistos"]
        self.assertEqual({x["familia"] for x in nv}, {"llama"})
        self.assertTrue(all(x["visto"] is False for x in nv))
        e2 = self._correr(Path(self.tmp.name) / "m2.json", chats_no_visto=True)
        self.assertTrue(all(x["visto"] is False for x in e2 if x["particion"] == "sellado"))

    def test_manifiesto_pasa_c1_y_sha_coincide(self):
        salida = Path(self.tmp.name) / "m.json"
        self._correr(salida)
        cargar_y_validar(salida, self.raiz)

    def test_csv_con_archivo_ausente_falla(self):
        with open(self.csv, "a", encoding="utf-8") as fh:
            fh.write("humanos_v3/A/no_existe.txt,A,2019,,true,true,x,u,l\n")
        with self.assertRaises(CommandError):
            self._correr(Path(self.tmp.name) / "m.json")

    def test_hash_estable_con_cambio_de_orden_en_csv(self):
        a, b = Path(self.tmp.name) / "a.json", Path(self.tmp.name) / "b.json"
        self._correr(a)
        lineas = self.csv.read_text(encoding="utf-8").splitlines()
        self.csv.write_text("\n".join([lineas[0], *reversed(lineas[1:])]) + "\n", encoding="utf-8")
        self._correr(b)
        self.assertEqual(sha256_archivo(a), sha256_archivo(b))
