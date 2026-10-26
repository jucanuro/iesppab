from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from xml.etree import ElementTree

import requests
from django.utils import timezone

from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.models import DocumentFingerprint, OaiRecord

logger = logging.getLogger(__name__)

OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"

# Algunos repositorios (ej. PUCP) devuelven 403 ante clientes HTTP sin
# User-Agent identificable (bloquean el default de `requests`/`curl` por
# heurística anti-bot), aunque el endpoint OAI-PMH es público y está pensado
# justamente para ser cosechado por máquinas. Nos identificamos con un UA
# descriptivo en vez de simular un navegador.
HTTP_HEADERS = {
    "User-Agent": (
        "IESPPAB-OAI-Harvester/1.0 (+https://iesppab.edu.pe; "
        "cosecha de metadatos OAI-PMH con fines academicos)"
    ),
}

THESIS_KEYWORDS = (
    "tesis",
    "trabajo de investigacion",
    "trabajo de investigación",
    "trabajo de suficiencia profesional",
    "trabajo academico",
    "trabajo académico",
    "tesina",
    "monografia",
    "monografía",
)


@dataclass(frozen=True)
class OaiRepositoryConfig:
    name: str
    domain: str
    base_url: str
    oai_set: str = ""


@dataclass(frozen=True)
class OaiHarvestSummary:
    repository_name: str
    created_count: int
    updated_count: int
    skipped_count: int
    error: str = ""


