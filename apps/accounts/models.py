from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class UserRole(models.TextChoices):
    ADMIN = "ADMIN", _("Administrador")
    DIRECTOR = "DIRECTOR", _("Director")
    TEACHER = "TEACHER", _("Docente / Revisor")
    STUDENT = "STUDENT", _("Alumno")


class StudentStatus(models.TextChoices):
    """
    Condición de un usuario STUDENT. Solo tiene efecto en ese rol.

    - ENROLLED: alumno en formación; cada envío requiere que su asesor lo
      autorice.
    - ALUMNI: exalumno / egresado que valida su tesis. Mientras un asesor o
      el admin no lo valide, sus envíos también requieren autorización; ya
      validado, sube y analiza sin autorización (con tope de envíos).
    """

    ENROLLED = "ENROLLED", _("Alumno en formación")
    ALUMNI = "ALUMNI", _("Exalumno / egresado")


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
    is_enabled = models.BooleanField(
        default=True,
        verbose_name=_("Habilitado para subir documentos"),
        help_text=_(
            "Alumnos auto-registrados inician deshabilitados hasta que un "
            "docente los habilite."
        ),
    )
    student_status = models.CharField(
        max_length=20,
        choices=StudentStatus.choices,
        default=StudentStatus.ENROLLED,
        verbose_name=_("Condición del alumno"),
        help_text=_(
            "Solo aplica a alumnos. Un exalumno validado sube y analiza sin "
            "autorización del asesor."
        ),
    )
    alumni_validated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Egresado validado el"),
    )
    alumni_validated_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("Egresado validado por"),
    )
    extra_submissions = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Envíos adicionales"),
        help_text=_(
            "Envíos que se suman al tope del rol (docente 5, exalumno 3). "
            "No afecta a administradores ni directores, que no tienen tope."
        ),
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

        if (
            self.role != UserRole.ADMIN
            and not self.is_superuser
            and self.institution_id is not None
            and self.institution.email_domain
            and not self.institution.email_belongs_to_domain(self.email)
        ):
            raise ValidationError(
                {
                    "email": _(
                        "El correo debe pertenecer al dominio institucional "
                        "@%(domain)s."
                    )
                    % {"domain": self.institution.email_domain}
                }
            )

    @property
    def is_admin_role(self) -> bool:
        return self.role == UserRole.ADMIN or self.is_superuser

    @property
    def is_director_role(self) -> bool:
        return self.role == UserRole.DIRECTOR

    @property
    def is_teacher_role(self) -> bool:
        return self.role == UserRole.TEACHER

    @property
    def is_student_role(self) -> bool:
        return self.role == UserRole.STUDENT

    @property
    def is_alumni(self) -> bool:
        """Alumno que se declaró (o fue marcado) exalumno / egresado."""
        return self.is_student_role and self.student_status == StudentStatus.ALUMNI

    @property
    def is_validated_alumni(self) -> bool:
        """Egresado confirmado por un asesor o el admin."""
        return self.is_alumni and self.alumni_validated_at is not None

    def __str__(self) -> str:
        full_name = self.get_full_name().strip()
        return full_name or self.username