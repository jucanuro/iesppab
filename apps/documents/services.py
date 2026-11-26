from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import magic
from django.core.exceptions import PermissionDenied, ValidationError
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import StudentStatus, User, UserRole
from apps.documents.exceptions import (
    AuthorizationError,
    DocumentUploadError,
    InvalidDocumentFileError,
    InvalidDocumentOwnerError,
    SubmissionLimitError,
)
from apps.documents.models import (
    AuthorizationStatus,
    Document,
    DocumentKind,
    DocumentStatus,
)
from apps.documents.validators import ALLOWED_DOCUMENT_MIME_TYPES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmissionQuota:
    """Tope de envíos de un usuario. ``limit`` None = sin tope."""

    limit: int | None
    used: int

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit


class SubmissionPolicy:
    """
    Reglas de envío por rol (decisión del Gerente TI, 2026-11-26):

    - ADMIN, DIRECTOR y superusuario: sin tope y sin autorización.
    - TEACHER (docente / asesor): sin autorización, tope de 5 envíos propios.
    - STUDENT exalumno validado: sin autorización, tope de 3 envíos.
    - STUDENT exalumno sin validar: tope de 3 envíos y cada envío requiere la
      autorización del asesor, que en la primera solicitud valida que es
      egresado.
    - STUDENT en formación: sin tope, pero cada envío requiere autorización.

    [Supuesto] Un «envío» es un documento propio subido (owner = el usuario),
    sin contar los fallidos ni los rechazados por el asesor. Reanalizar un
    documento no es un envío nuevo. ``User.extra_submissions`` suma envíos al
    tope del rol.
    """

    @staticmethod
    def base_limit(user: User) -> int | None:
        if user.is_superuser or user.is_admin_role or user.is_director_role:
            return None

        if user.is_teacher_role:
            return settings.SUBMISSION_LIMIT_TEACHER

        if user.is_alumni:
            return settings.SUBMISSION_LIMIT_ALUMNI

        return None

    @classmethod
    def quota(cls, user: User) -> SubmissionQuota:
        base = cls.base_limit(user)

        if base is None:
            return SubmissionQuota(limit=None, used=0)

        used = (
            Document.objects.filter(owner=user)
            .exclude(status=DocumentStatus.FAILED)
            .exclude(authorization_status=AuthorizationStatus.REJECTED)
            .count()
        )

        return SubmissionQuota(limit=base + user.extra_submissions, used=used)

    @staticmethod
    def requires_authorization(user: User) -> bool:
        return user.is_student_role and not user.is_validated_alumni


def ensure_analysis_authorized(document: Document) -> None:
    """
    Impide analizar un documento que espera, o no obtuvo, la autorización de
    su asesor. Los NOT_REQUIRED y APPROVED se analizan como siempre.
    """

    if document.authorization_status == AuthorizationStatus.PENDING:
        raise AuthorizationError(
            "Este documento espera la autorización de su asesor. Se analizará "
            "automáticamente cuando la apruebe."
        )

    if document.authorization_status == AuthorizationStatus.REJECTED:
        note = document.authorization_note.strip()
        raise AuthorizationError(
            "El asesor rechazó el análisis de este documento"
            + (f": {note}" if note else ".")
        )


@dataclass(frozen=True)
class DocumentUploadDTO:
    title: str
    kind: str
    course_name: str
    academic_period: str
    owner_id: str | UUID | None
    advisor_id: str | UUID | None
    uploaded_file: UploadedFile


