from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.documents.validators import (
    ALLOWED_DOCUMENT_EXTENSIONS,
    validate_document_mime_type,
    validate_document_size,
)

if TYPE_CHECKING:
    from apps.accounts.models import User


def document_upload_path(instance: "Document", filename: str) -> str:
    """
    Genera una ruta privada y no predecible para documentos académicos.
    No conserva el nombre original como nombre físico del archivo.
    """

    extension = filename.rsplit(".", 1)[-1].lower()
    institution_id = instance.institution_id or "pending"
    current_date = timezone.now().strftime("%Y/%m")
    secure_name = f"{uuid.uuid4()}.{extension}"

    return f"private/documents/{institution_id}/{current_date}/{secure_name}"


class DocumentStatus(models.TextChoices):
    UPLOADED = "UPLOADED", _("Subido")
    QUEUED = "QUEUED", _("En cola")
    PROCESSING = "PROCESSING", _("Procesando")
    COMPLETED = "COMPLETED", _("Completado")
    FAILED = "FAILED", _("Fallido")


class DocumentKind(models.TextChoices):
    THESIS = "THESIS", _("Tesis")
    MONOGRAPH = "MONOGRAPH", _("Monografía")
    ESSAY = "ESSAY", _("Ensayo")
    RESEARCH_ARTICLE = "RESEARCH_ARTICLE", _("Artículo de investigación")
    OTHER = "OTHER", _("Otro")


class Document(TimeStampedModel):
    """
    Documento académico subido para análisis de originalidad e IA.
    """

    institution = models.ForeignKey(
        "core.Institution",
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name=_("Institución"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_documents",
        verbose_name=_("Alumno / propietario"),
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_documents",
        verbose_name=_("Subido por"),
    )
    title = models.CharField(
        max_length=250,
        db_index=True,
        verbose_name=_("Título"),
    )
    kind = models.CharField(
        max_length=30,
        choices=DocumentKind.choices,
        default=DocumentKind.OTHER,
        db_index=True,
        verbose_name=_("Tipo de documento"),
    )
    course_name = models.CharField(
        max_length=180,
        blank=True,
        verbose_name=_("Curso / Unidad didáctica"),
    )
    academic_period = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Periodo académico"),
    )
    original_file = models.FileField(
        upload_to=document_upload_path,
        validators=[
            FileExtensionValidator(
                allowed_extensions=ALLOWED_DOCUMENT_EXTENSIONS,
            ),
            validate_document_size,
            validate_document_mime_type,
        ],
        verbose_name=_("Archivo original"),
    )
    original_filename = models.CharField(
        max_length=255,
        verbose_name=_("Nombre original del archivo"),
    )
    mime_type = models.CharField(
        max_length=120,
        verbose_name=_("MIME type detectado"),
    )
    file_size_bytes = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name=_("Tamaño en bytes"),
    )
    sha256_hash = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name=_("Hash SHA-256"),
    )
    language = models.CharField(
        max_length=10,
        default="es",
        db_index=True,
        verbose_name=_("Idioma"),
    )
    status = models.CharField(
        max_length=20,
        choices=DocumentStatus.choices,
        default=DocumentStatus.UPLOADED,
        db_index=True,
        verbose_name=_("Estado"),
    )
    error_message = models.TextField(
        blank=True,
        verbose_name=_("Mensaje de error"),
    )

    class Meta:
        verbose_name = _("Documento")
        verbose_name_plural = _("Documentos")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["institution", "created_at"]),
            models.Index(fields=["owner", "created_at"]),
            models.Index(fields=["sha256_hash"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(file_size_bytes__gte=1),
                name="document_file_size_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class DocumentText(TimeStampedModel):
    """
    Texto extraído del PDF/DOCX.

    Se separa del modelo Document para evitar cargar campos pesados
    en listados, dashboards y consultas frecuentes.
    """

    document = models.OneToOneField(
        Document,
        on_delete=models.CASCADE,
        related_name="extracted_text",
        verbose_name=_("Documento"),
    )
    content = models.TextField(
        verbose_name=_("Texto extraído"),
    )
    word_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Cantidad de palabras"),
    )
    character_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Cantidad de caracteres"),
    )
    extraction_engine = models.CharField(
        max_length=80,
        blank=True,
        verbose_name=_("Motor de extracción"),
    )
    extracted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Extraído el"),
    )

    class Meta:
        verbose_name = _("Texto extraído")
        verbose_name_plural = _("Textos extraídos")
        indexes = [
            models.Index(fields=["document"]),
            models.Index(fields=["word_count"]),
        ]

    def __str__(self) -> str:
        return f"Texto extraído - {self.document_id}"