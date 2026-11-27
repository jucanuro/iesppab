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
    """Genera, regenera y desactiva el código de invitación de un asesor."""

    def __init__(self, owner: User) -> None:
        if not can_advise(owner):
            raise PermissionDenied(
                "Solo docentes, asesores, directores y administradores "
                "pueden generar un código de invitación."
            )
        self.owner = owner

    def active_code(self) -> AdvisorInviteCode | None:
        return AdvisorInviteCode.objects.filter(
            owner=self.owner,
            is_active=True,
        ).first()

    @transaction.atomic
    def regenerate(self) -> AdvisorInviteCode:
        """Desactiva el código vigente (si lo hay) y crea uno nuevo."""

        AdvisorInviteCode.objects.filter(
            owner=self.owner,
            is_active=True,
        ).update(is_active=False)

        # El código es aleatorio y único; ante una colisión improbable, se
        # reintenta con otro.
        for _ in range(5):
            try:
                with transaction.atomic():
                    code = AdvisorInviteCode.objects.create(
                        owner=self.owner,
                        code=generate_invite_code(),
                    )
                break
            except IntegrityError:
                continue
        else:
            raise InviteCodeError("No se pudo generar un código. Inténtalo de nuevo.")

        logger.info("Código de invitación generado. owner_id=%s", self.owner.id)
        return code

    def deactivate(self) -> int:
        return AdvisorInviteCode.objects.filter(
            owner=self.owner,
            is_active=True,
        ).update(is_active=False)


def resolve_invite_code(raw_code: str, institution_id: Any) -> User:
    """
    Devuelve el asesor dueño del código. Falla con un mensaje claro si el
    código no existe, está desactivado o es de otra institución.
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

    return advisor


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
        students = User.objects.select_related("advisor").filter(
            role=UserRole.STUDENT,
            is_enabled=False,
        )

        if self.actor.is_superuser:
            return students.order_by("date_joined")

        if self.actor.is_admin_role:
            return students.filter(
                institution_id=self.actor.institution_id,
            ).order_by("date_joined")

        return students.filter(advisor=self.actor).order_by("date_joined")

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
