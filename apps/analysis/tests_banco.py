"""
Tests del banco de similitud (generador y calificador). Sin red ni BD: los
textos son sintéticos y el "motor" se simula con rangos marcados a mano.
"""

from __future__ import annotations

import random
from decimal import Decimal

from django.test import SimpleTestCase

from apps.analysis.banco.calificador import (
    Baseline,
    EngineOutput,
    align_words,
    baseline_from_output,
    canonical_url,
    case_word_labels,
    score_case,
    summarize,
)
from apps.analysis.banco.generador import (
    CATEGORIES,
    BankCase,
    BankGenerator,
    BaseDocument,
    Insertion,
    SourceDocument,
    institute_key,
    splice,
)
from apps.analysis.banco.textos import (
    count_words,
    is_prose_sentence,
    paraphrase,
    passage_candidates,
    pick_phrase,
    split_excerpts,
    split_sentences,
)

VOCABULARY = (
    "aprendizaje docente estudiante lectura escritura juego aula escuela "
    "comunidad familia lenguaje matemática ciencia valores proyecto taller "
    "material cuento música danza dibujo pintura huerto biblioteca "
    "evaluación retroalimentación convivencia autonomía creatividad emoción"
).split()


FUNCTION_WORDS = "de la el en que los las del por con para una".split()


def sentence(rng: random.Random, words: int = 14) -> str:
    body = " ".join(
        rng.choice(FUNCTION_WORDS if index % 2 else VOCABULARY) for index in range(words)
    )
    return body[0].upper() + body[1:] + "."


def paragraph(rng: random.Random, sentences: int = 5) -> str:
    return " ".join(sentence(rng, rng.randint(12, 18)) for _ in range(sentences))


def document(rng: random.Random, paragraphs: int) -> str:
    return "\n\n".join(paragraph(rng) for _ in range(paragraphs))


def fake_inputs(seed: int = 7):
    rng = random.Random(seed)
    bases = [
        BaseDocument(id=f"upeu_{i}", origin="upeu", title=f"Base UPeU {i}", text=document(rng, 160))
        for i in range(6)
    ] + [
        BaseDocument(id=f"inst_{i}", origin="instituto", title=f"Tesis {i}", text=document(rng, 160))
        for i in range(9)
    ]
    corpus = [
        SourceDocument(key=f"oai:{i}", kind="corpus", title=f"Corpus {i}", text=document(rng, 20))
        for i in range(30)
    ]
    web = [
        SourceDocument(key=f"web:https://ejemplo.pe/{i}", kind="web", title=f"Web {i}", text=document(rng, 10))
        for i in range(25)
    ]
    return bases, corpus, web


class BankTextTests(SimpleTestCase):
    def test_passages_are_whole_sentences_within_limits(self) -> None:
        text = document(random.Random(1), 6)

        for passage in passage_candidates(text):
            self.assertTrue(60 <= count_words(passage) <= 130)
            self.assertIn(passage, " ".join(split_sentences(text)))

    def test_paraphrase_changes_words_and_order_without_ai(self) -> None:
        passage = (
            "Los estudiantes mejoran su comprensión con el juego. Sin embargo, los docentes "
            "señalan problemas importantes en el aula. Además, el proceso permite utilizar "
            "diferentes materiales."
        )
        result = paraphrase(passage)

        self.assertNotEqual(result, passage)
        self.assertIn("No obstante, los profesores indican dificultades relevantes", result)
        self.assertIn("Asimismo, el procedimiento posibilita emplear distintos materiales", result)
        # La primera oración pasa al final.
        self.assertTrue(result.endswith("Los alumnos mejoran su comprensión con el juego."))
        self.assertEqual(paraphrase(passage), result)

    def test_english_sentences_are_not_copyable(self) -> None:
        english = (
            "Therefore, it is important to contribute to the development of manual skills "
            "so that the motor skills of the children are adequate for the tasks."
        )
        spanish = (
            "Por ello, es importante contribuir al desarrollo de las habilidades manuales "
            "para que la motricidad de los niños sea adecuada en las tareas."
        )

        self.assertFalse(is_prose_sentence(english))
        self.assertTrue(is_prose_sentence(spanish))

    def test_phrase_does_not_cut_words_with_combining_accents(self) -> None:
        text = "La docente nombro\u0301 a cada niño de la sala con mucha calma y paciencia."
        rng = random.Random(0)

        for _ in range(30):
            phrase = pick_phrase(text, rng, 2, 3)
            self.assertFalse(phrase.endswith("nombro"))

    def test_phrase_has_requested_length(self) -> None:
        rng = random.Random(3)
        text = document(rng, 5)

        for _ in range(20):
            phrase = pick_phrase(text, rng, 5, 10)
            self.assertTrue(5 <= count_words(phrase) <= 10)
            self.assertIn(phrase, " ".join(split_sentences(text)))

    def test_excerpts_do_not_overlap_and_skip_the_start(self) -> None:
        text = document(random.Random(4), 100)
        excerpts = split_excerpts(text, 1000)

        self.assertGreaterEqual(len(excerpts), 5)
        self.assertNotIn(text.split("\n\n")[0], excerpts[0])
        for excerpt in excerpts:
            self.assertGreaterEqual(count_words(excerpt), 1000)


