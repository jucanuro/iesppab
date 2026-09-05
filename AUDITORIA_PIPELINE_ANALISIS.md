# Auditoría senior del pipeline de análisis

Fecha: 2026-09-02 · Alcance: `apps/analysis/*`, `apps/documents/extractors.py`,
`apps/reports/{views,services,suggestions}.py`.
Método: lectura + reproducción con código real (scripts en scratchpad, sin tocar BD ni producción).
Foco: (A) dónde se cae, (B) dónde va lento, (C) dónde da números incorrectos.

Los porcentajes de gravedad asumen: institución de formación docente, tesis PDF
de 15–40k palabras, `WEB_ANALYSIS_ENABLED=True`, `PERPLEXITY_AI_DETECTION_ENABLED`
apagado en producción (encendido en `.env.local`).

---

## Resumen ejecutivo (orden de gravedad)

| # | Tipo | Hallazgo | Impacto |
|---|------|----------|---------|
| 1 | C — números incorrectos | Los offsets de los hallazgos se calculan contra el texto **filtrado**, pero el visor y el PDF los pintan sobre el texto **crudo** | Resaltados sobre frases equivocadas en casi todos los documentos reales |
| 2 | B — lento | `web_similarity` re-normaliza cada página web dentro del bucle por fragmento | 52 s medidos con 12 páginas; minutos en tesis grande; presión de RAM |
| 3 | C — números incorrectos | El filtro de portada borra prosa académica real (intro de tesis sobre educación) | Se pierde el primer párrafo de contenido y se desplazan todos los offsets |
| 4 | C — números incorrectos | Un `anexo`/`referencias` mencionado a mitad del cuerpo trunca todo lo que sigue | 52 % del documento descartado en el repro; sub-reporta plagio/IA |
| 5 | C — números incorrectos | Perplejidad solo mira los primeros ~900 tokens; texto repetitivo → ~100 % IA | Latente (flag off en prod); activo en demos locales |
| 6 | C — números incorrectos | Offsets de IA vía `str.find()` → hallazgos descolocados o descartados en silencio | Menor, pero se suma al #1 |
| 7 | B — lento/escala | `hash__in` con decenas de miles de enteros + `GROUP BY` sobre todas las coincidencias | La query más pesada del pipeline; crece con el corpus |
| 8 | B — lento | `ReportFinding/ReportSource.objects.create()` en bucle (100+ INSERT) | Menor |
| 9 | B — lento | `ReportDetailView` re-consulta `findings` ya prefetim­eados | 2 queries extra por vista |
| 10 | A — se cae | `ReportSource.domain`/`title` reciben cadenas OAI sin acotar | `DataError` → job FAILED (baja probabilidad) |

Nada de lo anterior produce un HTTP 500 al usuario: el `except Exception` de
`DocumentAnalysisService.execute` + los tres niveles de captura en la tarea Celery
contienen bien los fallos. El problema real es **(C): números y resaltados
incorrectos en los que el usuario confía**.

---

## A — Dónde se cae (robustez)

### A pesar de buscar activamente, NO se encontró:
- Ninguna excepción no manejada que tumbe el análisis con entradas degeneradas.
  Probado (TEST 9): documento vacío, 1 palabra, solo espacios, 100k palabras en un
  párrafo, 50k caracteres pegados sin espacios, solo saltos de línea, 20k acentos
  combinados. **Cero excepciones, cero tiempos patológicos** (todo < 0.5 s).
- División por cero en `SpanishAIDetector`: `total_weight` siempre ≥ 120 porque
  `_split_paragraphs` descarta los párrafos < `MIN_PARAGRAPH_LENGTH`, y `analyze`
  corta temprano si la lista queda vacía. Correcto.
- Fallo silencioso en el detector de perplejidad: si `torch`/`transformers` no
  están o el modelo revienta, `_estimate_ai_probability` lo captura
  (`services.py:281-291`) y degrada a la heurística. Correcto.
- Extracción de PDF/DOCX corrupto: `extractors._extract_pdf/_extract_docx`
  envuelven todo en `TextExtractionError`; `execute` lo captura y marca el job
  FAILED con `error_message`. Correcto.

