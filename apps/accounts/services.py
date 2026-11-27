from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.exceptions import InviteCodeError
from apps.accounts.models import (
    INVITE_CODE_ALPHABET,
    INVITE_CODE_LENGTH,
    AdvisorInviteCode,
    AdvisorLink,
    StudentStatus,
    User,
    UserRole,
    generate_invite_code,
)

logger = logging.getLogger(__name__)

ADVISOR_ROLES = [UserRole.TEACHER, UserRole.DIRECTOR, UserRole.ADMIN]



def can_advise(user: Any) -> bool:
    """Docentes/asesores, directores y admin pueden tener alumnos a cargo."""

    return bool(
        getattr(user, "is_authenticated", False)
        and (user.is_superuser or user.role in ADVISOR_ROLES)
    )


def normalize_invite_code(raw: str) -> str:
    return "".join(raw.split()).upper()


class InviteCodeService:
    """
    Códigos de invitación de un asesor: uno por curso o grupo. Crea, regenera
    (mismo curso, código nuevo) y activa o desactiva cada uno.
    """

    def __init__(self, owner: User) -> None:
        if not can_advise(owner):
            raise PermissionDenied(
                "Solo docentes, asesores, directores y administradores "
                "pueden generar un código de invitación."
            )
        self.owner = owner

    def codes(self) -> QuerySet[AdvisorInviteCode]:
        return AdvisorInviteCode.objects.filter(owner=self.owner).order_by(
            "-is_active",
            "-created_at",
        )

    def active_code(self) -> AdvisorInviteCode | None:
        return self.codes().filter(is_active=True).first()

    def _new_code(self, course_name: str, academic_period: str) -> AdvisorInviteCode:
        # El código es aleatorio y único; ante una colisión improbable, se
        # reintenta con otro.
        for _ in range(5):
            try:
                with transaction.atomic():
                    return AdvisorInviteCode.objects.create(
                        owner=self.owner,
                        code=generate_invite_code(),
                        course_name=course_name,
                        academic_period=academic_period,
                    )
            except IntegrityError:
                continue

        raise InviteCodeError("No se pudo generar un código. Inténtalo de nuevo.")

    @transaction.atomic
    def create(self, course_name: str = "", academic_period: str = "") -> AdvisorInviteCode:
        course_name = course_name.strip()[:180]
        academic_period = academic_period.strip()[:50]

        if not course_name:
            raise InviteCodeError(
                "Escribe el curso o grupo para el que es el código."
            )

        if self.codes().filter(
            is_active=True,
            course_name__iexact=course_name,
            academic_period__iexact=academic_period,
        ).exists():
            raise InviteCodeError(
                "Ya tienes un código activo para ese curso y periodo. "
                "Regenéralo si quieres cambiarlo."
            )

        code = self._new_code(course_name, academic_period)
        logger.info(
            "Código de invitación creado. owner_id=%s code_id=%s",
            self.owner.id,
            code.id,
        )
        return code

    @transaction.atomic
    def regenerate(self, code_id: Any = None) -> AdvisorInviteCode:
        """
        Desactiva el código indicado y crea otro para el mismo curso. Sin
        ``code_id`` crea uno sin curso (compatibilidad con un solo código).
        """

        if code_id is None:
            return self._new_code("", "")

        old = self.codes().select_for_update().filter(pk=code_id).first()

        if old is None:
            raise PermissionDenied("Ese código no es tuyo.")

        old.is_active = False
        old.save(update_fields=["is_active"])

        code = self._new_code(old.course_name, old.academic_period)
        logger.info(
            "Código de invitación regenerado. owner_id=%s code_id=%s",
            self.owner.id,
            code.id,
        )
        return code

    def set_active(self, code_id: Any, active: bool) -> AdvisorInviteCode:
        code = self.codes().filter(pk=code_id).first()

        if code is None:
            raise PermissionDenied("Ese código no es tuyo.")

        code.is_active = active
        code.save(update_fields=["is_active"])
        return code

    def deactivate(self) -> int:
        """Desactiva todos los códigos del asesor."""

        return self.codes().filter(is_active=True).update(is_active=False)


def resolve_invite_code(raw_code: str, institution_id: Any) -> AdvisorInviteCode:
    """
    Devuelve el código (con su asesor y curso). Falla con un mensaje claro si
    no existe, está desactivado o es de otra institución.
    """

    code = normalize_invite_code(raw_code)

    if not code:
        raise InviteCodeError(
            "Escribe el código que te envió tu docente o asesor."
        )

    if len(code) != INVITE_CODE_LENGTH or any(
        char not in INVITE_CODE_ALPHABET for char in code
    ):
        raise InviteCodeError(
            "El código no es válido. Revisa que tenga 8 caracteres, tal como "
            "te lo envió tu docente o asesor."
        )

    invite = (
        AdvisorInviteCode.objects.select_related("owner")
        .filter(code=code)
        .first()
    )

    if invite is None:
        raise InviteCodeError(
            "El código no existe. Pídele a tu docente o asesor que te lo "
            "vuelva a enviar."
        )

    advisor = invite.owner

    if (
        not invite.is_active
        or not advisor.is_active
        or not can_advise(advisor)
    ):
        raise InviteCodeError(
            "El código está desactivado. Pídele a tu docente o asesor uno "
            "nuevo."
        )

    if (
        not advisor.is_superuser
        and institution_id is not None
        and advisor.institution_id != institution_id
    ):
        raise InviteCodeError("El código no pertenece a esta institución.")

    return invite


