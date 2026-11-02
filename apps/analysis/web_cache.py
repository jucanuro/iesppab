"""
Caches persistentes de la búsqueda web: respuestas de Brave por consulta y
páginas leídas por URL (éxito o fallo).

La caché nunca debe tumbar un análisis: si la base de datos falla al leer
o escribir, se registra y se sigue como si no hubiera caché.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.analysis.models import WebPageCacheEntry, WebSearchCacheEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedPage:
    is_success: bool
    title: str
    text: str
    source_format: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def search_cache_key(
    query: str,
    count: int,
    country: str,
    search_lang: str,
    extra_snippets: bool = False,
) -> str:
    # Sin extra_snippets la clave es la de siempre: la caché vieja sigue sirviendo.
    parts: list = [query, count, country, search_lang]

    if extra_snippets:
        parts.append("extra_snippets")

    return _sha256(json.dumps(parts, ensure_ascii=False))


def get_search_results(cache_key: str) -> list[dict] | None:
    if getattr(settings, "WEB_SEARCH_CACHE_DAYS", 0) <= 0:
        return None

    try:
        entry = WebSearchCacheEntry.objects.filter(
            cache_key=cache_key,
            expires_at__gt=timezone.now(),
        ).first()
    except Exception as exc:
        logger.warning("No se pudo leer la caché de búsqueda web. error=%s", exc)
        return None

    return None if entry is None else list(entry.results)


def store_search_results(cache_key: str, query: str, results: list[dict]) -> None:
    days = getattr(settings, "WEB_SEARCH_CACHE_DAYS", 0)

    if days <= 0:
        return

    try:
        WebSearchCacheEntry.objects.update_or_create(
            cache_key=cache_key,
            defaults={
                "query": query,
                "results": results,
                "expires_at": timezone.now() + timedelta(days=days),
            },
        )
    except Exception as exc:
        logger.warning("No se pudo guardar la caché de búsqueda web. error=%s", exc)


def _page_cache_enabled() -> bool:
    return getattr(settings, "WEB_PAGE_CACHE_DAYS", 0) > 0


def get_page(url: str) -> CachedPage | None:
    if not _page_cache_enabled():
        return None

    try:
        entry = WebPageCacheEntry.objects.filter(
            url_hash=_sha256(url),
            expires_at__gt=timezone.now(),
        ).first()
    except Exception as exc:
        logger.warning("No se pudo leer la caché de páginas web. error=%s", exc)
        return None

    if entry is None:
        return None

    return CachedPage(
        is_success=entry.is_success,
        title=entry.title,
        text=entry.text,
        source_format=entry.source_format,
    )


def store_page(
    url: str,
    *,
    title: str = "",
    text: str = "",
    source_format: str = "",
    failure_reason: str = "",
) -> None:
    """
    Guarda la lectura de `url`. Sin `failure_reason` es un éxito (vigencia
    WEB_PAGE_CACHE_DAYS); con él, un fallo (WEB_PAGE_FAILURE_CACHE_HOURS).
    """
    if not _page_cache_enabled():
        return

    is_success = not failure_reason

    if is_success:
        ttl = timedelta(days=settings.WEB_PAGE_CACHE_DAYS)
    else:
        hours = getattr(settings, "WEB_PAGE_FAILURE_CACHE_HOURS", 0)

        if hours <= 0:
            return

        ttl = timedelta(hours=hours)

    try:
        WebPageCacheEntry.objects.update_or_create(
            url_hash=_sha256(url),
            defaults={
                "url": url,
                "is_success": is_success,
                "title": title,
                "text": text,
                "source_format": source_format,
                "failure_reason": failure_reason[:255],
                "expires_at": timezone.now() + ttl,
            },
        )
    except Exception as exc:
        logger.warning("No se pudo guardar la caché de páginas web. error=%s", exc)


def purge_expired() -> tuple[int, int]:
    """Borra las entradas vencidas: (búsquedas, páginas)."""
    now = timezone.now()
    searches, _ = WebSearchCacheEntry.objects.filter(expires_at__lte=now).delete()
    pages, _ = WebPageCacheEntry.objects.filter(expires_at__lte=now).delete()
    return searches, pages
