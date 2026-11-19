"""Tests de C2 (constructor de mixtos IA v3). Sin red ni BD."""

from django.test import SimpleTestCase

from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.mixtos import construir_mixto


def _texto(prefijo: str, n: int) -> str:
    return "\n\n".join(
        f"{prefijo} párrafo {i}: la enseñanza de la lectura en el nivel primario "
        f"requiere estrategias sostenidas y una evaluación formativa constante "
        f"durante todo el año escolar número {i}."
        for i in range(n)
    )


def _e(id_, etiqueta, particion="val"):
    return {"id": id_, "etiqueta": etiqueta, "particion": particion}


HUM = _texto("Humano", 24)
IA = _texto("Sintético", 24)


class MixtosTests(SimpleTestCase):
    def _mixto(self, objetivo, semilla=7, h=None, ia=None):
        return construir_mixto(
            h or _e("h1", "humano"), ia or _e("a1", "ia"), HUM, IA, objetivo, semilla
        )

    def test_determinismo_misma_semilla_mismo_sha(self):
        a = self._mixto(50, semilla=11)
        b = self._mixto(50, semilla=11)
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual(a["texto"], b["texto"])

    def test_semilla_distinta_cambia_el_documento(self):
        shas = {self._mixto(50, semilla=s)["sha256"] for s in range(1, 6)}
        self.assertGreater(len(shas), 1)

    def test_extremos_exactos(self):
        cero = self._mixto(0)
        cien = self._mixto(100)
        self.assertEqual(cero["pct_real_ia"], 0.0)
        self.assertEqual(cien["pct_real_ia"], 100.0)
        self.assertNotIn("Sintético", cero["texto"])
        self.assertNotIn("Humano", cien["texto"])

    def test_pct_real_coincide_con_tramos(self):
        for objetivo in range(10, 100, 10):
            doc = self._mixto(objetivo)
            ia = sum(t["palabras_evaluables"] for t in doc["tramos"] if t["etiqueta"] == "ia")
            total = sum(t["palabras_evaluables"] for t in doc["tramos"])
            self.assertAlmostEqual(doc["pct_real_ia"], round(100 * ia / total, 2), places=2)
            self.assertLessEqual(abs(doc["pct_real_ia"] - objetivo), 15, objetivo)

    def test_tramos_cubren_texto_en_orden_y_con_origen(self):
        doc = self._mixto(40)
        previo_fin = -1
        for t in doc["tramos"]:
            self.assertGreater(t["inicio"], previo_fin)
            previo_fin = t["fin"]
            fragmento = doc["texto"][t["inicio"] : t["fin"]]
            marca = "Sintético" if t["etiqueta"] == "ia" else "Humano"
            self.assertIn(marca, fragmento)
            self.assertEqual(t["origen_id"], "a1" if t["etiqueta"] == "ia" else "h1")
        self.assertEqual({t["etiqueta"] for t in doc["tramos"]}, {"ia", "humano"})

    def test_no_mezcla_particiones_distintas(self):
        with self.assertRaises(IABankError):
            self._mixto(50, h=_e("h1", "humano", "train"), ia=_e("a1", "ia", "val"))

    def test_rechaza_particiones_no_mezclables(self):
        for p in ("test_fpr_no_existe", "sellado", "no_vistos", "diagnostico"):
            with self.assertRaises(IABankError):
                self._mixto(50, h=_e("h1", "humano", p), ia=_e("a1", "ia", p))

    def test_rechaza_etiquetas_invertidas_y_objetivo_invalido(self):
        with self.assertRaises(IABankError):
            self._mixto(50, h=_e("a1", "ia"), ia=_e("h1", "humano"))
        with self.assertRaises(IABankError):
            self._mixto(101)
