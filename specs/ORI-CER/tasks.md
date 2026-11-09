Estado: Aprobado por Gerente TI 2026-11-08

# ORI-CER · Tareas v1 (base: requirements y design aprobados por Gerente TI 2026-11-08)

> Etiquetas: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto]. Todo el plan es [Propuesto] hasta la revisión del Gerente TI. Tareas ≤ ~½ día, ordenadas por dependencia. Sin migraciones (design §2). Sin commit ni push hasta aprobación; los tests se corren con `python manage.py test`.

## Fundamentos
- [x] **T01 — Nota única de uso responsable (RN-CER-08)** · REQ: RN-CER-02, RN-CER-08 (soporta REQ-CER-01/05/06) · Archivos: `apps/reports/models.py` (`AI_RESPONSIBLE_USE_NOTE`; `AI_ESTIMATE_DISCLAIMER` y `AI_CLASSIFIER_NOTE` pasan a alias), `apps/reports/tests.py` · Verificación: test que ambos alias == texto exacto de RN-CER-08; suite `apps.reports` y `apps.certificates` sin regresiones · Rama: `ORI-CER/T01-nota-uso-responsable`
- [x] **T02 — `AiEstimate` (fuente única de IA)** · REQ: REQ-CER-06, RN-CER-03 · Archivos: `apps/reports/ai_estimate.py` (nuevo), `apps/reports/tests.py` · Verificación: tests: % clasificador vs `ai_probability_percent`; bandas <20/20-49/≥50; `engine_version`/`ai_detector`/`generated_at` salen **del reporte** (reporte con versión distinta a la actual la conserva) · Depende: T01 · Rama: `ORI-CER/T02-ai-estimate`

## Verificación pública y certificado
- [x] **T03 — Verificación QR: IA estimada y robustez** · REQ: REQ-CER-01, REQ-CER-02 · Archivos: `apps/certificates/views.py` (`CertificateVerifyView`: `ai_estimate`, acceso a relaciones protegido), `templates/certificates/verify.html` · Verificación: tests de vista: 200 con código, fecha, versión, % similitud, IA estimada + banda + nota; hash inexistente/inactivo → "no válido" con 200; relación rota no da 500 · Depende: T02 · Rama: `ORI-CER/T03-verify-ia`
- [x] **T04 — Verificación QR: confidencialidad y fuentes internas agregadas** · REQ: REQ-CER-08, RN-CER-04 · Archivos: `apps/certificates/views.py` (agregación INTERNAL: N y X = unión solo `SOURCE_KIND_INTERNAL` desde hallazgos, sin migración), `templates/certificates/verify.html`, `apps/certificates/tests.py` · Verificación: test con fuente INTERNAL sembrada: aparece "Base interna: N fuentes, X %" y no su título/URL/id; fuentes web y OAI sí muestran título; respuesta sin texto del documento, `snippet` ni segmentos de IA; nombre del estudiante presente · Depende: T03 · Rama: `ORI-CER/T04-verify-fuentes-internas`
- [ ] **T05 — PDF del certificado con `AiEstimate`** · REQ: REQ-CER-06, RN-CER-02/03 · Archivos: `apps/certificates/services.py` (`_render_pdf`, rama de nota ~l.416 y métricas), `apps/certificates/tests.py` · Verificación: test `pypdf`: PDF contiene "IA estimada", banda, fecha, versión y nota exacta, idénticos a `AiEstimate` y a lo renderizado por la verificación · Depende: T02 · Rama: `ORI-CER/T05-certificado-ia`
- [ ] **T06 — Test de bloqueo por riesgo** · REQ: REQ-CER-09, RN-CER-01 · Archivos: `apps/certificates/tests.py` (`_validate_report` ya existe) · Verificación: test: riesgo Alto/Crítico por similitud impide emitir; Bajo/Medio emite; alta IA no bloquea · Rama: `ORI-CER/T06-bloqueo-riesgo`

