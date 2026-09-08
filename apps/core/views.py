from __future__ import annotations

from typing import Any

from django.utils import timezone
from django.views.generic import TemplateView

from apps.core.models import Institution


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
