from __future__ import annotations

import html as html_lib
import logging
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.client import RemoteDisconnected
from io import BytesIO
from urllib.parse import parse_qsl, unquote, urljoin, urlparse, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from pypdf import PdfReader
from urllib3.exceptions import ReadTimeoutError

from apps.analysis import web_cache
from apps.documents.extractors import normalize_pdf_pages, read_pdf_pages

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    domain: str
    snippet: str
    # Fragmentos adicionales de la página (`extra_snippets` de Brave).
    extra_snippets: tuple[str, ...] = ()

    @property
    def snippets(self) -> tuple[str, ...]:
        return tuple(item for item in (self.snippet, *self.extra_snippets) if item)


@dataclass(frozen=True)
class WebPageContent:
    title: str
    url: str
    domain: str
    text: str
    source_format: str
    # Tramos no contiguos que se comparan por separado (p. ej. los snippets
    # de Brave de una página que no se pudo descargar). Vacío: solo `text`.
    segments: tuple[str, ...] = ()

    @property
    def comparable_segments(self) -> tuple[str, ...]:
        return self.segments or (self.text,)


SOURCE_FORMAT_SNIPPET = "SNIPPET"

# Parámetros de la URL que identifican el documento (Scielo `pid`, Dialnet
# `codigo`, etc.); el resto (`lang`, `utm_*`, `script`...) no cambia la fuente.
_ID_QUERY_PARAMS = frozenset({"pid", "codigo", "id", "p", "page_id", "articulo", "doi", "handle"})
# OJS: .../article/view/1869, .../article/download/1869/3558/8028
_OJS_RE = re.compile(r"/article/(?:view|download|viewfile)/(\d+)(?:/|$)", flags=re.IGNORECASE)
# DSpace: /handle/20.500.12692/99635 y /bitstream/handle/20.500.12692/99635/x.pdf
_DSPACE_RE = re.compile(r"/(?:bitstream/)?handle/([\d.]+/\d+)(?:/|$)", flags=re.IGNORECASE)


