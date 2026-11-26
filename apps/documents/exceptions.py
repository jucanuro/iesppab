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

class SubmissionLimitError(DocumentUploadError):
    """El usuario alcanzó su tope de envíos."""

    pass


class AuthorizationError(Exception):
    """Error controlado al autorizar / rechazar un envío o validar un egresado."""

    pass
