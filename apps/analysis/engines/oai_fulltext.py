from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone

from apps.analysis.engines.oai_harvester import (
    DC_NS,
    OAI_NS,
    OaiRepositoryConfig,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.indexers import OaiFullTextIndexer
from apps.analysis.models import OaiFullText, OaiFullTextStatus, OaiRecord
from apps.documents.extractors import (
    DocumentTextExtractor,
    normalize_pdf_pages,
    read_pdf_pages,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "IESPPAB-FullText-Harvester/1.0 (+https://iesppab.edu.pe; "
    "texto completo de tesis para control de originalidad academica)"
)

_HANDLE_RE = re.compile(r"hdl\.handle\.net/(.+)$")
_LICENSE_URL_RE = re.compile(r"https?://creativecommons\.org/[^\s\"'<>]+")
_YEAR_RE = re.compile(r"\b(\d{4})\b")
_EMBARGO_END_RE = re.compile(r"embargoEnd/(\d{4}-\d{2}-\d{2})")
_RESTRICTED_RIGHTS = ("embargoedAccess", "restrictedAccess", "closedAccess")


class RepositoryThrottled(Exception):
    """El repositorio pide bajar el ritmo: hay que detener la cosecha."""


class FullTextFetchError(Exception):
    """Fallo de red o de contenido al descargar. Se reintenta en otra corrida."""


class RobotsUnavailable(Exception):
    """No se pudo leer robots.txt: sin él no se descarga nada."""


class ResponseTooLarge(FullTextFetchError):
    pass


@dataclass(frozen=True)
class FetchedResponse:
    status_code: int
    url: str
    content: bytes
    content_type: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class FullTextOutcome:
    status: str
    publication_year: int | None = None
    rights: str = ""
    pdf_url: str = ""
    embargo_end: date | None = None
    content: str = ""
    error_message: str = ""


class PoliteClient:
    """
    Cliente HTTP de la cosecha: un solo punto por el que pasa cada petición,
    incluidas las redirecciones y robots.txt. El intervalo se mide de inicio
    a inicio, y cada salto se comprueba contra robots.txt de su host.
    """

    MIN_INTERVAL_SECONDS = 3.0
    TIMEOUT_SECONDS = 30
    # `timeout` de requests es por lectura: un servidor que entrega de a
    # pocos bytes nunca lo dispara. Este es el tope de la descarga completa.
    MAX_DOWNLOAD_SECONDS = 120
    MAX_REDIRECTS = 5
    CHUNK_BYTES = 64 * 1024

    def __init__(
        self,
        session=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self._last_request_at: float | None = None
        self._robots: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"

        if host not in self._robots:
            self._robots[host] = self._load_robots(host)

        return self._robots[host].can_fetch(USER_AGENT, url)

    def get(
        self,
        url: str,
        *,
        max_bytes: int,
        params: dict | None = None,
    ) -> FetchedResponse:
        for _ in range(self.MAX_REDIRECTS + 1):
            if not self.allowed(url):
                raise FullTextFetchError(f"Bloqueado por robots.txt: {url}")

            response = self._request(url, params=params)
            params = None

            if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                response.close()

                if not location:
                    raise FullTextFetchError(f"Redirección sin destino: {url}")

                url = urljoin(url, location)
                continue

            return self._read(response, url=url, max_bytes=max_bytes)

        raise FullTextFetchError(f"Demasiadas redirecciones: {url}")

    def _load_robots(self, host: str) -> RobotFileParser:
        parser = RobotFileParser()

        try:
            response = self._request(f"{host}/robots.txt")
        except FullTextFetchError as exc:
            raise RobotsUnavailable(str(exc)) from exc

        if response.status_code >= 500 or response.status_code in (401, 403, 429):
            response.close()
            raise RobotsUnavailable(
                f"robots.txt respondió {response.status_code}: {host}"
            )

        body = self._read(response, url=f"{host}/robots.txt", max_bytes=1_000_000)
        lines = body.text.splitlines() if response.status_code == 200 else []
        parser.parse(lines)
        return parser

    def _request(self, url: str, params: dict | None = None):
        if self._last_request_at is not None:
            remaining = self.MIN_INTERVAL_SECONDS - (
                self.clock() - self._last_request_at
            )

            if remaining > 0:
                self.sleep(remaining)

        self._last_request_at = self.clock()

        try:
            response = self.session.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=self.TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
            )
        except requests.exceptions.RequestException as exc:
            raise FullTextFetchError(f"Error de red: {exc}") from exc

        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After", "")
            status_code = response.status_code
            response.close()
            raise RepositoryThrottled(
                f"{url} respondió {status_code} (Retry-After: {retry_after or '-'})."
            )

        return response

    def _read(self, response, *, url: str, max_bytes: int) -> FetchedResponse:
        declared = response.headers.get("Content-Length", "")

        if declared.isdigit() and int(declared) > max_bytes:
            response.close()
            raise ResponseTooLarge(f"{declared} bytes")

        started_at = self.clock()
        buffer = bytearray()

        try:
            for chunk in response.iter_content(chunk_size=self.CHUNK_BYTES):
                buffer.extend(chunk)

                if len(buffer) > max_bytes:
                    raise ResponseTooLarge(f"más de {max_bytes} bytes")

                if self.clock() - started_at > self.MAX_DOWNLOAD_SECONDS:
                    raise FullTextFetchError(f"Descarga demasiado lenta: {url}")
        except requests.exceptions.RequestException as exc:
            raise FullTextFetchError(f"Error de red: {exc}") from exc
        finally:
            response.close()

        return FetchedResponse(
            status_code=response.status_code,
            url=url,
            content=bytes(buffer),
            content_type=response.headers.get("Content-Type", ""),
        )


class OaiFullTextHarvester:
    """
    Descarga el PDF de un registro OAI, extrae su texto con el mismo
    extractor y filtro de los documentos subidos, y lo indexa. El PDF nunca
    se guarda en disco.
    """

    MAX_PDF_BYTES = 20 * 1024 * 1024
    MAX_PAGE_BYTES = 5 * 1024 * 1024
    # Por debajo de esto por página, casi todo el PDF es imagen.
    SCANNED_MAX_WORDS_PER_PAGE = 30
    # Menos que esto tras el filtro no es una tesis: suelen ser PDF de pocas
    # páginas que se publican en lugar del documento completo.
    MIN_INDEXED_WORDS = 1500

    def __init__(
        self,
        repository: OaiRepositoryConfig,
        client: PoliteClient | None = None,
        indexer: OaiFullTextIndexer | None = None,
    ) -> None:
        self.repository = repository
        self.client = client or PoliteClient()
        self.indexer = indexer or OaiFullTextIndexer()
        self.text_filter = AcademicTextFilter()
        self.extractor = DocumentTextExtractor()

    def harvest(self, record: OaiRecord) -> OaiFullText:
        return self.save(record, self.process(record))

    def process(self, record: OaiRecord) -> FullTextOutcome:
        try:
            return self._process(record)
        except (RepositoryThrottled, RobotsUnavailable):
            raise
        except Exception as exc:
            # Un registro raro no debe tumbar una descarga de horas.
            if not isinstance(exc, FullTextFetchError):
                logger.exception(
                    "Error inesperado cosechando texto completo. record_id=%s",
                    record.id,
                )

            return FullTextOutcome(
                status=OaiFullTextStatus.ERROR,
                error_message=str(exc)[:500],
            )

    def save(self, record: OaiRecord, outcome: FullTextOutcome) -> OaiFullText:
        word_count = len(outcome.content.split())

        with transaction.atomic():
            previous = OaiFullText.objects.filter(record=record).first()

            if outcome.status == OaiFullTextStatus.OK:
                self.indexer.index(record, content=outcome.content)
            else:
                self.indexer.clear(record)

            full_text, _ = OaiFullText.objects.update_or_create(
                record=record,
                defaults={
                    "status": outcome.status,
                    "publication_year": outcome.publication_year,
                    "rights": outcome.rights[:255],
                    "pdf_url": outcome.pdf_url,
                    "embargo_end": outcome.embargo_end,
                    "word_count": (
                        word_count if outcome.status == OaiFullTextStatus.OK else 0
                    ),
                    "attempts": previous.attempts + 1 if previous else 1,
                    "error_message": outcome.error_message,
                },
            )

        return full_text

    def _process(self, record: OaiRecord) -> FullTextOutcome:
        page = self.client.get(
            self._landing_url(record),
            max_bytes=self.MAX_PAGE_BYTES,
        )

        if page.status_code != 200:
            raise FullTextFetchError(f"Página respondió {page.status_code}.")

        soup = BeautifulSoup(page.text, "lxml")
        pdf_url = self._meta(soup, "citation_pdf_url")
        year = self._year(self._meta(soup, "citation_publication_date"))

        if not pdf_url:
            return FullTextOutcome(
                status=OaiFullTextStatus.SIN_PDF,
                publication_year=year,
            )

        rights_values, embargo_end = self._oai_rights(record)
        rights = self._pick_license(rights_values) or self._page_license(page.text)
        base = {
            "publication_year": year,
            "rights": rights,
            "pdf_url": pdf_url,
            "embargo_end": embargo_end,
        }

        if self._is_restricted(rights_values, embargo_end):
            return FullTextOutcome(
                status=OaiFullTextStatus.EMBARGADO,
                error_message=f"Acceso: {'; '.join(rights_values)}"[:500],
                **base,
            )

        try:
            pdf = self.client.get(pdf_url, max_bytes=self.MAX_PDF_BYTES)
        except ResponseTooLarge as exc:
            return FullTextOutcome(
                status=OaiFullTextStatus.SIN_PDF,
                error_message=f"PDF supera 20 MB: {exc}",
                **base,
            )

        if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
            raise FullTextFetchError(f"PDF inválido (HTTP {pdf.status_code}).")

        return self._extract(pdf.content, base)

    def _extract(self, pdf_bytes: bytes, base: dict) -> FullTextOutcome:
        try:
            pages = read_pdf_pages(io.BytesIO(pdf_bytes))
        except Exception as exc:
            logger.warning("PDF ilegible. pdf_url=%s error=%s", base["pdf_url"], exc)
            return FullTextOutcome(
                status=OaiFullTextStatus.ERROR,
                error_message=f"PDF ilegible: {exc}"[:500],
                **base,
            )

        raw_words = sum(len(page.split()) for page in pages)

        if raw_words < len(pages) * self.SCANNED_MAX_WORDS_PER_PAGE:
            return FullTextOutcome(status=OaiFullTextStatus.ESCANEADO, **base)

        # pypdf puede devolver sustitutos sueltos, que no se codifican en UTF-8.
        text = normalize_pdf_pages(pages).encode("utf-8", "ignore").decode("utf-8")
        normalized = self.extractor._normalize_text(text)
        content = self.text_filter.filter_for_similarity(content=normalized).content

        if len(content.split()) < self.MIN_INDEXED_WORDS:
            return FullTextOutcome(
                status=OaiFullTextStatus.TEXTO_INSUFICIENTE,
                error_message=(
                    f"{len(pages)} páginas, {len(content.split())} palabras "
                    "tras el filtro."
                ),
                **base,
            )

        return FullTextOutcome(status=OaiFullTextStatus.OK, content=content, **base)

    def _landing_url(self, record: OaiRecord) -> str:
        """
        El handle se resuelve directo en el repositorio: pasar por
        hdl.handle.net sería una petición a un tercero sin necesidad.
        """
        match = _HANDLE_RE.search(record.source_url)

        if match:
            return f"https://{self.repository.domain}/handle/{match.group(1)}"

        return record.source_url

    def _meta(self, soup: BeautifulSoup, name: str) -> str:
        element = soup.find("meta", attrs={"name": name})
        return (element.get("content") or "").strip() if element else ""

    def _year(self, value: str) -> int | None:
        match = _YEAR_RE.search(value)
        return int(match.group(1)) if match else None

    def _oai_rights(self, record: OaiRecord) -> tuple[list[str], date | None]:
        """
        dc:rights y fin de embargo: el `oai_dc` de OaiRecord no los guardó,
        y la página del ítem no los publica como meta.
        """
        response = self.client.get(
            self.repository.base_url,
            max_bytes=self.MAX_PAGE_BYTES,
            params={
                "verb": "GetRecord",
                "metadataPrefix": "oai_dc",
                "identifier": record.oai_identifier,
            },
        )

        if response.status_code != 200:
            raise FullTextFetchError(f"GetRecord respondió {response.status_code}.")

        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise FullTextFetchError(f"GetRecord ilegible: {exc}") from exc

        if root.find(f"{OAI_NS}error") is not None:
            raise FullTextFetchError("GetRecord devolvió un error OAI-PMH.")

        rights = [
            (element.text or "").strip()
            for element in root.iter(f"{DC_NS}rights")
            if (element.text or "").strip()
        ]
        embargo_end = None

        for element in root.iter(f"{DC_NS}date"):
            match = _EMBARGO_END_RE.search(element.text or "")

            if match:
                embargo_end = date.fromisoformat(match.group(1))

        return rights, embargo_end

    def _is_restricted(
        self,
        rights: list[str],
        embargo_end: date | None,
    ) -> bool:
        if embargo_end is not None:
            return embargo_end > timezone.localdate()

        return any(marker in value for value in rights for marker in _RESTRICTED_RIGHTS)

    def _pick_license(self, rights: list[str]) -> str:
        """La licencia concreta si la hay; si no, la condición de acceso."""
        for value in rights:
            if _LICENSE_URL_RE.search(value):
                return value

        return "; ".join(rights)

    def _page_license(self, html: str) -> str:
        match = _LICENSE_URL_RE.search(html)
        return match.group(0) if match else ""