class BankGeneratorTests(SimpleTestCase):
    def test_same_seed_same_bank(self) -> None:
        bases, corpus, web = fake_inputs()
        first = BankGenerator(seed=11, excerpt_words=1500).generate(bases, corpus, web)
        second = BankGenerator(seed=11, excerpt_words=1500).generate(bases, corpus, web)
        other = BankGenerator(seed=12, excerpt_words=1500).generate(bases, corpus, web)

        self.assertEqual(first.to_json(), second.to_json())
        self.assertNotEqual(first.to_json(), other.to_json())

    def test_categories_and_answers(self) -> None:
        bases, corpus, web = fake_inputs()
        bank = BankGenerator(seed=5, excerpt_words=1500).generate(bases, corpus, web)
        by_category = {category: [c for c in bank.cases if c.category == category] for category in CATEGORIES}
        source_ids = {base.id for base in bank.institute_sources}

        self.assertEqual({category: len(items) for category, items in by_category.items()},
                         {category: 10 for category in CATEGORIES})
        self.assertEqual(len(source_ids), 6)
        # Ningún fragmento base se repite ni sale de una tesis usada como fuente.
        self.assertEqual(len({(c.base_id, c.excerpt) for c in bank.cases}), 60)
        self.assertFalse({c.base_id for c in bank.cases} & source_ids)

        for case in bank.cases:
            # El contenido conserva la base palabra por palabra.
            base_words = [
                case.content[s.start : s.end] for s in case.segments if s.kind == "base"
            ]
            self.assertEqual("".join(base_words), case.base_text)

            for segment in case.segments:
                if segment.kind == "insercion":
                    self.assertEqual(
                        case.content[segment.start : segment.end],
                        case.insertions[segment.insertion].text,
                    )

        self.assertTrue(all(not c.insertions for c in by_category["original"]))
        for category, kind in (("corpus", "corpus"), ("web", "web"), ("instituto", "instituto")):
            for case in by_category[category]:
                self.assertTrue(3 <= len(case.insertions) <= 5)
                self.assertEqual({i.source_kind for i in case.insertions}, {kind})
                self.assertEqual(len({i.source_key for i in case.insertions}), len(case.insertions))

        for case, original in zip(by_category["parafrasis"], by_category["instituto"]):
            self.assertEqual([i.original for i in case.insertions], [i.text for i in original.insertions])
            self.assertEqual([i.source_key for i in case.insertions], [i.source_key for i in original.insertions])
            self.assertNotEqual(case.base_id + str(case.excerpt), original.base_id + str(original.excerpt))

        for case in by_category["frases"]:
            self.assertTrue(8 <= len(case.insertions) <= 12)
            self.assertEqual(len({i.source_key for i in case.insertions}), len(case.insertions))
            self.assertTrue(all(5 <= count_words(i.text) <= 10 for i in case.insertions))

        for case in by_category["web"]:
            self.assertTrue({i.source_key for i in case.insertions} <= set(case.web_pages))
            self.assertLessEqual(len(case.web_pages), 16)

        self.assertEqual(sum(1 for c in bank.cases if c.files == ["docx"]), 6)
        self.assertEqual(sum(1 for c in bank.cases if c.files == ["pdf"]), 6)

    def test_json_round_trip(self) -> None:
        bases, corpus, web = fake_inputs()
        bank = BankGenerator(seed=5, excerpt_words=1500).generate(bases, corpus, web)

        for case in bank.cases:
            self.assertEqual(BankCase.from_json(case.to_json()).to_json(), case.to_json())


