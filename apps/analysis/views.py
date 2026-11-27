from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.views import View

from apps.analysis.queueing import enqueue_document_analysis
from apps.analysis.services import DocumentAnalysisError, DocumentAnalysisService
from apps.documents.exceptions import AuthorizationError
from apps.documents.services import ensure_analysis_authorized

logger = logging.getLogger(__name__)


def _read_exclusion_flag(request: HttpRequest, name: str) -> bool:
    """
    Las opciones de exclusión llegan como checkbox junto a un campo oculto
    `exclusion_options` que indica que el formulario las ofrecía. Un
    formulario sin esas opciones (botón «Reanalizar» de la lista) usa el
    default: excluir.
    """
    if "exclusion_options" not in request.POST:
        return True

    return request.POST.get(name) == "on"


class DocumentAnalyzeView(LoginRequiredMixin, View):
    """
    Encola el análisis del documento para que corra en background (Celery),
    sin bloquear la petición HTTP.
    """

    def post(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        try:
            # Valida permisos de forma síncrona (rápido) antes de encolar,
            # para poder devolver un 403 inmediato si el usuario no puede
            # analizar este documento.
            document = DocumentAnalysisService(
                requested_by=request.user,
            ).check_permission(document_id=pk)

            # Un documento que espera (o no obtuvo) la autorización del
            # asesor no se analiza: se aprueba desde «Solicitudes».
            ensure_analysis_authorized(document)

            queued = enqueue_document_analysis(
                document.pk,
                request.user.id,
                exclude_quotes=_read_exclusion_flag(request, "exclude_quotes"),
                exclude_bibliography=_read_exclusion_flag(
                    request,
                    "exclude_bibliography",
                ),
            )

            if not queued:
                messages.info(
                    request,
                    (
                        "Este documento ya está en análisis, "
                        "el reporte estará listo en unos momentos."
                    ),
                )

                return redirect("reports:detail", pk=pk)

            messages.success(
                request,
                (
                    "Tu documento fue enviado a análisis, el reporte "
                    "estará listo en unos momentos."
                ),
            )

            return redirect("reports:detail", pk=pk)

        except PermissionDenied as exc:
            logger.warning(
                "Permiso denegado al analizar documento. user_id=%s doc_id=%s",
                request.user.id,
                pk,
                exc_info=True,
            )
            return HttpResponseForbidden(str(exc))

        except (DocumentAnalysisError, AuthorizationError) as exc:
            messages.error(request, str(exc))
            return redirect("documents:upload")

        except Exception:
            logger.exception(
                "Error inesperado en DocumentAnalyzeView. user_id=%s doc_id=%s",
                request.user.id,
                pk,
            )
            messages.error(
                request,
                "Ocurrió un error inesperado durante el análisis.",
            )
            return redirect("documents:upload")