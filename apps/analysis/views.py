from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View

from apps.analysis.services import DocumentAnalysisError, DocumentAnalysisService
from apps.analysis.tasks import run_document_analysis
from apps.documents.models import Document, DocumentStatus

# Estados desde los que se puede (re)encolar un análisis. QUEUED/PROCESSING
# quedan fuera: el documento ya está en análisis.
_ANALYZABLE_STATUSES = [
    DocumentStatus.UPLOADED,
    DocumentStatus.COMPLETED,
    DocumentStatus.FAILED,
]

logger = logging.getLogger(__name__)


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

            # Pasa a "en cola" con un UPDATE condicional: solo una petición
            # puede ganar la transición, así un doble clic (o dos pestañas)
            # no encola el mismo documento dos veces.
            queued = Document.objects.filter(
                pk=document.pk,
                status__in=_ANALYZABLE_STATUSES,
            ).update(
                status=DocumentStatus.QUEUED,
                error_message="",
                updated_at=timezone.now(),
            )

            if queued != 1:
                messages.info(
                    request,
                    (
                        "Este documento ya está en análisis, "
                        "el reporte estará listo en unos momentos."
                    ),
                )

                return redirect("reports:detail", pk=pk)

            try:
                run_document_analysis.delay(str(pk), str(request.user.id))
            except Exception:
                # Sin esto el documento quedaría en QUEUED para siempre.
                Document.objects.filter(
                    pk=document.pk,
                    status=DocumentStatus.QUEUED,
                ).update(
                    status=DocumentStatus.FAILED,
                    error_message=(
                        "No se pudo enviar el documento a análisis. "
                        "Vuelve a intentarlo en unos minutos."
                    ),
                    updated_at=timezone.now(),
                )
                raise

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

        except DocumentAnalysisError as exc:
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