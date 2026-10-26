from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.analysis.models import AnalysisJob, AnalysisJobStatus
from apps.documents.models import DocumentStatus

DEFAULT_OLDER_THAN_MINUTES = 60

STALE_JOB_ERROR_CODE = "STALE_WORKER_LOST"
STALE_JOB_MESSAGE = (
    "El análisis se interrumpió por falta de recursos del servidor. "
    "Por favor, vuelve a intentarlo."
)

# Un AnalysisJob solo existe en PENDING/QUEUED por microsegundos dentro de
# `DocumentAnalysisService.execute` antes de pasar a RUNNING (no hay espera
# real en cola a nivel de fila de BD, eso lo maneja el broker de Celery). Un
# job normal tarda ~2 minutos en cualquiera de sus pasos, así que cualquier
# fila en uno de estos tres estados con más de `older_than_minutes` de
# antigüedad solo puede explicarse por un worker que murió a mitad de tarea
# (OOM-kill, kill -9, caída del servidor) y nunca actualizó su propio estado.
STALE_STATUSES = [
    AnalysisJobStatus.PENDING,
    AnalysisJobStatus.QUEUED,
    AnalysisJobStatus.RUNNING,
]

# Solo estos estados de Document los puso el propio pipeline de análisis
# asociado al job huérfano (ver `_mark_as_processing` en services.py) y por
# lo tanto es seguro pisarlos. Si el documento ya está en otro estado, un
# intento más nuevo lo cambió y no debemos tocarlo.
DOCUMENT_IN_PROGRESS_STATUSES = [
    DocumentStatus.QUEUED,
    DocumentStatus.PROCESSING,
]


class Command(BaseCommand):
    help = (
        "Marca como FAILED los AnalysisJob (y su Document asociado) que "
        "quedaron huérfanos en PENDING/QUEUED/RUNNING porque el worker que "
        "los procesaba murió (OOM-kill, caída del servidor, etc.) sin "
        "poder actualizar su propio estado. Re-ejecutable sin efectos "
        "secundarios sobre jobs ya resueltos."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--older-than-minutes",
            type=int,
            default=DEFAULT_OLDER_THAN_MINUTES,
            help=(
                "Antigüedad mínima (en minutos) para considerar un job "
                f"huérfano. Por defecto {DEFAULT_OLDER_THAN_MINUTES}."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo muestra qué se marcaría, sin escribir en la base de datos.",
        )

    def handle(self, *args, **options) -> None:
        older_than_minutes: int = options["older_than_minutes"]
        dry_run: bool = options["dry_run"]

        cutoff = timezone.now() - timedelta(minutes=older_than_minutes)

        stale_jobs = (
            AnalysisJob.objects.select_related("document")
            .filter(status__in=STALE_STATUSES, created_at__lt=cutoff)
            .order_by("created_at")
        )

        if not stale_jobs.exists():
            self.stdout.write(
                self.style.SUCCESS(
                    "No hay jobs huérfanos (PENDING/QUEUED/RUNNING) de más "
                    f"de {older_than_minutes} minutos."
                )
            )
            return

        swept = 0

        for job in stale_jobs:
            document = job.document
            previous_job_status = job.status
            previous_document_status = document.status
            will_update_document = previous_document_status in DOCUMENT_IN_PROGRESS_STATUSES

            self.stdout.write(
                f"job={job.id} document={document.id} "
                f"estado_previo={previous_job_status} "
                f"paso={job.current_step!r} "
                f"creado={job.created_at.isoformat()} "
                f"documento_estado_previo={previous_document_status} "
                f"documento_se_actualiza={will_update_document}"
            )

            if dry_run:
                continue

            with transaction.atomic():
                locked_job = (
                    AnalysisJob.objects.select_for_update()
                    .select_related("document")
                    .get(pk=job.pk)
                )

                # Re-chequea bajo el lock: si un worker real terminó el job
                # justo entre el SELECT de arriba y este UPDATE, no lo pisamos.
                if locked_job.status not in STALE_STATUSES:
                    self.stdout.write(
                        f"  -> omitido, ya no está huérfano "
                        f"(estado actual={locked_job.status})"
                    )
                    continue

                locked_job.status = AnalysisJobStatus.FAILED
                locked_job.finished_at = timezone.now()
                locked_job.current_step = "Error durante el análisis"
                locked_job.error_code = STALE_JOB_ERROR_CODE
                locked_job.error_message = STALE_JOB_MESSAGE
                locked_job.save(
                    update_fields=[
                        "status",
                        "finished_at",
                        "current_step",
                        "error_code",
                        "error_message",
                        "updated_at",
                    ]
                )

                locked_document = locked_job.document

                if locked_document.status in DOCUMENT_IN_PROGRESS_STATUSES:
                    locked_document.status = DocumentStatus.FAILED
                    locked_document.error_message = STALE_JOB_MESSAGE
                    locked_document.save(
                        update_fields=[
                            "status",
                            "error_message",
                            "updated_at",
                        ]
                    )

            swept += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[dry-run] {stale_jobs.count()} job(s) huérfano(s) "
                    "detectado(s), no se modificó nada."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"{swept} job(s) huérfano(s) marcado(s) como FAILED.")
            )
