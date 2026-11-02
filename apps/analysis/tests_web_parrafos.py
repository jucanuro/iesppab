"""
Tests de la búsqueda web por párrafos (WEB_QUERY_MODE="parrafos"), del
cliente Brave con extra_snippets y de la lectura de PDFs de fichas de
repositorio. Sin red: Brave, las páginas y robots.txt son falsos.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.analysis.banco.ejecucion import CacheWriteRecorder
from apps.analysis.banco.generador import FULL_WEB_CASES, BankGenerator
from apps.analysis.engines.web_search import (
    SOURCE_FORMAT_SNIPPET,
    TIER_CITED,
    TIER_INTERNAL,
    TIER_LIST,
    TIER_PROSE,
    BraveWebSearchClient,
    WebCandidateCollector,
    WebPageContent,
    WebPageFetcher,
    WebSearchResult,
    clean_snippet,
    shares_run,
)
from apps.analysis.models import WebSearchCacheEntry

PARAGRAPHS = [
    "La comprensión lectora constituye una competencia fundamental para el desarrollo "
    "integral de los estudiantes, porque permite interpretar críticamente diversos textos "
    "académicos y relacionarlos con experiencias cotidianas dentro del aula.",
    "Según investigaciones previas (Pérez, 2019) la producción escrita favorece notablemente "
    "la organización coherente del pensamiento infantil, especialmente cuando los docentes "
    "acompañan pacientemente cada borrador elaborado por sus alumnos.",
    "- primer elemento de la lista con varias palabras sueltas\n"
    "- segundo elemento de la lista con varias palabras sueltas\n"
    "La expresión oral espontánea fortalece considerablemente la seguridad comunicativa "
    "de los niños pequeños cuando participan activamente en asambleas cotidianas organizadas "
    "cuidadosamente por la maestra responsable.",
    "La psicomotricidad infantil desarrolla gradualmente el equilibrio corporal, la "
    "coordinación dinámica general y la orientación espacial mediante juegos libres "
    "realizados frecuentemente en ambientes amplios y seguros.",
    "Texto corto.",
    "La convivencia escolar armoniosa depende principalmente de normas consensuadas "
    "democráticamente entre estudiantes, familias y docentes comprometidos con relaciones "
    "respetuosas, solidarias y verdaderamente inclusivas.",
]
# Cada párrafo largo suma una oración con cifras (no da frase buscable)
# para pasar de PARAGRAPH_MIN_WORDS.
FILLER = " Esto se comprobó en 2021 con 45 estudiantes de 3 aulas distintas."
PARAGRAPHS = [p if p == "Texto corto." else p + FILLER for p in PARAGRAPHS]
DOCUMENT = "\n\n".join(PARAGRAPHS)


def phrase_of(collector: WebCandidateCollector, paragraph_index: int) -> str:
    return next(p.phrase for p in collector._query_paragraphs(DOCUMENT, []) if p.index == paragraph_index)


class FakeBrave:
    """`responses`: frase (sin comillas) -> resultados; registra las consultas."""

    def __init__(self, responses=None, quoted_only=True):
        self.responses = responses or {}
        self.queries: list[str] = []

    def search(self, query: str, count: int = 5):
        self.queries.append(query)
        return list(self.responses.get(query, []))


class FakeFetcher:
    def __init__(self, failing: set[str] | None = None):
        self.failing = failing or set()
        self.fetched: list[str] = []

    def fetch(self, result):
        self.fetched.append(result.url)
        if result.url in self.failing:
            return None
        return WebPageContent(
            title=result.title, url=result.url, domain=result.domain,
            text="texto de la página " * 50, source_format="HTML",
        )


def result(url: str, snippet: str = "", extra=()) -> WebSearchResult:
    return WebSearchResult(
        title="Título", url=url, domain=url.split("/")[2], snippet=snippet,
        extra_snippets=tuple(extra),
    )


@override_settings(
    WEB_ANALYSIS_ENABLED=True,
    WEB_QUERY_MODE="parrafos",
    WEB_ANALYSIS_MAX_QUERIES=60,
    WEB_ANALYSIS_MAX_PAGES_TOTAL=16,
    WEB_ANALYSIS_MAX_PAGES_PER_QUERY=3,
    WEB_RESULTS_PER_QUERY=20,
    WEB_USE_EXTRA_SNIPPETS=True,
    WEB_SNIPPET_MATCH=True,
    WEB_UNQUOTED_RETRY=True,
    WEB_ADAPTIVE_NEIGHBORS=True,
    WEB_PAGE_VOTES=True,
    SIMILARITY_MIN_MATCH_WORDS=8,
)
class ParagraphQueryTests(SimpleTestCase):
    def collector(self, responses=None, failing=None, **attrs) -> WebCandidateCollector:
        collector = WebCandidateCollector()
        collector.search_client = FakeBrave(responses)
        collector.fetcher = FakeFetcher(failing)
        for name, value in attrs.items():
            setattr(collector, name, value)
        return collector

    def test_paragraph_tiers_and_phrases(self) -> None:
        collector = self.collector()
        last_start = DOCUMENT.index(PARAGRAPHS[-1])
        paragraphs = collector._query_paragraphs(
            DOCUMENT, covered_ranges=[(last_start, last_start + len(PARAGRAPHS[-1]))]
        )

        # El párrafo corto no es buscable.
        self.assertEqual(
            [p.tier for p in paragraphs],
            [TIER_PROSE, TIER_CITED, TIER_LIST, TIER_PROSE, TIER_INTERNAL],
        )
        for paragraph in paragraphs:
            words = paragraph.phrase.split()
            self.assertTrue(8 <= len(words) <= 14)
            self.assertFalse(any(char.isdigit() for char in paragraph.phrase))
            self.assertNotIn("(", paragraph.phrase)

    def test_cap_spends_queries_on_prose_without_citations_first(self) -> None:
        collector = self.collector(max_queries=2)
        collector.collect(DOCUMENT)

        paragraphs = collector._query_paragraphs(DOCUMENT, [])
        prose_phrases = {f'"{p.phrase}"' for p in paragraphs if p.tier == TIER_PROSE}
        self.assertEqual(len(collector.search_client.queries), 2)
        self.assertTrue(set(collector.search_client.queries) <= prose_phrases)

    def test_no_unquoted_round(self) -> None:
        collector = self.collector()
        collector.collect(DOCUMENT)

        self.assertTrue(all(query.startswith('"') for query in collector.search_client.queries))
        self.assertEqual(collector.last_stats["consultas"], 5)

    def test_repeated_paragraph_does_not_spend_another_query(self) -> None:
        collector = self.collector()
        collector.collect(DOCUMENT + "\n\n" + PARAGRAPHS[0])

        self.assertEqual(len(collector.search_client.queries), 5)

    def test_hit_brings_neighbor_paragraph_forward(self) -> None:
        base = self.collector()
        paragraphs = base._query_paragraphs(DOCUMENT, [])
        # Párrafo 3 (prosa) coincide: su vecino 2 (lista, prioridad baja)
        # pasa delante del párrafo con citas aunque el tope sea 4.
        target = paragraphs[3]
        responses = {f'"{target.phrase}"': [result("https://copia.pe/a", snippet=target.phrase)]}
        collector = self.collector(responses, max_queries=4)
        collector.collect(DOCUMENT)

        queried = collector.search_client.queries
        self.assertIn(f'"{paragraphs[2].phrase}"', queried)
        self.assertEqual(collector.last_stats["consultas_vecino"], 1)

    def test_without_adaptive_neighbors_the_cap_follows_priority(self) -> None:
        paragraphs = self.collector()._query_paragraphs(DOCUMENT, [])
        target = paragraphs[3]
        responses = {f'"{target.phrase}"': [result("https://copia.pe/a", snippet=target.phrase)]}
        collector = self.collector(responses, max_queries=4, adaptive_neighbors=False)
        collector.collect(DOCUMENT)

        self.assertNotIn(f'"{paragraphs[2].phrase}"', collector.search_client.queries)

    def test_zero_results_with_quotes_retried_without_quotes_only_next_to_a_hit(self) -> None:
        paragraphs = self.collector()._query_paragraphs(DOCUMENT, [])
        hit = paragraphs[0]
        responses = {f'"{hit.phrase}"': [result("https://copia.pe/a", snippet=hit.phrase)]}
        collector = self.collector(responses, adaptive_neighbors=False)
        collector.collect(DOCUMENT)

        queries = collector.search_client.queries
        # Vecino 1 dio 0 con comillas y está junto a una coincidencia.
        self.assertIn(paragraphs[1].phrase, queries)
        # Párrafo 3 también dio 0 pero sus vecinos no coincidieron.
        self.assertNotIn(paragraphs[3].phrase, queries)
        self.assertEqual(collector.last_stats["consultas_sin_comillas"], 1)

    def test_snippet_hit_becomes_a_source_when_the_page_fails(self) -> None:
        paragraphs = self.collector()._query_paragraphs(DOCUMENT, [])
        hit = paragraphs[0]
        snippet = f"Resumen: {hit.phrase} y algo más."
        responses = {
            f'"{hit.phrase}"': [
                result("https://bloqueada.pe/tesis", snippet=snippet, extra=["otro fragmento"]),
                result("https://ajena.pe/x", snippet="nada que ver con el documento"),
            ]
        }
        collector = self.collector(responses, failing={"https://bloqueada.pe/tesis"})
        pages = collector.collect(DOCUMENT)

        snippet_pages = [page for page in pages if page.source_format == SOURCE_FORMAT_SNIPPET]
        self.assertEqual([page.url for page in snippet_pages], ["https://bloqueada.pe/tesis"])
        self.assertEqual(snippet_pages[0].comparable_segments, (snippet, "otro fragmento"))
        # La página ajena sí se descargó, pero no recibe snippets.
        other = next(page for page in pages if page.url == "https://ajena.pe/x")
        self.assertEqual(other.comparable_segments, (other.text,))

    def test_snippet_match_off_keeps_only_downloaded_pages(self) -> None:
        paragraphs = self.collector()._query_paragraphs(DOCUMENT, [])
        hit = paragraphs[0]
        responses = {f'"{hit.phrase}"': [result("https://bloqueada.pe/t", snippet=hit.phrase)]}
        collector = self.collector(
            responses, failing={"https://bloqueada.pe/t"}, snippet_match=False
        )

        self.assertEqual(collector.collect(DOCUMENT), [])

    def test_extra_snippets_count_for_hits_only_when_enabled(self) -> None:
        phrase = "una frase bastante larga con al menos ocho palabras seguidas aquí"
        item = result("https://x.pe/a", snippet="otra cosa", extra=[phrase])
        collector = self.collector()

        self.assertTrue(collector._snippet_hit(phrase, item))
        collector.use_extra_snippets = False
        self.assertFalse(collector._snippet_hit(phrase, item))

    def test_voted_url_is_downloaded_before_the_per_query_cap(self) -> None:
        paragraphs = self.collector()._query_paragraphs(DOCUMENT, [])
        filler = [result(f"https://relleno.pe/{n}") for n in range(3)]
        voted = result("https://votada.pe/articulo")
        responses = {
            f'"{paragraphs[0].phrase}"': [*filler, voted],
            f'"{paragraphs[1].phrase}"': [result("https://otra.pe/1"), voted],
        }
        collector = self.collector(responses, max_pages_per_query=1, max_pages_total=3)
        pages = collector.collect(DOCUMENT)

        self.assertEqual(collector.fetcher.fetched[0], "https://votada.pe/articulo")
        self.assertIn("https://votada.pe/articulo", [page.url for page in pages])

    def test_results_per_query_truncates(self) -> None:
        paragraphs = self.collector()._query_paragraphs(DOCUMENT, [])
        responses = {
            f'"{paragraphs[0].phrase}"': [result(f"https://r.pe/{n}") for n in range(10)]
        }
        collector = self.collector(responses, results_per_query=5, page_votes=False)
        collector.collect(DOCUMENT)

        self.assertNotIn("https://r.pe/5", collector.fetcher.fetched)


class SnippetHelpersTests(SimpleTestCase):
    def test_clean_snippet_strips_bold_and_entities(self) -> None:
        self.assertEqual(clean_snippet("la <strong>producción</strong> &amp; texto"), "la producción & texto")

    def test_shares_run_needs_min_consecutive_words(self) -> None:
        phrase = "uno dos tres cuatro cinco seis siete ocho nueve"
        self.assertTrue(shares_run(phrase, "x uno dos tres cuatro cinco seis siete ocho y", 8))
        self.assertFalse(shares_run(phrase, "uno dos tres cuatro cinco seis siete x ocho", 8))


@override_settings(
    BRAVE_SEARCH_API_KEY="clave",
    BRAVE_EXTRA_SNIPPETS=True,
    WEB_SEARCH_CACHE_DAYS=30,
)
class BraveExtraSnippetsTests(TestCase):
    def test_requests_extra_snippets_and_caches_them_under_their_own_key(self) -> None:
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "web": {
                "results": [
                    {
                        "url": "https://x.pe/a",
                        "title": "A",
                        "description": "la <strong>frase</strong>",
                        "extra_snippets": ["fragmento <strong>dos</strong>"],
                    }
                ]
            }
        }
        client = BraveWebSearchClient()

        with mock.patch("apps.analysis.engines.web_search.requests.get", return_value=response) as get:
            results = client.search('"frase"', count=50)
            again = client.search('"frase"', count=50)

        self.assertEqual(get.call_count, 1)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["count"], 20)
        self.assertEqual(params["extra_snippets"], "true")
        self.assertEqual(results[0].snippets, ("la frase", "fragmento dos"))
        self.assertEqual(again, results)

        with override_settings(BRAVE_EXTRA_SNIPPETS=False):
            plain = BraveWebSearchClient()
            self.assertNotEqual(plain.cache_key('"frase"', 20), client.cache_key('"frase"', 20))
        self.assertEqual(WebSearchCacheEntry.objects.count(), 1)


def http(url: str, status: int = 200, content_type: str = "text/html", body: bytes = b"", text: str = ""):
    response = mock.Mock(status_code=status, url=url, encoding="utf-8", text=text)
    response.headers = {"Content-Type": content_type}
    response.iter_content.return_value = [body]
    return response


LANDING = (
    "<html><head><title>Tesis UCV</title>"
    "<meta name='citation_pdf_url' content='/bitstream/handle/1/2/tesis.pdf'></head>"
    "<body>" + "Resumen de la ficha. " * 40 + "</body></html>"
).encode()
PDF_TEXT = "Texto completo del PDF de la tesis. " * 40


@override_settings(WEB_FOLLOW_PDF=True, WEB_PAGE_CACHE_DAYS=0, WEB_PDF_MAX_MB=20)
class FollowPdfTests(SimpleTestCase):
    def fetch(self, robots: str):
        def get(url, **kwargs):
            if url.endswith("robots.txt"):
                return http(url, text=robots)
            if url.endswith(".pdf"):
                return http(url, content_type="application/pdf", body=b"%PDF-falso")
            return http(url, body=LANDING)

        fetcher = WebPageFetcher()
        with mock.patch("apps.analysis.engines.web_search.requests.get", side_effect=get), \
                mock.patch("apps.analysis.engines.web_search.read_pdf_pages", return_value=[PDF_TEXT]):
            return fetcher, fetcher.fetch(result("https://repositorio.ucv.edu.pe/handle/1/2"))

    def test_landing_page_follows_citation_pdf(self) -> None:
        fetcher, page = self.fetch(robots="User-agent: *\nAllow: /")

        self.assertEqual(page.source_format, "PDF")
        self.assertIn("Texto completo del PDF", page.text)
        self.assertEqual(page.url, "https://repositorio.ucv.edu.pe/handle/1/2")
        self.assertEqual(fetcher.max_download_bytes, 20 * 1024 * 1024)

    def test_robots_disallow_keeps_the_landing_page(self) -> None:
        _fetcher, page = self.fetch(robots="User-agent: *\nDisallow: /bitstream/")

        self.assertEqual(page.source_format, "HTML")
        self.assertIn("Resumen de la ficha", page.text)


class FullThesisCasesTests(SimpleTestCase):
    @mock.patch("apps.analysis.banco.generador.FULL_MIN_WORDS", 1000)
    def test_full_cases_use_whole_theses_and_skip_institute_sources(self) -> None:
        from apps.analysis.tests_banco import fake_inputs

        bases, _corpus, web = fake_inputs()
        bases = [base for base in bases if base.origin == "instituto"]
        generator = BankGenerator(seed=1)
        cases = generator.generate_full_web(bases, web, excluded_ids={"inst_0", "inst_1"})
        again = BankGenerator(seed=1).generate_full_web(bases, web, excluded_ids={"inst_0", "inst_1"})

        self.assertEqual(len(cases), FULL_WEB_CASES)
        self.assertEqual([c.content for c in cases], [c.content for c in again])
        for case in cases:
            self.assertNotIn(case.base_id, {"inst_0", "inst_1"})
            self.assertEqual(case.base_text, next(b.text for b in bases if b.id == case.base_id))
            self.assertTrue(6 <= len(case.insertions) <= 10)


class CacheWriteRecorderTests(TestCase):
    def test_writes_are_replayed_after_rollback(self) -> None:
        from django.db import transaction

        from apps.analysis import web_cache

        with override_settings(WEB_SEARCH_CACHE_DAYS=30):
            with CacheWriteRecorder() as recorder:
                with transaction.atomic():
                    web_cache.store_search_results("k" * 64, "consulta", [{"url": "u"}])
                    transaction.set_rollback(True)

            self.assertEqual(WebSearchCacheEntry.objects.count(), 0)
            self.assertEqual(recorder.replay(), 1)
            self.assertEqual(web_cache.get_search_results("k" * 64), [{"url": "u"}])


class SlowDownloadTests(SimpleTestCase):
    @override_settings(WEB_PAGE_MAX_DOWNLOAD_SECONDS=5)
    def test_trickling_download_is_cut_by_total_time(self) -> None:
        fetcher = WebPageFetcher()
        response = mock.Mock()
        response.iter_content.return_value = iter([b"a"] * 10)
        clock = iter(range(0, 100, 2))

        with mock.patch("apps.analysis.engines.web_search.time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(Exception) as raised:
                fetcher._read_limited_response(response)

        self.assertIn("5 s", str(raised.exception))


class TemplateRangesTests(SimpleTestCase):
    def test_turnitin_receipt_and_operationalization_header(self) -> None:
        from apps.analysis.engines.text_filters import AcademicTextFilter

        text = (
            "La lectura es importante para todos.\n"
            "Página 3 de 38 - Descripción general de integridad Identificador de la entrega "
            "trn:oid:::29566:556379570\n"
            "Engrega de integridad Identificador de la entrega trn:oid:::29566\n\n"
            "Tabla 1: Operacionalización de variables\n"
            "Variable Definición conceptual Definición operacional Dimensiones Indicadores\n"
            "La operacionalización de variables permite medir conceptos abstractos."
        )
        ranges = AcademicTextFilter().find_template_ranges(text)
        pieces = [text[start:end] for start, end in ranges]

        self.assertEqual(len(pieces), 4)
        self.assertTrue(pieces[0].startswith("Página 3 de 38"))
        self.assertIn("Engrega de integridad", pieces[1])
        self.assertEqual(pieces[2], "Tabla 1: Operacionalización de variables")
        self.assertTrue(pieces[3].endswith("Dimensiones Indicadores"))
        # La prosa que menciona la operacionalización no es plantilla.
        self.assertFalse(any("permite medir" in piece for piece in pieces))


@override_settings(BRAVE_SEARCH_API_KEY="clave", WEB_SEARCH_CACHE_DAYS=30)
class BraveQuotaTests(TestCase):
    def test_402_stops_the_paragraph_search(self) -> None:
        import requests

        response = mock.Mock(status_code=402)
        response.raise_for_status.side_effect = requests.HTTPError("402", response=response)
        collector = WebCandidateCollector()
        collector.query_mode = "parrafos"
        collector.fetcher = FakeFetcher()

        with mock.patch("apps.analysis.engines.web_search.requests.get", return_value=response) as get, \
                self.assertLogs("apps.analysis.engines.web_search", level="ERROR"):
            collector.collect(DOCUMENT)

        self.assertEqual(get.call_count, 1)
        self.assertTrue(collector.search_client.quota_exhausted)
        self.assertEqual(WebSearchCacheEntry.objects.count(), 0)
