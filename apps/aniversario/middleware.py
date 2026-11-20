"""Enruta el subdominio del aniversario a sus propias URLs.

Para cualquier otro host no hace nada, así originalidad sigue igual.
"""
from __future__ import annotations

from django.conf import settings
from django.utils.cache import patch_vary_headers

URLCONF_PUBLICA = "apps.aniversario.urls_publicas"


def es_host_aniversario(request) -> bool:
    host = (getattr(settings, "ANIVERSARIO_HOST", "") or "").strip().lower()
    if not host:
        return False
    try:
        actual = request.get_host()
    except Exception:  # DisallowedHost: lo maneja Django más adelante.
        return False
    return actual.split(":")[0].lower() == host


class HostUrlconfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        es_aniv = es_host_aniversario(request)
        request.es_aniversario = es_aniv
        if es_aniv:
            request.urlconf = URLCONF_PUBLICA
        response = self.get_response(request)
        if es_aniv:
            # El global "same-origin" hace que YouTube rechace el video
            # embebido (error 153). Solo se relaja en este host.
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            patch_vary_headers(response, ("Host",))
        return response