def canonical_url(url: str) -> str:
    """
    Forma canónica de una URL para decidir si dos URLs son la misma fuente:
    sin esquema, sin "www.", sin barra final ni fragmento, solo con los
    parámetros que identifican el documento, y con el id del artículo en
    OJS (view/download) y DSpace (handle/bitstream).
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = unquote(parts.path).rstrip("/")

    if match := _OJS_RE.search(path):
        path = path[: match.start()] + f"/article/{match.group(1)}"
    elif match := _DSPACE_RE.search(path):
        path = f"/handle/{match.group(1)}"

    params = sorted(
        (name.lower(), value)
        for name, value in parse_qsl(parts.query)
        if name.lower() in _ID_QUERY_PARAMS
    )
    query = "&".join(f"{name}={value}" for name, value in params)
    return f"{host}{path}" + (f"?{query}" if query else "")


def clean_snippet(text: str) -> str:
    """Brave resalta con <strong> y escapa entidades HTML."""
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def normalized_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def shares_run(phrase: str, text: str, min_words: int) -> bool:
    """¿`text` contiene `min_words` palabras seguidas de `phrase`?"""
    phrase_words = normalized_words(phrase)

    if len(phrase_words) < min_words:
        return False

    text_words = normalized_words(text)
    grams = {
        tuple(text_words[index : index + min_words])
        for index in range(len(text_words) - min_words + 1)
    }
    return any(
        tuple(phrase_words[index : index + min_words]) in grams
        for index in range(len(phrase_words) - min_words + 1)
    )


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
    # Máximo de resultados por consulta que acepta la API.
    MAX_COUNT = 20
    QUOTA_STATUS_CODES = (402, 429)

    def __init__(self) -> None:
        self.api_key = getattr(settings, "BRAVE_SEARCH_API_KEY", "")
        self.timeout = getattr(settings, "WEB_ANALYSIS_TIMEOUT_SECONDS", 10)
        self.country = getattr(settings, "BRAVE_SEARCH_COUNTRY", "PE")
        self.search_lang = getattr(settings, "BRAVE_SEARCH_LANG", "es")
        # Hasta 5 fragmentos más por resultado, por el mismo costo.
        self.extra_snippets = getattr(settings, "BRAVE_EXTRA_SNIPPETS", False)
        # 402 (límite de gasto agotado) o 429 (cuota): seguir consultando
        # no sirve; el colector deja de buscar.
        self.quota_exhausted = False
        # Consultas reales a la API (no desde caché) y errores seguidos, solo
        # para el log del análisis; no se guarda nada en la base.
        self.api_queries = 0
        self.consecutive_errors = 0

    # Errores seguidos de la API a partir de los cuales se avisa en el log.
    CONSECUTIVE_ERRORS_WARNING = 3

    def _register_api_error(self, code: str, query: str) -> None:
        self.consecutive_errors += 1
        logger.error(
            "Brave Search falló: error=%s consulta_n=%s errores_seguidos=%s query=%s",
            code,
            self.api_queries,
            self.consecutive_errors,
            query,
        )
        if self.consecutive_errors == self.CONSECUTIVE_ERRORS_WARNING:
            logger.warning(
                "AVISO: Brave Search lleva %s errores seguidos (último: %s, "
                "consulta n.º %s). La similitud web puede estar incompleta; "
                "revisa la cuota y la clave de la API.",
                self.consecutive_errors,
                code,
                self.api_queries,
            )

    def cache_key(self, query: str, count: int) -> str | None:
        cleaned_query = self._clean_query(query)

        if not cleaned_query:
            return None

        return web_cache.search_cache_key(
            query=cleaned_query,
            count=min(count, self.MAX_COUNT),
            country=self.country,
            search_lang=self.search_lang,
            extra_snippets=self.extra_snippets,
        )

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

        count = min(count, self.MAX_COUNT)
        cache_key = self.cache_key(query=cleaned_query, count=count)
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

        if self.extra_snippets:
            params["extra_snippets"] = "true"

        self.api_queries += 1

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
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                code = f"HTTP {status}"
            elif isinstance(exc, requests.Timeout):
                code = "timeout"
            else:
                code = type(exc).__name__
            self._register_api_error(code, cleaned_query)

            if status in self.QUOTA_STATUS_CODES:
                self.quota_exhausted = True
                logger.error(
                    "Brave Search sin cuota (HTTP %s): la búsqueda web queda sin "
                    "resultados hasta ampliar el límite. query=%s",
                    status,
                    cleaned_query,
                )
            else:
                logger.warning("Error Brave Search. query=%s error=%s", cleaned_query, exc)
            return []

        except ValueError as exc:
            logger.warning("JSON inválido Brave Search. query=%s error=%s", cleaned_query, exc)
            self._register_api_error("JSON inválido", cleaned_query)
            return []

        self.consecutive_errors = 0

        # Solo se guarda una respuesta válida (aunque venga vacía); los
        # errores se vuelven a consultar en el próximo análisis.
        items = [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                **(
                    {"extra_snippets": list(item.get("extra_snippets") or [])}
                    if self.extra_snippets
                    else {}
                ),
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
            snippet = clean_snippet(item.get("description") or "")
            extra = tuple(
                cleaned
                for cleaned in (clean_snippet(text) for text in item.get("extra_snippets") or [])
                if cleaned
            )

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
                    extra_snippets=extra,
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
    BOT_NAME = "IESPP-OriginalityBot"
    USER_AGENT = (
        "Mozilla/5.0 (compatible; IESPP-OriginalityBot/1.0; +https://iespp.local)"
    )
    # Con WEB_FOLLOW_PDF la lectura cambia (PDF con el extractor de
    # documentos, ficha -> PDF): se guarda en la caché con otra clave para
    # no mezclarla con las lecturas viejas.
    FOLLOW_PDF_CACHE_SUFFIX = "#pdf-v2"

    def __init__(self) -> None:
        self.timeout = getattr(settings, "WEB_ANALYSIS_TIMEOUT_SECONDS", 10)
        # PDFs con el mismo extractor que los documentos, fichas de
        # repositorio que enlazan su PDF (citation_pdf_url) y robots.txt.
        self.follow_pdf = getattr(settings, "WEB_FOLLOW_PDF", False)
        self.max_download_bytes = (
            getattr(settings, "WEB_PDF_MAX_MB", 20) * 1024 * 1024
            if self.follow_pdf
            else self.MAX_DOWNLOAD_BYTES
        )
        self.max_download_seconds = getattr(settings, "WEB_PAGE_MAX_DOWNLOAD_SECONDS", 30)
        self._robots: dict[str, RobotFileParser] = {}

    def fetch(self, result: WebSearchResult) -> WebPageContent | None:
        """
        Página leída o None. Se consulta primero la caché por URL: tanto la
        lectura exitosa como el fallo, así un reanálisis ve exactamente las
        mismas páginas.
        """
        if self._looks_like_blocked_file(result.url):
            return None

        cache_url = result.url + (self.FOLLOW_PDF_CACHE_SUFFIX if self.follow_pdf else "")
        cached = web_cache.get_page(cache_url)

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
            web_cache.store_page(cache_url, failure_reason=failure_reason)
        else:
            web_cache.store_page(
                cache_url,
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
        if self.follow_pdf and self._is_pdf_url(result.url) and not self._robots_allows(result.url):
            return None, "robots.txt"

        try:
            response, raw_content = self._get(result.url)

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
                html = raw_content.decode(response.encoding or "utf-8", errors="ignore")
                page = None

                if self.follow_pdf:
                    page = self._follow_citation_pdf(html=html, base_url=response.url, result=result)

                if page is None:
                    page = self._parse_html(html=html, result=result)
            else:
                return None, f"tipo no soportado: {content_type[:80]}"

            return page, "" if page is not None else "sin texto suficiente"

        except requests.RequestException as exc:
            logger.info("No se pudo leer web. url=%s error=%s", result.url, exc)
            return None, f"{type(exc).__name__}: {exc}"

        except Exception as exc:
            logger.info("Error procesando web. url=%s error=%s", result.url, exc)
            return None, f"{type(exc).__name__}: {exc}"

    def _get(self, url: str) -> tuple[requests.Response, bytes]:
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf",
        }

        def download() -> tuple[requests.Response, bytes]:
            response = requests.get(
                url,
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

        return call_with_retry(download, f"url={url}")

    def _follow_citation_pdf(
        self,
        html: str,
        base_url: str,
        result: WebSearchResult,
    ) -> WebPageContent | None:
        """
        Ficha de repositorio (DSpace, OJS...): si declara su PDF en
        `citation_pdf_url` y robots.txt lo permite, se lee el PDF en lugar
        de la ficha, que solo trae el resumen.
        """
        soup = BeautifulSoup(html, "lxml")
        meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        pdf_url = (meta.get("content") or "").strip() if meta else ""

        if not pdf_url:
            return None

        pdf_url = urljoin(base_url, pdf_url)

        if not self._robots_allows(pdf_url):
            logger.info("PDF de ficha bloqueado por robots.txt. url=%s", pdf_url)
            return None

        try:
            response, raw_content = self._get(pdf_url)
        except requests.RequestException as exc:
            logger.info("No se pudo leer el PDF de la ficha. url=%s error=%s", pdf_url, exc)
            return None

        if response.status_code >= 400 or not raw_content:
            return None

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        return self._parse_pdf(
            raw_content=raw_content,
            result=WebSearchResult(
                title=title or result.title,
                url=result.url,
                domain=result.domain,
                snippet=result.snippet,
            ),
        )

    def _is_pdf_url(self, url: str) -> bool:
        return url.lower().split("?")[0].endswith(".pdf")

    def _robots_allows(self, url: str) -> bool:
        """robots.txt del sitio (uno por sitio y por análisis)."""
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(site)

        if parser is None:
            parser = RobotFileParser()

            try:
                response = requests.get(
                    f"{site}/robots.txt",
                    headers={"User-Agent": self.USER_AGENT},
                    timeout=self.timeout,
                )
                if response.status_code in (401, 403):
                    parser.disallow_all = True
                elif response.status_code >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(response.text.splitlines())
            except requests.RequestException:
                # Sin robots.txt legible se permite, como hacen los buscadores.
                parser.allow_all = True

            self._robots[site] = parser

        return parser.can_fetch(self.BOT_NAME, url)

    def _read_limited_response(self, response: requests.Response) -> bytes:
        """
        Cuerpo hasta `max_download_bytes`. El timeout de requests es por
        lectura: un servidor que manda bytes a goteo nunca lo dispara. Por
        eso hay además un tope de tiempo total por descarga; si se pasa, la
        descarga se descarta.
        """
        chunks: list[bytes] = []
        total = 0
        deadline = time.monotonic() + self.max_download_seconds

        for chunk in response.iter_content(chunk_size=8192):
            if time.monotonic() > deadline:
                raise requests.ConnectionError(
                    f"descarga más lenta que {self.max_download_seconds} s"
                )

            if not chunk:
                continue

            total += len(chunk)

            if total > self.max_download_bytes:
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
            if self.follow_pdf:
                # El mismo extractor que los documentos subidos: mismos
                # párrafos, sin cabeceras ni números de página.
                content = self._normalize_visible_text(
                    normalize_pdf_pages(read_pdf_pages(BytesIO(raw_content)))
                )
            else:
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
# Cita entre paréntesis con año: "(Pérez, 2019)", "(2020, p. 4)", "(Ruiz y Sol, 2018a)".
_CITATION_RE = re.compile(r"\([^()]{0,80}?\b(?:19|20)\d{2}[a-z]?\b[^()]{0,40}\)")
# Viñeta o numeración al inicio de una línea: "-", "•", "a)", "1.", "1.2."
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-•▪●◦*–]|[a-zA-Z]\)|\d+(?:\.\d+)*[.)])\s", flags=re.MULTILINE)

# Prioridad de un párrafo para gastar consultas (menor = antes).
TIER_PROSE = 0
TIER_CITED = 1
TIER_LIST = 2
TIER_INTERNAL = 3


@dataclass(frozen=True)
class QueryParagraph:
    """Un párrafo buscable: su frase más distintiva y su prioridad."""

    index: int  # orden entre los párrafos buscables del documento
    start: int
    end: int
    phrase: str
    words: int
    tier: int


@dataclass
class ParagraphQuery:
    paragraph: int
    query: str
    quoted: bool
    results: list[WebSearchResult]
    # Resultados cuyo snippet contiene >= 8 palabras seguidas de la frase.
    hits: list[WebSearchResult] = field(default_factory=list)
    reason: str = "inicial"  # "inicial" | "vecino" | "sin_comillas"


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
        self.max_queries = getattr(settings, "WEB_ANALYSIS_MAX_QUERIES", 60)
        self.max_pages_total = getattr(settings, "WEB_ANALYSIS_MAX_PAGES_TOTAL", 16)
        # Tope de páginas que aporta cada consulta (ver `run_queries`).
        self.max_pages_per_query = getattr(settings, "WEB_ANALYSIS_MAX_PAGES_PER_QUERY", 3)
        # "parrafos": una frase por párrafo de prosa; "tramos": modo anterior
        # (8 tramos, 2 frases con comillas + 1 sin comillas por tramo).
        self.query_mode = getattr(settings, "WEB_QUERY_MODE", "parrafos")
        # Resultados pedidos a Brave y cuántos se usan de cada consulta.
        self.search_count = getattr(settings, "BRAVE_SEARCH_COUNT", 20)
        self.results_per_query = getattr(settings, "WEB_RESULTS_PER_QUERY", 20)
        self.use_extra_snippets = getattr(settings, "WEB_USE_EXTRA_SNIPPETS", True)
        self.snippet_match = getattr(settings, "WEB_SNIPPET_MATCH", True)
        self.unquoted_retry = getattr(settings, "WEB_UNQUOTED_RETRY", True)
        self.adaptive_neighbors = getattr(settings, "WEB_ADAPTIVE_NEIGHBORS", True)
        self.page_votes = getattr(settings, "WEB_PAGE_VOTES", True)
        self.snippet_min_words = getattr(settings, "SIMILARITY_MIN_MATCH_WORDS", 8)
        # Resumen de la última búsqueda (lo lee el banco de similitud).
        self.last_stats: dict = {}

    def collect(
        self,
        content: str,
        covered_ranges: Sequence[tuple[int, int]] = (),
    ) -> list[WebPageContent]:
        """
        Páginas web contra las que comparar el documento. `covered_ranges`
        son los tramos (por carácter) que ya coincidieron con la base
        interna: esos párrafos son los últimos en recibir consulta.
        """
        if not getattr(settings, "WEB_ANALYSIS_ENABLED", True):
            return []

        if self.query_mode == "parrafos":
            return self._collect_by_paragraphs(content=content, covered_ranges=covered_ranges)

        phrase_groups = self._extract_search_phrases(content=content)

        if not phrase_groups:
            logger.info(
                "Búsqueda web omitida: no se encontraron frases buscables en el documento."
            )
            return []

        queries = self._build_queries(phrase_groups=phrase_groups)
        run = self.run_queries(queries=queries)
        pages = [page for _query, page in run.pages]
        self.last_stats = {
            "consultas": run.executed_queries,
            "paginas_leidas": run.fetched_pages,
            "paginas": len(pages),
        }

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
        pages: list[tuple[str, WebPageContent]] = []
        fetched_pages = self._fetch_in_rounds(results_by_query, seen_urls=set(), pages=pages)

        return WebSearchRun(
            pages=pages,
            executed_queries=len(results_by_query),
            fetched_pages=fetched_pages,
        )

    def _fetch_in_rounds(
        self,
        results_by_query: list[tuple[str, list[WebSearchResult]]],
        seen_urls: set[str],
        pages: list[tuple[str, WebPageContent]],
    ) -> int:
        """Agrega a `pages` las páginas leídas por rondas; devuelve cuántas se intentó leer."""
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

        return fetched_pages

    # --- modo "parrafos" ---------------------------------------------------

    def _collect_by_paragraphs(
        self,
        content: str,
        covered_ranges: Sequence[tuple[int, int]],
    ) -> list[WebPageContent]:
        paragraphs = self._query_paragraphs(content=content, covered_ranges=covered_ranges)

        if not paragraphs:
            self.last_stats = {"parrafos": 0, "consultas": 0}
            logger.info("Búsqueda web omitida: no hay párrafos buscables en el documento.")
            return []

        queries = self._run_paragraph_queries(paragraphs)
        pages, fetched = self._pages_for_queries(queries)

        reasons = Counter(query.reason for query in queries)
        self.last_stats = {
            "parrafos": len(paragraphs),
            "consultas": len(queries),
            "consultas_vecino": reasons["vecino"],
            "consultas_sin_comillas": reasons["sin_comillas"],
            "consultas_con_coincidencia": sum(1 for query in queries if query.hits),
            "paginas_leidas": fetched,
            "paginas": sum(1 for page in pages if page.source_format != SOURCE_FORMAT_SNIPPET),
            "solo_snippet": sum(1 for page in pages if page.source_format == SOURCE_FORMAT_SNIPPET),
        }
        logger.info(
            "Búsqueda web por párrafos terminada. párrafos=%s consultas=%s/%s "
            "vecinos=%s sin_comillas=%s con_coincidencia=%s páginas_leídas=%s "
            "páginas=%s solo_snippet=%s",
            len(paragraphs),
            len(queries),
            self.max_queries,
            reasons["vecino"],
            reasons["sin_comillas"],
            self.last_stats["consultas_con_coincidencia"],
            fetched,
            self.last_stats["paginas"],
            self.last_stats["solo_snippet"],
        )
        return pages

    def _query_paragraphs(
        self,
        content: str,
        covered_ranges: Sequence[tuple[int, int]],
    ) -> list[QueryParagraph]:
        """
        Párrafos de prosa en español con al menos PARAGRAPH_MIN_WORDS
        palabras y una frase buscable (8-14 palabras sin citas, cifras ni
        siglas). Prioridad: prosa sin citas, prosa con citas (Autor, año),
        listas o tablas, y al final los que ya coincidieron con la base
        interna.
        """
        covered = sorted(covered_ranges)
        paragraphs: list[QueryParagraph] = []
        paragraph_start = 0

        for separator in re.finditer(r"\n\s*\n|\Z", content):
            raw = content[paragraph_start : separator.start()]
            start = paragraph_start
            paragraph_start = separator.end()
            clean = re.sub(r"\s+", " ", raw).strip()
            word_count = len(clean.split())

            if word_count < self.PARAGRAPH_MIN_WORDS or self._is_low_value_paragraph(clean):
                continue

            if not self._looks_spanish(clean):
                continue

            phrase = self._best_phrase(raw)

            if phrase is None:
                continue

            end = start + len(raw)

            if self._covered_fraction(covered, start, end) >= 0.5:
                tier = TIER_INTERNAL
            elif self._looks_like_list_or_table(raw):
                tier = TIER_LIST
            elif _CITATION_RE.search(clean):
                tier = TIER_CITED
            else:
                tier = TIER_PROSE

            paragraphs.append(
                QueryParagraph(
                    index=len(paragraphs),
                    start=start,
                    end=end,
                    phrase=phrase,
                    words=word_count,
                    tier=tier,
                )
            )

        return paragraphs

    PARAGRAPH_MIN_WORDS = 30

    def _best_phrase(self, paragraph: str) -> str | None:
        best: tuple[float, str] | None = None

        # Cada línea cierra oración: un ítem de lista no se pega al texto
        # siguiente.
        for match in re.finditer(r"\S[^\n]*?(?:[.!?](?=\s)|$)", paragraph, flags=re.MULTILINE):
            sentence = re.sub(r"\s+", " ", match.group(0)).strip()

            if len(sentence.split()) < self.PHRASE_MIN_WORDS or not self._looks_spanish(sentence):
                continue

            for score, phrase in self._phrase_windows(sentence):
                if best is None or score > best[0]:
                    best = (score, phrase)

        return None if best is None else best[1]

    def _looks_like_list_or_table(self, raw: str) -> bool:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]

        if len(_LIST_ITEM_RE.findall(raw)) >= 2:
            return True

        if len(lines) >= 3:
            lengths = sorted(len(line) for line in lines)
            if lengths[len(lengths) // 2] < 80:
                return True

        tokens = raw.split()
        numeric = sum(1 for token in tokens if re.search(r"\d", token))
        return bool(tokens) and numeric / len(tokens) > 0.15

    def _covered_fraction(
        self,
        covered: list[tuple[int, int]],
        start: int,
        end: int,
    ) -> float:
        if not covered or end <= start:
            return 0.0

        overlap = sum(
            max(0, min(end, range_end) - max(start, range_start))
            for range_start, range_end in covered
            if range_start < end and start < range_end
        )
        return overlap / (end - start)

    def _run_paragraph_queries(self, paragraphs: list[QueryParagraph]) -> list[ParagraphQuery]:
        """
        Una consulta entre comillas por párrafo, en orden de prioridad,
        hasta `max_queries`. Cuando una consulta encuentra la frase en un
        snippet, los párrafos vecinos pasan al frente de la cola (la copia
        suele venir en bloques) y, si un vecino ya dio 0 resultados con
        comillas, se repite sin comillas.
        """
        ranked = sorted(paragraphs, key=lambda item: (item.tier, -item.words, item.index))
        queue: deque[tuple[str, int]] = deque(("comillas", item.index) for item in ranked)
        status: dict[int, str] = {}  # párrafo -> "coincidencia" | "cero" | "sin_coincidencia"
        retried: set[int] = set()
        done_phrases: dict[tuple[str, bool], str] = {}
        queries: list[ParagraphQuery] = []

        while queue and len(queries) < self.max_queries:
            if getattr(self.search_client, "quota_exhausted", False):
                logger.error("Búsqueda web cortada: Brave sin cuota tras %s consultas.", len(queries))
                break

            kind, index = queue.popleft()

            quoted = kind != "sin_comillas"

            if quoted and index in status:
                continue

            if not quoted and index in retried:
                continue

            paragraph = paragraphs[index]
            phrase_key = (paragraph.phrase.lower(), quoted)

            # Un párrafo repetido (p. ej. la conclusión que repite el
            # resumen) no gasta otra consulta.
            if phrase_key in done_phrases:
                if quoted:
                    status[index] = done_phrases[phrase_key]
                else:
                    retried.add(index)
                continue
            query = f'"{paragraph.phrase}"' if quoted else paragraph.phrase
            results = list(self.search_client.search(query=query, count=self.search_count))
            results = results[: self.results_per_query]
            hits = [result for result in results if self._snippet_hit(paragraph.phrase, result)]

            reason = "sin_comillas" if not quoted else ("vecino" if kind == "vecino" else "inicial")
            queries.append(
                ParagraphQuery(
                    paragraph=index, query=query, quoted=quoted, results=results,
                    hits=hits, reason=reason,
                )
            )

            if quoted:
                status[index] = "coincidencia" if hits else ("cero" if not results else "sin_coincidencia")
                done_phrases[phrase_key] = status[index]
            else:
                retried.add(index)
                done_phrases[phrase_key] = "hecha"
                if hits:
                    status[index] = "coincidencia"

            if hits:
                # appendleft invierte: el vecino anterior queda primero.
                for neighbor in (index + 1, index - 1):
                    if not 0 <= neighbor < len(paragraphs):
                        continue
                    if (
                        self.adaptive_neighbors
                        and neighbor not in status
                        and paragraphs[neighbor].tier != TIER_INTERNAL
                    ):
                        queue.appendleft(("vecino", neighbor))
                    elif (
                        self.unquoted_retry
                        and status.get(neighbor) == "cero"
                        and neighbor not in retried
                    ):
                        queue.appendleft(("sin_comillas", neighbor))
            elif (
                self.unquoted_retry
                and quoted
                and not results
                and any(status.get(n) == "coincidencia" for n in (index - 1, index + 1))
            ):
                queue.appendleft(("sin_comillas", index))

        return queries

    def _snippet_hit(self, phrase: str, result: WebSearchResult) -> bool:
        snippets = result.snippets if self.use_extra_snippets else (result.snippet,)
        return any(
            shares_run(phrase, text, self.snippet_min_words)
            for text in (result.title, *snippets)
            if text
        )

    def _pages_for_queries(
        self,
        queries: list[ParagraphQuery],
    ) -> tuple[list[WebPageContent], int]:
        """
        Descarga páginas: primero (con votos) las que tuvieron la frase en
        el snippet y las que aparecieron en varias consultas, aunque no
        estén en el top de ninguna; después el reparto por rondas de
        `run_queries`. Con WEB_SNIPPET_MATCH, toda URL cuyo snippet
        contiene la frase se compara también con sus snippets, aunque la
        página no se haya podido descargar.
        """
        seen_urls: set[str] = set()
        pages: list[tuple[str, WebPageContent]] = []
        fetched = 0

        if self.page_votes:
            fetched += self._fetch_voted(queries, seen_urls, pages)

        run = self._fetch_in_rounds(
            [(query.query, query.results) for query in queries],
            seen_urls=seen_urls,
            pages=pages,
        )
        fetched += run

        result_pages = [page for _query, page in pages]

        if self.snippet_match:
            result_pages = self._with_snippets(result_pages, queries)

        return result_pages, fetched

    def _fetch_voted(
        self,
        queries: list[ParagraphQuery],
        seen_urls: set[str],
        pages: list[tuple[str, WebPageContent]],
    ) -> int:
        url_votes: Counter[str] = Counter()
        domain_votes: Counter[str] = Counter()
        hit_urls: set[str] = set()
        first: dict[str, tuple[int, str, WebSearchResult]] = {}

        for position, query in enumerate(queries):
            canonical = {canonical_url(result.url): result for result in query.results}
            url_votes.update(canonical.keys())
            domain_votes.update({result.domain for result in query.results})
            hit_urls.update(canonical_url(result.url) for result in query.hits)

            for key, result in canonical.items():
                first.setdefault(key, (position, query.query, result))

        priority = sorted(
            (key for key in first if key in hit_urls or url_votes[key] >= 2),
            key=lambda key: (
                key not in hit_urls,
                -url_votes[key],
                -domain_votes[first[key][2].domain],
                first[key][0],
            ),
        )
        fetched = 0

        for key in priority:
            if len(pages) >= self.max_pages_total:
                break

            _position, query_text, result = first[key]

            if result.url in seen_urls:
                continue

            seen_urls.add(result.url)
            fetched += 1
            page = self.fetcher.fetch(result=result)

            if page is not None:
                pages.append((query_text, page))

        return fetched

    def _with_snippets(
        self,
        pages: list[WebPageContent],
        queries: list[ParagraphQuery],
    ) -> list[WebPageContent]:
        snippets_by_url: dict[str, list[str]] = {}
        result_by_url: dict[str, WebSearchResult] = {}
        hit_urls: list[str] = []

        for query in queries:
            for result in query.results:
                key = canonical_url(result.url)
                bucket = snippets_by_url.setdefault(key, [])
                for text in result.snippets if self.use_extra_snippets else (result.snippet,):
                    if text and text not in bucket:
                        bucket.append(text)
                result_by_url.setdefault(key, result)
            for result in query.hits:
                key = canonical_url(result.url)
                if key not in hit_urls:
                    hit_urls.append(key)

        by_url = {canonical_url(page.url): index for index, page in enumerate(pages)}
        pages = list(pages)

        for key in hit_urls:
            snippets = tuple(snippets_by_url.get(key, ()))

            if key in by_url:
                page = pages[by_url[key]]
                pages[by_url[key]] = WebPageContent(
                    title=page.title, url=page.url, domain=page.domain, text=page.text,
                    source_format=page.source_format,
                    segments=(*page.comparable_segments, *snippets),
                )
                continue

            result = result_by_url[key]
            pages.append(
                WebPageContent(
                    title=result.title,
                    url=result.url,
                    domain=result.domain,
                    text=" ".join(snippets),
                    source_format=SOURCE_FORMAT_SNIPPET,
                    segments=snippets,
                )
            )

        return pages

    # --- modo "tramos" (anterior) -----------------------------------------

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