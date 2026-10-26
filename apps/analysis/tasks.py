from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.analysis.services import DocumentAnalysisError, DocumentAnalysisService

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="apps.analysis.tasks.run_document_analysis",
    soft_time_limit=settings.ANALYSIS_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.ANALYSIS_TASK_TIME_LIMIT,
    # Si el worker muere a mitad del análisis (OOM, deploy), la tarea vuelve
    # a la cola en vez de perderse. Es seguro porque execute() reutiliza el
    # AnalysisJob abierto y reemplaza reporte/hallazgos en vez de duplicarlos,
    # y corta tras MAX_ANALYSIS_DELIVERIES entregas para no entrar en bucle.
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_document_analysis(
    self,
    document_id: str,
    requested_by_id: str,
    exclude_quotes: bool = True,
    exclude_bibliography: bool = True,
) -> None:
    """
    Ejecuta el pipeline de análisis de un documento en background.

    Recibe IDs (str) en vez de instancias porque los argumentos de una
    tarea Celery se serializan a JSON. Las opciones de exclusión tienen
    default para que las tareas ya encoladas con la firma anterior sigan
    funcionando.
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
        service.execute(
            document_id=document_id,
            celery_task_id=self.request.id or "",
            exclude_quotes=exclude_quotes,
            exclude_bibliography=exclude_bibliography,
        )

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