### Hallazgo 10 — `ReportSource` con campos acotados y datos OAI sin truncar
**Archivo:** `apps/analysis/services.py:691-717`
**Disparador:** un `OaiRecord.repository_name` de más de 180 caracteres, o un
`title` de más de 300, entra directo a `ReportSource.objects.create(domain=…,
title=…)`. `.create()` no corre validadores, así que solo salta el límite de
columna: `django.db.utils.DataError` dentro del `transaction.atomic()` →
`_save_report` y todo el análisis se marca FAILED.
**Gravedad / frecuencia:** baja. Depende de la calidad de los repositorios OAI
cosechados; hoy la lista está curada a mano. Pero es un fallo de análisis completo
por un dato de terceros.
**Arreglo:** truncar defensivamente antes de crear (`value[:180]`, `value[:300]`),
o validar en la cosecha OAI.

### Observación de robustez (no bug): filtro que borra TODO
**Archivo:** `apps/analysis/services.py:119`
Si el documento es pura carátula + índice (TEST 4), `filter_for_similarity`
devuelve `""` y `analysis_content = filtered_text.content or document_text.content`
cae de vuelta al **texto crudo sin filtrar**. No se cae (bien), pero entonces
similitud e IA corren sobre el boilerplate de la carátula. El fallback es
correcto como anti-crash; conviene además registrar un warning cuando ocurre.

---

## B — Dónde va lento (rendimiento)

### Hallazgo 2 — `web_similarity` re-normaliza cada página dentro del bucle por fragmento  ⚠️ el más caro
**Archivo:** `apps/analysis/engines/web_similarity.py:119-136`
(`_compare_chunk_with_page`), llamado desde el bucle de `analyze` (`:69-91`); y de
nuevo en `_source_excerpt` (`:271`).

```
for chunk in chunks:            # ~ palabras/45  (≈ 550 en una tesis de 25k)
    for page in web_pages:      # hasta 16
        normalized_page = self._normalize(page.text)        # <-- re-hecho cada vez
        page_shingles  = self._build_shingles(normalized_page)  # <-- re-hecho cada vez
```

**Reproducción (TEST 5):** documento de 82.950 caracteres (~11.5k palabras) contra
12 páginas web de ~97 KB cada una:
- `analyze()` tardó **52,27 s**.
- `_normalize()` se llamó **3.598 veces**, procesando **326.903.041 caracteres**
  en total, cuando la entrada real es 83 KB de documento + 1,16 MB de páginas
  (**~280× de trabajo redundante**).
- Además genera ~327 MB de cadenas transitorias → presión de memoria en el
  servidor de 2 GB.

Con "Proyecto prueba 11" (~25k palabras) y 16 páginas esto escala a **2–4 minutos
solo en la etapa web**, y puede chocar con timeouts del worker.

**Contraste:** `similarity.py:77-80` ya hace bien esto — precalcula
`candidate_shingles` **una sola vez** fuera del bucle. `web_similarity` quedó sin
ese mismo arreglo.

**Arreglo:** antes del bucle de fragmentos, construir una vez
`prepared = [(page, self._normalize(page.text), self._build_shingles(...)) for page in web_pages]`
y consumir eso dentro del bucle. Es el mismo patrón que ya usa el motor interno.
Reduce el coste de O(fragmentos × páginas × |página|) a O(páginas × |página| +
fragmentos × páginas × |fragmento|).

### Hallazgo 7 — Selección de candidatos por huellas: `hash__in` gigante + `GROUP BY`
**Archivo:** `apps/analysis/services.py:503, 516-540`
Para una tesis de 25k palabras, `compute_fingerprints` sobre el documento completo
devuelve del orden de 10–30k enteros. Luego:
`DocumentFingerprint.objects.filter(hash__in={~20k ints}).exclude(...).values("source_type","source_id").annotate(shared=Count("id")).order_by("-shared")[:40]`.
- El `IN` con ~20k parámetros y el `GROUP BY` sobre **todas** las filas de huellas
  que coinciden es, con diferencia, la consulta más pesada del pipeline.
- `DocumentFingerprint.hash` sí tiene índice (`models.BigIntegerField(db_index=True)`
  y además un `models.Index(fields=["hash"])` redundante — índice duplicado, ruido
  menor). No hay índice compuesto `(hash, source_type)`.
- No es O(n²), pero crece linealmente con el tamaño del corpus (interno + OAI):
  los 4-gramas académicos comunes ("de la investigación en el") devuelven muchísimas
  filas para agrupar.
**Gravedad / frecuencia:** media a escala; hoy con corpus pequeño es tolerable.
**Arreglo:** submuestrear las huellas del documento antes del lookup (p. ej. quedarse
con 1 de cada k, o top-N por valor de hash), y/o añadir índice compuesto
`(hash, source_type)`. Quitar el índice duplicado sobre `hash`.

