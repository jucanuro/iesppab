Estado: Aprobado por Gerente TI 2026-11-09

# ORI-CER · Design v1 (base: requirements aprobado por Gerente TI 2026-11-08; código leído en solo lectura)

> Etiquetas: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto]. Lo existente se marca **(existe)**; lo nuevo, **(nuevo)**. Las decisiones de este diseño son [Propuesto] hasta la revisión del Gerente TI.

## 1. Alcance y principios
- Dos informes PDF A4 imprimibles (similitud; IA estimada) + certificado y verificación QR con los mismos datos de IA. [Decisión Gerente]
- **Reutilizar, no reescribir:** `build_highlighted_document_pdf` (`apps/reports/services.py:705`, ReportLab, A4 **(existe)**) y `CertificateGenerationService` (`apps/certificates/services.py`, ReportLab + QR **(existe)**). [Decisión Gerente]
- Sin nuevas dependencias de runtime: ReportLab 5.0.0 ya está en `requirements.txt`. (CLAUDE.md dice WeasyPrint para certificados: el código usa ReportLab; se corrige CLAUDE.md fuera de este alcance.) [Supuesto → verificado en código]
- Fuente única de verdad para IA: una función/objeto (`AiEstimate`) que consumen certificado, PDF de IA y QR; así RN-CER-03 se cumple por construcción. [Propuesto]

## 2. Modelo de datos
- **Sin migraciones nuevas.** [Propuesto] Todo se deriva de campos **(existen)** de `reports.AnalysisReport`: `similarity_percent`, `internal_similarity_percent`, `web_similarity_percent`, `ai_probability_percent`, `engine_version`, `ai_detector`, `generated_at`, `summary` (`ai_score_breakdown`), `span_alignment`; `ReportSource.matched_percent`/`is_excluded`/`word_count`; `ReportFinding` (`finding_type`, `start_offset`, `end_offset`, `confidence_percent`); `certificates.Certificate` (`is_active`, `verification_hash`).
- Los certificados ya emitidos (13) no se tocan: RN-CER-05. [Decisión Gerente]
- REQ-CER-11: **DIFERIDO, fuera de alcance [Decisión Gerente]**; no hay migración. Nota histórica del análisis: la reemisión usaría `is_active=False`, `revoked_at`, `revoke_reason` **(existen)**. Obstáculo: `Certificate.report` es `OneToOneField` → no admite dos certificados por reporte. Opciones: (a) reutilizar la fila y regenerar PDF/hash/código (pierde histórico); (b) migración `0002` que cambia a `ForeignKey` + restricción única condicional sobre `is_active=True`. Se descarta para esta versión; si se retoma, la opción (b) con backup previo. [Decisión Gerente]

## 3. Cálculo del porcentaje (RN-CER-06 · D-09)
Definiciones (todas **(existen)** en el motor span; el diseño las documenta, no las cambia): [Decisión Gerente]

| Magnitud | Cálculo | Dónde |
|---|---|---|
| **% total de similitud** (`similarity_percent`) | Palabras contables del documento cubiertas por **la unión** de tramos coincidentes (cada palabra cuenta una vez, aunque coincida con varias fuentes) ÷ `countable_word_count` × 100. Excluye texto común (`common_text`), citas y bibliografía según opciones, y fuentes con `is_excluded=True`. | `SpanReportWriter.write` ← `result.similarity_percent` |
| **% interno / % web** | Misma unión restringida a fuentes internas (`INTERNAL_SOURCE_KINDS`) o web (`SOURCE_KIND_WEB`). Cada una es una unión propia; **interno + web puede superar al total** si un tramo coincide con ambos tipos. | `result.percent_for(...)` |
| **% por fuente** (`ReportSource.matched_percent`) | Palabras del documento cubiertas por los tramos de **esa** fuente ÷ `countable_word_count` × 100. | `item.percent` |

Consecuencia (se imprime en el PDF, REQ-CER-04): **Σ % por fuente ≥ % total**; la igualdad solo ocurre si ningún tramo coincide con más de una fuente. Nunca se presenta la suma como total. [Decisión Gerente]

