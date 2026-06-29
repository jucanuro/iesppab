from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


def certificate_upload_path(instance: "Certificate", filename: str) -> str:
    report_id = instance.report_id or "pending"
    secure_name = f"{uuid.uuid4()}.pdf"
    return f"private/certificates/{report_id}/{secure_name}"


class Certificate(TimeStampedModel):
    """
    Certificado institucional oficial generado desde un reporte final.
    """

    report = models.OneToOneField(
        "reports.AnalysisReport",
        on_delete=models.PROTECT,
        related_name="certificate",
        verbose_name=_("Reporte"),
    )
    code = models.CharField(
        max_length=40,
        unique=True,
        db_index=True,
        verbose_name=_("Código de certificado"),
    )
    pdf_file = models.FileField(
        upload_to=certificate_upload_path,
        blank=True,
        null=True,
        verbose_name=_("PDF del certificado"),
    )
    verification_hash = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        verbose_name=_("Hash de verificación"),
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_certificates",
        verbose_name=_("Emitido por"),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Activo"),
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Revocado el"),
    )
    revoke_reason = models.TextField(
        blank=True,
        verbose_name=_("Motivo de revocación"),
    )

    class Meta:
        verbose_name = _("Certificado")
        verbose_name_plural = _("Certificados")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["code"]),
            models.Index(fields=["verification_hash"]),
            models.Index(fields=["is_active", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.code