class OaiHarvester:
    """
    Cliente mínimo OAI-PMH (verb=ListRecords, metadataPrefix=oai_dc).

    Pagina usando resumptionToken y aísla los errores por repositorio para
    que un repositorio caído no interrumpa el harvest de los demás.
    """

    TIMEOUT_SECONDS = 20
    MAX_PAGES = 200
    PAGE_DELAY_SECONDS = 0.4
    MAX_RETRIES = 3
    RETRY_BACKOFF_SECONDS = 5

    def __init__(self, max_pages: int | None = None) -> None:
        if max_pages is not None:
            self.MAX_PAGES = max_pages

    def harvest_repository(
        self,
        repository: OaiRepositoryConfig,
    ) -> OaiHarvestSummary:
        created_count = 0
        updated_count = 0
        skipped_count = 0

        try:
            for record in self._iter_records(repository=repository):
                if record is None:
                    skipped_count += 1
                    continue

                oai_record, created = OaiRecord.objects.update_or_create(
                    oai_identifier=record["oai_identifier"],
                    defaults={
                        "repository_name": repository.name,
                        "repository_domain": repository.domain,
                        "title": record["title"],
                        "authors": record["authors"],
                        "source_url": record["source_url"],
                        "text_excerpt": record["text_excerpt"],
                        "normalized_text": record["normalized_text"],
                        "source_date": record["source_date"],
                        "harvested_at": timezone.now(),
                        "is_active": True,
                    },
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1
                    DocumentFingerprint.objects.filter(
                        source_type="oai",
                        source_id=oai_record.id,
                    ).delete()

                fingerprints = compute_fingerprints(oai_record.normalized_text)

                DocumentFingerprint.objects.bulk_create(
                    [
                        DocumentFingerprint(
                            hash=fingerprint_hash,
                            source_type="oai",
                            source_id=oai_record.id,
                        )
                        for fingerprint_hash in fingerprints
                    ],
                    ignore_conflicts=True,
                )

        except Exception as exc:
            logger.warning(
                "Error cosechando repositorio OAI. repository=%s error=%s",
                repository.name,
                exc,
            )
            return OaiHarvestSummary(
                repository_name=repository.name,
                created_count=created_count,
                updated_count=updated_count,
                skipped_count=skipped_count,
                error=str(exc),
            )

        logger.info(
            "Harvest OAI completado. repository=%s nuevos=%s actualizados=%s omitidos=%s",
            repository.name,
            created_count,
            updated_count,
            skipped_count,
        )

        return OaiHarvestSummary(
            repository_name=repository.name,
            created_count=created_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
        )

    def _get_with_retry(
        self,
        repository: OaiRepositoryConfig,
        request_params: dict,
    ) -> requests.Response:
        """
        GET con reintentos ante errores transitorios (timeouts, errores de
        conexión, 5xx de proxy/servidor). Los errores 4xx (mal request, set
        inválido, etc.) no se reintentan porque no son transitorios.
        """
        last_exc: Exception | None = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = requests.get(
                    repository.base_url,
                    params=request_params,
                    timeout=self.TIMEOUT_SECONDS,
                    headers=HTTP_HEADERS,
                )

                if response.status_code >= 500:
                    response.raise_for_status()

                return response

            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError,
            ) as exc:
                last_exc = exc

                if attempt == self.MAX_RETRIES:
                    break

                wait_seconds = self.RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "Error transitorio en request OAI, reintentando. "
                    "repository=%s intento=%s/%s espera=%ss error=%s",
                    repository.name,
                    attempt,
                    self.MAX_RETRIES,
                    wait_seconds,
                    exc,
                )
                time.sleep(wait_seconds)

        raise last_exc

    def _iter_records(
        self,
        repository: OaiRepositoryConfig,
    ) -> Iterator[dict | None]:
        initial_params = {
            "verb": "ListRecords",
            "metadataPrefix": "oai_dc",
        }

        if repository.oai_set:
            initial_params["set"] = repository.oai_set

        resumption_token = ""
        page_count = 0

        while True:
            page_count += 1

            if page_count > self.MAX_PAGES:
                logger.warning(
                    "Se alcanzó el máximo de páginas OAI. repository=%s",
                    repository.name,
                )
                return

            request_params = (
                {"verb": "ListRecords", "resumptionToken": resumption_token}
                if resumption_token
                else initial_params
            )

            response = self._get_with_retry(
                repository=repository,
                request_params=request_params,
            )
            response.raise_for_status()

            root = ElementTree.fromstring(response.content)

            error_element = root.find(f"{OAI_NS}error")

            if error_element is not None:
                logger.warning(
                    "Error respondido por OAI-PMH. repository=%s code=%s mensaje=%s",
                    repository.name,
                    error_element.get("code", ""),
                    error_element.text,
                )
                return

            list_records = root.find(f"{OAI_NS}ListRecords")

            if list_records is None:
                return

            for record_element in list_records.findall(f"{OAI_NS}record"):
                yield self._parse_record(record_element=record_element)

            resumption_element = list_records.find(
                f"{OAI_NS}resumptionToken"
            )

            next_token = (
                (resumption_element.text or "").strip()
                if resumption_element is not None
                else ""
            )

            if not next_token:
                return

            time.sleep(self.PAGE_DELAY_SECONDS)
            resumption_token = next_token

    def _parse_record(self, record_element) -> dict | None:
        header = record_element.find(f"{OAI_NS}header")

        if header is None or header.get("status") == "deleted":
            return None

        identifier_element = header.find(f"{OAI_NS}identifier")

        if identifier_element is None:
            return None

        oai_identifier = (identifier_element.text or "").strip()

        if not oai_identifier:
            return None

        metadata_element = record_element.find(f"{OAI_NS}metadata")

        if metadata_element is None:
            return None

        dc_element = next(iter(metadata_element), None)

        if dc_element is None:
            return None

        title = self._first_text(dc_element=dc_element, tag="title")
        description = self._first_text(dc_element=dc_element, tag="description")
        creators = self._all_text(dc_element=dc_element, tag="creator")
        identifiers = self._all_text(dc_element=dc_element, tag="identifier")
        types = self._all_text(dc_element=dc_element, tag="type")
        date = self._first_text(dc_element=dc_element, tag="date")

        if not title:
            return None

        if not self._looks_like_thesis(title=title, description=description, types=types):
            return None

        normalized_text = self._normalize(f"{title} {description}".strip())

        if not normalized_text:
            return None

        return {
            "oai_identifier": oai_identifier,
            "title": title[:500],
            "authors": "; ".join(creators),
            "source_url": self._pick_source_url(identifiers=identifiers),
            "text_excerpt": description or title,
            "normalized_text": normalized_text,
            "source_date": date[:40],
        }

    def _first_text(self, dc_element, tag: str) -> str:
        element = dc_element.find(f"{DC_NS}{tag}")
        return (element.text or "").strip() if element is not None else ""

    def _all_text(self, dc_element, tag: str) -> list[str]:
        return [
            (element.text or "").strip()
            for element in dc_element.findall(f"{DC_NS}{tag}")
            if (element.text or "").strip()
        ]

    def _pick_source_url(self, identifiers: list[str]) -> str:
        for identifier in identifiers:
            if identifier.startswith("http://") or identifier.startswith("https://"):
                return identifier[:200]

        return ""

    def _looks_like_thesis(
        self,
        title: str,
        description: str,
        types: list[str],
    ) -> bool:
        if types:
            return any("thesis" in dc_type.lower() for dc_type in types)

        combined = f"{title} {description}".lower()
        return any(keyword in combined for keyword in THESIS_KEYWORDS)

    def _normalize(self, text: str) -> str:
        text = text.lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(
            char for char in text if not unicodedata.combining(char)
        )
        text = re.sub(r"[^a-z0-9ñü\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
