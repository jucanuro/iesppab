from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import magic
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from apps.accounts.models import User, UserRole
from apps.documents.exceptions import (
    DocumentUploadError,
    InvalidDocumentFileError,
    InvalidDocumentOwnerError,
)
from apps.documents.models import Document, DocumentKind, DocumentStatus
from apps.documents.validators import ALLOWED_DOCUMENT_MIME_TYPES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentUploadDTO:
    title: str
    kind: str
    course_name: str
    academic_period: str
    owner_id: str | UUID | None
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

            self._validate_file(dto.uploaded_file)

            detected_mime = self._detect_mime_type(dto.uploaded_file)
            sha256_hash = self._calculate_sha256(dto.uploaded_file)

            dto.uploaded_file.seek(0)

            document = Document(
                institution=owner.institution,
                owner=owner,
                uploaded_by=self.uploaded_by,
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