from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import RemoteDisconnected
from io import BytesIO
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from pypdf import PdfReader
from urllib3.exceptions import ReadTimeoutError

from apps.analysis import web_cache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    domain: str
    snippet: str


@dataclass(frozen=True)
class WebPageContent:
    title: str
    url: str
    domain: str
    text: str
    source_format: str


@dataclass(frozen=True)
class WebSearchRun:
    # (consulta que la trajo, página) en el orden en que se obtuvieron.
    pages: list[tuple[str, WebPageContent]]
    executed_queries: int
    fetched_pages: int


# Un solo reintento, tras una espera corta, cuando el servidor no respondió
# a tiempo o cerró la conexión sin respuesta: suelen ser cortes pasajeros.
# Un 403/429 es un bloqueo o límite de cuota y reintentar no lo arregla.
RETRY_DELAY_SECONDS = 1.0


def _is_transient_error(exc: BaseException) -> bool:
    pending: list[object] = [exc]
    seen: set[int] = set()

    while pending:
        current = pending.pop()

        if not isinstance(current, BaseException) or id(current) in seen:
            continue

        seen.add(id(current))

        if isinstance(
            current, (requests.Timeout, RemoteDisconnected, ReadTimeoutError)
        ):
            return True

        pending.extend(current.args)
        pending.extend([current.__cause__, current.__context__])

    return False


def call_with_retry(operation: Callable[[], object], description: str):
    """
    Ejecuta `operation` y, si falla por timeout o RemoteDisconnected, la
    repite una vez tras RETRY_DELAY_SECONDS. Cualquier otro error (y el
    segundo fallo) se propaga.
    """
    try:
        return operation()
    except requests.RequestException as exc:
        if not _is_transient_error(exc):
            raise

        logger.info("Reintentando tras corte pasajero. %s error=%s", description, exc)
        time.sleep(RETRY_DELAY_SECONDS)
        return operation()


class BraveWebSearchClient:
    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self) -> None:
        self.api_key = getattr(settings, "BRAVE_SEARCH_API_KEY", "")
        self.timeout = getattr(settings, "WEB_ANALYSIS_TIMEOUT_SECONDS", 10)
        self.country = getattr(settings, "BRAVE_SEARCH_COUNTRY", "PE")
        self.search_lang = getattr(settings, "BRAVE_SEARCH_LANG", "es")

    def search(
        self,
        query: str,
        count: int = 5,
    ) -> list[WebSearchResult]:
        if not self.api_key:
            logger.warning("BRAVE_SEARCH_API_KEY no configurada.")
            return []

        cleaned_query = self._clean_query(query)

        if not cleaned_query:
            return []

        count = min(count, 10)
        cache_key = web_cache.search_cache_key(
            query=cleaned_query,
            count=count,
            country=self.country,
            search_lang=self.search_lang,
        )
        cached = web_cache.get_search_results(cache_key)

        if cached is not None:
            return self._parse_results(cached)

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }

        params = {
            "q": cleaned_query,
            "count": count,
            "country": self.country,
            "search_lang": self.search_lang,
        }

        def request() -> requests.Response:
            response = requests.get(
                self.BASE_URL,
                headers=headers,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response

        try:
            response = call_with_retry(request, f"query={cleaned_query}")
            payload = response.json()

        except requests.RequestException as exc:
            logger.warning("Error Brave Search. query=%s error=%s", cleaned_query, exc)
            return []

        except ValueError as exc:
            logger.warning("JSON inválido Brave Search. query=%s error=%s", cleaned_query, exc)
            return []

        # Solo se guarda una respuesta válida (aunque venga vacía); los
        # errores se vuelven a consultar en el próximo análisis.
        items = [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
            }
            for item in payload.get("web", {}).get("results", [])
        ]
        web_cache.store_search_results(cache_key, cleaned_query, items)

        return self._parse_results(items)

    def _parse_results(self, items: list[dict]) -> list[WebSearchResult]:
        results: list[WebSearchResult] = []

        for item in items:
            url = (item.get("url") or "").strip()
            title = (item.get("title") or "").strip()
            snippet = (item.get("description") or "").strip()

            if not url:
                continue

            domain = self._domain_from_url(url)

            if not domain:
                continue

            results.append(
                WebSearchResult(
                    title=title or domain,
                    url=url,
                    domain=domain,
                    snippet=snippet,
                )
            )

        return results

    def _clean_query(self, query: str) -> str:
        query = re.sub(r"\s+", " ", query).strip()
        return query[:380].strip()

    def _domain_from_url(self, url: str) -> str:
        try:
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return ""