**Nota de cálculo (texto único del PDF de similitud, `SIMILARITY_CALC_NOTE`)** [Propuesto]:
> "El porcentaje total es la proporción de palabras del documento que coinciden con alguna fuente, contadas una sola vez. El porcentaje de cada fuente es la proporción coincidente con esa fuente; como un mismo fragmento puede coincidir con varias fuentes, la suma de los porcentajes por fuente puede superar al porcentaje total. No se cuentan citas, bibliografía, texto común ni fuentes excluidas."

Salvaguardas de presentación: (1) el PDF lee los % persistidos, no recalcula; (2) si Σ por fuente < total (inconsistencia de datos) se registra `logger.warning` en español y se imprime igual; (3) redondeo: 2 decimales almacenados, 1 decimal impreso, mismo formateador (`_format_decimal`) en PDF y QR. [Propuesto]

## 4. Componentes y servicios
### 4.1 `AiEstimate` (nuevo) — `apps/reports/ai_estimate.py`
`build_ai_estimate(report) -> AiEstimate` con: `percent` (clasificador si `report.ai_classifier`, si no `ai_probability_percent`), `band` (etiqueta de texto + color), `generated_at` (`report.generated_at`), `engine_version` (`report.engine_version` + `report.ai_detector` si existe), `method` (detector/modelo), `note` (= RN-CER-08). Banda: unifica los dos umbrales hoy divergentes (`ai_level` 15/30 vs `ai_classifier_band` 20/50). **Bandas fijadas: <20 / 20-49 / ≥50 (`ai_classifier_band`), sin cambios, siempre con rótulo de texto.** [Decisión Gerente, §9-1] `engine_version`, `ai_detector` y `generated_at` se leen **siempre del reporte**, nunca de la versión actual del motor (un reporte antiguo conserva su versión). [Decisión Gerente]
- Consumidores: certificado, PDF de IA, `CertificateVerifyView` y la página de detalle del reporte (`reports:detail` / `templates/reports/detail.html`), que debe mostrar bandas, % con 1 decimal (`_format_decimal`) y nota desde `AiEstimate`, sustituyendo los umbrales/textos propios actuales (`ai_level` 15/30, `AI_DISCLAIMER`) en esa página. REQ-CER-06. [Propuesto]

### 4.2 Nota de uso responsable (RN-CER-08)
- Constante `AI_RESPONSIBLE_USE_NOTE` en `apps/reports/models.py` con el texto exacto aprobado. [Decisión Gerente]
- Pregunta 7: `AI_ESTIMATE_DISCLAIMER` (`models.py:19`) y `AI_CLASSIFIER_NOTE` (`models.py:67`) **se reemplazan** por el alias `AI_RESPONSIBLE_USE_NOTE` (mismos nombres como alias para no romper `AnalysisReport.AI_DISCLAIMER`, plantillas ni tests). Elimina la divergencia señalada en requirements. [Propuesto]

### 4.3 PDF de similitud (REQ-CER-03/04/12, RN-CER-09)
- **Se reutiliza `build_highlighted_document_pdf`** **(existe)**: ya entrega título, métricas, fuentes primarias numeradas, fuentes excluidas, leyenda, texto resaltado con chips numerados y A4 con márgenes. [Supuesto → leído en código]
- Cambios **(nuevo)** dentro de `_HighlightedDocumentPdfBuilder`:
  1. Cabecera institucional (logo/nombre vía `_prepare_letterhead` análogo al del certificado).
  2. Bloque de métricas con **% total, % interno, % web** rotulados.
  3. Tabla de fuentes con columna **% por fuente** (`matched_percent`) y tipo (interno/web).
  4. `SIMILARITY_CALC_NOTE` bajo la tabla.
  5. **Quitar la línea "IA estimada"** del PDF de similitud y los resaltados azules de IA (pasan al PDF de IA); el PDF de similitud no pinta `AI_GENERATED`. [Decisión Gerente]
  6. Rótulos textuales ("Coincidencia con fuente N") además del color (RN-CER-07).
