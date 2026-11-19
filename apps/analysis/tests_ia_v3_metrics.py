"""Tests de C3 (métricas IA v3, parte 1: FPR + Clopper-Pearson). Sin red ni BD."""

import math

from django.test import SimpleTestCase

from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.metrics import (
    clopper_pearson,
    contar_falsos_positivos,
    evaluar_fpr,
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