### Hallazgo 8 — INSERT fila por fila al persistir hallazgos
**Archivo:** `apps/analysis/services.py:653-733` (interno), `:735-793` (web),
`:795-818` (IA). Hasta ~35 `ReportSource` + 35+ `ReportFinding` por motor, todos con
`.objects.create()` individual dentro del `atomic()`. **Arreglo:** `bulk_create`.
Impacto bajo (docenas de INSERT), pero gratis de corregir.

### Hallazgo 9 — `ReportDetailView` rompe su propio prefetch
**Archivo:** `apps/reports/views.py:103-108` vs `:175-180`
El `Prefetch("findings", ...)` cachea todos los hallazgos, pero luego
`report.findings.filter(finding_type=SIMILARITY)` y
`report.findings.filter(finding_type=AI_GENERATED)` **ignoran la caché** y lanzan
2 queries nuevas por cada carga del reporte. `report.findings.all()` (para los
segmentos) sí usa la caché.
**Arreglo:** filtrar en Python sobre `report.findings.all()`.

### Genuinamente bien en rendimiento
- El motor interno (`similarity.py`) **sí** iza `candidate_shingles` fuera del
  bucle, y usa `all_matched_ranges` (completo) para el score y `matches[:MAX]`
  solo para el detalle. Los cambios recientes en `similarity.py` / `indexers.py` /
  `_calculate_total_similarity_percent` son correctos y están bien comentados.
- El fingerprinting Winnowing thina de verdad el número de huellas (mín. por
  ventana, desempate a la derecha según el paper) y reusa la secuencia de shingles
  en vez de duplicar la lógica.
- Ninguna entrada degenerada produjo tiempo patológico (TEST 9: 100k palabras
  filtradas + comparadas en 0,44 s).

---

## C — Dónde da resultados incorrectos (correctitud)

### Hallazgo 1 — Los offsets de los hallazgos no corresponden al texto que se pinta  ⚠️ el más grave
**Archivos:**
- Se calculan contra el filtrado: `apps/analysis/services.py:115-119`
  (`analysis_content = filtered_text.content or document_text.content`) y
  `:143-174` (los tres motores reciben `analysis_content`); se persisten tal cual
  en `:719-733`, `:779-793`, `:805-818`.
- Se pintan contra el crudo: `apps/reports/views.py:83`
  (`content=document.extracted_text.content`) → `_build_highlight_segments`
  (`views.py:226-241`); y `apps/reports/services.py:125` → `_build_segments`
  (`services.py:213-244`). **No hay ningún remapeo de offsets en ninguna parte.**

**Reproducción (TEST 1):** documento = carátula + índice de tablas + cuerpo.
El filtro elimina la corrida de índice (484 caracteres). El motor interno devuelve
un match en offsets `(327, 922)`:
- `analysis_content[327:922]` → *"…del nivel primario en instituciones educativas
  públicas del distrito. La investigación parte de la observación de que los niños
  cuyos padres…"* (el párrafo realmente plagiado).
- `raw_extracted_text[327:922]` → *"........... 12\n\nTabla 3 Resultados de la
  dimensión 3 ................ 13\n\nTabla 4…"* (basura del índice).
- `SAME TEXT? False`.

**Por qué pasa siempre, no solo en casos raros:** aunque no se elimine ninguna
sección, `_normalize_line_breaks` colapsa `[ \t]+`→espacio, `\n{3,}`→`\n\n` y hace
`strip()`, y `filter_for_similarity` re-une los párrafos con `"\n\n".join(kept)`
descartando párrafos "de bajo valor" intermedios. Cualquiera de esas operaciones
desplaza los offsets. Con carátula/índice/bibliografía removidos, el desfase es de
cientos a miles de caracteres y **crece a lo largo del documento** (un hallazgo en
las conclusiones puede estar corrido varios miles de caracteres).

**Gravedad / frecuencia:** crítica. Afecta a prácticamente todos los documentos
reales (todo PDF de tesis tiene carátula e índice). El usuario ve el visor
interactivo y el "documento señalado" en PDF con los resaltados sobre frases
equivocadas — y encima confía en ellos para sustentar el dictamen.

**Arreglo (opciones):**
1. **Persistir el texto analizado** (`analysis_content`) junto al reporte
   (campo nuevo, p. ej. `AnalysisReport.analyzed_text` o
   `DocumentText.filtered_content`) y que el visor y el PDF pinten **ese** mismo
   string. Es el cambio más chico y hace todo consistente.
2. Construir durante el filtrado un mapa de offsets (lista de tramos cortados +
   deltas acumulados) y traducir `start/end` de vuelta al crudo antes de guardar
   los `ReportFinding`.