def manual_case() -> BankCase:
    base = "Uno dos tres cuatro.\n\nCinco seis siete ocho."
    insertion = Insertion(
        source_kind="instituto",
        source_key=institute_key("inst_a"),
        source_title="Tesis A",
        text="Texto copiado de otra tesis aquí.",
    )
    content, segments = splice(base, [insertion], random.Random(0))
    return BankCase(
        id="instituto_01", category="instituto", base_id="b", excerpt=0,
        base_text=base, content=content, segments=segments, insertions=[insertion],
    )


def ranges_of(text: str, *pieces: str) -> list[tuple[int, int]]:
    return [(text.index(piece), text.index(piece) + len(piece)) for piece in pieces]


class BankScorerTests(SimpleTestCase):
    def test_labels_follow_base_word_order(self) -> None:
        case = manual_case()
        labels = case_word_labels(case)

        self.assertEqual(case.content, "Uno dos tres cuatro.\n\nTexto copiado de otra tesis aquí.\n\nCinco seis siete ocho.")
        self.assertEqual(labels[:4], [("base", 0), ("base", 1), ("base", 2), ("base", 3)])
        self.assertEqual(labels[4:10], [("insercion", 0)] * 6)
        self.assertEqual(labels[10:], [("base", 4), ("base", 5), ("base", 6), ("base", 7)])

    def test_recall_and_new_false_positives_against_baseline(self) -> None:
        case = manual_case()
        baseline = baseline_from_output(
            EngineOutput(
                analyzed_text=case.base_text,
                marked_ranges=ranges_of(case.base_text, "Uno dos"),
                quoted_ranges=[],
                source_keys={"oai:x"},
                percent=Decimal("25.00"),
                seconds=0.1,
            )
        )
        self.assertEqual(baseline.marked_words, frozenset({0, 1}))

        output = EngineOutput(
            analyzed_text=case.content,
            # "Uno dos" ya estaba en la línea base; "siete ocho" es nuevo.
            marked_ranges=ranges_of(case.content, "Uno dos", "copiado de otra tesis", "siete ocho"),
            quoted_ranges=[],
            source_keys={"internal:doc-1", "oai:x", "web:https://otra.pe"},
            percent=Decimal("50.00"),
            seconds=1.5,
        )
        score = score_case(case, output, baseline, institute_keys={"internal:doc-1": "instituto:inst_a"})

        self.assertEqual(score.inserted_words, 6)
        self.assertEqual(score.detected_words, 4)
        self.assertEqual(score.word_recall, Decimal("66.7"))
        self.assertEqual(score.source_recall, Decimal("100.0"))
        self.assertEqual(score.new_false_positive_words, 2)
        self.assertEqual(score.baseline_words_marked_again, 2)
        self.assertEqual(score.extra_sources, ["web:otra.pe"])
        self.assertEqual(score.missed_sources, [])

    def test_quoted_inserted_words_do_not_count(self) -> None:
        case = manual_case()
        output = EngineOutput(
            analyzed_text=case.content,
            marked_ranges=[],
            quoted_ranges=ranges_of(case.content, "Texto copiado"),
            source_keys=set(),
            percent=Decimal("0"),
            seconds=0.1,
        )
        score = score_case(case, output, Baseline(frozenset(), frozenset(), Decimal("0")))

        self.assertEqual(score.inserted_words, 4)
        self.assertEqual(score.detected_words, 0)
        self.assertEqual(score.missed_sources, ["instituto:inst_a"])

    def test_extracted_text_is_aligned_by_words(self) -> None:
        case = manual_case()
        # Como sale de un PDF: otro espaciado, un encabezado y una palabra perdida.
        extracted = "Página 1\nUno dos tres cuatro. Texto copiado de otra aquí. Cinco seis siete ocho."
        mapping = align_words(case.content, extracted)

        self.assertNotIn(0, mapping)  # "Página"
        self.assertEqual(mapping[2], 0)  # "Uno"

        output = EngineOutput(
            analyzed_text=extracted,
            marked_ranges=ranges_of(extracted, "Página 1", "Texto copiado de otra aquí"),
            quoted_ranges=[],
            source_keys={"internal:doc-1"},
            percent=Decimal("40"),
            seconds=2.0,
        )
        score = score_case(
            case, output, Baseline(frozenset(), frozenset(), Decimal("0")),
            institute_keys={"internal:doc-1": "instituto:inst_a"}, variant="pdf",
        )

        # "tesis" se perdió en la extracción: cuenta como no detectada.
        self.assertEqual(score.inserted_words, 6)
        self.assertEqual(score.detected_words, 5)
        # El encabezado marcado no estaba en la base: falso positivo nuevo.
        self.assertEqual(score.new_false_positive_words, 2)

        summary = summarize([score])
        self.assertIn("instituto.pdf", summary)
        self.assertEqual(summary["instituto.pdf"]["recall_palabras"], "83.3")


