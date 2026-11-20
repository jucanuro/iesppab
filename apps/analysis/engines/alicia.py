"""
Cosecha de tesis peruanas a través de ALICIA (CONCYTEC).

ALICIA no publica OAI-PMH (su `vufind/OAI/Server` responde "OAI Server Not
Configured"), pero su API pública de VuFind (`/vufind/api/v1/search`) entrega
en JSON los metadatos del registro original, su licencia y los enlaces
directos a los archivos en el repositorio de origen. Su robots.txt pide
`Crawl-delay: 10` y no prohíbe `/vufind/api`.

El flujo tiene dos pasos, igual que con UPeU:
1. `harvest_alicia` guarda metadatos como `OaiRecord` (con los enlaces en
   `extra_metadata`), sin descargar archivos.
2. `harvest_oai_fulltext --repositorio ALICIA` descarga los PDF desde cada
   repositorio de origen, respetando su robots.txt, sin guardarlos.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone

from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.oai_fulltext import (
    FetchedResponse,
    FullTextFetchError,
    FullTextOutcome,
    OaiFullTextHarvester,
    PoliteClient,
    ResponseTooLarge,
)
from apps.analysis.engines.oai_harvester import OaiHarvester, OaiRepositoryConfig
from apps.analysis.models import DocumentFingerprint, OaiFullTextStatus, OaiRecord

logger = logging.getLogger(__name__)

ALICIA_SEARCH_URL = "https://alicia.concytec.gob.pe/vufind/api/v1/search"
ALICIA_SUFFIX = " (vía ALICIA)"
ALICIA_REPOSITORY = OaiRepositoryConfig(
    name="ALICIA (CONCYTEC)",
    domain="alicia.concytec.gob.pe",
    base_url=ALICIA_SEARCH_URL,
)
THESIS_FORMATS = ("bachelorThesis", "masterThesis", "doctoralThesis")

# Repositorios cuyo robots.txt o protección anti-bots ya impidió descargar
# (ver memoria del proyecto, 2026-10-31). Se omiten para no gastar
# peticiones: los metadatos sin texto completo no aportan a la similitud.
DEFAULT_EXCLUDED_DOMAINS = (
    "repositorio.ucv.edu.pe",
    "repositorio.une.edu.pe",
    "repositorio.usil.edu.pe",
    "tesis.pucp.edu.pe",
    "repositorio.uni.edu.pe",
)

_FIELDS = ("id", "title", "rawData", "dcDateEmbargoEnd")
_DSPACE7_DOWNLOAD_RE = re.compile(r"/bitstreams/([0-9a-fA-F-]{36})/download")
_LEGACY_BITSTREAM_RE = re.compile(r"/bitstream/(?:handle/)?(\d[\d.]*/[^/]+)/(\d+)/")
_HANDLE_IN_TEXT_RE = re.compile(r"(?:handle(?:\.net)?/|:)(\d+(?:\.\d+)*/\d+)\b")
_DSPACE7_REST_PREFIXES = ("/server/api", "/backend/api")
# Derivados de DSpace (texto extraído, miniaturas) y licencia: no son la tesis.
_SKIP_FILE_RE = re.compile(r"(license\.txt|\.txt|\.jpe?g|\.png|\.gif)$", re.IGNORECASE)
# Archivos que suelen acompañar a la tesis y no son ella.
_SIDE_FILE_RE = re.compile(
    r"autoriza|ficha|reporte|turnitin|similitud|originalidad|acta|constancia|"
    r"declaraci|formato|anexo|caratula|car%c3%a1tula",
    re.IGNORECASE,
)
_MAX_BITSTREAMS = 6


class AliciaClient(PoliteClient):
    """Cliente cortés para ALICIA: 10 s entre peticiones (su Crawl-delay)."""

    MIN_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class AliciaQuery:
    subject: str = "educación"
    institutions: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    page_size: int = 100

    def params(self, page: int) -> dict:
        filters = [f'~format:"{value}"' for value in THESIS_FORMATS]
        filters += ['language:"spa"', 'eu_rights_str_mv:"openAccess"']
        filters += [f'~institution:"{value}"' for value in self.institutions]

        if self.year_from is not None or self.year_to is not None:
            start = self.year_from if self.year_from is not None else "*"
            end = self.year_to if self.year_to is not None else "*"
            filters.append(f'publishDate:"[{start} TO {end}]"')

        return {
            "lookfor": self.subject,
            "type": "Subject" if self.subject else "AllFields",
            "filter[]": filters,
            "field[]": list(_FIELDS),
            "limit": self.page_size,
            "page": page,
        }


@dataclass
class AliciaRecord:
    alicia_id: str
    oai_identifier: str
    title: str
    authors: str
    abstract: str
    source_url: str
    domain: str
    repository_name: str
    year: int | None
    license: str
    bitstream_urls: list[str] = field(default_factory=list)
    embargo_end: date | None = None


class AliciaSearch:
    """Recorre la API de búsqueda de ALICIA página a página."""

    MAX_RESPONSE_BYTES = 20 * 1024 * 1024
    MAX_PAGES = 500

    def __init__(self, client: PoliteClient | None = None) -> None:
        self.client = client or AliciaClient()

    def total(self, query: AliciaQuery) -> int:
        params = query.params(page=1)
        params["limit"] = 0
        return int(self._get(params).get("resultCount") or 0)

    def iter_records(self, query: AliciaQuery) -> Iterator[AliciaRecord | None]:
        for page in range(1, self.MAX_PAGES + 1):
            data = self._get(query.params(page=page))
            records = data.get("records") or []

            for raw in records:
                yield parse_record(raw)

            if len(records) < query.page_size:
                return

    def _get(self, params: dict) -> dict:
        response = self.client.get(
            ALICIA_SEARCH_URL,
            max_bytes=self.MAX_RESPONSE_BYTES,
            params=params,
        )

        if response.status_code != 200:
            raise FullTextFetchError(f"ALICIA respondió {response.status_code}.")

        try:
            data = json.loads(response.content)
        except ValueError as exc:
            raise FullTextFetchError(f"Respuesta de ALICIA ilegible: {exc}") from exc

        if data.get("status") != "OK":
            raise FullTextFetchError(
                f"ALICIA devolvió error: {data.get('statusMessage', '')}"
            )

        return data


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _handle(record: OaiRecord) -> str:
    """El handle (prefijo/número) del registro, desde la URL o el id OAI."""
    for value in (record.source_url, record.oai_identifier):
        match = _HANDLE_IN_TEXT_RE.search(value or "")

        if match:
            return match.group(1)

    return ""


def rank_file_urls(urls: list[str]) -> list[str]:
    """
    Quita licencias, textos extraídos y miniaturas, y deja al final los
    archivos que parecen autorizaciones o reportes de similitud.
    """
    useful = [url for url in urls if not _SKIP_FILE_RE.search(urlsplit(url).path)]
    return sorted(
        useful,
        key=lambda url: bool(_SIDE_FILE_RE.search(urlsplit(url).path.rsplit("/", 1)[-1])),
    )


def _first(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""

    return str(value or "").strip()


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower() if url.startswith("http") else ""


def _domain(oai_identifier: str, bitstreams: list[str], url: str) -> str:
    """Dominio del repositorio de origen, donde están los archivos."""
    for candidate in bitstreams:
        if host := _host(candidate):
            return host

    parts = oai_identifier.split(":")

    if len(parts) >= 3 and "." in parts[1]:
        return parts[1].lower()

    host = _host(url)
    return "" if host.endswith("handle.net") else host


def _year(raw: dict) -> int | None:
    value = _first(raw.get("publishDate"))
    return int(value[:4]) if value[:4].isdigit() else None


def _embargo_end(record: dict) -> date | None:
    raw = record.get("rawData") or {}
    values = list(record.get("dcDateEmbargoEnd") or [])
    values += [
        _first(value) for key, value in raw.items() if "embargoend" in key.lower()
    ]

    for value in values:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue

    return None


def parse_record(record: dict) -> AliciaRecord | None:
    raw = record.get("rawData") or {}
    oai_identifier = _first(raw.get("oai_identifier_str"))
    title = _first(raw.get("title")) or _first(record.get("title"))

    if not oai_identifier or not title:
        return None

    bitstreams = [
        url
        for url in (raw.get("bitstream.url.fl_str_mv") or [])
        if isinstance(url, str) and url.startswith("http")
    ][:_MAX_BITSTREAMS]
    url = _first(raw.get("url"))
    domain = _domain(oai_identifier, bitstreams, url)

    if not domain:
        return None

    license_value = next(
        (
            _first(value)
            for key, value in raw.items()
            if key.startswith("dc.rights.uri")
        ),
        "",
    )

    return AliciaRecord(
        alicia_id=str(record.get("id") or raw.get("id") or ""),
        oai_identifier=oai_identifier[:255],
        title=title[:500],
        authors="; ".join(raw.get("author_facet") or raw.get("author") or []),
        abstract=_first(raw.get("description")),
        source_url=url[:200],
        domain=domain,
        repository_name=_first(raw.get("repository.name.fl_str_mv")) or domain,
        year=_year(raw),
        license=license_value or "info:eu-repo/semantics/openAccess",
        bitstream_urls=bitstreams,
        embargo_end=_embargo_end(record),
    )


def save_record(record: AliciaRecord) -> OaiRecord:
    """Crea el `OaiRecord` y las huellas del título y resumen."""
    normalized = OaiHarvester()._normalize(f"{record.title} {record.abstract}")

    with transaction.atomic():
        oai_record = OaiRecord.objects.create(
            oai_identifier=record.oai_identifier,
            repository_name=f"{record.repository_name[:150]}{ALICIA_SUFFIX}",
            repository_domain=record.domain[:180],
            title=record.title,
            authors=record.authors,
            source_url=record.source_url,
            text_excerpt=record.abstract or record.title,
            normalized_text=normalized,
            source_date=str(record.year or "")[:40],
            harvested_at=timezone.now(),
            is_active=True,
            extra_metadata={
                "fuente": "alicia",
                "alicia_id": record.alicia_id,
                "bitstream_urls": record.bitstream_urls,
                "licencia": record.license,
                "anio": record.year,
            },
        )
        DocumentFingerprint.objects.bulk_create(
            [
                DocumentFingerprint(hash=value, source_type="oai", source_id=oai_record.id)
                for value in compute_fingerprints(normalized)
            ],
            ignore_conflicts=True,
        )

    return oai_record


class AliciaFullTextHarvester(OaiFullTextHarvester):
    """
    Texto completo de registros traídos desde ALICIA. Los archivos se piden
    directo al repositorio de origen con los enlaces de ALICIA; los derechos
    ya se filtraron al cosechar los metadatos (solo acceso abierto y sin
    embargo vigente). Si no hay enlaces, se usa la página del ítem como con
    los repositorios OAI.
    """

    # Muchos repositorios suben aparte la autorización o el reporte de
    # similitud: si el primer PDF es corto, se prueba el siguiente.
    MAX_PDFS_PER_RECORD = 3

    def __init__(self, client: PoliteClient | None = None, indexer=None) -> None:
        super().__init__(ALICIA_REPOSITORY, client=client, indexer=indexer)
        self._dspace7_hosts: set[str] = set()
        self._rest_bases: dict[str, str] = {}
        self._last_was_html = False
        self._notes: list[str] = []

    def _process(self, record: OaiRecord) -> FullTextOutcome:
        extra = record.extra_metadata or {}
        urls = rank_file_urls(extra.get("bitstream_urls") or [])
        base = {
            "publication_year": extra.get("anio"),
            "rights": extra.get("licencia") or "",
            "pdf_url": "",
            "embargo_end": None,
        }

        if not urls:
            return self._process_landing(record, base)

        allowed = [url for url in urls if self.client.allowed(url)]

        if not allowed:
            return FullTextOutcome(
                status=OaiFullTextStatus.SIN_PDF,
                error_message="robots.txt del repositorio no permite descargar.",
                **base,
            )

        self._notes = []
        best: FullTextOutcome | None = None
        pdfs_tried = 0

        for url in self._candidates(record, allowed):
            if pdfs_tried >= self.MAX_PDFS_PER_RECORD:
                break

            response = self._fetch_file(url) if url in allowed else self._get_pdf(url)

            if response is None:
                continue

            pdfs_tried += 1
            outcome = self._extract(response.content, {**base, "pdf_url": response.url})

            if outcome.status == OaiFullTextStatus.OK:
                return outcome

            best = best or outcome

        if best is not None:
            return best

        detail = "; ".join(self._notes[:4])
        return FullTextOutcome(
            status=OaiFullTextStatus.SIN_PDF,
            error_message=f"Ningún enlace entregó un PDF. {detail}"[:500],
            **base,
        )

    def _candidates(self, record: OaiRecord, links: list[str]) -> Iterator[str]:
        """
        Primero los enlaces de ALICIA; si ninguno sirvió, los PDF que el
        repositorio lista hoy para el ítem en su API REST (ALICIA puede
        tener enlaces viejos: el archivo se reemplazó o cambió de número).
        """
        yield from links

        for url in self._rest_item_files(record, links):
            if url not in links:
                yield url

    def _rest_item_files(self, record: OaiRecord, links: list[str]) -> list[str]:
        handle = _handle(record)

        if not handle:
            return []

        host = _origin(links[0]) if links else f"https://{record.repository_domain}"
        rest = self._rest_base(host)

        if not rest:
            return []

        item = self._get_json(f"{rest}/pid/find", params={"id": handle})
        item_id = (item or {}).get("uuid")

        if not item_id:
            self._notes.append(f"API REST no encontró el ítem {handle}")
            return []

        bundles = self._get_json(
            f"{rest}/core/items/{item_id}/bundles",
            params={"embed": "bitstreams"},
        )
        files = []

        for bundle in ((bundles or {}).get("_embedded") or {}).get("bundles") or []:
            if bundle.get("name") != "ORIGINAL":
                continue

            embedded = ((bundle.get("_embedded") or {}).get("bitstreams") or {})

            for bitstream in (embedded.get("_embedded") or {}).get("bitstreams") or []:
                name = str(bitstream.get("name") or "")
                href = ((bitstream.get("_links") or {}).get("content") or {}).get("href")

                if href and name.lower().endswith(".pdf"):
                    files.append((bool(_SIDE_FILE_RE.search(name)), href))

        return [href for _, href in sorted(files, key=lambda pair: pair[0])]

    def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        if not self.client.allowed(url):
            return None

        response = self.client.get(url, max_bytes=self.MAX_PAGE_BYTES, params=params)

        if response.status_code != 200:
            self._notes.append(f"{urlsplit(url).path[-40:]} respondió {response.status_code}")
            return None

        try:
            return json.loads(response.content)
        except ValueError:
            return None

    def _fetch_file(self, url: str) -> FetchedResponse | None:
        """
        El PDF del enlace, o None. En DSpace 7 los enlaces de ALICIA
        (`/bitstreams/<uuid>/download` y los antiguos `/bitstream/<handle>/
        <n>/<nombre>`) devuelven la página de Angular, no el archivo: en ese
        caso se pide el archivo a la API REST del repositorio.
        """
        host = _origin(url)

        if host not in self._dspace7_hosts:
            response = self._get_pdf(url)

            if response is not None or not self._last_was_html:
                return response

            self._dspace7_hosts.add(host)

        rest_url = self._rest_content_url(url)
        return self._get_pdf(rest_url) if rest_url else None

    def _get_pdf(self, url: str) -> FetchedResponse | None:
        self._last_was_html = False

        if not self.client.allowed(url):
            return None

        try:
            response = self.client.get(url, max_bytes=self.MAX_PDF_BYTES)
        except ResponseTooLarge:
            return None

        if response.status_code == 200 and response.content.startswith(b"%PDF"):
            return response

        self._notes.append(
            f"{unquote(urlsplit(url).path)[-40:]} → HTTP {response.status_code}"
            f" {response.content_type.split(';')[0]}"
        )
        self._last_was_html = response.status_code == 200 and (
            "html" in response.content_type.lower()
            or response.content.lstrip()[:15].lower().startswith(b"<!doctype html")
        )
        return None

    def _rest_content_url(self, url: str) -> str:
        host = _origin(url)
        rest = self._rest_base(host)

        if not rest:
            return ""

        path = urlsplit(url).path

        if match := _DSPACE7_DOWNLOAD_RE.search(path):
            return f"{rest}/core/bitstreams/{match.group(1)}/content"

        if match := _LEGACY_BITSTREAM_RE.search(path):
            handle, sequence = match.group(1), match.group(2)
            response = self.client.get(
                f"{rest}/core/bitstreams/search/byItemHandle",
                max_bytes=self.MAX_PAGE_BYTES,
                params={"handle": unquote(handle), "sequence": sequence},
            )

            if response.status_code != 200:
                return ""

            try:
                data = json.loads(response.content)
            except ValueError:
                return ""

            return str(((data.get("_links") or {}).get("content") or {}).get("href") or "")

        return ""

    def _rest_base(self, host: str) -> str:
        """La API REST de DSpace 7 suele estar en /server o /backend."""
        if host in self._rest_bases:
            return self._rest_bases[host]

        found = ""

        for prefix in _DSPACE7_REST_PREFIXES:
            candidate = f"{host}{prefix}"

            if not self.client.allowed(candidate):
                continue

            response = self.client.get(candidate, max_bytes=self.MAX_PAGE_BYTES)

            if response.status_code == 200 and "json" in response.content_type.lower():
                found = candidate
                break

        self._rest_bases[host] = found
        return found

    def _process_landing(self, record: OaiRecord, base: dict) -> FullTextOutcome:
        page = self.client.get(self._landing_url(record), max_bytes=self.MAX_PAGE_BYTES)

        if page.status_code != 200:
            raise FullTextFetchError(f"Página respondió {page.status_code}.")

        pdf_url = self._meta(BeautifulSoup(page.text, "lxml"), "citation_pdf_url")

        if not pdf_url:
            return FullTextOutcome(status=OaiFullTextStatus.SIN_PDF, **base)

        pdf = self.client.get(pdf_url, max_bytes=self.MAX_PDF_BYTES)

        if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
            raise FullTextFetchError(f"PDF inválido (HTTP {pdf.status_code}).")

        return self._extract(pdf.content, {**base, "pdf_url": pdf_url})

    def _landing_url(self, record: OaiRecord) -> str:
        match = record.source_url.split("hdl.handle.net/", 1)

        if len(match) == 2:
            return f"https://{record.repository_domain}/handle/{match[1]}"

        return record.source_url
