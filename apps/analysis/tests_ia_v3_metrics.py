"""Tests de C3 (métricas IA v3, parte 1: FPR + Clopper-Pearson). Sin red ni BD."""

import math

from django.test import SimpleTestCase

from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.metrics import (
    clopper_pearson,
    contar_falsos_positivos,
    curva_fiabilidad,
    evaluar_fpr,
    evaluar_mae,
    evaluar_recall,
)


class FPRClopperPearsonTests(SimpleTestCase):
    def test_cero_de_183_cumple(self):
        r = evaluar_fpr(0, 183)
        self.assertTrue(r.cumple)
        self.assertEqual(r.veredicto, "CUMPLE")
        self.assertEqual(r.fpr, 0.0)
        self.assertLessEqual(r.ic_superior, 0.02)

    def test_uno_de_183_no_cumple(self):
        r = evaluar_fpr(1, 183)
        self.assertFalse(r.cumple)
        self.assertEqual(r.veredicto, "NO CUMPLE")
        self.assertGreater(r.ic_superior, 0.02)

    def test_cero_exitos_forma_cerrada(self):
        # Cota bilateral 95 % con k=0: 1 - (0,025)^(1/n)
        esperado = 1 - 0.025 ** (1 / 183)
        _, sup = clopper_pearson(0, 183)
        self.assertAlmostEqual(sup, esperado, places=8)

    def test_todos_falsos_forma_cerrada(self):
        inf, sup = clopper_pearson(10, 10)
        self.assertAlmostEqual(inf, 0.025 ** (1 / 10), places=8)
        self.assertEqual(sup, 1.0)

    def test_valores_de_referencia_tabulados(self):
        # (k, n, inferior, superior) — tablas estándar de IC exacto 95 %
        referencias = [
            (1, 10, 0.0025, 0.4450),
            (2, 20, 0.0123, 0.3170),
            (5, 100, 0.0164, 0.1128),
        ]
        for k, n, inf_ref, sup_ref in referencias:
            with self.subTest(k=k, n=n):
                inf, sup = clopper_pearson(k, n)
                self.assertAlmostEqual(inf, inf_ref, delta=5e-4)
                self.assertAlmostEqual(sup, sup_ref, delta=5e-4)

    def test_el_intervalo_contiene_la_proporcion(self):
        for k, n in [(0, 50), (3, 183), (20, 200)]:
            inf, sup = clopper_pearson(k, n)
            self.assertLessEqual(inf, k / n)
            self.assertGreaterEqual(sup, k / n)
            self.assertTrue(math.isfinite(inf) and math.isfinite(sup))

    def test_entradas_invalidas(self):
        with self.assertRaises(IABankError):
            evaluar_fpr(0, 0)
        with self.assertRaises(IABankError):
            evaluar_fpr(5, 4)
        with self.assertRaises(IABankError):
            evaluar_fpr(-1, 10)

    def test_contar_falsos_positivos_usa_umbral_inclusivo(self):
        self.assertEqual(contar_falsos_positivos([0, 19.9, 20, 55]), 2)


class MAETests(SimpleTestCase):
    def test_mae_9_9_cumple(self):
        r = evaluar_mae([19.9, 29.9], [10.0, 20.0])
        self.assertAlmostEqual(r.mae, 9.9)
        self.assertTrue(r.cumple)
        self.assertEqual(r.veredicto, "CUMPLE")

    def test_mae_10_1_no_cumple(self):
        r = evaluar_mae([20.1, 30.1], [10.0, 20.0])
        self.assertAlmostEqual(r.mae, 10.1)
        self.assertFalse(r.cumple)
        self.assertEqual(r.veredicto, "NO CUMPLE")

    def test_p90_se_reporta_pero_no_decide(self):
        estimados = [0.0] * 9 + [100.0]
        r = evaluar_mae(estimados, [0.0] * 10)
        self.assertAlmostEqual(r.mae, 10.0)
        self.assertAlmostEqual(r.p90, 10.0)
        self.assertTrue(r.cumple)

    def test_entradas_invalidas(self):
        with self.assertRaises(IABankError):
            evaluar_mae([], [])
        with self.assertRaises(IABankError):
            evaluar_mae([1.0], [1.0, 2.0])


def _docs(familia, vista, detectados, total):
    return [(familia, vista, 50.0 if i < detectados else 5.0) for i in range(total)]