3. Correr los motores sobre el texto crudo y usar el filtro solo para ponderar el
   score (más invasivo).
La opción 1 es la recomendada.

### Hallazgo 3 — El filtro de portada se come prosa académica real
**Archivo:** `apps/analysis/engines/text_filters.py:240-244` (`_looks_like_cover`),
invocado para párrafos `index <= 3` en `:94`.
```
COVER_MARKERS = universidad, facultad, escuela, instituto, tesis,
                trabajo de investigación, autor, asesor, docente, curso
_looks_like_cover = (>= 3 marcadores) and len(parrafo) < 1200
```
**Reproducción (TEST 2):** párrafo de introducción real, 366 caracteres:
> "En el ámbito educativo peruano, la escuela rural enfrenta una tensión
> permanente: el docente único atiende varios grados a la vez, la universidad
> más cercana que forma a esos maestros queda a horas de distancia, y el
> instituto de formación continua rara vez llega con acompañamiento real al aula…"

`_looks_like_cover` → **True** (marcadores: universidad, escuela, instituto, tesis,
docente). Resultado: el párrafo se elimina con `excluded_sections=['portada']`.

**Gravedad / frecuencia:** alta **para esta institución en particular**. Es un
instituto de formación docente: casi toda tesis habla de escuelas, docentes,
universidades e institutos en su introducción, y la introducción vive justo en los
primeros 4 párrafos. Se pierde contenido real del análisis **y** se disparan los
offsets del Hallazgo 1.
**Arreglo:** exigir señales más fuertes y propias de carátula: varias líneas de
una sola palabra/frase corta, presencia de "para optar el título", alto ratio de
MAYÚSCULAS, ausencia de oraciones completas. No descartar nunca un párrafo que
contenga más de una oración de prosa corrida.

### Hallazgo 4 — Un marcador de sección a mitad del cuerpo trunca el resto del documento
**Archivo:** `apps/analysis/engines/text_filters.py:22-28, 115-134`
(`_remove_reference_tail`). `SECTION_STOP_MARKERS` incluye `\banexo\b`,
`\breferencias\b`, `\bbibliografía\b`… buscados **en cualquier posición**. Si el
primer match cae después del 45 % del documento, se descarta **todo** lo que sigue.
**Reproducción (TEST 8):** el cuerpo dice *"Los instrumentos completos se presentan
en el anexo correspondiente de este informe."* al 48 % del documento →
`filtered len = 2806` vs `doc len = 5809` (**se descarta el 52 %**), el capítulo de
discusión desaparece, `excluded_sections=['bibliografía/anexos']`.
**Gravedad / frecuencia:** media-alta. Es muy común escribir "ver anexo N" o
"según las referencias de X" en resultados/discusión. Un capítulo de conclusiones
copiado, ubicado después de esa mención, se vuelve invisible para similitud e IA.
**Arreglo:** tratar el marcador como límite de sección **solo si es un
encabezado**: línea propia y corta, al inicio de línea, opcionalmente numerada o
en MAYÚSCULAS — p. ej. `^\s*(\d+\.?\s*)?(REFERENCIAS|BIBLIOGRAF[IÍ]A|ANEXOS?)\s*$`
multilínea. Nunca por substring dentro de una oración.

### Hallazgo 5 — Perplejidad: solo se miden los primeros ~900 tokens; texto repetitivo → ~100 % IA
**Archivo:** `apps/analysis/engines/perplexity_detector.py:28` (`MAX_TOKENS = 900`),
`:177-201` (`_sequence_perplexity` trunca), `:452` (`analyze` pasa el documento
entero a `compute_perplexity`).
- Para una tesis de 25k palabras, `mean_perplexity` se calcula **solo sobre las
  primeras ~2 páginas**. El resto del documento no influye en esa señal.
- **Reproducción (TEST 7):** `"La casa es azul."` × 30 → `mean_perplexity = 1.61`
  → `ai_probability_percent = 97.23`. El formato típico de tesis peruana (objetivo
  general / objetivos específicos con andamiaje casi idéntico, hipótesis repetidas)
  está exactamente en esa primera ventana.
- Estado: `settings.PERPLEXITY_AI_DETECTION_ENABLED` = **False** por defecto y en
  `.env`; **True** en `.env.local`. Latente en producción, activo en demos locales.
