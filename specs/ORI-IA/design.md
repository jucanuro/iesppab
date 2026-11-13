Estado: Aprobado por Juan Carlos Núñez (Gerente), 2026-11-13

# ORI-IA · Design v3 — Detector calibrado con textos mixtos

> Base: `requirements.md` v3.1 (Aprobado por Juan Carlos Núñez (Gerente), 2026-11-13) y el borrador AS-IS anterior.
> Marcas: **[Existe]** = ya está en el código (leído 2026-11-13); **[Nuevo]** = lo propone este diseño.
> Etiquetas de hecho: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto].

## 0. Punto de partida (AS-IS) [Existe]

- Motor vigente: BETO afinado `dccuchile/bert-base-spanish-wwm-cased`, `max_length` 384, 3 épocas, en `~/models/clasificador_ia_v2` (`AI_CLASSIFIER_MODEL_DIR`). `apps/analysis/engines/ai_classifier.py` → `SegmentAIClassifier(model_dir).analyze(...)`; `unload(model_dir)` libera memoria. [Supuesto] (lectura de código y `umbral.json`)
- % IA vigente = % de palabras evaluadas en trozos con probabilidad ≥ `umbral` de `umbral.json` (**0,0014**, elegido para ≤5 % FP en humanos de validación). Que el umbral útil sea 0,0014 indica que **las probabilidades crudas no están calibradas**. [Supuesto]
- Resultado guardado en `AnalysisReport.summary["ai_score_breakdown"]["classifier"]`; `ai_probability_percent` y `ai_detector="classifier-beto-v1"`; bandas en `reports.models.ai_classifier_band`; el riesgo no depende de la IA (RN-IA-01). [Supuesto]
- Datos: `exportar_dataset_ia_v2` (semilla 20261110; particiones train/val/test, `test_wiki`, `control` 20 UPeU ≤2021, `test_reciente` 2022+, `test_chat`); `dataset/resumen.json`; IA ya generada en `eval_ia/ia_api/{claude,gemini}` y `eval_ia/ia`; chat en `eval_ia/ia_externa` (ChatGPT/Gemini). [Supuesto]
- **Evidencia para V-01:** `umbral.json` contiene experimentos `final->chat (60 + 6 docx)` con dos umbrales (0,0004 y 0,0014). Indica que los 60 textos de chat se evaluaron *antes* de fijar el umbral v2 → previsiblemente "vistos". V-01 lo confirma formalmente. [Supuesto]

## 1. Modelo de datos

### 1.1 Base de datos

| Elemento | Detalle | Estado | Etiqueta |
|---|---|---|---|
| `reports.AIShadowEstimate` (TimeStampedModel) | `report` FK→`AnalysisReport` (CASCADE, `related_name="ai_shadow_estimates"`), `detector` Char(40) (p. ej. `classifier-beto-v3`), `model_hash` Char(64), `ai_percent` Decimal(5,2) null, `evaluable` Bool null, `breakdown` JSON (por trozo: offsets, palabras, probabilidad calibrada), `duration_ms` Int, `peak_rss_mb` Int null, `error` Text blank. Índices: (`detector`, `created_at`). `unique_together` (`report`, `detector`) | [Nuevo] | [Propuesto] |
| Migración | `apps/reports/migrations/00NN_aishadowestimate.py`: solo crea la tabla; sin cambios en tablas existentes; reversible | [Nuevo] | [Propuesto] |
| `AnalysisReport` | Sin cambios de campos. El % visible sigue leyendo solo `ai_score_breakdown["classifier"]` | [Existe] | [Supuesto] |

Admin: `AIShadowEstimate` solo lectura, visible solo para superusuario. [Propuesto]

### 1.2 Banco de evaluación (fuera de git, manifiesto en git)

