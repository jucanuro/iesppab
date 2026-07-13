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

from apps.analysis.services import (
    DocumentAnalysisError,
    DocumentAnalysisService,
)

logger = logging.getLogger(__name__)


class DocumentAnalyzeView(LoginRequiredMixin, View):
    """
    Ejecuta el análisis del documento.

    Por ahora es síncrono.
    Luego lo pasaremos a Celery.
    """

    def post(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        try:
            service = DocumentAnalysisService(requested_by=request.user)
            report = service.execute(document_id=pk)

            messages.success(
                request,
                (
                    "Análisis completado. "
                    f"Similitud: {report.similarity_percent}% | "
                    f"IA: {report.ai_probability_percent}%"
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