"""
Prueba de identidad para el batching adaptativo de compute_burstiness()
(apps/analysis/engines/perplexity_detector.py).

Carga el modelo GPT-2 real (~1GB de RAM, varios segundos de carga), así que
NO se llama "test_*.py"/"tests.py" a propósito: `manage.py test` con el
patrón de descubrimiento por defecto ("test*.py") no la recoge. Se corre
explícitamente:

    python manage.py test apps.analysis.perplexity_batching_manual_tests
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.analysis.engines import perplexity_detector as pd

FIXTURES_DIR = (
    Path(__file__).resolve().parent / "evaluation" / "fixtures"
)


def _load_sample_text() -> str:
    """
    Concatena una muestra moderada de los fixtures reales de evaluación
    (humano + IA) — suficientes oraciones para que el presupuesto de
    memoria grande arme un solo lote y el presupuesto mínimo arme decenas
    de lotes de 1 oración, sin que la prueba tarde minutos.
    """
    chunks: list[str] = []

    for subdir in ("human_samples", "ai_samples"):
        directory = FIXTURES_DIR / subdir
        files = sorted(os.listdir(directory))[:12]

        for filename in files:
            chunks.append((directory / filename).read_text(encoding="utf-8"))

    return "\n\n".join(chunks)


class BurstinessAdaptiveBatchingIdentityTests(SimpleTestCase):
    """
    Regla de oro: el batching cambia CÓMO se agrupan las oraciones para el
    forward pass, nunca EL VALOR calculado. Se fuerza el mismo documento a
    procesarse con un presupuesto de memoria enorme (un solo lote grande) y
    con un presupuesto ínfimo (lotes de 1 oración, el mínimo absoluto) y se
    exige el mismo resultado final.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pd._get_model_and_tokenizer()  # precarga una sola vez para ambas corridas
        cls.sample_text = _load_sample_text()

    def _analyze_with_forced_budget(self, budget_bytes: int) -> pd.PerplexityAnalysisResult:
        with mock.patch.object(
            pd, "_available_batch_memory_budget_bytes", return_value=budget_bytes
        ):
            return pd.SpanishPerplexityDetector().analyze(self.sample_text)

    def test_large_budget_matches_minimum_budget(self):
        # Presupuesto real de la máquina (sin mockear): con un documento de
        # muestra moderado agrupa varias decenas de oraciones por lote. No
        # se fuerza un valor "grande" arbitrario (p.ej. 10GB) porque podría
        # superar la RAM real disponible en la máquina que corre la prueba
        # y auto-provocar el mismo OOM que este cambio busca evitar.
        large_batch_result = pd.SpanishPerplexityDetector().analyze(self.sample_text)
        # 1 byte: ni la oración más corta cabe, así que
        # `_pack_indices_by_memory_budget` cae en su mínimo absoluto de 1
        # oración por lote en cada paso.
        minimum_batch_result = self._analyze_with_forced_budget(1)

        self.assertEqual(
            large_batch_result.sentence_count,
            minimum_batch_result.sentence_count,
        )
        self.assertIsNotNone(large_batch_result.burstiness)
        self.assertIsNotNone(minimum_batch_result.burstiness)

        self.assertAlmostEqual(
            large_batch_result.mean_perplexity,
            minimum_batch_result.mean_perplexity,
            places=4,
        )
        self.assertAlmostEqual(
            large_batch_result.burstiness,
            minimum_batch_result.burstiness,
            places=1,
        )
        self.assertEqual(
            large_batch_result.ai_probability_percent,
            minimum_batch_result.ai_probability_percent,
        )