| Elemento | Detalle | Estado | Etiqueta |
|---|---|---|---|
| Manifiesto `apps/analysis/evaluation/ia_v3/manifiesto.json` (en git) | Una entrada por texto fuente: `id`, `ruta` relativa a `eval_ia/`, `sha256`, `etiqueta` (humano/ia), `familia` (humano, claude_api, gemini_api, chatgpt_chat, gemini_chat y las familias de chat nuevas de §1.3), `anio`, `institucion`, `publico` (bool), `particion` (train/val/test/sellado/no_vistos/diagnostico), `verificado` (bool + criterio), `visto` (bool, solo IA de prueba), `fuente_url` y `licencia` (humanos, §1.4), `generado_por`/`fecha_generacion`/`chat` (IA de §1.3) | [Nuevo] | [Propuesto] |
| Banco no vistos `eval_ia/no_vistos_v3/<familia>/<id>.txt` (fuera de git) | ≥60 documentos IA puros de ≥3 familias de LLM, generados a mano por el Gerente en chats gratuitos (§1.3) | [Nuevo] | [Decisión Gerente] |
| Mixtos `eval_ia/mixtos_v3/<particion>/<id>.json` (fuera de git) | `texto`, `pct_real_ia`, `tramos` [{inicio, fin, origen_id, etiqueta}], `semilla`, `sha256` | [Nuevo] | [Propuesto] |
| Informes `eval_ia/_informes/ia_v3_<fecha>_<commit>.{json,txt}` | Métricas + commit + hash banco + hash modelo + semilla + veredictos | [Nuevo] | [Propuesto] |
| Sello `eval_ia/_informes/sello_chat_v3.json` | Fecha de apertura, commit, hash modelo, "vistos" sí/no (según V-01) | [Nuevo] | [Propuesto] |
| Modelo `~/models/clasificador_ia_v3/` | Pesos + `calibracion.json`: `temperatura`, `estimador` (umbral/esperanza), `umbral_doc`=20, `tau` (si estimador umbral), `hash_banco`, `semilla`, `commit` | [Nuevo] | [Propuesto] |

Asignación de particiones (antes de mezclar, por texto fuente, semilla fija `IA_V3_SEED`):
- Humanos públicos ≤2021 verificados (Decisión 2): train / val / **test_fpr (≥183)**. Wikipedia: solo train/val, nunca en la meta. [Decisión Gerente]
- IA Claude/Gemini API ya generada: train / val / test. [Decisión Gerente] (RN-IA-11)
- 60 textos de chat existentes (ChatGPT 30 + Gemini 30): `sellado`, con `visto=true` salvo que V-01 concluya "no usados" (V-01 se ejecuta y se documenta antes de cualquier evaluación). Se informan aparte y **no** cuentan para la meta de recall en no vistos. [Decisión Gerente]
- ≥60 documentos nuevos de §1.3: partición `no_vistos`, nunca en train/val ni en Colab; sobre ellos se mide el **recall en LLM no vistos** (REQ-IA-07), una sola vez, con el mismo mecanismo de sello que REQ-IA-17. [Decisión Gerente] (conjunto) / [Propuesto] (sello)
- Humanos ≥2022 y tesis del instituto: `diagnostico`, nunca en Colab. [Decisión Gerente]

### 1.3 Conjunto de LLM no vistos [Decisión Gerente]
- Tamaño y diversidad: **≥60 documentos de ≥3 familias de LLM** distintas de las de entrenamiento (Claude API, Gemini API); ChatGPT puede ser una de ellas. Las familias concretas las elige el Gerente. [Decisión Gerente]
- Generación: **a mano por el Gerente en chats gratuitos** (sin API, sin Claude). Es la única excepción a RN-IA-11, decidida por el Gerente el 2026-11-13 y recogida en requirements.md v3.1 (RN-IA-11, Decisión 9). [Decisión Gerente]
- Registro por documento: familia, nombre del chat y versión visible, fecha, consigna usada (archivada junto al texto), `sha256`. Las consignas se redactan como temas académicos plausibles del instituto, sin texto de tesis reales. [Propuesto]
- Meta: recall ≥70 % (REQ-IA-07) sobre este conjunto, con IC 95 % y desglose por familia; si alguna familia tiene <15 documentos se informa pero se marca "n bajo". [Decisión Gerente] (meta) / [Propuesto] (n bajo)

### 1.4 Humanos de ≥3 instituciones (plan de obtención) [Decisión Gerente]
- Fuentes: **descargas manuales** de repositorios institucionales y **agregadores abiertos (CORE, SciELO, ALICIA-CONCYTEC)**. [Decisión Gerente]
- Reglas: respetar la licencia de cada texto (registrada en `licencia`; solo acceso abierto) y el `robots.txt` de cada host; **no se cosecha ningún host que lo bloquee** (se descarta o se descarga a mano solo si la licencia lo permite). Sin APIs de pago; los agregadores se consultan por su interfaz pública o API abierta, con ritmo bajo y User-Agent genérico. [Decisión Gerente] / [Propuesto] (ritmo, User-Agent)
- Criterios: fecha de depósito ≤2021, prosa evaluable ≥ el mínimo de producción, una tesis por autor, distribución equilibrada entre instituciones (ninguna >50 % de test_fpr). [Propuesto]
- `harvest_oai_fulltext` solo se usa con los endpoints ya confirmados en `OAI_HARVEST_REPOSITORIES`. [Existe]