- Parámetro nuevo `mode="similarity"`; el firmante público `build_highlighted_document_pdf(report)` se mantiene (los ~15 tests existentes siguen válidos). [Propuesto]
- La vista `DownloadHighlightedDocumentView` (`reports:download_highlighted`) **se conserva como URL** y pasa a servir el PDF de similitud: RN-CER-09 "reemplaza" sin romper enlaces. Se renombra el texto del botón en `templates/reports/detail.html:86` a "Informe de similitud". [Decisión Gerente / Propuesto]

### 4.4 PDF de IA estimada (REQ-CER-05/12) (nuevo)
- `build_ai_report_pdf(report) -> bytes` en `apps/reports/services.py`, reutilizando del builder: estilos, `_prepare_letterhead`, `_decorate_page`, `_render_body_markup` en modo IA. Implementación: extraer una clase base `_BasePdfBuilder` con lo común; `_HighlightedDocumentPdfBuilder` pasa a ser subclase (refactor sin cambio de comportamiento, cubierto por los tests existentes). [Propuesto]
- Contenido: cabecera; **"IA estimada: NN,N %"** + banda (texto) + fecha + versión del motor (`AiEstimate`); texto con segmentos `AI_GENERATED` resaltados y **probabilidad por segmento** (`ReportFinding.confidence_percent`, nota al pie numerada); sección *Método* (detector/modelo/versión desde `ai_detector`, `engine_version`); sección *Limitaciones*; `AI_RESPONSIBLE_USE_NOTE`. [Decisión Gerente]
- Texto de *Limitaciones* (constante `AI_LIMITATIONS`) [Propuesto]: no concluyente; sensible a texto técnico, tablas, listas y paratexto; puede dar falsos positivos en prosa académica formal; DOCX y PDF del mismo texto pueden diferir (ver memoria `ai_docx_vs_pdf_extraction`); no evalúa autoría.
- Si no hay hallazgos de IA: PDF igualmente válido con el % y "Sin segmentos señalados". [Propuesto]
- Si `report` no es `is_final`/documento no COMPLETED: **no se genera** (REQ-CER-10).

