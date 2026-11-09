# Resultado del motor de similitud por tramos (span)

Rama `feat/oai-texto-completo`, commit `c97908f` (paráfrasis ligera incluida). Mediciones del 2026-10-28.

Parámetros: `SIMILARITY_MIN_MATCH_WORDS=8`, `SIMILARITY_MAX_GAP_WORDS=2`, `SIMILARITY_MIN_SHORT_RUN=4`,
`SIMILARITY_MIN_FUZZY_SPAN=15`, `SIMILARITY_MIN_FUZZY_RATIO=0.7`, `SIMILARITY_MAX_DOCUMENT_FREQUENCY=50`,
`SIMILARITY_MAX_CANDIDATES=200`.

Suite completa (`DB_ENGINE=django.db.backends.sqlite3 python manage.py test`): 125 tests, OK.

## Benchmark (`python manage.py benchmark_similarity`)

Criterio: casos sintéticos ±3 puntos; casos originales ≤ 1 %; paráfrasis ≥ 60 %.
En la columna "fuentes", `f` = fuentes esperadas detectadas / esperadas y `+N` = fuentes no esperadas.

| caso | categoría | palabras | esperado | configuración | total | interno/OAI | web | criterio | fuentes | seg. |
|---|---|---:|---:|---|---:|---:|---:|---|---|---:|
| copia_100 | sintético | 770 | 100,00 | chunk+vieja | 99,98 | 99,98 | 0,00 | OK | 1/1 | 0,45 |
| copia_100 | sintético | 770 | 100,00 | chunk+nueva | 99,98 | 99,98 | 0,00 | OK | 1/1 | 0,38 |
| copia_100 | sintético | 770 | 100,00 | span | 100,00 | 100,00 | 0,00 | OK | 1/1 | 0,35 |
| copia_50 | sintético | 788 | 52,92 | chunk+vieja | 73,30 | 73,30 | 0,00 | FALLA | 1/1 | 0,32 |
| copia_50 | sintético | 788 | 52,92 | chunk+nueva | 73,30 | 73,30 | 0,00 | FALLA | 1/1 | 0,33 |
| copia_50 | sintético | 788 | 52,92 | span | 52,92 | 52,92 | 0,00 | OK | 1/1 | 0,33 |
| copia_10 | sintético | 1866 | 9,65 | chunk+vieja | 20,73 | 6,91 | 13,82 | FALLA | 1/1 +1 | 0,28 |
| copia_10 | sintético | 1866 | 9,65 | chunk+nueva | 20,73 | 6,91 | 13,82 | FALLA | 1/1 +1 | 0,35 |
| copia_10 | sintético | 1866 | 9,65 | span | 12,22 | 2,57 | 9,65 | OK | 1/1 +4 | 0,23 |
| frases_20 | sintético | 2202 | 9,08 | chunk+vieja | 23,94 | 17,42 | 6,51 | FALLA | 3/5 | 0,31 |
| frases_20 | sintético | 2202 | 9,08 | chunk+nueva | 23,94 | 17,42 | 6,51 | FALLA | 3/5 | 0,47 |
| frases_20 | sintético | 2202 | 9,08 | span | 10,17 | 8,36 | 1,82 | OK | 5/5 +3 | 0,35 |
| parafrasis_1de6 | paráfrasis | 758 | ≥ 60,00 | chunk+vieja | 99,98 | 99,98 | 0,00 | OK | 1/1 | 0,15 |
| parafrasis_1de6 | paráfrasis | 758 | ≥ 60,00 | chunk+nueva | 99,98 | 99,98 | 0,00 | OK | 1/1 | 0,23 |
| parafrasis_1de6 | paráfrasis | 758 | ≥ 60,00 | span | 99,60 | 99,60 | 0,00 | OK | 1/1 | 0,19 |
| original | original | 960 | 0,00 | chunk+vieja | 0,00 | 0,00 | 0,00 | OK | 0/0 | 0,15 |
| original | original | 960 | 0,00 | chunk+nueva | 0,00 | 0,00 | 0,00 | OK | 0/0 | 0,25 |
| original | original | 960 | 0,00 | span | 0,83 | 0,83 | 0,00 | OK | 0/0 +1 | 0,19 |
| misma_tematica | original | 916 | 0,00 | chunk+vieja | 0,00 | 0,00 | 0,00 | OK | 0/0 | 0,09 |
| misma_tematica | original | 916 | 0,00 | chunk+nueva | 0,00 | 0,00 | 0,00 | OK | 0/0 | 0,23 |
| misma_tematica | original | 916 | 0,00 | span | 0,00 | 0,00 | 0,00 | OK | 0/0 | 0,17 |

Notas:

