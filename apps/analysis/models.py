from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class AnalysisType(models.TextChoices):
    FULL = "FULL", _("Similitud + IA")
    SIMILARITY = "SIMILARITY", _("Solo similitud")
    AI_DETECTION = "AI_DETECTION", _("Solo detección IA")


class AnalysisJobStatus(models.TextChoices):
    PENDING = "PENDING", _("Pendiente")
    QUEUED = "QUEUED", _("En cola")
    RUNNING = "RUNNING", _("Ejecutando")
    SUCCESS = "SUCCESS", _("Exitoso")
    FAILED = "FAILED", _("Fallido")
    CANCELLED = "CANCELLED", _("Cancelado")


class AnalysisJob(TimeStampedModel):
    """
    Representa una ejecución asíncrona de análisis.
    Se integra con Celery mediante celery_task_id.
    """

    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.CASCADE,
        related_name="analysis_jobs",
        verbose_name=_("Documento"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_analysis_jobs",
        verbose_name=_("Solicitado por"),
    )
    analysis_type = models.CharField(
        max_length=30,
        choices=AnalysisType.choices,
        default=AnalysisType.FULL,
        db_index=True,
        verbose_name=_("Tipo de análisis"),
    )
    status = models.CharField(
        max_length=20,
        choices=AnalysisJobStatus.choices,
        default=AnalysisJobStatus.PENDING,
        db_index=True,
        verbose_name=_("Estado"),
    )
    attempt_number = models.PositiveSmallIntegerField(
        default=1,
        verbose_name=_("Número de intento"),
    )
    celery_task_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name=_("ID de tarea Celery"),
    )
    current_step = models.CharField(
        max_length=120,
        blank=True,
        verbose_name=_("Paso actual"),
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Iniciado el"),
    )
    finished_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Finalizado el"),
    )
    error_code = models.CharField(
        max_length=80,
        blank=True,
        verbose_name=_("Código de error"),
    )
    error_message = models.TextField(
        blank=True,
        verbose_name=_("Mensaje de error"),
    )

    class Meta:
        verbose_name = _("Trabajo de análisis")
        verbose_name_plural = _("Trabajos de análisis")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["document", "status"]),
            models.Index(fields=["requested_by", "created_at"]),
            models.Index(fields=["celery_task_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "analysis_type", "attempt_number"],
                name="unique_analysis_attempt_per_document",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id} - {self.analysis_type} - {self.status}"
    

class DocumentKnowledgeChunk(TimeStampedModel):
    """
    Fragmento interno aprendido desde documentos analizados.

    Este modelo permite que el sistema construya una base interna de
    conocimiento académico y mejore la detección de similitud con cada
    documento subido/procesado.
    """

    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.CASCADE,
        related_name="knowledge_chunks",
        verbose_name=_("Documento"),
    )
    text_excerpt = models.TextField(
        verbose_name=_("Fragmento original"),
    )
    normalized_text = models.TextField(
        verbose_name=_("Fragmento normalizado"),
    )
    start_offset = models.PositiveIntegerField(
        verbose_name=_("Inicio en texto"),
    )
    end_offset = models.PositiveIntegerField(
        verbose_name=_("Fin en texto"),
    )
    word_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Cantidad de palabras"),
    )
    content_hash = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name=_("Hash del fragmento"),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Activo"),
    )

    class Meta:
        verbose_name = _("Fragmento aprendido")
        verbose_name_plural = _("Fragmentos aprendidos")
        indexes = [
            models.Index(fields=["document", "is_active"]),
            models.Index(fields=["content_hash"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.document_id} - {self.word_count} palabras"