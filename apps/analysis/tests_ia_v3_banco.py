"""Tests de T09 (construir_banco_ia_v3). Sin red ni BD."""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.analysis.ia_v3.manifest import sha256_archivo


def _texto(prefijo: str, n: int = 12) -> str:
    return "\n\n".join(
        f"{prefijo} párrafo {i}: la enseñanza de la lectura en el nivel primario "
        f"requiere estrategias sostenidas y una evaluación formativa constante "
        f"durante todo el año escolar número {i}."
        for i in range(n)
    )


class ConstruirBancoTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name) / "eval_ia"
        (self.raiz / "t").mkdir(parents=True)
        self.entradas = []
        for part in ("train", "val", "test", "sellado", "no_vistos", "diagnostico"):
            for etq in ("humano", "ia"):
                if part in ("sellado", "no_vistos") and etq == "humano":
                    continue
                if part == "diagnostico" and etq == "ia":
                    continue
                for k in range(2):
                    self._add(f"{part}_{etq}_{k}", etq, part)

    def _add(self, id_, etq, part, publico=True):
        ruta = f"t/{id_}.txt"
        (self.raiz / ruta).write_text(_texto(id_), encoding="utf-8")
        self.entradas.append({
            "id": id_, "ruta": ruta, "sha256": sha256_archivo(self.raiz / ruta),
            "etiqueta": etq, "particion": part, "publico": publico,
            "familia": "humano" if etq == "humano" else "claude_api",
        })

    def _manifiesto(self):
        p = Path(self.tmp.name) / "manifiesto.json"
        p.write_text(json.dumps({"entradas": self.entradas}), encoding="utf-8")
        return p

    def _correr(self, salida, *extra):
        out = StringIO()
        call_command("construir_banco_ia_v3", "--manifiesto", str(self._manifiesto()),
                     "--raiz", str(self.raiz), "--salida", str(salida),
                     "--mixtos-por-objetivo", "2", *extra, stdout=out)
        return out.getvalue()

    def _jsonl(self, salida, part):
        with open(Path(salida) / f"{part}.jsonl", encoding="utf-8") as fh:
            return [json.loads(l) for l in fh]

    def test_para_colab_exporta_solo_train_y_val(self):
        salida = Path(self.tmp.name) / "colab"
        self._correr(salida, "--para-colab")
        nombres = {p.stem for p in salida.glob("*.jsonl")}
        self.assertEqual(nombres, {"train", "val"})
        for n in ("test", "sellado", "no_vistos", "diagnostico"):
            self.assertFalse((salida / f"{n}.jsonl").exists())

    def test_sin_para_colab_exporta_todo(self):
        salida = Path(self.tmp.name) / "full"
        self._correr(salida)
        nombres = {p.stem for p in salida.glob("*.jsonl")}
        self.assertEqual(nombres, {"train", "val", "test", "sellado", "no_vistos", "diagnostico"})
        # sellado/no_vistos/diagnostico solo llevan textos puros, sin mixtos
        self.assertTrue(all(f["tipo"] == "puro" for f in self._jsonl(salida, "sellado")))

    def test_publico_false_aborta_para_colab(self):
        self._add("privado", "humano", "train", publico=False)
        salida = Path(self.tmp.name) / "colab"
        with self.assertRaises(CommandError) as cm:
            self._correr(salida, "--para-colab")
        self.assertIn("privado", str(cm.exception))
        self.assertFalse(salida.exists())

    def test_publico_false_en_particion_excluida_no_aborta(self):
        self._add("priv_diag", "humano", "diagnostico", publico=False)
        self._add("priv_test", "humano", "test", publico=False)
        self._correr(Path(self.tmp.name) / "colab", "--para-colab")

    def test_publico_false_local_se_permite(self):
        self._add("privado", "humano", "train", publico=False)
        self._correr(Path(self.tmp.name) / "full")

    def test_hash_reproducible(self):
        a, b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        sa = self._correr(a)
        sb = self._correr(b)
        ja = json.loads((a / "banco.json").read_text())
        jb = json.loads((b / "banco.json").read_text())
        self.assertEqual(ja["hash_banco"], jb["hash_banco"])
        self.assertEqual(ja["particiones"], jb["particiones"])
        self.assertIn(ja["hash_banco"], sa)
        self.assertIn(jb["hash_banco"], sb)
        for part in ja["particiones"]:
            self.assertEqual((a / f"{part}.jsonl").read_bytes(), (b / f"{part}.jsonl").read_bytes())

    def test_semilla_distinta_cambia_el_hash(self):
        a, b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        self._correr(a, "--semilla", "1")
        self._correr(b, "--semilla", "2")
        self.assertNotEqual(json.loads((a / "banco.json").read_text())["hash_banco"],
                            json.loads((b / "banco.json").read_text())["hash_banco"])

    def test_mixtos_solo_dentro_de_su_particion(self):
        salida = Path(self.tmp.name) / "full"
        self._correr(salida)
        mixtos = [f for f in self._jsonl(salida, "val") if f["tipo"] == "mixto"]
        self.assertEqual(len(mixtos), 9 * 2)
        for m in mixtos:
            self.assertTrue(m["origen_humano"].startswith("val_"))
            self.assertTrue(m["origen_ia"].startswith("val_"))
            self.assertIn("tramos", m)
            self.assertTrue(0 <= m["pct_real_ia"] <= 100)

    def test_fuente_en_dos_particiones_aborta(self):
        dup = dict(self.entradas[0], id="dup", particion="val")
        self.entradas.append(dup)
        with self.assertRaises(CommandError):
            self._correr(Path(self.tmp.name) / "x")

    def test_hash_de_archivo_alterado_aborta(self):
        (self.raiz / self.entradas[0]["ruta"]).write_text("alterado", encoding="utf-8")
        with self.assertRaises(CommandError):
            self._correr(Path(self.tmp.name) / "x")