## Informes PDF
- [ ] **T07 — Refactor `_BasePdfBuilder` sin cambio de comportamiento** · REQ: soporte de REQ-CER-03/05/12 (design R1) · Archivos: `apps/reports/services.py` · Verificación: los ~15 tests de PDF existentes pasan sin modificarse · Rama: `ORI-CER/T07-refactor-pdf-base`
- [ ] **T08 — Permisos y mixin de descarga** · REQ: REQ-CER-07 · Archivos: `apps/reports/views.py` (`ReportDownloadMixin._get_allowed_document`), `apps/reports/tests.py` · Verificación: tests: dueño y docente/staff de la institución → 200; estudiante ajeno, otra institución → 403; anónimo → login; superusuario permitido · Depende: T07 · Rama: `ORI-CER/T08-permisos-descarga`
- [ ] **T09 — PDF de similitud: contenido** · REQ: REQ-CER-03, REQ-CER-04, RN-CER-06, RN-CER-09 · Archivos: `apps/reports/services.py` (`mode="similarity"`, cabecera, % total/interno/web, columna % por fuente, `SIMILARITY_CALC_NOTE`, sin IA ni resaltado `AI_GENERATED`), `apps/reports/models.py`/constantes, `templates/reports/detail.html` (rótulo "Informe de similitud") · Verificación: tests `pypdf`: textos obligatorios y nota de cálculo; no contiene "IA estimada"; caso de dos fuentes solapadas ⇒ total < Σ por fuente; `download_highlighted` sirve este PDF · Depende: T07, T08 · Rama: `ORI-CER/T09-pdf-similitud`
- [ ] **T10 — PDF de IA estimada** · REQ: REQ-CER-05, REQ-CER-06 · Archivos: `apps/reports/services.py` (`build_ai_report_pdf`, `AI_LIMITATIONS`), `apps/reports/views.py` (`DownloadAiReportView`), `apps/reports/urls.py`, `templates/reports/detail.html` (botón "Informe de IA") · Verificación: tests `pypdf`: % "IA estimada", banda, fecha, versión, segmentos con probabilidad, método, limitaciones y nota exacta; valores == certificado/verificación; sin hallazgos → PDF válido con "Sin segmentos señalados" · Depende: T02, T07, T08 · Rama: `ORI-CER/T10-pdf-ia`
- [ ] **T11 — Reporte no terminado** · REQ: REQ-CER-10 · Archivos: `apps/reports/views.py`, `apps/reports/tests.py` · Verificación: tests: reporte no final/documento no COMPLETED o `HighlightedDocumentPdfError` → redirect a `reports:detail` con mensaje en español, sin PDF · Depende: T09, T10 · Rama: `ORI-CER/T11-reporte-no-terminado`
- [ ] **T12 — Diseño para impresión** · REQ: REQ-CER-12, RN-CER-07 · Archivos: `apps/reports/services.py` (`NumberedCanvas`, rótulos de texto, fuentes ≥ 8 pt) · Verificación: tests `pypdf`: `mediabox` A4, "Página X de N" en ambos PDF, rótulos "Coincidencia con fuente N" / "IA NN %" además del color · Depende: T09, T10 · Rama: `ORI-CER/T12-impresion`

## QA de extremo a extremo
- [ ] **T13 — Playwright local** · REQ: REQ-CER-01, REQ-CER-02, REQ-CER-06, REQ-CER-07 · Archivos: `qa/test_cer_playwright.py` (nuevo; usa `conftest.py` raíz; solo local, SQLite y datos de prueba) · Verificación: (1) verify == detalle del reporte en % IA, banda, fecha, versión y nota; (2) hash inválido → "no válido" sin 500; (3) ambos PDF descargables y ajeno recibe 403; (4) `page.pdf(format="A4")` de verify sin desbordar. Capturas en el directorio temporal · Depende: T03–T05, T08–T10 · Rama: `ORI-CER/T13-qa-playwright`

## Cobertura REQ → tareas
| REQ | Tareas |
|---|---|
| REQ-CER-01 | T03, T13 |
| REQ-CER-02 | T03, T13 |
| REQ-CER-03 | T09 |
| REQ-CER-04 | T09 |
| REQ-CER-05 | T10 |
| REQ-CER-06 | T02, T05, T10, T13 |
| REQ-CER-07 | T08, T13 |
| REQ-CER-08 | T04 |
| REQ-CER-09 | T06 |
| REQ-CER-10 | T11 |
| REQ-CER-12 | T12 |
| RN-CER-05 | Sin tarea de código: sin migración ni regeneración de los 13 PDFs [Decisión Gerente]; T03 los mantiene verificables (el QR lee datos vivos) |

## Aparte
- **REQ-CER-11 (reemisión): DIFERIDO, fuera de alcance** [Decisión Gerente]; sin tarea.
- **No verificable:** que el PDF "se vea bien impreso" (REQ-CER-12); el error 500 original del QR (#1, HU-CER-04) hasta contar con la URL real. Se verifican solo A4, paginación y rótulos de texto.

**Pendiente: revisión y aprobación del Gerente TI.** Este archivo permanece en `Estado: Borrador`.