**Gravedad / frecuencia:** media, condicionada al flag. Si se llega a activar sin
corregir esto, habrá falsos positivos de IA altos en tesis humanas con front
matter repetitivo.
**Arreglo:** muestrear la perplejidad en varias ventanas a lo largo del documento
y agregar con la mediana (igual criterio que ya se aplicó al burstiness por
fragmentos), y amortiguar el score cuando el texto muestreado tenga type/token
ratio muy bajo (líneas casi duplicadas).

### Hallazgo 6 — Offsets de IA vía `str.find()`: hallazgos descolocados o descartados en silencio
**Archivo:** `apps/analysis/engines/ai_detector.py:129-154` (`_split_paragraphs`).
`start = content.find(paragraph, cursor)`; si da `-1`, cae a
`content.find(paragraph)` desde 0 → puede devolver un offset **anterior** a un
hallazgo previo. Aguas abajo, `_build_highlight_segments`
(`views.py:243`, `services.py:230`) hace `if start < cursor: continue` → el
hallazgo se descarta sin aviso. Párrafos duplicados (frases de plantilla) también
se localizan mal.
Además, todo offset de IA es contra `analysis_content` → arrastra el Hallazgo 1.
**Gravedad / frecuencia:** baja-media por sí solo.
**Arreglo:** llevar el offset de forma posicional durante el split (acumular
longitudes de los trozos crudos + separadores) en vez de `str.find`.

### Casos límite que SÍ están coherentes (verificado)
- Documento contra sí mismo, otro dueño → **99,91 %** (TEST 10). ✔
- Documento sin ninguna coincidencia → **0,00 %** (TEST 11). ✔
- `min(percent, 100)` + fusión de rangos evita que el plagio masivo pase de 100 %. ✔
- La combinación interna + web en `_calculate_total_similarity_percent` es
  consistente: ambos conjuntos de rangos son offsets dentro del mismo
  `analysis_content` y se fusionan antes de dividir. ✔ (el problema es que ese
  `analysis_content` no es lo que se pinta — Hallazgo 1, no este cálculo).

### Sobre `risk_level` vs el % mostrado
`_resolve_risk_level` (`services.py:867-880`) usa umbrales 15/30/50 sobre
`similarity_percent` **o** `ai_probability_percent`. `AnalysisReport.similarity_level`
usa 15/30. Son coherentes entre sí, pero nota: un documento con 2 % de similitud y
85 % de IA queda `risk_level = CRITICAL` mientras el badge de similitud dice
"bajo". Es el comportamiento buscado (la IA manda), pero puede confundir en la UI;
conviene que la plantilla explique cuál señal disparó el nivel.

---

## Lo que está genuinamente bien hecho

- **Contención de fallos:** `execute()` captura todo, marca el job FAILED con
  `error_message`, re-lanza como `DocumentAnalysisError`; la tarea Celery
  distingue `PermissionDenied` / `DocumentAnalysisError` / `Exception`. Un
  documento malo revienta su propio job y nada más — sin 500, sin dejar el
  `Document` en PROCESSING colgado.
- **Robustez de los motores ante basura:** probado con 7 entradas degeneradas,
  cero excepciones y cero lentitud. Las guardas (`if not content.strip() or not
  candidates`) están puestas de forma consistente en los tres motores.
- **Coherencia numérica en los límites:** self≈100 %, sin-coincidencia=0 %, clamp
  a 100, fusión de rangos correcta (O(n log n)).
- **El arreglo de rendimiento del motor interno** (izar `candidate_shingles`,
  separar `all_matched_ranges` del `matches[:MAX]` truncado) es correcto y está
  bien documentado. El defecto es que `web_similarity` no recibió el mismo trato.
- **Fingerprint determinista:** blake2b en vez de `hash()` nativo (que varía por
  `PYTHONHASHSEED`), acotado a 63 bits para el `BIGINT` de Postgres. Bien pensado
  y bien comentado.
- **Winnowing fiel al paper** (mínimo por ventana, desempate a la derecha) y sin
  duplicar la construcción de shingles.
- **Degradación del detector de perplejidad:** si `torch`/`transformers` faltan o
  el modelo falla, se vuelve a la heurística sin romper el análisis; el flag está
  apagado por defecto en `settings.py`.
- **`_get_fingerprint_candidates`** ya usa `select_related("document",
  "document__owner")` — no hay N+1 al construir los candidatos internos — y excluye
  correctamente los propios chunks del documento y los trabajos previos del mismo
  alumno.

---

## Reproducciones

Scripts usados (pura lógica de motores, sin BD, sin red, sin tocar producción):
`scratchpad/repro.py`, `repro2.py`, `repro3.py`, `repro4.py`.
Comando: `source venv/bin/activate && python scratchpad/reproN.py`.
