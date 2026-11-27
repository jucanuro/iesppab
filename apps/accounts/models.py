from __future__ import annotations

import secrets
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


# Sin caracteres ambiguos (0/O, 1/I/L) para dictar el código sin errores.
INVITE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_CODE_LENGTH = 8


def generate_invite_code() -> str:
    return "".join(
        secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH)
    )


class AdvisorInviteCode(models.Model):
    """
    Código que un docente, asesor, director o admin envía a los alumnos de un
    curso (p. ej. por el grupo de WhatsApp) para que se relacionen con él en
    ese curso. Puede tener varios, uno por curso o grupo; regenerar uno lo
    reemplaza por otro con el mismo curso.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="invite_codes",
        verbose_name=_("Docente / asesor"),
        limit_choices_to={"role__in": ["TEACHER", "DIRECTOR", "ADMIN"]},
    )
    code = models.CharField(
        max_length=INVITE_CODE_LENGTH,
        unique=True,
        default=generate_invite_code,
        verbose_name=_("Código"),
    )
    course_name = models.CharField(
        max_length=180,
        blank=True,
        verbose_name=_("Curso / grupo"),
    )
    academic_period = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Periodo"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Activo"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Creado el"))

    class Meta:
        verbose_name = _("Código de invitación")
        verbose_name_plural = _("Códigos de invitación")
        ordering = ["-created_at"]

    @property
    def course_label(self) -> str:
        parts = [part for part in (self.course_name, self.academic_period) if part]
        return " · ".join(parts) or "Sin curso"

    def __str__(self) -> str:
        return f"{self.code} · {self.owner} · {self.course_label}"


class AdvisorLink(models.Model):
    """
    Vínculo alumno–docente–curso. Un alumno puede tener varios (uno por curso
    o docente); se crea al registrarse con un código o al agregar otro desde
    «Mis docentes». El documento que sube el alumno guarda el docente y el
    curso del vínculo elegido.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="advisor_links",
        verbose_name=_("Alumno"),
    )
    advisor = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="student_links",
        verbose_name=_("Docente / asesor"),
        limit_choices_to={"role__in": ["TEACHER", "DIRECTOR", "ADMIN"]},
    )
    invite_code = models.ForeignKey(
        AdvisorInviteCode,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="links",
        verbose_name=_("Código usado"),
    )
    course_name = models.CharField(max_length=180, blank=True, verbose_name=_("Curso / grupo"))
    academic_period = models.CharField(max_length=50, blank=True, verbose_name=_("Periodo"))
    is_active = models.BooleanField(default=True, verbose_name=_("Activo"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Vinculado el"))

    class Meta:
        verbose_name = _("Vínculo alumno–docente")
        verbose_name_plural = _("Vínculos alumno–docente")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "advisor", "course_name", "academic_period"],
                name="vinculo_unico_por_curso",
            ),
        ]

    @property
    def course_label(self) -> str:
        parts = [part for part in (self.course_name, self.academic_period) if part]
        return " · ".join(parts) or "Sin curso"

    def __str__(self) -> str:
        return f"{self.student} → {self.advisor} ({self.course_label})"
