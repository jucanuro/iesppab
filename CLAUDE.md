# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Django 5.2 app for an academic institution (IESPP "Alfonso Barrantes Lingán") that lets students upload
academic documents (PDF/DOCX) for originality/AI-detection analysis and issues verifiable PDF certificates.
Spanish is the language for all user-facing strings, model verbose names, and log messages — follow this
convention in new code.

## Commands

```bash
# Activate the project virtualenv first (repo ships venv/ at root)
source venv/bin/activate

python manage.py runserver
python manage.py migrate
python manage.py makemigrations <app_label>
python manage.py test                       # full suite
python manage.py test apps.documents         # single app
python manage.py test apps.documents.tests.SomeTestCase.test_method  # single test
python manage.py createsuperuser
python manage.py collectstatic
```

Environment config is loaded via `python-decouple`/`dotenv` from `.env.local` (local dev, preferred if present)
or `.env` (production), selectable via `DJANGO_ENV_FILE`. `DJANGO_ENV` must be `local` or `production`; in
`production`, `DEBUG` and empty `ALLOWED_HOSTS` raise at startup. See `.env.example` for all variables
(Postgres connection, Brave Search API key for web-similarity, OAI-PMH harvesting toggles).

Default DB is Postgres (`DB_ENGINE=django.db.backends.postgresql`); set `DB_ENGINE=django.db.backends.sqlite3`
to fall back to the bundled `db.sqlite3` for quick local work.

## Architecture

Apps live under `apps/` and are wired together in a fixed pipeline — a document moves through these apps in
order:

1. **`apps.accounts`** — custom `User` model (`AUTH_USER_MODEL = "accounts.User"`), UUID PK, role-based
   (`UserRole`: ADMIN, DIRECTOR, TEACHER, STUDENT — exposed as `user.is_admin_role`/`is_teacher_role`/etc.
   properties, `is_admin_role` also true for superusers). Every non-superuser belongs to an `Institution`
   (`apps.core`), enforced in `User.clean()`, which also enforces that non-admin emails match the
   institution's `email_domain` when one is configured. Institutional login (`InstitutionalLoginView`)
   accepts username OR institutional email but restricts non-privileged roles to email login matched against
   the institution's domain. `PublicRegistrationView` is a self-service registration path for students that
   validates against the (single, `is_active=True`) institution's email domain.
2. **`apps.core`** — shared abstractions: `TimeStampedModel` (UUID PK + `created_at`/`updated_at`, abstract
   base most models inherit) and `Institution`. The system is designed for a single active institution today
   but modeled as multi-tenant (`Institution.objects.filter(is_active=True).first()` is the common lookup;
   most querysets are scoped by `institution_id`, with superusers bypassing that scoping).
3. **`apps.documents`** — `Document`/`DocumentText` models and `DocumentUploadService`
   (`apps/documents/services.py`), which is the sole entry point for creating documents — there is
   intentionally no `forms.py`; all business validation (payload, owner resolution/permissions, file
   extension/size, real MIME-type sniffing via `python-magic`, SHA-256 hashing) lives in this service class.
   Files are stored under unpredictable paths (`private/documents/<institution_id>/<yyyy>/<mm>/<uuid>.<ext>`)
   via `document_upload_path`, not by original filename. Controlled errors are raised as
   `DocumentUploadError`/`InvalidDocumentFileError`/`InvalidDocumentOwnerError` (`apps/documents/exceptions.py`)
   and translated to user messages in the view; anything else is logged and wrapped. `_resolve_owner` allows a
   student to upload only for themselves, and staff/teachers to upload on behalf of any student in their own
   institution (or any institution, if superuser).
4. **`apps.analysis`** — `DocumentAnalysisService` (`apps/analysis/services.py`) runs the originality/AI
   pipeline: text extraction (`apps.documents.extractors`), Winnowing fingerprinting
   (`engines/fingerprint.py`), internal similarity against previously indexed documents
   (`engines/similarity.py` + `DocumentKnowledgeIndexer` in `indexers.py`), Spanish AI-generated-text detection
   (`engines/ai_detector.py`), and optional external web similarity via Brave Search
   (`engines/web_search.py`, gated by `WEB_ANALYSIS_ENABLED`/`BRAVE_SEARCH_API_KEY`). It also harvests
   external repositories via OAI-PMH (`OAI_HARVEST_REPOSITORIES` in `config/settings.py` — each entry is a
   manually-confirmed working endpoint; see the comment block above it for repos that were tried and don't
   have a confirmed OAI endpoint yet) into `OaiRecord`/knowledge chunks for similarity comparisons.
5. **`apps.reports`** — `AnalysisReport`/`ReportFinding`/`ReportSource` persist the outcome of an analysis job
   (risk level, findings, detected sources) for display and downstream certificate generation.
6. **`apps.certificates`** — generates a verifiable PDF certificate (WeasyPrint) once a report clears, with a
   public hash-based verification URL (`certificates:verify`).

URL routing: each app owns its own `urls.py` with an `app_name` namespace, all mounted at root in
`config/urls.py` (no shared URL prefix per app — routes like `documentos/<uuid:pk>/analizar/`,
`documentos/<uuid:pk>/reporte/`, `documentos/<uuid:pk>/certificado/generar/` interleave across apps by
document UUID).

Templates live centrally under `templates/<app_name>/...` (not inside each app), sharing `templates/base.html`.
Frontend styling is Tailwind via the CDN script tag (no build step, no npm) — institutional blue is `#123f9e`
(text) / `#236bfd` (accent) / `#eef3ff` (soft background), cards use rounded-xl + soft blue shadow. Keep new UI
consistent with this and avoid introducing a JS/CSS build pipeline.

Services over forms: business logic consistently lives in a `services.py` per app (`DocumentUploadService`,
`DocumentAnalysisService`, certificate generation service) rather than Django `forms.ModelForm`. Follow this
pattern for new write paths — validate in the service, raise a domain-specific exception, catch and translate
to a user message in the view.
