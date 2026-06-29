from __future__ import annotations

from typing import Any

import magic
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


ALLOWED_DOCUMENT_EXTENSIONS: tuple[str, ...] = ("pdf", "docx")

ALLOWED_DOCUMENT_MIME_TYPES: set[str] = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

MAX_DOCUMENT_UPLOAD_SIZE_BYTES = 25 * 1024 * 1024


def validate_document_size(file_obj: Any) -> None:
    """
    Valida tamaño máximo del documento.
    MVP: 25 MB.
    """

    file_size = getattr(file_obj, "size", 0)

    if file_size > MAX_DOCUMENT_UPLOAD_SIZE_BYTES:
        raise ValidationError(
            _(
                "El archivo supera el tamaño máximo permitido de 25 MB. "
                "Comprima o divida el documento antes de subirlo."
            ),
            code="file_too_large",
        )


def validate_document_mime_type(file_obj: Any) -> None:
    """
    Valida MIME real leyendo los primeros bytes del archivo.
    Requiere python-magic.
    """

    current_position = file_obj.tell() if hasattr(file_obj, "tell") else 0

    try:
        header = file_obj.read(4096)
        detected_mime = magic.from_buffer(header, mime=True)
    finally:
        if hasattr(file_obj, "seek"):
            file_obj.seek(current_position)

    if detected_mime not in ALLOWED_DOCUMENT_MIME_TYPES:
        raise ValidationError(
            _(
                "Tipo de archivo no permitido. Solo se aceptan documentos "
                "PDF o Word .docx válidos."
            ),
            code="invalid_mime_type",
        )