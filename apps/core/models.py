from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    """
    Modelo abstracto reutilizable para auditoría temporal.
    Todas las entidades principales heredarán de aquí.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name=_("Creado el"),
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Actualizado el"),
    )

    class Meta:
        abstract = True


class Institution(TimeStampedModel):
    """
    Representa al instituto propietario del sistema.
    Se deja preparado para multi-institución sin romper el MVP.
    """

    name = models.CharField(
        max_length=180,
        unique=True,
        verbose_name=_("Nombre institucional"),
    )
    slug = models.SlugField(
        max_length=120,
        unique=True,
        verbose_name=_("Identificador URL"),
    )
    ruc = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("RUC"),
    )
    logo = models.ImageField(
        upload_to="private/institutions/logos/",
        blank=True,
        null=True,
        verbose_name=_("Logo institucional"),
    )
    certificate_signature_name = models.CharField(
        max_length=150,
        blank=True,
        verbose_name=_("Nombre de firmante"),
    )
    certificate_signature_position = models.CharField(
        max_length=150,
        blank=True,
        verbose_name=_("Cargo de firmante"),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Activo"),
    )

    class Meta:
        verbose_name = _("Institución")
        verbose_name_plural = _("Instituciones")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name