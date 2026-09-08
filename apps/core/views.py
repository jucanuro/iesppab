from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.generic import TemplateView

from apps.core.models import Institution


def csrf_failure(
    request: HttpRequest,
    reason: str = "",
    *args: Any,
    **kwargs: Any,
) -> HttpResponse:
    """
    Vista de fallo de CSRF (configurada en `CSRF_FAILURE_VIEW`). En vez de
    devolver el 403 crudo de Django, muestra la página de "sesión expirada"
    con un enlace para volver a iniciar sesión. El caso típico es un
    formulario enviado tras caducar la sesión por inactividad.
    """
    return render(
        request,
        "403.html",
        {"is_csrf": True, "reason": reason},
        status=403,
    )


class _InstitutionContextMixin:
    """Expone la institución activa a las plantillas de páginas estáticas."""

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        institution = Institution.objects.filter(is_active=True).first()

        context["institution"] = institution
        context["institution_name"] = (
            institution.name
            if institution
            else 'IESPP "Alfonso Barrantes Lingán"'
        )
        context["last_updated"] = timezone.datetime(2026, 9, 8).date()

        return context


class PrivacyPolicyView(_InstitutionContextMixin, TemplateView):
    """Política de privacidad y tratamiento de datos personales."""

    template_name = "core/privacy.html"


class HelpCenterView(_InstitutionContextMixin, TemplateView):
    """Centro de ayuda: guía de uso de la plataforma y preguntas frecuentes."""

    template_name = "core/help.html"
