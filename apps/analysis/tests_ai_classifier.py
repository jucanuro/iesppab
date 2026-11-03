from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.analysis.engines import ai_classifier
from apps.analysis.engines.ai_classifier import SegmentAIClassifier

def _prosa(seed: int, sentences: int = 18) -> str:
    return " ".join(
        f"El docente {seed}{i} analiza la estrategia número {chr(97 + i)}{seed} con el grupo de "
        f"estudiantes y registra hallazgos distintos sobre el aprendizaje de la lectura."
        for i in range(sentences)
    )


PROSA = _prosa(1)
TEXTO = _prosa(1) + "\n\n" + _prosa(2)


class ClassifierTests(SimpleTestCase):
    def _classifier(self, probabilities):
        classifier = SegmentAIClassifier(model_dir="x")
        patches = [
            mock.patch.object(SegmentAIClassifier, "predict", return_value=probabilities),
            mock.patch.object(SegmentAIClassifier, "threshold", new_callable=mock.PropertyMock, return_value=0.5),
        ]
        return classifier, patches

    def test_percent_is_share_of_words_above_threshold(self):
        pieces = ai_classifier.classifier_pieces(PROSA)
        self.assertTrue(pieces)
        classifier, patches = self._classifier([0.9] + [0.1] * 20)
        with patches[0], patches[1]:
            result = classifier.analyze(TEXTO)
        flagged = result.pieces[0].word_count
        self.assertEqual(
            result.ai_percent,
            (Decimal(flagged) * 100 / Decimal(result.evaluated_words)).quantize(Decimal("0.01")),
        )

    def test_empty_text_gives_zero(self):
        classifier, patches = self._classifier([])
        with patches[0], patches[1]:
            self.assertEqual(classifier.analyze("").ai_percent, Decimal("0.00"))

    def test_missing_model_raises(self):
        ai_classifier._loaded.clear()
        with self.assertRaises(ai_classifier.ClassifierUnavailableError):
            SegmentAIClassifier(model_dir="/no/existe").predict(["hola"])


class MemoryTests(SimpleTestCase):
    def test_batch_size_and_threads_are_small(self):
        self.assertEqual(ai_classifier.BATCH_SIZE, 2)
        self.assertEqual(ai_classifier.NUM_THREADS, 2)

    def test_predict_runs_in_batches_of_two_and_frees_memory(self):
        import torch

        calls = []

        class FakeModel:
            def __call__(self, **batch):
                calls.append(batch["input_ids"].shape[0])
                return mock.Mock(logits=torch.zeros(batch["input_ids"].shape[0], 2))

        def fake_tokenizer(texts, **kwargs):
            return {"input_ids": torch.zeros(len(texts), 4, dtype=torch.long)}

        ai_classifier._loaded["x"] = (fake_tokenizer, FakeModel(), 0.5, 384, torch)
        try:
            probabilities = SegmentAIClassifier(model_dir="x").predict(["a"] * 5)
        finally:
            ai_classifier._loaded.clear()
        self.assertEqual(calls, [2, 2, 1])
        self.assertEqual(len(probabilities), 5)

    def test_analyze_unloads_model_and_threshold_does_not_load_it(self):
        ai_classifier._loaded["x"] = ("tok", "model", 0.5, 384, None)
        classifier = SegmentAIClassifier(model_dir="x")
        with mock.patch.object(ai_classifier, "_read_config", return_value={"umbral": 0.7}) as cfg, \
                mock.patch.object(SegmentAIClassifier, "predict", return_value=[]):
            self.assertEqual(classifier.threshold, 0.7)
            classifier.analyze("")
        self.assertTrue(cfg.called)
        self.assertNotIn("x", ai_classifier._loaded)


class ServiceModeTests(SimpleTestCase):
    @override_settings(AI_DETECTION_MODE="classifier", PERPLEXITY_AI_DETECTION_ENABLED=False)
    def test_classifier_does_not_change_percent_or_detector(self):
        from apps.analysis.services import DocumentAnalysisService

        service = DocumentAnalysisService.__new__(DocumentAnalysisService)
        heuristic = mock.Mock(ai_probability_percent=Decimal("12.00"))
        fake = mock.Mock()
        fake.analyze.return_value.to_breakdown.return_value = {"indicative": True, "ai_percent": "99.00"}
        fake.analyze.return_value.ai_percent = Decimal("99.00")
        fake.analyze.return_value.pieces = []
        with mock.patch("apps.analysis.services.SegmentAIClassifier", return_value=fake):
            percent, breakdown, detector = service._estimate_ai_probability(
                document=mock.Mock(id=1), analysis_content="x", heuristic_result=heuristic
            )
        self.assertEqual(percent, Decimal("12.00"))
        self.assertEqual(detector, DocumentAnalysisService.AI_DETECTOR_HEURISTIC)
        self.assertEqual(breakdown["classifier"]["ai_percent"], "99.00")

    @override_settings(AI_DETECTION_MODE="classifier", PERPLEXITY_AI_DETECTION_ENABLED=False)
    def test_classifier_failure_is_swallowed(self):
        from apps.analysis.services import DocumentAnalysisService

        service = DocumentAnalysisService.__new__(DocumentAnalysisService)
        heuristic = mock.Mock(ai_probability_percent=Decimal("12.00"))
        with mock.patch("apps.analysis.services.SegmentAIClassifier", side_effect=RuntimeError):
            percent, breakdown, _ = service._estimate_ai_probability(
                document=mock.Mock(id=1), analysis_content="x", heuristic_result=heuristic
            )
        self.assertEqual(percent, Decimal("12.00"))
        self.assertTrue(breakdown["classifier"]["error"])
