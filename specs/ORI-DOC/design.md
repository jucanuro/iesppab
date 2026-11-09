Estado: Borrador — BLOQUEADO hasta G1

# ORI-DOC · Design (borrador AS-IS, solo lectura del código) — todo [Supuesto]

Alcance asumido [Supuesto]: apps `accounts`, `core`, `documents`.

## Modelo de datos
- `core.TimeStampedModel` (abstracto): UUID pk, `created_at`, `updated_at`.
- `core.Institution`: name, slug, ruc, short_code, logo, certificate_signature_name/position, is_active, email_domain.
- `accounts.User` (AbstractUser, pk UUID): institution (FK), role (`UserRole`: ADMIN, DIRECTOR, TEACHER, STUDENT), email, document_number, phone, must_change_password, is_enabled. `clean()` exige institución a no-superusers y dominio de email.
- `documents.Document`: institution, owner, uploaded_by, advisor (FKs), title, kind (`DocumentKind`: THESIS, MONOGRAPH, ESSAY, RESEARCH_ARTICLE, OTHER), course_name, academic_period, original_file, original_filename, mime_type, file_size_bytes, sha256 (hash), language, status (`DocumentStatus`: UPLOADED, QUEUED, PROCESSING, COMPLETED, FAILED), error_message.
- `documents.DocumentText` (1:1): content, word_count, character_count, extraction_engine, extracted_at.

## Vistas / URLs
| URL | Vista | name |
|---|---|---|
| `/` | InstitutionalLoginView | accounts:login |
| `/salir/` | InstitutionalLogoutView | accounts:logout |
| `/registro/` | PublicRegistrationView | accounts:register |
| `/accounts/pendientes/` | PendingStudentsView | accounts:pending-students |
| `/accounts/pendientes/habilitar/` | BulkEnableStudentsView | accounts:bulk-enable-students |
| `/clave/recuperar/`, `/clave/recuperar/enviado/`, `/clave/nueva/<uidb64>/<token>/`, `/clave/nueva/lista/` | PasswordReset* | accounts:password_reset* |
| `/privacidad/`, `/ayuda/` | PrivacyPolicyView, HelpCenterView | core:privacy, core:help |
| `/documentos/subir/` | DocumentUploadView | documents:upload |

## Componentes
- `DocumentUploadService` (`documents/services.py`): único punto de alta; `_validate_payload`, `_resolve_owner`, `_resolve_advisor`, `_validate_file`, `_detect_mime_type` (python-magic), `_calculate_sha256`. Sin `forms.py` por diseño.
- Excepciones: `DocumentUploadError`, `InvalidDocumentFileError`, `InvalidDocumentOwnerError`.
- `documents/extractors.py` (extracción PDF/DOCX), `validators.py`.
- Ruta de archivo impredecible: `private/documents/<institution_id>/<yyyy>/<mm>/<uuid>.<ext>`.

## Flags y constantes
- `ALLOWED_DOCUMENT_EXTENSIONS=("pdf","docx")`, `MAX_DOCUMENT_UPLOAD_SIZE_BYTES=25 MiB` (validators.py).
- settings: `LOGIN_URL="/"`, `LOGIN_REDIRECT_URL="/documentos/subir/"`, `PASSWORD_RESET_TIMEOUT=259200`, `DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE`, `EMAIL_*`.

## Riesgos observados [Supuesto]
- Una sola institución activa asumida (`Institution.objects.filter(is_active=True).first()`).
- `docs/run-proyect.md` contiene credenciales de ejemplo en claro: revisar/retirar (no se reproducen aquí).
