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
    def test_classifier_percent_is_the_official_one_and_heuristic_stays_in_breakdown(self):
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
        self.assertEqual(percent, Decimal("99.00"))
        self.assertEqual(detector, DocumentAnalysisService.AI_DETECTOR_CLASSIFIER)
        self.assertEqual(breakdown["classifier"]["ai_percent"], "99.00")
        self.assertEqual(breakdown["heuristic_ai_score"], "12.00")

    @override_settings(AI_DETECTION_MODE="classifier", PERPLEXITY_AI_DETECTION_ENABLED=False)
    def test_not_evaluable_saves_zero(self):
        from apps.analysis.services import DocumentAnalysisService

        service = DocumentAnalysisService.__new__(DocumentAnalysisService)
        heuristic = mock.Mock(ai_probability_percent=Decimal("12.00"))
        fake = mock.Mock()
        fake.analyze.return_value.to_breakdown.return_value = {
            "indicative": True, "evaluable": False, "ai_percent": "0.00",
        }
        fake.analyze.return_value.ai_percent = Decimal("0.00")
        fake.analyze.return_value.pieces = []
        with mock.patch("apps.analysis.services.SegmentAIClassifier", return_value=fake):
            percent, _, _ = service._estimate_ai_probability(
                document=mock.Mock(id=1), analysis_content="x", heuristic_result=heuristic
            )
        self.assertEqual(percent, Decimal("0.00"))

    @override_settings(AI_DETECTION_MODE="classifier", PERPLEXITY_AI_DETECTION_ENABLED=False)
    def test_classifier_failure_is_swallowed(self):
        from apps.analysis.services import DocumentAnalysisService

        service = DocumentAnalysisService.__new__(DocumentAnalysisService)
        heuristic = mock.Mock(ai_probability_percent=Decimal("12.00"))
        with mock.patch("apps.analysis.services.SegmentAIClassifier", side_effect=RuntimeError):
            percent, breakdown, _ = service._estimate_ai_probability(
                document=mock.Mock(id=1), analysis_content="x", heuristic_result=heuristic
            )
        self.assertEqual(percent, Decimal("12.00"))  # sin clasificador: queda la heurística
        self.assertTrue(breakdown["classifier"]["error"])


INGLES = " ".join(
    f"The study {i} of the teachers is based on the results that they found in the classroom "
    f"and the students have been using this method with their own time in the school."
    for i in range(18)
)


class NoEvaluableTests(SimpleTestCase):
    _classifier = ClassifierTests._classifier

    def test_mostly_excluded_segments_are_not_evaluable_and_skip_the_model(self):
        # Una sola "lista" de viñetas: todo el documento queda excluido.
        lista = "\n".join(f"• Objetivo {i} del plan de trabajo" for i in range(120))
        classifier = SegmentAIClassifier(model_dir="x")
        with mock.patch.object(SegmentAIClassifier, "predict") as predict, \
                mock.patch.object(SegmentAIClassifier, "threshold", new_callable=mock.PropertyMock, return_value=0.5):
            result = classifier.analyze(lista)
        predict.assert_not_called()
        self.assertFalse(result.evaluable)
        breakdown = result.to_breakdown()
        self.assertIs(breakdown["evaluable"], False)
        self.assertEqual(breakdown["evaluated_words"], 0)
        self.assertIn("no evaluable", breakdown["note"])

    def test_fewer_than_200_words_is_not_evaluable(self):
        corto = _prosa(3, sentences=5)
        classifier, patches = self._classifier([0.9] * 10)
        with patches[0] as predict, patches[1]:
            result = classifier.analyze(corto)
        predict.assert_not_called()
        self.assertFalse(result.evaluable)
        self.assertEqual(result.not_evaluable_reason, "pocas-palabras-evaluadas")

    def test_normal_document_is_evaluable(self):
        classifier, patches = self._classifier([0.1] * 20)
        with patches[0], patches[1]:
            result = classifier.analyze(TEXTO)
        self.assertTrue(result.evaluable)
        self.assertIs(result.to_breakdown()["evaluable"], True)

    def test_english_segment_is_excluded_from_the_classifier(self):
        classifier, patches = self._classifier([0.1] * 20)
        # Solo el segundo párrafo se considera en otro idioma.
        marcador = iter([False, False, True, True, True, True, True, True, True, True])
        with patches[0], patches[1], mock.patch.object(
            ai_classifier, "is_other_language", side_effect=lambda _t: next(marcador, False)
        ):
            result = classifier.analyze(TEXTO)
        reasons = [s.excluded_reason for s in result.segments]
        self.assertIn("otro-idioma", reasons)
        self.assertTrue(result.evaluable)
        english = [s for s in result.segments if s.excluded_reason == "otro-idioma"]
        self.assertTrue(all(not s.pieces for s in english))

    def test_only_flagged_pieces_carry_text(self):
        classifier, patches = self._classifier([0.9] + [0.1] * 20)
        with patches[0], patches[1]:
            breakdown = classifier.analyze(TEXTO).to_breakdown()
        pieces = [p for s in breakdown["segments"] for p in s["pieces"]]
        self.assertEqual(sum("text" in p for p in pieces), 1)
        self.assertEqual(breakdown["flagged_pieces"], 1)

    def test_pieces_below_50_percent_are_not_listed_but_still_count(self):
        # Umbral del modelo muy bajo: 0.3 cuenta para el % pero no se lista.
        classifier = SegmentAIClassifier(model_dir="x")
        with mock.patch.object(
            SegmentAIClassifier, "predict", return_value=[0.9, 0.3] + [0.0001] * 20
        ), mock.patch.object(
            SegmentAIClassifier, "threshold", new_callable=mock.PropertyMock, return_value=0.0003
        ):
            result = classifier.analyze(TEXTO)
        breakdown = result.to_breakdown()
        pieces = [p for s in breakdown["segments"] for p in s["pieces"]]
        self.assertEqual(sum("text" in p for p in pieces), 1)
        self.assertEqual(breakdown["flagged_pieces"], 2)
        self.assertEqual(result.ai_percent, Decimal(str(result.ai_percent)))
        self.assertGreater(result.ai_percent, 0)

    def test_model_listing_ignores_old_pieces_below_50(self):
        from apps.reports.models import AnalysisReport

        report = AnalysisReport(summary={"ai_score_breakdown": {"classifier": {
            "ai_percent": "10.00", "threshold": 0.0003,
            "segments": [{"pieces": [
                {"text": "alta", "probability": 0.8, "words": 10},
                {"text": "baja", "probability": 0.2, "words": 10},
            ]}],
        }}})
        self.assertEqual([p["text"] for p in report.ai_classifier_flagged_pieces], ["alta"])


class LanguageTests(SimpleTestCase):
    def test_spanish_and_english(self):
        from apps.analysis.engines.language import is_other_language

        self.assertFalse(is_other_language(PROSA))
        self.assertTrue(is_other_language(INGLES))
        self.assertFalse(is_other_language("Muy corto"))