## 2. Componentes / servicios

| # | Componente | Responsabilidad | Estado | Etiqueta |
|---|---|---|---|---|
| C1 | `apps/analysis/ia_v3/manifest.py` | Cargar/validar manifiesto; verificar SHA-256 (falta o no coincide → `IABankError` con el id); comprobar que ningún texto fuente esté en dos particiones; contar humanos test_fpr e instituciones (≥183 / ≥3, si no → "insuficiente") | [Nuevo] | [Propuesto] |
| C2 | `apps/analysis/ia_v3/mixtos.py` | Construir mixtos por **bloques de párrafos intercalados** con objetivo 0/10/…/100 % de IA; `pct_real_ia` = palabras IA / palabras evaluables tras `AcademicTextFilter` (mismo filtro que producción); determinista con semilla. Solo mezcla fuentes de la misma partición | [Nuevo] | [Decisión Gerente] (método) / [Propuesto] (código) |
| C3 | `apps/analysis/ia_v3/metrics.py` | FPR + **Clopper-Pearson bilateral** (cota superior al 97,5 %, por bisección de la CDF binomial en Python puro, sin scipy); MAE y p90; recall por familia con marca vista/no vista; curva de fiabilidad de 10 tramos y ECE; veredictos frente a 2 % / 10 pts / 70 % / 0,05 | [Nuevo] | [Decisión Gerente] (metas) / [Propuesto] (implementación) |
| C4 | `apps/analysis/engines/ai_classifier.py` | Ampliar: si existe `calibracion.json`, aplicar **escalado de temperatura** a los logits y calcular el % con el estimador congelado; si no existe, comportamiento actual (v2 intacto). Añadir un plazo cooperativo (`deadline`) comprobado entre lotes | [Existe] + cambio | [Propuesto] |
| C5 | `notebooks/entrenar_clasificador_ia_v3.ipynb` | Colab T4: reentrenar BETO sobre trozos puros de train (humanos públicos + IA ya generada); puntos de control en Drive cada época; en val: ajustar temperatura (NLL), elegir estimador y `tau` (regla en §2.1); exportar modelo + `calibracion.json`. Aborta si el manifiesto contiene algún texto con `publico=false` | [Nuevo] | [Decisión Gerente] (Colab T4, sin tesis del instituto) / [Propuesto] |
| C6 | Shadow en `DocumentAnalysisService._estimate_ai_probability` | Tras calcular el % oficial, si `AI_SHADOW_ENABLED`: opcionalmente `unload` del v2, ejecutar v3 dentro de `try/except` con plazo, medir duración y RSS pico, guardar `AIShadowEstimate`. Cualquier error → `logger.warning` + fila con `error`; el reporte sigue igual | [Existe] + cambio | [Decisión Gerente] (sombra) / [Propuesto] |

### 2.1 Regla de elección del estimador (se fija en val, se congela antes de test) [Propuesto]
Dos estimadores del % sobre trozos con probabilidad calibrada p y palabras w:
- **E1 (umbral):** Σw[p ≥ τ] / Σw, con τ = el menor valor que deja la tasa de documentos humanos de val con % ≥ 20 en ≤1 %.
- **E2 (esperanza):** Σ p·w / Σ w.
Se elige el que tenga menor MAE en mixtos de val **cumpliendo** FPR de documento en val ≤1 %; empate → E1 (continuidad con v2 y con el resaltado por trozo de HU-IA-08).

## 3. Comandos de gestión (sin vistas ni URLs nuevas)

