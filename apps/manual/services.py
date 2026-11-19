from __future__ import annotations

import logging
from dataclasses import dataclass

from django.template.loader import render_to_string
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.core.models import Institution

logger = logging.getLogger(__name__)

MANUAL_VERSION = "1.0"


class ManualGenerationError(Exception):
    """Error controlado al generar el PDF del manual."""


@dataclass(frozen=True)
class ManualSection:
    key: str
    title: str
    template: str


# Orden en que aparecen cuando se muestran varias secciones.
SECTIONS: tuple[ManualSection, ...] = (
    ManualSection("estudiante", "estudiantes", "manual/_estudiante.html"),
    ManualSection("docente", "docentes", "manual/_docente.html"),
    ManualSection("director", "directores", "manual/_director.html"),
    ManualSection("administrador", "administradores", "manual/_administrador.html"),
)

_ROLE_TO_KEY = {
    UserRole.STUDENT: "estudiante",
    UserRole.TEACHER: "docente",
    UserRole.DIRECTOR: "director",
    UserRole.ADMIN: "administrador",
}


class ManualService:
    """Decide qué secciones ve cada usuario y genera el PDF del manual."""

    def sections_for_user(self, user) -> list[ManualSection]:
        if user.is_superuser or user.is_admin_role:
            return list(SECTIONS)
        key = _ROLE_TO_KEY.get(user.role)
        return [s for s in SECTIONS if s.key == key]

    def build_context(self, user) -> dict:
        institution = (
            user.institution
            if getattr(user, "institution_id", None)
            else Institution.objects.filter(is_active=True).first()
        )
        return {
            "sections": self.sections_for_user(user),
            "institution_name": (
                institution.name if institution else 'IESPP "Alfonso Barrantes Lingán"'
            ),
            "manual_version": MANUAL_VERSION,
            "generated_on": timezone.localdate(),
        }

    def render_pdf(self, user) -> bytes:
        # Import diferido: WeasyPrint carga librerías del sistema (pango).
        try:
            from weasyprint import HTML

            html = render_to_string("manual/pdf.html", self.build_context(user))
            return HTML(string=html).write_pdf()
        except Exception as exc:
            logger.exception("No se pudo generar el PDF del manual.")
            raise ManualGenerationError(
                "No se pudo generar el PDF del manual. Inténtalo de nuevo más tarde."
            ) from exc
