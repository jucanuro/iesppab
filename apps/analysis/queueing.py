from __future__ import annotations

import logging
from uuid import UUID

from django.utils import timezone

from apps.documents.models import Document, DocumentStatus

logger = logging.getLogger(__name__)

# Estados desde los que se puede (re)encolar un análisis. QUEUED/PROCESSING
# quedan fuera: el documento ya está en análisis.
ANALYZABLE_STATUSES = [
    DocumentStatus.UPLOADED,
    DocumentStatus.COMPLETED,
    DocumentStatus.FAILED,
]


def enqueue_document_analysis(
    document_id: UUID | str,
    requested_by_id: UUID | str,
    exclude_quotes: bool = True,
    exclude_bibliography: bool = True,
) -> bool:
    """
    Pasa el documento a «en cola» y envía la tarea de análisis a Celery.

    Devuelve False si el documento ya estaba en análisis. El paso a QUEUED es
    un UPDATE condicional: solo una petición gana la transición, así un doble
    clic (o dos pestañas) no encola el mismo documento dos veces. Si Celery no
    acepta la tarea, el documento queda FAILED en vez de en cola para siempre.
    """

    from apps.analysis.tasks import run_document_analysis

    queued = Document.objects.filter(
        pk=document_id,
        status__in=ANALYZABLE_STATUSES,
    ).update(
        status=DocumentStatus.QUEUED,
        error_message="",
        updated_at=timezone.now(),
    )

    if queued != 1:
        return False

    try:
        run_document_analysis.delay(
            str(document_id),
            str(requested_by_id),
            exclude_quotes=exclude_quotes,
            exclude_bibliography=exclude_bibliography,
        )
    except Exception:
        Document.objects.filter(
            pk=document_id,
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

    return True