- `parafrasis_1de6`: 1 de cada 6 palabras cambiada, así que la copia real es ≈ 83 %. El motor span da 99,60 % porque el
  bloque unido cuenta también las palabras cambiadas. Con solo tramos exactos de 8+ palabras daría 0 %. Los hallazgos
  quedan marcados como `match_type: "aproximada"` ("paráfrasis/coincidencia aproximada").
- Que chunk también pase en paráfrasis no indica precisión: ese motor da ≈ 100 % a cualquier fragmento parecido.
- Con la paráfrasis activada, "original" (0,83 %) y "misma temática" (0,00 %) no cambiaron.

## Mantilla con span

Documento `d0aca469-105e-4bc4-8fa7-330deea140c2` ("Proyecto Mantilla", `DTA_C5_MANTILLA_EDUCACIÓN PRIMARIA.docx`).
Análisis interno + OAI, **sin búsqueda web**.

| medida | valor |
|---|---|
| Palabras analizadas | ≈ 3.557 (25 citas excluidas) |
| Huellas del documento / raras (DF ≤ 50) | 1.299 / 1.214 |
| Consulta de documento-frecuencia (tabla de huellas, ≈ 21,1 M filas según la estimación de Postgres) | 0,28–0,30 s (caché de Postgres caliente) |
| Análisis span completo (recuperación + alineación) | 0,43 s (1,30 s con `tracemalloc` activo) |
| RAM pico del proceso (RSS) | 94 MB (88 MB antes del análisis: +6 MB) |
| Pico de memoria Python (`tracemalloc`) | 6 MB |
| **Similitud total** | **2,05 %** (interno/OAI 2,05 %, web 0,00 %) |

Tramos marcados (todos exactos, ninguno aproximado):

| # | tramo del documento | fuente (registro OAI) | % de la fuente |
|---|---|---|---:|
| 1 | "La metodología se desarrolló bajo un enfoque cualitativo de tipo" | UCV — *Revisión bibliográfica de estudiantes con trastorno del espectro autista…* · `oai:repositorio.ucv.edu.pe:20.500.12692/175082` · https://hdl.handle.net/20.500.12692/175082 | 0,51 |
| 2 | "se desarrolló bajo un enfoque cualitativo de tipo" | UCV — misma fuente que #1 (175082) | 0,51 |
| 3 | "las bases de datos Scopus, Web of Science y SciELO mediante" | UCV — *Factores asociados a la violencia doméstica: una revisión sistemática…* · `oai:repositorio.ucv.edu.pe:20.500.12692/94254` · https://hdl.handle.net/20.500.12692/94254 | 0,31 |
| 4 | "como técnica principal de recolección de datos. Se" | PUCP — *Percepciones sobre el uso pedagógico de la Inteligencia artificial en…* · `oai:tesis.pucp.edu.pe:20.500.12404/33151` · http://hdl.handle.net/20.500.12404/33151 | 0,23 |
| 5 | "el desarrollo de la competencia comunicativa oral en" | USIL — *Retroalimentación integrativa contextual para potenciar la competencia…* · `oai:repositorio.usil.edu.pe:20.500.14005/16733` · https://hdl.handle.net/20.500.14005/16733 | 0,23 |
| 6 | "garantizar una educación de calidad, inclusiva y equitativa" | UCV — *Alfabetización científica y competencias de indagación en niños de 5 años…* · `oai:repositorio.ucv.edu.pe:20.500.12692/177478` · https://hdl.handle.net/20.500.12692/177478 | 0,23 |
| 7 | "la influencia de los juegos tradicionales en el desarrollo de la" | UCV — *Los juegos tradicionales en el desarrollo de la psicomotricidad en inf…* · `oai:repositorio.ucv.edu.pe:20.500.12692/107386` · https://hdl.handle.net/20.500.12692/107386 | 0,31 |
| 8 | "de la Teoría Sociocultural de Vygotsky y el" | USIL — *Trabajo colegiado de docentes para mejorar el nivel de resolución de p…* · `oai:repositorio.usil.edu.pe:20.500.14005/4134` · https://hdl.handle.net/20.500.14005/4134 | 0,23 |

Los títulos terminados en "…" están truncados en la salida de la medición. Todos los tramos son fórmulas académicas
habituales. Queda pendiente decidir si `MIN_MATCH_WORDS=8` es el umbral adecuado.

## Caso "original" del benchmark (span 0,83 %)

- Tramo coincidente (8 palabras, exacto): **"de las distintas escuelas profesionales de la universidad"**
- Fuente: registro OAI `oai:repositorio.unap.edu.pe:20.500.14082/20012`, *"Engagement académico y gestión del tiempo en
  estudiantes universitarios"* (Repositorio UNAP), https://repositorio.unap.edu.pe/handle/20.500.14082/20012
- Texto en la fuente: "de las distintas escuelas profesionales de la Universidad"
- Es una coincidencia real pero trivial. Aparece como fuente no esperada (`+1`) y queda dentro del criterio (≤ 1 %).
