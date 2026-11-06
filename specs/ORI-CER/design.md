Estado: Borrador — BLOQUEADO hasta G1

# ORI-CER · Design (borrador AS-IS, solo lectura del código) — todo [Supuesto]

## Modelo de datos
- `certificates.Certificate` (TimeStamped): report (1:1 `AnalysisReport`), code, pdf_file, verification_hash, issued_by (FK User), is_active, revoked_at, revoke_reason.

## Vistas / URLs
| URL | Vista | name |
|---|---|---|
| `/documentos/<uuid:pk>/certificado/generar/` | CertificateGenerateView | certificates:generate |
| `/certificados/<uuid:pk>/descargar/` | CertificateDownloadView | certificates:download |
| `/certificados/verificar/<str:verification_hash>/` | CertificateVerifyView (pública) | certificates:verify |

## Componentes
- `CertificateGenerationService(issued_by, absolute_base_url).execute(document_id)`: `_get_allowed_report` (alumno solo el suyo; no-superuser limitado a su institución), `_validate_report`, `_get_or_create_certificate`, `_generate_certificate_code`, `_generate_verification_hash`, `_build_verification_url`, `_render_pdf`.
- PDF con **ReportLab** + QR; membrete por institución (`_prepare_letterhead`). (CLAUDE.md dice WeasyPrint: discrepancia.)
- Excepción: `CertificateGenerationError`.
- Revocación: `ReportSourceExclusionService._revoke_certificate` (en reports) al recalcular.

## Reglas vigentes en código
- Solo reporte `is_final`, documento COMPLETED y riesgo ∉ {HIGH, CRITICAL} (`CERTIFICATE_BLOCKING_RISK_LEVELS`).

## Flags y constantes
- `CERTIFICATE_BLOCKING_RISK_LEVELS`; umbrales de riesgo en `reports.services.resolve_risk_level`. Sin flags en settings específicos de certificados.

## Riesgos observados [Supuesto]
- El hash de verificación es la única credencial de la URL pública; revisar longitud/entropía y política de revocación visible.
