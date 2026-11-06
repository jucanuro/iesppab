Estado: Borrador — BLOQUEADO hasta G1

# ORI-INFRA · Design (borrador AS-IS, solo lectura del código) — todo [Supuesto]

## Modelo de datos
Sin modelos propios. Usa `AnalysisJob` (estado de ejecución) y las cachés/OAI de `analysis`.

## Vistas / URLs
`/admin/` (Django admin); estáticos y media servidos con WhiteNoise / `static()` solo con DEBUG.

## Componentes
- Entorno: `DJANGO_ENV` (local|production), `DJANGO_ENV_FILE`, `.env.local` / `.env`; en production DEBUG o ALLOWED_HOSTS vacío abortan el arranque.
- BD: `DB_ENGINE` (Postgres por defecto; sqlite3 opcional).
- Celery + Redis: tarea `apps.analysis.tasks.run_document_analysis` (acks_late, reject_on_worker_lost); `MAX_ANALYSIS_DELIVERIES`=2.
- Comandos de mantenimiento: `sweep_stale_jobs`, `purge_web_cache`, `harvest_oai`, `harvest_oai_fulltext`, `reindex_knowledge`, `backfill_fingerprints`, `backfill_analyzed_content`, `recalculate_risk_levels`.
- Cosecha OAI-PMH: `OAI_HARVEST_ENABLED`, `OAI_HARVEST_REPOSITORIES` (endpoints confirmados manualmente).
- Seguridad HTTP: cookies Secure/HttpOnly/SameSite, HSTS, nosniff, X-Frame DENY, CSRF view propia.
- Correo: `EMAIL_*`, `DEFAULT_FROM_EMAIL`. Logging: `LOG_LEVEL`, `LOGGING`.

## Flags y constantes
- `ANALYSIS_TASK_SOFT_TIME_LIMIT`=1200 s, `ANALYSIS_TASK_TIME_LIMIT`=1500 s, `CELERY_BROKER_TRANSPORT_OPTIONS.visibility_timeout`=3600 (debe ser > time limit), `ANALYSIS_STALE_JOB_MINUTES`=TIME_LIMIT/60+10 (debe ser > time limit). Settings validan estas relaciones al arrancar.
- `CELERY_BROKER_URL`/`RESULT_BACKEND` (Redis local por defecto), `WEB_ANALYSIS_ENABLED`, `PERPLEXITY_AI_DETECTION_ENABLED`, `AI_DETECTION_MODE`, `SIMILARITY_ENGINE` (ver otros módulos).
- Secretos (no se imprimen): SECRET_KEY, BRAVE_SEARCH_API_KEY, EMAIL_HOST_PASSWORD, credenciales de BD.

## Riesgos observados [Supuesto]
- Servidor de producción con ~2 GB RAM (limita modelos de IA).
- `docs/run-proyect.md` describe arranque en Windows/PowerShell y contiene credenciales de ejemplo; `CLAUDE.md` asume Linux (`source venv/bin/activate`).
- No hay definición versionada de timers/systemd para `sweep_stale_jobs`/`purge_web_cache` en el repo [Supuesto: se configuran en el servidor].