class WebPageFetcher:
    """
    Lee contenido público HTML o PDF.

    No intenta romper captchas, sesiones privadas, paywalls o bloqueos.
    """

    BLOCKED_EXTENSIONS = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".zip",
        ".rar",
        ".7z",
        ".mp4",
        ".mp3",
        ".avi",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    )

    MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

    def __init__(self) -> None:
        self.timeout = getattr(settings, "WEB_ANALYSIS_TIMEOUT_SECONDS", 10)

    def fetch(self, result: WebSearchResult) -> WebPageContent | None:
        """
        Página leída o None. Se consulta primero la caché por URL: tanto la
        lectura exitosa como el fallo, así un reanálisis ve exactamente las
        mismas páginas.
        """
        if self._looks_like_blocked_file(result.url):
            return None

        cached = web_cache.get_page(result.url)

        if cached is not None:
            if not cached.is_success:
                return None

            return WebPageContent(
                title=cached.title or result.title,
                url=result.url,
                domain=result.domain,
                text=cached.text,
                source_format=cached.source_format,
            )

        page, failure_reason = self._download(result=result)

        if page is None:
            web_cache.store_page(result.url, failure_reason=failure_reason)
        else:
            web_cache.store_page(
                result.url,
                title=page.title,
                text=page.text,
                source_format=page.source_format,
            )

        return page

    def _download(
        self,
        result: WebSearchResult,
    ) -> tuple[WebPageContent | None, str]:
        """(página, "") si se pudo leer; (None, motivo) si no."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; IESPP-OriginalityBot/1.0; "
                "+https://iespp.local)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/pdf",
        }

        def download() -> tuple[requests.Response, bytes]:
            response = requests.get(
                result.url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )

            if response.status_code >= 400:
                return response, b""

            # La lectura va dentro del reintento: un corte a mitad de la
            # descarga también es pasajero.
            return response, self._read_limited_response(response=response)

        try:
            response, raw_content = call_with_retry(download, f"url={result.url}")

            if response.status_code >= 400:
                return None, f"HTTP {response.status_code}"

            content_type = response.headers.get("Content-Type", "").lower()
            url_lower = response.url.lower().split("?")[0]

            if not raw_content:
                return None, "respuesta vacía"

            if "application/pdf" in content_type or url_lower.endswith(".pdf"):
                page = self._parse_pdf(
                    raw_content=raw_content,
                    result=result,
                )
            elif "text/html" in content_type or "application/xhtml" in content_type:
                page = self._parse_html(
                    html=raw_content.decode(response.encoding or "utf-8", errors="ignore"),
                    result=result,
                )
            else:
                return None, f"tipo no soportado: {content_type[:80]}"

            return page, "" if page is not None else "sin texto suficiente"

        except requests.RequestException as exc:
            logger.info("No se pudo leer web. url=%s error=%s", result.url, exc)
            return None, f"{type(exc).__name__}: {exc}"

        except Exception as exc:
            logger.info("Error procesando web. url=%s error=%s", result.url, exc)
            return None, f"{type(exc).__name__}: {exc}"

    def _read_limited_response(self, response: requests.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0

        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue

            total += len(chunk)

            if total > self.MAX_DOWNLOAD_BYTES:
                break

            chunks.append(chunk)

        return b"".join(chunks)

    def _parse_html(
        self,
        html: str,
        result: WebSearchResult,
    ) -> WebPageContent | None:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"]):
            tag.decompose()

        title = ""

        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        text = soup.get_text(separator=" ", strip=True)
        text = self._normalize_visible_text(text)

        if len(text) < 500:
            return None

        time.sleep(0.10)

        return WebPageContent(
            title=title or result.title,
            url=result.url,
            domain=result.domain,
            text=text,
            source_format="HTML",
        )

    def _parse_pdf(
        self,
        raw_content: bytes,
        result: WebSearchResult,
    ) -> WebPageContent | None:
        try:
            reader = PdfReader(BytesIO(raw_content))
            pages_text: list[str] = []

            for page in reader.pages[:40]:
                text = page.extract_text() or ""

                if text.strip():
                    pages_text.append(text.strip())

            content = self._normalize_visible_text("\n\n".join(pages_text))

            if len(content) < 500:
                return None

            return WebPageContent(
                title=result.title or result.domain,
                url=result.url,
                domain=result.domain,
                text=content,
                source_format="PDF",
            )

        except Exception as exc:
            logger.info("No se pudo extraer PDF web. url=%s error=%s", result.url, exc)
            return None

    def _looks_like_blocked_file(self, url: str) -> bool:
        lowered = url.lower().split("?")[0]
        return lowered.endswith(self.BLOCKED_EXTENSIONS)

    def _normalize_visible_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text)
        return text.strip()


SPANISH_STOPWORDS = frozenset(
    """
    a al algo algunas algunos ante antes aquel aquella aquellas aquellos bajo
    aqui así asi aunque cada como con contra cual cuales cuando de del desde
    donde dos durante e el ella ellas ellos en entre era eran es esa esas ese
    eso esos esta estaba estas este esto estos fue fueron ha han hasta hacia
    hay la
    las le les lo los mas más me mediante mi mientras muy ni no nos o otra
    otras otro otros para pero por porque que qué se sea segun según ser si
    sí sido sin sino sobre son su sus tal también tambien tan tanto te toda
    todas todo todos tras u un una unas uno unos y ya
    """.split()
)

ENGLISH_STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from has have in into is it its
    of on or that the their these this those to was were which while with
    """.split()
)

