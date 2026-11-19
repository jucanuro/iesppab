from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.core.models import Institution

logger = logging.getLogger(__name__)

MANUAL_VERSION = "2.0"

# Clave → (archivo en static/capturas/, pie de figura).
CAPTURAS: dict[str, tuple[str, str]] = {
    "login": ("01-login.png", "Pantalla de inicio de sesión: usuario o correo institucional y contraseña."),
    "bandeja": ("02-bandeja.png", "Bandeja de documentos con búsqueda, filtros, estado de cada trabajo y botón «Subir documento»."),
    "subir": ("03-subir.png", "Formulario «Subir documento»: título, tipo, alumno, curso, periodo y archivo PDF o DOCX (hasta 25 MB)."),
    "acciones": ("04-acciones.png", "Menú de acciones («…») de cada documento: reanalizar, informes, generar certificado y eliminar."),
    "reporte_resumen": ("05-reporte-resumen.png", "Pestaña Resumen del reporte: valoración global, anillos de similitud e IA estimada y opciones de exclusión."),
    "reporte_fuentes": ("06-reporte-fuentes.png", "Pestaña Fuentes: cada fuente detectada con su porcentaje de coincidencia."),
    "reporte_ia": ("07-reporte-ia.png", "Pestaña IA: estimación orientativa por segmentos del texto."),
    "admin": ("08-admin.png", "Panel de administración: usuarios, instituciones, documentos, reportes y certificados."),
}


class ManualGenerationError(Exception):
    """Error controlado al generar el PDF del manual."""


@dataclass(frozen=True)
class ManualSection:
    key: str
    title: str
    template: str
    # (sufijo del id del h3, título) en el orden en que aparecen en la plantilla.
    items: tuple[tuple[str, str], ...] = ()


_PASO_INGRESAR = ("sistema", "Ingresar al sistema")
_PASO_SUBIR = ("subir", "Cómo subir un documento")
_PASO_ACCIONES = ("acciones", "Acciones sobre un documento")
_PASO_REPORTE = ("reporte", "Cómo leer el reporte")
_PASO_CERT = ("certificado", "Cómo generar y verificar el certificado")
_PASO_LIMITES = ("limitaciones", "Limitaciones importantes")


def _numerar(*items: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple((k, f"{i}. {t}") for i, (k, t) in enumerate(items, start=1))


# Orden en que aparecen cuando se muestran varias secciones.
SECTIONS: tuple[ManualSection, ...] = (
    ManualSection(
        "estudiante", "estudiantes", "manual/_estudiante.html",
        _numerar(_PASO_INGRESAR, _PASO_SUBIR, _PASO_REPORTE, _PASO_CERT, _PASO_LIMITES),
    ),
    ManualSection(
        "docente", "docentes", "manual/_docente.html",
        _numerar(_PASO_INGRESAR, _PASO_SUBIR, _PASO_ACCIONES, _PASO_REPORTE, _PASO_CERT, _PASO_LIMITES),
    ),
    ManualSection(
        "director", "directores", "manual/_director.html",
        _numerar(_PASO_INGRESAR, _PASO_SUBIR, _PASO_ACCIONES, _PASO_REPORTE, _PASO_CERT, _PASO_LIMITES),
    ),
    ManualSection(
        "administrador", "administradores", "manual/_administrador.html",
        _numerar(
            _PASO_INGRESAR, _PASO_SUBIR, _PASO_ACCIONES, _PASO_REPORTE, _PASO_CERT,
            ("panel", "Panel de administración"), _PASO_LIMITES,
        ),
    ),
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

    def build_shots(self, *, for_pdf: bool) -> dict:
        """
        Capturas del manual. Si falta un archivo, `available` es False y la
        plantilla muestra «Captura no disponible» en vez de fallar. Para el PDF
        se usa la ruta absoluta (file://) para que WeasyPrint cargue la imagen.
        """
        shots: dict[str, dict] = {}
        for key, (filename, caption) in CAPTURAS.items():
            relative = f"capturas/{filename}"
            found = finders.find(relative)
            path = Path(found) if isinstance(found, str) else None
            available = bool(path and path.is_file())
            if available:
                src = path.resolve().as_uri() if for_pdf else static(relative)
            else:
                src = ""
            shots[key] = {"available": available, "src": src, "caption": caption}
        return shots

    def build_context(self, user, *, for_pdf: bool = False) -> dict:
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
            "shots": self.build_shots(for_pdf=for_pdf),
            "logo_src": self._logo_src(for_pdf),
            "for_pdf": for_pdf,
        }

    @staticmethod
    def _logo_src(for_pdf: bool) -> str:
        found = finders.find("img/logo-iesppabl.png")
        if for_pdf:
            return Path(found).resolve().as_uri() if isinstance(found, str) else ""
        return static("img/logo-iesppabl.png")

    def render_pdf(self, user) -> bytes:
        # Import diferido: WeasyPrint carga librerías del sistema (pango).
        try:
            from weasyprint import HTML

            html = render_to_string(
                "manual/pdf.html", self.build_context(user, for_pdf=True)
            )
            return HTML(string=html, base_url=str(settings.BASE_DIR)).write_pdf()
        except Exception as exc:
            logger.exception("No se pudo generar el PDF del manual.")
            raise ManualGenerationError(
                "No se pudo generar el PDF del manual. Inténtalo de nuevo más tarde."
            ) from exc