### 4.5 Certificado y QR (REQ-CER-01/02/06/08/09)
- `CertificateGenerationService._render_pdf` **(existe)**: reemplazar la rama `clf ? AI_CLASSIFIER_NOTE : AI_ESTIMATE_DISCLAIMER` (`services.py:416`) y el bloque de métricas por datos de `AiEstimate`: "IA estimada", banda, fecha, versión del motor, nota única. [Propuesto]
- `_get_allowed_report` **(existe)** filtra al estudiante por propietario y al no-superusuario por institución: compatible con REQ-07. Se conserva. [Supuesto → leído]
- `_validate_report`/`CERTIFICATE_BLOCKING_RISK_LEVELS` **(existen)** ya implementan REQ-09 (solo por similitud); el diseño solo añade el test. [Decisión Gerente]
- **Pregunta 6 (certificados previos):** el QR lee datos vivos de `AnalysisReport`, así que los 13 certificados previos mostrarán IA estimada + nota en la verificación sin regenerar. Sus PDFs almacenados **no se modifican ni regeneran** (podrían carecer de la nota). RN-CER-05 se cumple porque siguen `is_active=True` y verificables. [Decisión Gerente]
- `CertificateVerifyView` **(existe)**: añadir al contexto `ai_estimate`; `templates/certificates/verify.html` añade tarjeta "IA estimada" con banda en texto, fecha, versión y nota. Mantiene nombre del estudiante (REQ-08, Resp. 4). **No** se pasa texto ni hallazgos a la plantilla (RN-CER-04); verificado en código: `verify.html` hoy solo imprime código, fechas, hash, % de similitud, riesgo y fuentes (título/URL/%), sin `snippet`, hallazgos ni texto. Un test lo fija. [Decisión Gerente]
- **Fuentes internas agregadas en la verificación pública** [Decisión Gerente]: las fuentes `SOURCE_KIND_INTERNAL` (documentos de la base interna) se muestran solo como línea agregada "Base interna: N fuentes, X %", sin título, URL ni enlace. Las fuentes web (`SOURCE_KIND_WEB`) y de repositorios públicos (`SOURCE_KIND_OAI`) conservan título/URL/%. Atención: `INTERNAL_SOURCE_KINDS` (`apps/reports/services.py:75`) incluye `OAI`; la agregación usa **solo** `SOURCE_KIND_INTERNAL`, no esa tupla. N = fuentes internas no excluidas. X = [Decisión Gerente, §9-bis] unión de palabras cubiertas solo por fuentes `SOURCE_KIND_INTERNAL`, calculada al renderizar desde los hallazgos, sin migración ni campo persistido (`internal_similarity_percent` incluye OAI y no se usa); sin sumar % por fuente (RN-CER-06). La agregación se arma en la vista; la plantilla no recibe las fuentes internas individuales. El PDF de similitud para el titular no cambia. Las fuentes listadas conservan solo título/URL/% (sin `snippet`: se verificará que la plantilla no lo imprima). [Propuesto]
- REQ-CER-02: la vista ya devuelve `certificate=None` para hash inexistente/inactivo; se garantiza "no válido" con HTTP 200 y se envuelve el acceso a `report/document/owner` para que ninguna relación rota produzca 500. El 500 original del QR (#1) **queda fuera** hasta tener la URL real (pregunta 5). [Supuesto]

### 4.6 Vistas y URLs
| URL | Vista | name | Estado |
|---|---|---|---|
| `documentos/<uuid:pk>/reporte/descargar-senalado/` | `DownloadHighlightedDocumentView` → PDF de similitud | `reports:download_highlighted` | existe, cambia contenido |
| `documentos/<uuid:pk>/reporte/informe-ia/` | `DownloadAiReportView` | `reports:download_ai_report` | **nuevo** |
| `documentos/<uuid:pk>/certificado/generar/` | `CertificateGenerateView` | `certificates:generate` | existe |
| `certificados/<uuid:pk>/descargar/` | `CertificateDownloadView` | `certificates:download` | existe |
| `certificados/verificar/<str:verification_hash>/` | `CertificateVerifyView` | `certificates:verify` | existe, añade IA |
| `certificados/<uuid:pk>/reemitir/` (POST) | `CertificateReissueView` | `certificates:reissue` | **DIFERIDO, fuera de alcance (REQ-11)** |

- **Permisos (REQ-07):** ambas vistas de descarga comparten `_get_allowed_document` (extraído a un mixin `ReportDownloadMixin`): estudiante solo si `document.owner == user`; docente/staff/director solo si `document.institution == user.institution`; superusuario sin restricción; otro caso → `PermissionDenied` (403). Hoy el método existe en `DownloadHighlightedDocumentView`; se verificará que no admita otros roles. [Decisión Gerente / Supuesto en el detalle]
- **Reporte no terminado (REQ-10):** sin reporte final o `HighlightedDocumentPdfError` → `messages.error(...)` en español y `redirect("reports:detail")` (patrón **(existe)**). [Propuesto]
- Botón "Informe de IA" en `templates/reports/detail.html`, junto al de similitud, con rótulo de texto. [Propuesto]

## 5. Diseño para impresión (REQ-CER-12, RN-CER-07)
- `pagesize=A4`, márgenes 1,7 / 1,8 / 1,5 cm **(existen)**; igual en el PDF de IA. [Propuesto]
- Paginación "Página X de N": `NumberedCanvas` (clase `canvasmaker` de ReportLab con segunda pasada) en `_decorate_page`; sustituye al contador actual. [Propuesto]
- Sin depender del color: cada resaltado lleva marca textual (número de fuente en chip con texto; en IA, nota "IA NN %"); leyenda con texto; fuentes de texto ≥ 8 pt; contraste oscuro sobre fondo claro. "Se ve bien impreso" es **No verificable** (REQ-12); se verifica A4, paginación y rótulos. [Propuesto]

## 6. Flags y constantes de configuración
- Sin flags nuevas en `settings.py`. [Propuesto] Constantes en código: `AI_RESPONSIBLE_USE_NOTE`, `SIMILARITY_CALC_NOTE`, `AI_LIMITATIONS`, umbrales de banda (existentes). `CERTIFICATE_BLOCKING_RISK_LEVELS` **(existe)**.
- REQ-11 diferido: no se crea vista ni flag. [Decisión Gerente]
- Variables existentes que condicionan los datos: `AI_DETECTION_MODE`, `WEB_ANALYSIS_ENABLED`. Sin cambios.

## 7. Estrategia de pruebas
- **Unitarias/servicio (`manage.py test`)**: contenido de los PDF con `pypdf` (patrón **(existe)** en `apps/reports/tests.py`): textos obligatorios (REQ-03/04/05), A4 (`mediabox`), "Página X de N", nota exacta RN-CER-08; igualdad certificado↔QR↔PDF IA con `AiEstimate`; permisos (REQ-07, 403 para otra institución, para otro estudiante y anónimo→login); reporte no final (REQ-10); riesgo alto/crítico bloquea (REQ-09); hash inexistente/inactivo (REQ-02); respuesta pública sin texto del documento (REQ-08); `AiEstimate` de un reporte con `engine_version` distinto al actual conserva el del reporte; con una fuente INTERNAL de título conocido, `certificates:verify` muestra "Base interna: N fuentes, X %" y **no** contiene su título, URL ni identificador, mientras que una fuente web y una OAI sembradas sí muestran su título; la respuesta de `certificates:verify` no contiene texto del documento, `snippet` ni segmentos de IA (aserción sobre contenido conocido sembrado); cálculo: caso con dos fuentes solapadas ⇒ total < Σ por fuente.
- **QA de extremo a extremo con `pytest-playwright`** (**ya instalado, no se reinstala**; `conftest.py` raíz ya fija `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64`, solo QA local). Pruebas en `qa/test_cer_playwright.py` (nuevo) contra `runserver` con SQLite y datos de prueba: (1) abrir `certificates:verify` y comparar % de IA, banda, fecha, versión y nota con el valor mostrado en el detalle del reporte (REQ-01/06); (2) hash inválido → "no válido", sin 500 (REQ-02); (3) descargar ambos PDF desde el detalle y comprobar `content-type` y que el estudiante ajeno recibe 403 (REQ-07); (4) `page.pdf(format="A4")` del verify como comprobación de que imprime sin desbordar. Capturas en el directorio temporal; no se imprimen secretos. [Propuesto]
- Los scripts de QA no corren en CI de producción ni tocan producción. [Decisión Gerente — reglas comunes SDD]

## 8. Riesgos y alternativas descartadas
| # | Riesgo / alternativa | Tratamiento |
|---|---|---|
| R1 | Refactor del builder rompe los ~15 tests de PDF existentes | Refactor sin cambio de comportamiento primero; los tests actuales son la red. [Propuesto] |
| R2 | Dos umbrales de banda de IA (15/30 vs 20/50) dan bandas distintas para un mismo valor | `AiEstimate` centraliza con 20/50 (§9-1). [Decisión Gerente] |
| R3 | Reporte recalculado por exclusión de fuente revoca el certificado (`_revoke_certificate`) y cambia el %: QR y PDF ya emitidos divergirían | El certificado se revoca al recalcular **(existe)**; no se regenera en silencio. [Supuesto] |
| R4 | Σ % por fuente > total se lee como error | Nota RN-CER-06 impresa en el PDF y rótulo "no acumulable". [Decisión Gerente] |
| R5 | Hash como única credencial pública; revisar entropía/longitud | Fuera de alcance; se registra como deuda. [Supuesto] |
| R6 | REQ-11 exigiría cambiar `OneToOne` a `ForeignKey` | Diferido; sin migración. [Decisión Gerente] |
| D1 | **Descartado:** nuevo generador con WeasyPrint/HTML→PDF (consistencia visual con plantillas web) | Duplica motor y dependencias del sistema; el Gerente pidió reutilizar ReportLab. [Decisión Gerente] |
| D2 | **Descartado:** guardar el PDF de informes en disco | Los informes se generan al vuelo desde el reporte; evita archivos sensibles persistentes. [Propuesto] |
| D3 | **Descartado:** un único PDF combinado similitud+IA | Contradice el objetivo (dos informes). [Decisión Gerente] |
| D4 | **Descartado:** campos denormalizados de IA en `Certificate` | Duplicarían datos y romperían la coherencia por construcción. [Propuesto] |

## 9. Decisiones que requiere el Gerente TI
Resueltas por el Gerente TI (2026-11-08):
1. **Bandas de IA sin cambios: <20 / 20-49 / ≥50** (escala del clasificador, `ai_classifier_band`). No se modifican umbrales. [Decisión Gerente]
2. **REQ-CER-11 (reemisión) DIFERIDO: fuera de alcance de ORI-CER v1.** Sin migración 0002, sin `CertificateReissueView`, sin `CERTIFICATE_REISSUE_ENABLED`. `Certificate.report` sigue `OneToOneField`. [Decisión Gerente]
3. **No se regeneran los PDFs de los 13 certificados previos** (§4.5). [Decisión Gerente]
4. **Se quita el resaltado de IA del PDF de similitud** (§4.3-5). [Decisión Gerente]

Verificaciones exigidas por el Gerente (cubiertas por tests, ver §7): (a) `AiEstimate` toma `engine_version`/`ai_detector`/`generated_at` **del reporte**, nunca de la configuración o constante actual del motor; (b) la verificación QR **nunca** muestra segmentos ni texto del documento. [Decisión Gerente]

## 10. Matriz REQ → elemento de diseño
| REQ | Elemento de diseño | Sección |
|---|---|---|
| REQ-CER-01 | `CertificateVerifyView` + `verify.html` con `AiEstimate`; Playwright | 4.5, 7 |
| REQ-CER-02 | `CertificateVerifyView` (certificado `None` ⇒ "no válido", 200) | 4.5 |
| REQ-CER-03 | Builder similitud: cabecera, % total/interno/web, tabla de fuentes con %, texto señalado | 3, 4.3 |
| REQ-CER-04 | `SIMILARITY_CALC_NOTE` en el PDF de similitud | 3, 4.3 |
| REQ-CER-05 | `build_ai_report_pdf` + `AiEstimate` (versión/fecha del reporte, bandas 20/50) + `AI_LIMITATIONS` | 4.1, 4.4 |
| REQ-CER-06 | `AiEstimate` como fuente única para certificado, PDF de IA, QR y página de detalle del reporte (`reports:detail`, bandas 20/50, % 1 decimal, nota única) | 4.1, 4.5 |
| REQ-CER-07 | `ReportDownloadMixin._get_allowed_document` (403) en ambas descargas | 4.6 |
| REQ-CER-08 | Plantilla pública sin texto/hallazgos/`snippet` ni fuentes INTERNAL individuales (agregadas, [Decisión Gerente]; verificado + test); nombre del estudiante se mantiene | 4.5, 7 |
| REQ-CER-09 | `_validate_report` + `CERTIFICATE_BLOCKING_RISK_LEVELS` (existe) + test | 4.5 |
| REQ-CER-10 | Redirect con mensaje si el reporte no es final o falla el builder | 4.4, 4.6 |
| REQ-CER-11 | **DIFERIDO / fuera de alcance** [Decisión Gerente]; sin vista ni migración | 2, 9 |
| REQ-CER-12 | A4, `NumberedCanvas` "Página X de N", rótulos de texto | 5 |
| RN-CER-05 | Sin migración que toque datos; QR vivo | 2, 4.5 |
| RN-CER-06 | Definiciones y nota de cálculo | 3 |
| RN-CER-08 | `AI_RESPONSIBLE_USE_NOTE` única | 4.2 |
| RN-CER-09 | Misma URL `download_highlighted` sirve el PDF de similitud | 4.3, 4.6 |

### 9-bis. Punto abierto derivado
- Cálculo de X en "Base interna: N fuentes, X %": ¿unión solo-INTERNAL (exacto, requiere calcularlo/persistirlo) o `internal_similarity_percent` (incluye OAI, ya persistido)? **Resuelto [Decisión Gerente]:** unión solo-`SOURCE_KIND_INTERNAL` calculada al renderizar desde los hallazgos, sin migración; OAI conserva título.