class CanonicalUrlTests(SimpleTestCase):
    def assertSame(self, left: str, right: str) -> None:
        self.assertEqual(canonical_url(left), canonical_url(right))

    def test_equivalent_urls_are_the_same_source(self) -> None:
        self.assertSame(
            "https://revistamerito.org/index.php/merito/article/view/1869",
            "https://revistamerito.org/index.php/merito/article/download/1869/3558/8028",
        )
        self.assertSame("http://www.minedu.gob.pe/eib/", "https://minedu.gob.pe/eib")
        self.assertSame(
            "https://repositorio.ucv.edu.pe/handle/20.500.12692/99635",
            "https://repositorio.ucv.edu.pe/bitstream/handle/20.500.12692/99635/Tesis.pdf?sequence=1",
        )
        self.assertSame("https://ejemplo.pe/nota?utm_source=x#arriba", "https://ejemplo.pe/nota")
        self.assertSame(
            "https://scielo.org.bo/scielo.php?script=sci_arttext&pid=S2616-79642023000301423",
            "https://www.scielo.org.bo/scielo.php?pid=S2616-79642023000301423&script=sci_abstract",
        )

    def test_different_documents_stay_apart(self) -> None:
        self.assertNotEqual(
            canonical_url("https://revistamerito.org/index.php/merito/article/view/1869"),
            canonical_url("https://revistamerito.org/index.php/merito/article/view/1870"),
        )
        self.assertNotEqual(
            canonical_url("https://dialnet.unirioja.es/servlet/articulo?codigo=8588035"),
            canonical_url("https://dialnet.unirioja.es/servlet/articulo?codigo=7398049"),
        )
        self.assertNotEqual(
            canonical_url("https://www.bebegenial.com/cuentos-infantiles/"),
            canonical_url("https://www.bebegenial.com/blog/habito-de-lectura-primera-infancia/"),
        )

    def test_scorer_counts_download_link_as_expected_source(self) -> None:
        insertion = Insertion(
            source_kind="web",
            source_key="web:https://revista.pe/index.php/r/article/view/12",
            source_title="Artículo",
            text="Texto copiado de otra revista aquí.",
        )
        base = "Uno dos tres cuatro.\n\nCinco seis siete ocho."
        content, segments = splice(base, [insertion], random.Random(0))
        case = BankCase(
            id="web_01", category="web", base_id="b", excerpt=0,
            base_text=base, content=content, segments=segments, insertions=[insertion],
        )
        output = EngineOutput(
            analyzed_text=content,
            marked_ranges=ranges_of(content, "Texto copiado de otra revista aquí"),
            quoted_ranges=[],
            source_keys={"web:http://www.revista.pe/index.php/r/article/download/12/30/7/"},
            percent=Decimal("40"),
            seconds=0.1,
        )
        score = score_case(case, output, Baseline(frozenset(), frozenset(), Decimal("0")))

        self.assertEqual(score.source_recall, Decimal("100.0"))
        self.assertEqual(score.extra_sources, [])
        self.assertEqual(score.missed_sources, [])