| Comando | Qué hace | REQ | Estado |
|---|---|---|---|
| `construir_banco_ia_v3 [--para-colab]` | Valida manifiesto (C1), genera mixtos (C2), exporta JSONL por partición; `--para-colab` excluye `sellado`/`diagnostico` y aborta si hay `publico=false` | 01–04, 16 | [Nuevo] |
| `evaluar_ia_v3 --modelo DIR [--particion val\|test] [--sellado]` | Ejecuta el modelo con el **mismo pipeline de producción** (extractor + filtro + `SegmentAIClassifier`) sobre la partición indicada (test_fpr, mixtos, IA) y diagnóstico; escribe informe. Sirve también para v2 (sin `calibracion.json`). `--sellado` evalúa además `sellado` (60 de chat, marcados "vistos") y `no_vistos` (§1.3): primera vez crea el sello; siguientes se marcan "no sellada" | 01, 02, 05–08, 13, 17 | [Nuevo] |
| `comparar_v2_v3 --v2 INFORME --v3 INFORME` | Aplica el criterio de parada de §8 sobre informes de **val** y emite CONTINUAR / DETENER | §8 | [Nuevo] |
| `comparar_ia_sombra [--desde FECHA]` | Sobre `AIShadowEstimate` + `AnalysisReport`: distribución de ambos %, diferencia por documento, lista de documentos con diferencia > 20 pts, nº de errores/tiempos, y estado del criterio de salida (≥4 semanas **y** ≥100 documentos) | 11 | [Nuevo] |
| `medir_recursos_ia --modelo DIR docs…` | Pico de RSS (`resource.getrusage`) y s/1000 palabras sobre 5 documentos; veredicto ≤1,5 GB / ≤30 s | 12 | [Nuevo] |
| `evaluate_ai_classifier`, `exportar_dataset_ia_v2`, `scripts/eval_ia.py` | Se conservan para v2; no se modifican | — | [Existe] |

Vistas/URLs: **ninguna nueva**. Plantillas: sin cambios; el test de REQ-IA-09 comprueba que el % v3 no aparece en `reports:detail`, PDF de IA, certificado ni `certificates:verify`. [Propuesto]

## 4. Flags y constantes

| Nombre | Dónde | Defecto | Estado | Etiqueta |
|---|---|---|---|---|
| `AI_DETECTION_MODE` | settings | `global` (prod: `classifier`) | [Existe] | [Supuesto] |
| `AI_CLASSIFIER_MODEL_DIR` | settings | `~/models/clasificador_ia` | [Existe] | [Supuesto] |
| `AI_SHADOW_ENABLED` | settings/env | `False` | [Nuevo] | [Propuesto] |
| `AI_SHADOW_MODEL_DIR` | settings/env | `~/models/clasificador_ia_v3` | [Nuevo] | [Propuesto] |
| `AI_SHADOW_TIMEOUT_SECONDS` | settings/env | `300` (≈30 s/1000 palabras × 10 000) | [Nuevo] | [Propuesto] |
| `AI_SHADOW_UNLOAD_PRIMARY` | settings/env | `True` (descarga v2 antes de cargar v3 para no tener dos modelos residentes) | [Nuevo] | [Propuesto] |
| `AI_DETECTOR_CLASSIFIER_V3` | `DocumentAnalysisService` | `"classifier-beto-v3"` | [Nuevo] | [Propuesto] |
| `IA_V3_SEED` | `ia_v3/manifest.py` | `20261113` | [Nuevo] | [Propuesto] |
| `FPR_META=0.02`, `DOC_UMBRAL_PCT=20`, `MIN_HUMANOS=150`, `MIN_INSTITUCIONES=3`, `MAE_META=10`, `RECALL_NO_VISTOS_META=0.70`, `ECE_META=0.05`, `N_TRAMOS=10`, `SOMBRA_MIN_SEMANAS=4`, `SOMBRA_MIN_DOCS=100`, `RAM_MAX_MB=1536`, `SEG_POR_1000_MAX=30` | `ia_v3/metrics.py` | — | [Nuevo] | [Decisión Gerente] |

Rollback: `AI_SHADOW_ENABLED=False` (sombra fuera, sin migración inversa necesaria). La activación de v3 como visible (cambiar `AI_CLASSIFIER_MODEL_DIR` y `ai_detector`) queda **fuera de alcance** y requiere decisión del Gerente (RN-IA-14). [Decisión Gerente]

## 5. Matriz REQ → diseño

