from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class UserRole(models.TextChoices):
    ADMIN = "ADMIN", _("Administrador")
    TEACHER = "TEACHER", _("Docente / Revisor")
    STUDENT = "STUDENT", _("Alumno")


class User(AbstractUser):
    """
    Usuario personalizado del sistema.

    Roles:
    - ADMIN: administra institución, usuarios, reportes y certificados.
    - TEACHER: sube/revisa documentos de alumnos asignados.
    - STUDENT: sube y consulta sus propios documentos.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    institution = models.ForeignKey(
        "core.Institution",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
        verbose_name=_("Institución"),
    )
    role = models.CharField(
        max_length=20,
        choices=UserRole.choices,
        default=UserRole.STUDENT,
        db_index=True,
        verbose_name=_("Rol"),
    )
    email = models.EmailField(
        unique=True,
        verbose_name=_("Correo electrónico"),
    )
    document_number = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("DNI / Código institucional"),
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("Teléfono"),
    )
    must_change_password = models.BooleanField(
        default=False,
        verbose_name=_("Debe cambiar contraseña"),
    )

    class Meta:
        verbose_name = _("Usuario")
        verbose_name_plural = _("Usuarios")
        indexes = [
            models.Index(fields=["institution", "role"]),
            models.Index(fields=["email"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "document_number"],
                condition=~models.Q(document_number=""),
                name="unique_document_number_per_institution",
            ),
        ]

    def clean(self) -> None:
        super().clean()

        if not self.is_superuser and self.institution_id is None:
            raise ValidationError(
                {
                    "institution": _(
                        "Todo usuario no superadministrador debe pertenecer "
                        "a una institución."
                    )
                }
            )

    @property
    def is_admin_role(self) -> bool:
        return self.role == UserRole.ADMIN or self.is_superuser

    @property
    def is_teacher_role(self) -> bool:
        return self.role == UserRole.TEACHER

    @property
    def is_student_role(self) -> bool:
        return self.role == UserRole.STUDENT

    def __str__(self) -> str:
        full_name = self.get_full_name().strip()
        return full_name or self.username