_WORD_EDGE_PUNCTUATION = ".,;:¿?¡!"
# Letras (con tildes) y, como mucho, guiones internos: descarta dígitos,
# paréntesis, comillas, barras, "&", etc.
_PLAIN_WORD_RE = re.compile(r"^[^\W\d_]+(?:-[^\W\d_]+)*$", flags=re.UNICODE)
_ACRONYM_RE = re.compile(r"[A-ZÁÉÍÓÚÑÜ]{2,}")


class WebCandidateCollector:
    """
    Busca en la web frases cortas y distintivas de todo el documento.

    Una oración completa de 30 palabras casi nunca aparece literal en otra
    fuente; las coincidencias reales (como las que reporta Turnitin) son
    tramos académicos de 8-20 palabras. Por eso se buscan ventanas de
    PHRASE_MIN_WORDS-PHRASE_MAX_WORDS palabras dentro de cada oración.
    """

    # Referencia de repositorios académicos peruanos. Ya no se usan en
    # consultas `site:` (gastaban presupuesto en muy pocas frases); la
    # lista sigue siendo la fuente de dominios para OAI_HARVEST_REPOSITORIES.
    ACADEMIC_DOMAINS = [
        "repositorio.ucv.edu.pe",
        "repositorio.upn.edu.pe",
        "repositorio.usil.edu.pe",
        "repositorio.unasam.edu.pe",
        "repositorio.une.edu.pe",
        "repositorio.minedu.gob.pe",
        "repositorio.concytec.gob.pe",
        "alicia.concytec.gob.pe",
        "scielo.org",
        "redalyc.org",
        "dialnet.unirioja.es",
    ]

    PHRASE_MIN_WORDS = 8
    PHRASE_MAX_WORDS = 14
    PHRASES_PER_SECTION = 2
    CONTENT_WORD_MIN_LENGTH = 4

    def __init__(self) -> None:
        self.search_client = BraveWebSearchClient()
        self.fetcher = WebPageFetcher()
        # Cantidad de tramos iguales en que se divide el documento: se
        # buscan las mejores frases de CADA tramo, para cubrir inicio,
        # medio y final.
        self.max_query_phrases = getattr(settings, "WEB_ANALYSIS_MAX_QUERY_PHRASES", 8)
        self.max_queries = getattr(settings, "WEB_ANALYSIS_MAX_QUERIES", 24)
        self.max_pages_total = getattr(settings, "WEB_ANALYSIS_MAX_PAGES_TOTAL", 16)
        # Tope de páginas que aporta cada consulta (ver `run_queries`).
        self.max_pages_per_query = getattr(settings, "WEB_ANALYSIS_MAX_PAGES_PER_QUERY", 2)

    def collect(self, content: str) -> list[WebPageContent]:
        if not getattr(settings, "WEB_ANALYSIS_ENABLED", True):
            return []

        phrase_groups = self._extract_search_phrases(content=content)

        if not phrase_groups:
            logger.info(
                "Búsqueda web omitida: no se encontraron frases buscables en el documento."
            )
            return []

        queries = self._build_queries(phrase_groups=phrase_groups)
        run = self.run_queries(queries=queries)
        pages = [page for _query, page in run.pages]

        logger.info(
            "Búsqueda web terminada. frases=%s consultas=%s/%s (límite=%s) "
            "páginas_consultadas=%s páginas_con_contenido=%s",
            sum(len(group) for group in phrase_groups),
            run.executed_queries,
            len(queries),
            self.max_queries,
            run.fetched_pages,
            len(pages),
        )

        return pages

    def run_queries(self, queries: list[str]) -> WebSearchRun:
        """
        Ejecuta TODAS las consultas y después reparte la descarga de páginas
        por rondas: en cada ronda cada consulta, en orden de prioridad,
        aporta como mucho una página más, hasta `max_pages_per_query` por
        consulta y `max_pages_total` en total.

        Antes se descargaba consulta por consulta y, al llenarse el tope de
        páginas, las consultas restantes ni se ejecutaban: las primeras
        consultas se llevaban todo el presupuesto. Devuelve cada página junto
        a la consulta que la trajo (lo usa `web_search_probe`).
        """
        results_by_query = [
            (query, list(self.search_client.search(query=query, count=5)))
            for query in queries
        ]

        seen_urls: set[str] = set()
        pages: list[tuple[str, WebPageContent]] = []
        pages_by_query = [0] * len(results_by_query)
        next_result = [0] * len(results_by_query)
        fetched_pages = 0
        progress = True

        while progress and len(pages) < self.max_pages_total:
            progress = False

            for index, (query, results) in enumerate(results_by_query):
                if len(pages) >= self.max_pages_total:
                    break

                if pages_by_query[index] >= self.max_pages_per_query:
                    continue

                # Se avanza por los resultados de la consulta hasta leer una
                # página; las URLs ya vistas (por esta u otra consulta) y las
                # que fallan no consumen su cupo.
                while next_result[index] < len(results):
                    result = results[next_result[index]]
                    next_result[index] += 1

                    if result.url in seen_urls:
                        continue

                    seen_urls.add(result.url)
                    fetched_pages += 1
                    page = self.fetcher.fetch(result=result)

                    if page is None:
                        continue

                    pages.append((query, page))
                    pages_by_query[index] += 1
                    progress = True
                    break

        return WebSearchRun(
            pages=pages,
            executed_queries=len(results_by_query),
            fetched_pages=fetched_pages,
        )

    def _build_queries(self, phrase_groups: list[list[str]]) -> list[str]:
        """
        Consultas por rondas, recortadas a `max_queries`:
        1. la mejor frase de cada tramo, entre comillas;
        2. la segunda frase de cada tramo, entre comillas;
        3. la mejor frase de cada tramo SIN comillas (textos parafraseados).
        Cada ronda recorre todos los tramos antes de pasar a la siguiente,
        así el recorte nunca deja sin buscar el final del documento.
        """
        groups = [group for group in phrase_groups[: self.max_query_phrases] if group]

        queries = [f'"{group[0]}"' for group in groups]
        queries.extend(f'"{group[1]}"' for group in groups if len(group) > 1)
        queries.extend(group[0] for group in groups)

        return self._dedupe_keep_order(queries)[: self.max_queries]

    def _extract_search_phrases(self, content: str) -> list[list[str]]:
        """
        Divide el documento en `max_query_phrases` tramos iguales (por
        posición de carácter) y devuelve, en orden del documento, hasta
        PHRASES_PER_SECTION frases por tramo: la ventana más distintiva y
        la mejor de OTRA oración, para no buscar dos veces casi lo mismo.
        """
        if self.max_query_phrases <= 0:
            return []

        sentences = self._sentence_candidates(content=content)

        if not sentences:
            return []

        section_size = max(len(content), 1) / self.max_query_phrases
        # tramo -> [(puntaje, índice de oración, frase)]
        windows_by_section: dict[int, list[tuple[float, int, str]]] = {}
        window_count = 0

        for sentence_index, (offset, sentence) in enumerate(sentences):
            section = min(int(offset / section_size), self.max_query_phrases - 1)

            for score, phrase in self._phrase_windows(sentence):
                window_count += 1
                windows_by_section.setdefault(section, []).append(
                    (score, sentence_index, phrase)
                )

        phrase_groups: list[list[str]] = []
        # Una oración repetida (p. ej. la conclusión que repite el resumen)
        # no debe gastar dos consultas en la misma frase.
        chosen_phrases: set[str] = set()

        for section in sorted(windows_by_section):
            # Orden estable: a igual puntaje gana la ventana más temprana.
            ranked = sorted(windows_by_section[section], key=lambda item: -item[0])
            group: list[str] = []
            used_sentences: set[int] = set()

            for _score, sentence_index, phrase in ranked:
                key = phrase.lower()

                if sentence_index in used_sentences or key in chosen_phrases:
                    continue

                group.append(phrase)
                used_sentences.add(sentence_index)
                chosen_phrases.add(key)

                if len(group) >= self.PHRASES_PER_SECTION:
                    break

            if group:
                phrase_groups.append(group)

        logger.info(
            "Frases de búsqueda web seleccionadas. oraciones=%s ventanas=%s "
            "tramos=%s tramos_con_frase=%s frases=%s",
            len(sentences),
            window_count,
            self.max_query_phrases,
            len(phrase_groups),
            sum(len(group) for group in phrase_groups),
        )

        return phrase_groups

    def _sentence_candidates(self, content: str) -> list[tuple[int, str]]:
        """
        (offset, oración) de cada oración en español de los párrafos útiles.
        """
        candidates: list[tuple[int, str]] = []
        paragraph_start = 0

        for separator in re.finditer(r"\n\s*\n|\Z", content):
            paragraph = content[paragraph_start : separator.start()]
            offset = paragraph_start
            paragraph_start = separator.end()

            clean = re.sub(r"\s+", " ", paragraph).strip()

            if len(clean) < 160:
                continue

            if self._is_low_value_paragraph(clean):
                continue

            for match in re.finditer(
                r"\S.*?(?:[.!?](?=\s)|$)", paragraph, flags=re.DOTALL
            ):
                sentence = re.sub(r"\s+", " ", match.group(0)).strip()

                if len(sentence.split()) < self.PHRASE_MIN_WORDS:
                    continue

                # El resumen en inglés (abstract) no se busca: gastaba
                # consultas en un texto que casi nunca se copia literal.
                if not self._looks_spanish(sentence):
                    continue

                candidates.append((offset + match.start(), sentence))

        return candidates

    def _phrase_windows(self, sentence: str) -> list[tuple[float, str]]:
        """
        (puntaje, frase) de cada ventana válida de PHRASE_MIN_WORDS a
        PHRASE_MAX_WORDS palabras dentro de la oración. Se descartan
        ventanas con citas, números, siglas o que empiezan/terminan en
        palabra vacía.
        """
        tokens = sentence.split()
        cores = [token.strip(_WORD_EDGE_PUNCTUATION) for token in tokens]
        windows: list[tuple[float, str]] = []

        for start in range(len(tokens)):
            for size in range(self.PHRASE_MIN_WORDS, self.PHRASE_MAX_WORDS + 1):
                end = start + size

                if end > len(tokens):
                    break

                if not self._is_valid_window(tokens[start:end], cores[start:end]):
                    continue

                phrase = " ".join(tokens[start:end]).strip(_WORD_EDGE_PUNCTUATION)
                windows.append((self._distinctiveness(cores[start:end]), phrase))

        return windows

    def _is_valid_window(self, tokens: list[str], cores: list[str]) -> bool:
        if cores[0].lower() in SPANISH_STOPWORDS or cores[-1].lower() in SPANISH_STOPWORDS:
            return False

        # Ninguna palabra interna cierra con punto (abreviaturas como "al."
        # de "et al." o "Dr." delatan citas o cortes de oración).
        if any(token.endswith(".") for token in tokens[:-1]):
            return False

        for core in cores:
            if not core or not _PLAIN_WORD_RE.match(core):
                return False

            if _ACRONYM_RE.search(core):
                return False

        lowered = " ".join(cores).lower()

        return "et al" not in lowered

    def _distinctiveness(self, cores: list[str]) -> float:
        """
        Más palabras de contenido (y más largas) => frase más distintiva;
        se penalizan ventanas dominadas por palabras vacías.
        """
        content_words = [
            core
            for core in cores
            if core.lower() not in SPANISH_STOPWORDS
            and len(core) >= self.CONTENT_WORD_MIN_LENGTH
        ]
        stopword_ratio = sum(
            1 for core in cores if core.lower() in SPANISH_STOPWORDS
        ) / len(cores)
        length_bonus = sum(max(len(word) - 6, 0) for word in content_words) * 0.1

        return len(content_words) + length_bonus - 2 * max(stopword_ratio - 0.5, 0)

    def _looks_spanish(self, sentence: str) -> bool:
        words = [
            word.strip(_WORD_EDGE_PUNCTUATION).lower() for word in sentence.split()
        ]
        spanish = sum(1 for word in words if word in SPANISH_STOPWORDS)
        english = sum(1 for word in words if word in ENGLISH_STOPWORDS)

        return spanish >= english

    def _is_low_value_paragraph(self, text: str) -> bool:
        lowered = text.lower()

        markers = [
            "índice",
            "indice",
            "dedicatoria",
            "agradecimiento",
            "bibliografía",
            "bibliografia",
            "referencias",
            "anexo",
            "tabla de contenido",
        ]

        return any(marker in lowered[:90] for marker in markers)

    def _dedupe_keep_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []

        for value in values:
            key = value.lower().strip()

            if not key or key in seen:
                continue

            seen.add(key)
            result.append(value)

        return result