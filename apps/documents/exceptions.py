from __future__ import annotations


class DocumentUploadError(Exception):
    """Error base controlado para carga de documentos."""

    pass


class InvalidDocumentFileError(DocumentUploadError):
    """Archivo inválido o no permitido."""

    pass


class InvalidDocumentOwnerError(DocumentUploadError):
    """Alumno propietario inválido."""

    pass