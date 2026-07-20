from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from pypdf import PdfReader

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

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }

        params = {
            "q": cleaned_query,
            "count": min(count, 10),
            "country": self.country,
            "search_lang": self.search_lang,
        }

        try:
            response = requests.get(
                self.BASE_URL,
                headers=headers,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()

        except requests.RequestException as exc:
            logger.warning("Error Brave Search. query=%s error=%s", cleaned_query, exc)
            return []

        except ValueError as exc:
            logger.warning("JSON inválido Brave Search. query=%s error=%s", cleaned_query, exc)
            return []

        results: list[WebSearchResult] = []

        for item in payload.get("web", {}).get("results", []):
            url = item.get("url", "").strip()
            title = item.get("title", "").strip()
            snippet = item.get("description", "").strip()

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
        if self._looks_like_blocked_file(result.url):
            return None

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; IESPP-OriginalityBot/1.0; "
                "+https://iespp.local)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/pdf",
        }

        try:
            response = requests.get(
                result.url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )

            if response.status_code >= 400:
                return None

            content_type = response.headers.get("Content-Type", "").lower()
            url_lower = response.url.lower().split("?")[0]

            raw_content = self._read_limited_response(response=response)

            if not raw_content:
                return None

            if "application/pdf" in content_type or url_lower.endswith(".pdf"):
                return self._parse_pdf(
                    raw_content=raw_content,
                    result=result,
                )

            if "text/html" in content_type or "application/xhtml" in content_type:
                return self._parse_html(
                    html=raw_content.decode(response.encoding or "utf-8", errors="ignore"),
                    result=result,
                )

            return None

        except requests.RequestException as exc:
            logger.info("No se pudo leer web. url=%s error=%s", result.url, exc)
            return None

        except Exception as exc:
            logger.info("Error procesando web. url=%s error=%s", result.url, exc)
            return None

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


class WebCandidateCollector:
    """
    Busca candidatos web normales y también búsqueda dirigida académica.
    """

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

    def __init__(self) -> None:
        self.search_client = BraveWebSearchClient()
        self.fetcher = WebPageFetcher()
        self.max_query_phrases = getattr(settings, "WEB_ANALYSIS_MAX_QUERY_PHRASES", 8)
        self.max_pages_total = getattr(settings, "WEB_ANALYSIS_MAX_PAGES_TOTAL", 16)

    def collect(self, content: str) -> list[WebPageContent]:
        if not getattr(settings, "WEB_ANALYSIS_ENABLED", True):
            return []

        phrases = self._extract_search_phrases(content=content)

        if not phrases:
            return []

        queries = self._build_queries(phrases=phrases)

        seen_urls: set[str] = set()
        pages: list[WebPageContent] = []

        for query in queries:
            results = self.search_client.search(
                query=query,
                count=5,
            )

            for result in results:
                if result.url in seen_urls:
                    continue

                seen_urls.add(result.url)

                page = self.fetcher.fetch(result=result)

                if page is None:
                    continue

                pages.append(page)

                if len(pages) >= self.max_pages_total:
                    return pages

        return pages

    def _build_queries(self, phrases: list[str]) -> list[str]:
        queries: list[str] = []

        selected_phrases = phrases[: self.max_query_phrases]

        for phrase in selected_phrases:
            queries.append(f'"{phrase}"')

        for phrase in selected_phrases[:4]:
            queries.append(phrase)

        for phrase in selected_phrases[:3]:
            for domain in self.ACADEMIC_DOMAINS:
                queries.append(f'"{phrase}" site:{domain}')

        for phrase in selected_phrases[:3]:
            queries.append(f'"{phrase}" filetype:pdf')

        return self._dedupe_keep_order(queries)

    def _extract_search_phrases(self, content: str) -> list[str]:
        paragraphs = re.split(r"\n\s*\n", content)
        candidates: list[str] = []

        for paragraph in paragraphs:
            clean = re.sub(r"\s+", " ", paragraph).strip()

            if len(clean) < 160:
                continue

            if self._is_low_value_paragraph(clean):
                continue

            phrase = self._best_phrase_from_paragraph(clean)

            if phrase:
                candidates.append(phrase)

        return self._dedupe_keep_order(candidates)

    def _best_phrase_from_paragraph(self, paragraph: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)

        sentence_candidates: list[str] = []

        for sentence in sentences:
            clean = re.sub(r"\s+", " ", sentence).strip()
            words = clean.split()

            if 10 <= len(words) <= 34:
                sentence_candidates.append(clean)

        if sentence_candidates:
            sentence_candidates.sort(key=len, reverse=True)
            return sentence_candidates[0][:260]

        words = paragraph.split()

        if len(words) >= 18:
            return " ".join(words[:30])[:260]

        return ""

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