| REQ | Elemento de diseño | Verificación prevista |
|---|---|---|
| REQ-IA-01 | Manifiesto (partición test_fpr, `verificado`, `institucion`) + C1 (≥183 / ≥3 → "insuficiente") | Test C1 con 149/150/183 entradas |
| REQ-IA-02 | Partición `diagnostico` + sección aparte en `evaluar_ia_v3` | Test: un 2022 no cambia el FPR de la meta |
| REQ-IA-03 | C2 `mixtos.py` + `construir_banco_ia_v3` | Test de determinismo (hash idéntico con la misma semilla) |
| REQ-IA-04 | C1 control de fuga antes de mezclar | Test: fuente en dos particiones → `IABankError` |
| REQ-IA-05 | C3 MAE/p90 + `evaluar_ia_v3` | Test con MAE 9,9 / 10,1 |
| REQ-IA-06 | C3 Clopper-Pearson bilateral, umbral doc 20 % | Test: 0/183 → CUMPLE; 1/183 → NO CUMPLE |
| REQ-IA-07 | Partición `no_vistos` (≥60 docs, ≥3 familias, §1.3) + C3 recall por familia con IC; los 60 existentes en `sellado` como "vistos" | Test del informe por familia; test: <60 docs o <3 familias → "insuficiente" |
| REQ-IA-08 | C4 temperatura + C3 curva 10 tramos + ECE | Test con predicciones calibradas / sesgadas |
| REQ-IA-09 | `AIShadowEstimate` + C6 + flags `AI_SHADOW_*`; plantillas sin cambios | Test de servicio + test de vistas/PDF |
| REQ-IA-10 | C6 `try/except` + plazo cooperativo en C4 | Test: v3 lanza excepción / supera plazo → reporte íntegro |
| REQ-IA-11 | `comparar_ia_sombra` | Test sobre filas sintéticas |
| REQ-IA-12 | `AI_SHADOW_UNLOAD_PRIMARY` + `medir_recursos_ia` + `peak_rss_mb` en sombra | Medición en servidor (manual) |
| REQ-IA-13 | Manifiesto con SHA-256 + informe con commit/hashes/semilla | Test: dos corridas → métricas idénticas |
| REQ-IA-14 | Solo textos ya generados; sin dependencias/variables de API nuevas | Revisión manual en PR (No verificable automáticamente) |
| REQ-IA-15 | C5 notebook con puntos de control en Drive | Registro de ejecución en el informe de entrenamiento |
| REQ-IA-16 | `publico` en manifiesto + `--para-colab` + verificación en C5 | Test: texto `publico=false` → aborta |
| REQ-IA-17 | Partición `sellado` + `sello_chat_v3.json` + marca "vistos" según V-01 | Test: segunda corrida → "no sellada" |
| V-01 | Revisión de `umbral.json` + notebook v2 (evidencia en §0) | Informe breve al Gerente |

Todos los REQ-IA-01…17 quedan cubiertos. [Propuesto]

## 6. Riesgos

| # | Riesgo | Mitigación en diseño | Etiqueta |
|---|---|---|---|
| D1 | No hay 183 humanos públicos ≤2021 verificados de ≥3 instituciones (hoy son mayoritariamente UPeU) | C1 marca "insuficiente"; plan de §1.4 (descargas manuales + CORE, SciELO, ALICIA-CONCYTEC, respetando licencias y robots.txt) antes de entrenar | [Decisión Gerente] |
| D2 | Dos modelos BETO (~420 MB c/u) + app + extracción PDF superan 1,5 GB | `AI_SHADOW_UNLOAD_PRIMARY` (secuencial, más latencia por recarga); medir con `medir_recursos_ia` antes de activar la sombra | [Supuesto] |
| D3 | El plazo cooperativo no corta un lote de inferencia ya en curso | Lotes de 2 trozos (`BATCH_SIZE`) → retraso acotado; sin hilos ni señales | [Propuesto] |
| D4 | Mixtos sintéticos por bloques son más fáciles que la reescritura real | Se informa MAE por proporción y por longitud de bloque; los documentos `dudoso` del instituto quedan en diagnóstico | [Supuesto] |
| D5 | Recall en no vistos con n≈60 y una única medición; la generación manual depende del tiempo del Gerente | Informar IC 95 % y desglose por familia; la tarea de generación va en paralelo al entrenamiento y bloquea solo la evaluación final | [Propuesto] |
| D9 | v3 no supera a v2 | Criterio de parada de §8: se detiene y se mantiene v2 | [Decisión Gerente] |
| D10 | Desplegar juntas la migración de sombra y la corrección de extracción mezcla dos cambios de riesgo | Despliegues separados con respaldo previo (§9) | [Decisión Gerente] |
| D6 | V-01 confirma "vistos" (evidencia en §0) | Decisión 8: se usan igual, marcados "vistos"; el informe lo advierte | [Decisión Gerente] |
| D7 | Colab corta la sesión | Puntos de control por época en Drive; solo datos públicos | [Supuesto] |
| D8 | Diferencia PDF vs DOCX en extracción | La evaluación usa el pipeline de producción; los mixtos son texto plano → se añade un subconjunto de humanos test en PDF para medir el efecto | [Supuesto] |