class DocumentUploadService:
    """
    Servicio profesional para registrar documentos.
    No usamos forms.py. Toda la validación de negocio vive aquí.
    """

    def __init__(self, uploaded_by: User) -> None:
        self.uploaded_by = uploaded_by

    @transaction.atomic
    def execute(self, dto: DocumentUploadDTO) -> Document:
        try:
            self._validate_payload(dto)
            owner = self._resolve_owner(dto.owner_id)

            if owner.institution_id is None:
                raise InvalidDocumentOwnerError(
                    "El alumno seleccionado no tiene institución asignada."
                )

            if not owner.is_enabled:
                raise ValidationError(
                    "Este alumno aún no ha sido habilitado por un docente."
                )

            # Los topes y la autorización aplican a los envíos propios. Un
            # docente que sube en nombre de un alumno actúa como asesor.
            # [Supuesto] ese envío no consume tope ni requiere autorización.
            own_submission = owner.pk == self.uploaded_by.pk

            if own_submission:
                self._check_quota(owner)

            requires_authorization = (
                own_submission
                and SubmissionPolicy.requires_authorization(owner)
            )

            advisor = self._resolve_advisor(dto.advisor_id, owner=owner)

            if requires_authorization and advisor is None:
                raise DocumentUploadError(
                    "Elige a tu asesor o docente: él debe autorizar el "
                    "análisis de tu documento."
                )

            self._validate_file(dto.uploaded_file)

            detected_mime = self._detect_mime_type(dto.uploaded_file)
            sha256_hash = self._calculate_sha256(dto.uploaded_file)

            dto.uploaded_file.seek(0)

            document = Document(
                institution=owner.institution,
                owner=owner,
                uploaded_by=self.uploaded_by,
                advisor=advisor,
                title=dto.title.strip(),
                kind=dto.kind,
                course_name=dto.course_name.strip(),
                academic_period=dto.academic_period.strip(),
                original_file=dto.uploaded_file,
                original_filename=dto.uploaded_file.name,
                mime_type=detected_mime,
                file_size_bytes=dto.uploaded_file.size,
                sha256_hash=sha256_hash,
                language="es",
                status=DocumentStatus.UPLOADED,
                authorization_status=(
                    AuthorizationStatus.PENDING
                    if requires_authorization
                    else AuthorizationStatus.NOT_REQUIRED
                ),
            )

            document.full_clean()
            document.save()

            logger.info(
                "Documento registrado correctamente. document_id=%s user_id=%s",
                document.id,
                self.uploaded_by.id,
            )

            return document

        except (DocumentUploadError, ValidationError, PermissionDenied):
            logger.warning(
                "Error controlado al registrar documento. user_id=%s",
                self.uploaded_by.id,
                exc_info=True,
            )
            raise

        except Exception as exc:
            logger.exception(
                "Error inesperado al registrar documento. user_id=%s",
                self.uploaded_by.id,
            )
            raise DocumentUploadError(
                "Ocurrió un error inesperado al registrar el documento."
            ) from exc

    def _check_quota(self, owner: User) -> None:
        # Bloquea la fila del usuario para que dos subidas simultáneas no
        # pasen ambas el último envío disponible.
        User.objects.select_for_update().filter(pk=owner.pk).first()

        quota = SubmissionPolicy.quota(owner)

        if not quota.exhausted:
            return

        if owner.is_teacher_role:
            raise SubmissionLimitError(
                f"Alcanzaste el máximo de {quota.limit} envíos de prueba para "
                "docentes. Si necesitas más, pídeselos a un administrador."
            )

        raise SubmissionLimitError(
            f"Alcanzaste el máximo de {quota.limit} envíos para exalumnos. "
            "Si necesitas más, pídeselos a tu asesor o a un administrador."
        )

    def _validate_payload(self, dto: DocumentUploadDTO) -> None:
        if not dto.title.strip():
            raise DocumentUploadError("El título del documento es obligatorio.")

        valid_kinds = {value for value, _label in DocumentKind.choices}

        if dto.kind not in valid_kinds:
            raise DocumentUploadError("El tipo de documento no es válido.")

        if dto.uploaded_file is None:
            raise InvalidDocumentFileError("Debe seleccionar un archivo.")

    def _resolve_owner(self, owner_id: str | UUID | None) -> User:
        if self.uploaded_by.is_student_role:
            return self.uploaded_by

        if not owner_id:
            raise InvalidDocumentOwnerError(
                "Debe seleccionar el alumno propietario del documento."
            )

        if str(owner_id) == str(self.uploaded_by.id):
            return self.uploaded_by

        students = User.objects.select_related("institution").filter(
            id=owner_id,
            role=UserRole.STUDENT,
            is_active=True,
        )

        if not self.uploaded_by.is_superuser:
            if self.uploaded_by.institution_id is None:
                raise PermissionDenied(
                    "Tu usuario no tiene institución asignada."
                )

            students = students.filter(
                institution_id=self.uploaded_by.institution_id
            )

        owner = students.first()

        if owner is None:
            raise InvalidDocumentOwnerError(
                "El alumno seleccionado no existe o no pertenece a tu institución."
            )

        return owner

    def _resolve_advisor(
        self,
        advisor_id: str | UUID | None,
        owner: User,
    ) -> User | None:
        if not advisor_id:
            return None

        advisors = User.objects.filter(
            id=advisor_id,
            role__in=[UserRole.TEACHER, UserRole.DIRECTOR],
            is_active=True,
        )

        if not self.uploaded_by.is_superuser:
            advisors = advisors.filter(institution_id=owner.institution_id)

        advisor = advisors.first()

        if advisor is None:
            raise DocumentUploadError(
                "El asesor seleccionado no existe o no pertenece a la institución del alumno."
            )

        return advisor

    def _validate_file(self, uploaded_file: UploadedFile) -> None:
        filename = uploaded_file.name.lower()

        if not filename.endswith((".pdf", ".docx")):
            raise InvalidDocumentFileError(
                "Solo se permiten archivos PDF o Word .docx."
            )

        if uploaded_file.size <= 0:
            raise InvalidDocumentFileError("El archivo está vacío.")

        max_size_bytes = 25 * 1024 * 1024

        if uploaded_file.size > max_size_bytes:
            raise InvalidDocumentFileError(
                "El archivo supera el tamaño máximo permitido de 25 MB."
            )

    def _detect_mime_type(self, uploaded_file: UploadedFile) -> str:
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)
            header = uploaded_file.read(4096)
            detected_mime = magic.from_buffer(header, mime=True)
        finally:
            uploaded_file.seek(current_position)

        if detected_mime not in ALLOWED_DOCUMENT_MIME_TYPES:
            raise InvalidDocumentFileError(
                "El archivo no corresponde a un PDF o DOCX válido."
            )

        return detected_mime

    def _calculate_sha256(self, uploaded_file: UploadedFile) -> str:
        sha256 = hashlib.sha256()
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)

            for chunk in uploaded_file.chunks():
                sha256.update(chunk)

        finally:
            uploaded_file.seek(current_position)

        return sha256.hexdigest()