class AdvisorLinkService:
    """Vínculos de un alumno con sus docentes / asesores (uno por curso)."""

    def __init__(self, student: User) -> None:
        if not student.is_student_role:
            raise PermissionDenied(
                "Solo los alumnos se vinculan con un docente por código."
            )
        self.student = student

    def links(self) -> QuerySet[AdvisorLink]:
        return (
            AdvisorLink.objects.select_related("advisor")
            .filter(student=self.student, is_active=True)
            .order_by("created_at")
        )

    @transaction.atomic
    def add_by_code(self, raw_code: str) -> AdvisorLink:
        invite = resolve_invite_code(raw_code, self.student.institution_id)
        return self.link_with(invite)

    def link_with(self, invite: AdvisorInviteCode) -> AdvisorLink:
        if invite.owner_id == self.student.pk:
            raise InviteCodeError("No puedes usar tu propio código.")

        existing = AdvisorLink.objects.filter(
            student=self.student,
            advisor=invite.owner,
            course_name=invite.course_name,
            academic_period=invite.academic_period,
        ).first()

        if existing is not None:
            if existing.is_active:
                advisor = invite.owner.get_full_name().strip() or invite.owner.username
                raise InviteCodeError(
                    f"Ya estás vinculado con {advisor} en {invite.course_label}."
                )
            existing.is_active = True
            existing.invite_code = invite
            existing.save(update_fields=["is_active", "invite_code"])
            return existing

        link = AdvisorLink.objects.create(
            student=self.student,
            advisor=invite.owner,
            invite_code=invite,
            course_name=invite.course_name,
            academic_period=invite.academic_period,
        )

        logger.info(
            "Alumno vinculado. student_id=%s advisor_id=%s code_id=%s",
            self.student.id,
            invite.owner_id,
            invite.id,
        )

        return link


class StudentEnablementService:
    """
    Habilitación de alumnos y exalumnos que se registran solos. Cada asesor
    ve y habilita a los que se registraron con su código; el admin ve a los
    de toda la institución (incluidos los que no tienen asesor).
    """

    def __init__(self, actor: User) -> None:
        if not can_advise(actor):
            raise PermissionDenied("No tienes permisos para habilitar alumnos.")
        self.actor = actor

    def pending_students(self) -> QuerySet[User]:
        students = User.objects.filter(
            role=UserRole.STUDENT,
            is_enabled=False,
        )

        if self.actor.is_superuser:
            return students.order_by("date_joined")

        if self.actor.is_admin_role:
            return students.filter(
                institution_id=self.actor.institution_id,
            ).order_by("date_joined")

        return (
            students.filter(advisor_links__advisor=self.actor)
            .distinct()
            .order_by("date_joined")
        )

    def student_links(self) -> QuerySet[AdvisorLink]:
        """Alumnos vinculados (con su curso): los del asesor, o todos (admin)."""

        links = AdvisorLink.objects.select_related(
            "student",
            "advisor",
            "student__alumni_validated_by",
        ).filter(is_active=True, student__role=UserRole.STUDENT)

        if self.actor.is_superuser:
            pass
        elif self.actor.is_admin_role:
            links = links.filter(student__institution_id=self.actor.institution_id)
        else:
            links = links.filter(advisor=self.actor)

        return links.order_by("course_name", "academic_period", "student__first_name")

    @transaction.atomic
    def enable(
        self,
        student_ids: Iterable[Any],
        validate_alumni: bool = False,
    ) -> int:
        """
        Habilita a los alumnos indicados que el actor puede ver. Con
        ``validate_alumni`` además valida como egresados a los exalumnos.
        """

        students = list(
            self.pending_students()
            .filter(id__in=list(student_ids))
            .select_for_update()
        )

        now = timezone.now()

        for student in students:
            student.is_enabled = True
            fields = ["is_enabled"]

            if (
                validate_alumni
                and student.student_status == StudentStatus.ALUMNI
                and student.alumni_validated_at is None
            ):
                student.alumni_validated_at = now
                student.alumni_validated_by = self.actor
                fields += ["alumni_validated_at", "alumni_validated_by"]

            student.save(update_fields=fields)

        logger.info(
            "Alumnos habilitados. actor_id=%s total=%s valida_egresado=%s",
            self.actor.id,
            len(students),
            validate_alumni,
        )

        return len(students)
