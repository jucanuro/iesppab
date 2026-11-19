"""Tests de C1 (manifiesto del banco IA v3). Sin red ni BD."""

import hashlib
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.manifest import (
    cargar_manifiesto,
    cargar_y_validar,
    evaluar_no_vistos,
    evaluar_test_fpr,
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