## 7. Alternativas descartadas

| Alternativa | Motivo | Etiqueta |
|---|---|---|
| Binoculars / perplejidad GPT-2 como motor | Pico 2,2–2,7 GB en un servidor de 2 GB; latencia ~10× | [Decisión Gerente] (RAM) / [Supuesto] (cifras) |
| Detectores comerciales por API | Prohibido: sin APIs de pago nuevas | [Decisión Gerente] |
| Generar textos de IA nuevos por API o con Claude | Reutilizar los ya generados; Claude solo para programar. Única excepción: el conjunto no vistos de §1.3, generado a mano por el Gerente en chats gratuitos | [Decisión Gerente] |
| Regresor que predice el % por documento directamente | Pierde el resaltado por trozo (HU-IA-08) y la curva de fiabilidad por segmento | [Propuesto] |
| Guardar la sombra en `summary["ai_score_breakdown"]` | Riesgo de que una plantilla lo lea por error; más difícil de consultar y purgar | [Propuesto] |
| Calibración isotónica | Con pocos datos de val tiende a sobreajustar; la temperatura tiene 1 parámetro | [Propuesto] |
| Reutilizar el umbral 0,0014 de v2 | Elegido mirando los 60 de chat (§0) y sin calibrar | [Supuesto] |

## 8. Criterio de parada [Decisión Gerente]
- Tras entrenar y calibrar, v2 y v3 se evalúan con `evaluar_ia_v3 --particion val` sobre **el mismo banco de validación** (humanos val + mixtos val). [Decisión Gerente]
- v3 **supera** a v2 solo si: FPR de documento en val (≥20 %) de v3 ≤ el de v2 **y** MAE en mixtos val de v3 < el de v2. [Decisión Gerente] (criterio) / [Propuesto] (definición operativa: empate en FP permitido, MAE estrictamente menor)
- Si no lo supera: **se detiene v3**, no se abre el sello, no se despliega la sombra y se mantiene v2; el informe `comparar_v2_v3` queda como evidencia. Solo si supera se pasa a test, sellado y sombra. [Decisión Gerente]

## 9. Despliegue [Decisión Gerente]
- La migración de `AIShadowEstimate` se despliega **por separado** de la corrección de extracción (rama `fix/extraccion-pdf-palabra-por-linea`), en ventanas distintas. [Decisión Gerente]
- Antes de aplicar la migración en producción: respaldo de la base de datos (`pg_dump`) verificado (restaurable, tamaño > 0). La ejecuta el Gerente; Claude no toca producción. [Decisión Gerente]
- Orden: (1) corrección de extracción, (2) migración con `AI_SHADOW_ENABLED=False`, (3) modelo v3 en el servidor + `medir_recursos_ia`, (4) activar la sombra. Rollback de (2): `migrate reports <anterior>` tras respaldo. [Propuesto]

## Historial
| Versión | Fecha | Autor | Cambio |
|---|---|---|---|
| v0 | 2026-11-06 | Claude PM | Borrador AS-IS (solo lectura del código) |
| v3-borrador | 2026-11-13 | Claude PM | Diseño v3 sobre requirements v3 aprobado; AS-IS integrado en §0 |
| v3 | 2026-11-13 | Claude PM | Aprobado por el Gerente con 4 ajustes: conjunto no vistos ≥60 docs/≥3 familias (§1.3), criterio de parada (§8), despliegue separado con respaldo (§9), plan de humanos de ≥3 instituciones (§1.4) |
| v3.1 | 2026-11-13 | Claude PM | §1.3 y base remiten a requirements v3.1 (RN-IA-11 con excepción `no_vistos`) |
