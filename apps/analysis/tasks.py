from __future__ import annotations

import logging

from celery import shared_task
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.analysis.services import DocumentAnalysisError, DocumentAnalysisService

logger = logging.getLogger(__name__)


@shared_task(name="apps.analysis.tasks.run_document_analysis")
def run_document_analysis(document_id: str, requested_by_id: str) -> None:
    """
    Ejecuta el pipeline de análisis de un documento en background.

    Recibe IDs (str) en vez de instancias porque los argumentos de una
    tarea Celery se serializan a JSON.
    """

    try:
        requested_by = User.objects.get(pk=requested_by_id)
    except User.DoesNotExist:
        logger.error(
            "Usuario solicitante no encontrado al analizar documento. "
            "user_id=%s document_id=%s",
            requested_by_id,
            document_id,
        )
        return

    try:
        service = DocumentAnalysisService(requested_by=requested_by)
        service.execute(document_id=document_id)

    except PermissionDenied:
        logger.warning(
            "Permiso denegado al analizar documento en background. "
            "user_id=%s document_id=%s",
            requested_by_id,
            document_id,
            exc_info=True,
        )

    except DocumentAnalysisError:
        logger.warning(
            "Error de análisis al procesar documento en background. "
            "user_id=%s document_id=%s",
            requested_by_id,
            document_id,
            exc_info=True,
        )

    except Exception:
        logger.exception(
            "Error inesperado al analizar documento en background. "
            "user_id=%s document_id=%s",
            requested_by_id,
            document_id,
        )