class RecallTests(SimpleTestCase):
    def test_separa_vistas_y_no_vistas(self):
        docs = (
            _docs("claude", True, 20, 20)
            + _docs("llama", False, 14, 20)
            + _docs("mistral", False, 15, 20)
            + _docs("qwen", False, 16, 20)
        )
        r = evaluar_recall(docs)
        marcas = {f.familia: f.vista for f in r.familias}
        self.assertTrue(marcas["claude"])
        self.assertFalse(marcas["llama"])
        self.assertEqual(r.n_no_vistos, 60)
        self.assertEqual(r.familias_no_vistas, 3)
        self.assertFalse(r.insuficiente)
        # Solo no vistas: 45/60 = 0,75 → CUMPLE (las vistas no inflan la cifra)
        self.assertAlmostEqual(r.recall_no_vistos, 0.75)
        self.assertEqual(r.veredicto, "CUMPLE")
        llama = next(f for f in r.familias if f.familia == "llama")
        self.assertAlmostEqual(llama.recall, 0.7)
        self.assertLess(llama.ic_inferior, 0.7)
        self.assertGreater(llama.ic_superior, 0.7)

    def test_recall_bajo_no_cumple(self):
        docs = (
            _docs("a", False, 10, 20)
            + _docs("b", False, 10, 20)
            + _docs("c", False, 10, 20)
        )
        self.assertEqual(evaluar_recall(docs).veredicto, "NO CUMPLE")

    def test_menos_de_60_documentos_insuficiente(self):
        docs = _docs("a", False, 19, 19) + _docs("b", False, 20, 20) + _docs("c", False, 20, 20)
        r = evaluar_recall(docs)
        self.assertTrue(r.insuficiente)
        self.assertEqual(r.veredicto, "INSUFICIENTE")

    def test_menos_de_3_familias_insuficiente(self):
        docs = _docs("a", False, 40, 40) + _docs("b", False, 40, 40)
        self.assertTrue(evaluar_recall(docs).insuficiente)

    def test_n_bajo_por_familia(self):
        docs = _docs("a", False, 10, 14) + _docs("b", False, 15, 15)
        r = evaluar_recall(docs)
        marcas = {f.familia: f.n_bajo for f in r.familias}
        self.assertTrue(marcas["a"])
        self.assertFalse(marcas["b"])

    def test_sin_no_vistos(self):
        r = evaluar_recall(_docs("claude", True, 10, 10))
        self.assertTrue(r.insuficiente)
        self.assertIsNone(r.recall_no_vistos)


class CalibracionTests(SimpleTestCase):
    def test_calibradas_ece_cercano_a_cero(self):
        probs, etiquetas = [], []
        for p, positivos in [(0.05, 1), (0.25, 5), (0.55, 11), (0.85, 17), (0.95, 19)]:
            probs += [p] * 20
            etiquetas += [1] * positivos + [0] * (20 - positivos)
        r = curva_fiabilidad(probs, etiquetas)
        self.assertLess(r.ece, 0.01)
        self.assertTrue(r.cumple)
        self.assertEqual(len(r.tramos), 10)
        self.assertEqual(sum(t.n for t in r.tramos), 100)

    def test_sesgadas_ece_mayor_que_meta(self):
        # Dice 0,9 pero acierta el 60 %
        probs = [0.9] * 50
        etiquetas = [1] * 30 + [0] * 20
        r = curva_fiabilidad(probs, etiquetas)
        self.assertAlmostEqual(r.ece, 0.3)
        self.assertFalse(r.cumple)
        self.assertEqual(r.veredicto, "NO CUMPLE")

    def test_tramos_vacios_y_p_igual_a_uno(self):
        r = curva_fiabilidad([1.0, 0.0], [1, 0])
        self.assertEqual(r.tramos[9].n, 1)
        self.assertEqual(r.tramos[0].n, 1)
        self.assertIsNone(r.tramos[5].prob_media)
        self.assertEqual(r.ece, 0.0)

    def test_entradas_invalidas(self):
        with self.assertRaises(IABankError):
            curva_fiabilidad([], [])
        with self.assertRaises(IABankError):
            curva_fiabilidad([1.2], [1])
        with self.assertRaises(IABankError):
            curva_fiabilidad([0.5], [1, 0])