def can_decide_authorizations(user: User) -> bool:
    """Docentes, directores y admin ven la bandeja de solicitudes."""

    return bool(
        user.is_superuser
        or user.is_admin_role
        or user.is_director_role
        or user.is_teacher_role
    )


class SubmissionAuthorizationService:
    """
    Autoriza o rechaza los envíos pendientes y valida egresados.

    Decide el asesor elegido en el documento, o un administrador de la misma
    institución (el superusuario, de cualquiera). Al autorizar se encola el
    análisis. Se registra quién y cuándo.
    """

    def __init__(self, actor: User) -> None:
        self.actor = actor

    # --- Consultas -------------------------------------------------------

    def pending_documents(self) -> QuerySet[Document]:
        if not can_decide_authorizations(self.actor):
            raise PermissionDenied(
                "No tienes permisos para autorizar envíos."
            )

        documents = Document.objects.select_related(
            "owner",
            "owner__alumni_validated_by",
            "advisor",
        ).filter(authorization_status=AuthorizationStatus.PENDING)

        if self.actor.is_superuser:
            return documents.order_by("created_at")

        if self.actor.is_admin_role:
            return documents.filter(
                institution_id=self.actor.institution_id,
            ).order_by("created_at")

        return documents.filter(advisor=self.actor).order_by("created_at")

    def recent_decisions(self, limit: int = 10) -> QuerySet[Document]:
        return (
            Document.objects.select_related("owner", "advisor")
            .filter(
                authorized_by=self.actor,
                authorization_status__in=[
                    AuthorizationStatus.APPROVED,
                    AuthorizationStatus.REJECTED,
                ],
            )
            .order_by("-authorized_at")[:limit]
        )

    # --- Decisiones ------------------------------------------------------

    @transaction.atomic
    def approve(
        self,
        document_id: str | UUID,
        validate_alumni: bool = False,
    ) -> Document:
        document = self._get_pending_for_decision(document_id)

        if validate_alumni:
            if not document.owner.is_alumni:
                raise AuthorizationError(
                    "Este alumno no se registró como exalumno."
                )
            self._set_validated_alumni(document.owner)

        document.authorization_status = AuthorizationStatus.APPROVED
        document.authorized_by = self.actor
        document.authorized_at = timezone.now()
        document.save(
            update_fields=[
                "authorization_status",
                "authorized_by",
                "authorized_at",
                "updated_at",
            ]
        )

        logger.info(
            "Envío autorizado. document_id=%s actor_id=%s valida_egresado=%s",
            document.id,
            self.actor.id,
            validate_alumni,
        )

        document_pk, actor_pk = document.pk, self.actor.pk
        transaction.on_commit(lambda: _enqueue_after_approval(document_pk, actor_pk))

        return document

    @transaction.atomic
    def reject(self, document_id: str | UUID, note: str = "") -> Document:
        document = self._get_pending_for_decision(document_id)

        document.authorization_status = AuthorizationStatus.REJECTED
        document.authorized_by = self.actor
        document.authorized_at = timezone.now()
        document.authorization_note = note.strip()[:1000]
        document.save(
            update_fields=[
                "authorization_status",
                "authorized_by",
                "authorized_at",
                "authorization_note",
                "updated_at",
            ]
        )

        logger.info(
            "Envío rechazado. document_id=%s actor_id=%s",
            document.id,
            self.actor.id,
        )

        return document

    @transaction.atomic
    def mark_alumni(self, student_id: str | UUID) -> User:
        """
        Marca a un alumno como egresado validado: desde entonces sube y
        analiza sin autorización (con el tope de exalumno). Lo hace el admin
        o un asesor de alguno de los documentos del alumno.
        """

        student = (
            User.objects.select_for_update()
            .filter(pk=student_id, role=UserRole.STUDENT)
            .first()
        )

        if student is None or not self._can_manage_student(student):
            raise PermissionDenied(
                "No puedes marcar como egresado a este alumno."
            )

        self._set_validated_alumni(student)

        logger.info(
            "Alumno marcado como egresado. student_id=%s actor_id=%s",
            student.id,
            self.actor.id,
        )

        return student

    # --- Internos --------------------------------------------------------

    def _get_pending_for_decision(self, document_id: str | UUID) -> Document:
        document = (
            Document.objects.select_for_update()
            .filter(pk=document_id)
            .first()
        )

        if document is None or not self._can_decide(document):
            raise PermissionDenied(
                "Solo el asesor elegido o un administrador puede decidir "
                "este envío."
            )

        if document.authorization_status != AuthorizationStatus.PENDING:
            raise AuthorizationError("Esta solicitud ya fue resuelta.")

        return document

    def _is_admin_for(self, institution_id: Any) -> bool:
        if self.actor.is_superuser:
            return True

        return bool(
            self.actor.is_admin_role
            and self.actor.institution_id is not None
            and self.actor.institution_id == institution_id
        )

    def _can_decide(self, document: Document) -> bool:
        if self._is_admin_for(document.institution_id):
            return True

        return (
            document.advisor_id is not None
            and document.advisor_id == self.actor.pk
            and (self.actor.is_teacher_role or self.actor.is_director_role)
        )

    def _can_manage_student(self, student: User) -> bool:
        if self._is_admin_for(student.institution_id):
            return True

        if not (self.actor.is_teacher_role or self.actor.is_director_role):
            return False

        return Document.objects.filter(
            owner=student,
            advisor=self.actor,
        ).exists()

    def _set_validated_alumni(self, student: User) -> None:
        student.student_status = StudentStatus.ALUMNI

        if student.alumni_validated_at is None:
            student.alumni_validated_at = timezone.now()
            student.alumni_validated_by = self.actor

        student.save(
            update_fields=[
                "student_status",
                "alumni_validated_at",
                "alumni_validated_by",
            ]
        )


def _enqueue_after_approval(document_id: Any, actor_id: Any) -> None:
    from apps.analysis.queueing import enqueue_document_analysis

    try:
        enqueue_document_analysis(document_id, actor_id)
    except Exception:
        logger.exception(
            "No se pudo encolar el análisis tras autorizar. document_id=%s",
            document_id,
        )


def pending_authorizations_count(user: User) -> int:
    if not getattr(user, "is_authenticated", False):
        return 0

    if not can_decide_authorizations(user):
        return 0

    return SubmissionAuthorizationService(user).pending_documents().count()
