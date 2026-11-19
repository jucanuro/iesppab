Estado: Aprobado por Juan Carlos Núñez (Gerente), 2026-11-13

# ORI-IA · Requisitos v3 — Detector calibrado con textos mixtos

> Fuente: `docs/proc/IA.md` (v2, G1 aprobado 2026-11-08) + alcance v3 indicado por el Gerente en conversación (2026-11-13).
> Sin este archivo aprobado (G1 v3) no hay design.md, tasks.md ni código.
> Etiquetas: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto].

## 1. Contexto y alcance

**Problema.** El % de IA actual (clasificador BETO por segmentos, D-02) se evaluó sobre textos *puros* (100 % humano o 100 % IA). Un documento real de un estudiante suele ser **mixto** (parte escrita a mano, parte generada o reescrita con IA), y hoy no se sabe cuánto se equivoca el % en ese caso ni si su probabilidad está calibrada. [Supuesto]

**Objetivo v3.** Un detector cuyo % de IA (a) casi no marque textos humanos, (b) se acerque al % real de texto IA en documentos mixtos, (c) generalice a LLM no vistos en el entrenamiento y (d) tenga probabilidades calibradas; y que entre a producción primero en **modo sombra**. [Propuesto]

**Entra:**
- Banco de documentos mixtos con % IA conocido, construido a partir de textos humanos ≤2021 y de los textos de IA **ya generados**. [Propuesto]
- Conjunto de prueba `no_vistos`: ≥60 documentos IA de ≥3 familias de LLM, generados a mano por el Gerente en chats gratuitos (única excepción a RN-IA-11). [Decisión Gerente]
- Reentrenamiento/recalibración del detector en Colab gratuito (T4). [Decisión Gerente]
- Evaluación reproducible con FPR + IC 95 %, error absoluto en mixtos, recall por familia de LLM no vista y curva de fiabilidad. [Propuesto]
- Modo sombra en producción (calcula y guarda v3 sin mostrarlo). [Decisión Gerente]

**No entra:**
- Cambiar la presentación del % ya aprobada en v2 (etiqueta "IA estimada", banda, nota; RN-IA-07). [Decisión Gerente]
- Que el % de IA afecte riesgo o certificado (RN-IA-01). [Decisión Gerente]
- APIs de pago nuevas, generación de textos nuevos con LLM (salvo la partición `no_vistos`, RN-IA-11), Binoculars/perplejidad en producción. [Decisión Gerente]
- Activar v3 como motor visible: requiere decisión aparte del Gerente tras el modo sombra. [Propuesto]

## 2. Historias de usuario (v3)

| ID | Historia | Etiqueta |
|---|---|---|
| HU-IA-09 | Como **docente** quiero que el % de IA de un documento que solo tiene una parte hecha con IA se acerque a esa parte real, para no sobre- ni sub-reaccionar | [Propuesto] |
| HU-IA-10 | Como **docente** quiero que una tesis escrita sin IA casi nunca salga marcada, para no acusar injustamente a un estudiante | [Propuesto] (refina HU-IA-03/07) |
| HU-IA-11 | Como **Gerente** quiero saber si el detector sigue funcionando con LLM que no se usaron para entrenarlo | [Propuesto] |
| HU-IA-12 | Como **Gerente** quiero que una probabilidad del 70 % signifique de verdad ~70 % de acierto (calibración) | [Propuesto] |
| HU-IA-13 | Como **Gerente** quiero probar v3 en producción sin que los usuarios lo vean, comparándolo con el motor actual, antes de decidir activarlo | [Propuesto] |
| HU-IA-14 | Como **Gerente** quiero entrenar y evaluar sin gastar en APIs ni infraestructura y sin sacar tesis del instituto | [Decisión Gerente] |

## 3. Trazabilidad HU → REQ

| HU | REQ | Etiqueta |
|---|---|---|
| HU-IA-09 | REQ-IA-03, REQ-IA-04, REQ-IA-05 | [Propuesto] |
| HU-IA-10 | REQ-IA-01, REQ-IA-02, REQ-IA-06 | [Propuesto] |
| HU-IA-11 | REQ-IA-07 | [Propuesto] |
| HU-IA-12 | REQ-IA-08 | [Propuesto] |
| HU-IA-13 | REQ-IA-09, REQ-IA-10, REQ-IA-11 | [Propuesto] |
| HU-IA-14 | REQ-IA-12 … REQ-IA-17 | [Decisión Gerente] |
| HU-IA-02 (v2) | REQ-IA-13 | [Decisión Gerente] |

## 4. Requisitos funcionales (EARS)

### Conjuntos de datos

#### REQ-IA-01 — Conjunto humano de prueba de FPR
- **HU:** HU-IA-10
- **Criterio:** EL SISTEMA DEBE evaluar el FPR solo sobre un conjunto de **≥150 documentos humanos verificados con fecha ≤2021**, que no se usen en entrenamiento ni en la elección del umbral; SI el conjunto tiene <150, ENTONCES el informe DEBE marcar la evaluación como "insuficiente" (AC-IA-07).
- **Verificación:** test del comando de evaluación con manifiesto de 149 y 150 documentos.
- **Etiqueta:** [Decisión Gerente] (meta D-10). "Verificado" = tesis de repositorios públicos peruanos con fecha de depósito ≤2021, de ≥3 instituciones, sin Wikipedia en la meta; con IC bilateral el mínimo práctico es 183 → [Decisión Gerente] (Decisión 2).

#### REQ-IA-02 — Humanos 2022+ solo diagnóstico
- **HU:** HU-IA-10
- **Criterio:** DONDE un documento humano tenga fecha ≥2022, EL SISTEMA DEBE excluirlo del entrenamiento, de la calibración y del cálculo de la meta de FPR, y reportarlo en una sección aparte "diagnóstico".
- **Verificación:** test: un documento 2022 en el manifiesto no cambia el FPR de la meta y aparece en la sección diagnóstico.
- **Etiqueta:** [Decisión Gerente]

#### REQ-IA-03 — Banco de documentos mixtos
- **HU:** HU-IA-09
- **Criterio:** EL SISTEMA DEBE construir documentos mixtos combinando párrafos humanos ≤2021 con párrafos de los textos de IA ya generados, registrando por documento el **% real de IA** (palabras IA / palabras evaluables), el origen de cada tramo y una semilla, con proporciones que cubran 0–100 %.
- **Verificación:** test: regenerar con la misma semilla produce los mismos documentos y el mismo % real (hash idéntico).
- **Etiqueta:** [Decisión Gerente] (Decisión 3): mezcla por bloques de párrafos intercalados con proporciones 0/10/…/100 %.

#### REQ-IA-04 — Particiones sin fuga
- **HU:** HU-IA-09
- **Criterio:** EL SISTEMA DEBE asignar cada texto fuente (humano o IA) a una sola partición (entrenamiento / validación / prueba) antes de mezclar, de modo que ningún párrafo de prueba aparezca en un documento de entrenamiento; SI detecta un texto fuente en dos particiones, ENTONCES DEBE abortar indicando cuál.
- **Verificación:** test con un texto fuente duplicado entre particiones → aborta.
- **Etiqueta:** [Propuesto] (ya existe `documentos_en_varias_particiones` en `dataset/resumen.json`) [Supuesto].

### Métricas y evaluación

#### REQ-IA-05 — Error en documentos mixtos
- **HU:** HU-IA-09
- **Criterio:** CUANDO termina la evaluación sobre la partición de prueba de mixtos, EL SISTEMA DEBE reportar el error absoluto |% estimado − % real| por documento, su media (MAE) y su percentil 90, y el veredicto CUMPLE si **MAE ≤ 10 puntos**.
- **Verificación:** test con estimaciones simuladas de MAE conocido (9,9 → CUMPLE; 10,1 → NO CUMPLE).
- **Etiqueta:** [Decisión Gerente] (≤10 puntos; la meta es el MAE y el p90 solo se reporta — Decisión 3).

#### REQ-IA-06 — FPR con intervalo de confianza
- **HU:** HU-IA-10
- **Criterio:** CUANDO termina la evaluación, EL SISTEMA DEBE reportar n, número de falsos positivos, FPR y la **cota superior del IC 95 %** (Clopper-Pearson) sobre el conjunto de REQ-IA-01, y el veredicto CUMPLE solo si esa cota es **≤ 2 %**. Un documento humano cuenta como falso positivo si su % de IA ≥ el umbral de documento acordado.
- **Verificación:** test: 0 FP de 183 → cota bilateral 1,99 % → CUMPLE; 1 FP de 183 → NO CUMPLE.
- **Etiqueta:** [Decisión Gerente] (≤2 %, IC 95 %, D-10; Clopper-Pearson **bilateral**; umbral de documento **20 %** — Decisión 1).

#### REQ-IA-07 — Recall en LLM no vistos
- **HU:** HU-IA-11
- **Criterio:** EL SISTEMA DEBE reportar el recall (documentos IA con % ≥ umbral de documento / total), con IC 95 %, **por familia de LLM**, separando las familias usadas en entrenamiento de las **no vistas**, y dar el veredicto frente a la meta de recall sobre la partición `no_vistos` (≥60 documentos de ≥3 familias, ninguno en entrenamiento, validación ni Colab); SI `no_vistos` tiene <60 documentos o <3 familias, ENTONCES el informe DEBE marcar la evaluación como "insuficiente". Los 60 textos de chat existentes (REQ-IA-17) se informan aparte y no cuentan para esta meta.
- **Verificación:** test: el informe lista cada familia con su marca "vista/no vista" y su recall; `no_vistos` con <60 documentos o <3 familias → "insuficiente".
- **Etiqueta:** [Decisión Gerente] (Decisiones 4 y 9): meta **recall ≥ 70 %** en documentos IA puros sobre el umbral de documento, medida sobre `no_vistos`. Familias de entrenamiento hoy: Claude API y Gemini API [Supuesto, según `dataset/resumen.json`].

#### REQ-IA-08 — Calibración
- **HU:** HU-IA-12
- **Criterio:** CUANDO termina la evaluación, EL SISTEMA DEBE generar una **curva de fiabilidad** por segmento (10 tramos de probabilidad: probabilidad media predicha vs. fracción real de IA, con n por tramo) y el **ECE**; veredicto CUMPLE si ECE ≤ 0,05.
- **Verificación:** test con predicciones perfectamente calibradas (ECE ≈ 0) y sesgadas (ECE > 0,05).
- **Etiqueta:** [Decisión Gerente] (Decisión 5: ECE ≤ 0,05 por segmento, 10 tramos). Método de calibración (p. ej. temperatura/isotónica) se fija en design.

### Modo sombra

#### REQ-IA-09 — Cálculo en sombra
- **HU:** HU-IA-13
- **Criterio:** MIENTRAS el modo sombra esté activo, CUANDO se analiza un documento, EL SISTEMA DEBE calcular también el % de IA de v3 y guardarlo junto al del motor vigente, **sin mostrarlo** en reporte, PDF, certificado ni QR.
- **Verificación:** test: con sombra activa se guarda el valor v3 y las plantillas no lo renderizan (Playwright/test de vista).
- **Etiqueta:** [Decisión Gerente]

#### REQ-IA-10 — Aislamiento de fallos en sombra
- **HU:** HU-IA-13
- **Criterio:** SI el cálculo v3 falla o supera su tiempo límite en modo sombra, ENTONCES EL SISTEMA DEBE registrar el error en el log y completar el análisis con el motor vigente sin cambios para el usuario.
- **Verificación:** test con v3 que lanza excepción → el reporte se genera igual.
- **Etiqueta:** [Propuesto]

#### REQ-IA-11 — Comparación sombra vs. vigente
- **HU:** HU-IA-13
- **Criterio:** CUANDO el Gerente ejecuta el comando de comparación, EL SISTEMA DEBE reportar, sobre los documentos analizados en sombra, la distribución de % de ambos motores, la diferencia por documento y los documentos con diferencia > 20 puntos.
- **Verificación:** test sobre datos de sombra sintéticos.
- **Etiqueta:** [Propuesto]. Duración y criterio de salida del modo sombra: ≥4 semanas o ≥100 documentos reales, lo que ocurra después; v3 solo se activa con el informe de comparación aprobado → [Decisión Gerente] (Decisión 6).

## 5. Requisitos no funcionales

#### REQ-IA-12 — Memoria y latencia en producción
- **Criterio:** MIENTRAS v3 corre en producción (sombra o activo), EL SISTEMA DEBE mantener el pico de RAM del proceso de análisis ≤ 1,5 GB y el tiempo de v3 ≤ 30 s por 1000 palabras en el servidor de 2 GB.
- **Verificación:** medición con `/usr/bin/time -v` sobre 5 documentos representativos.
- **Etiqueta:** RAM 2 GB [Validado con usuario]; límites 1,5 GB / 30 s [Decisión Gerente] (Decisión 7). Referencia: Binoculars medía 2,19–2,7 GB (descartado); el BETO actual pesa ~420 MB [Supuesto, según memoria y design AS-IS].

#### REQ-IA-13 — Reproducibilidad
- **Criterio:** CUANDO se ejecuta la evaluación dos veces con el mismo banco (manifiesto con SHA-256), modelo y commit, EL SISTEMA DEBE producir métricas idénticas y guardar en el informe fecha, commit, hash del banco, hash del modelo y semilla.
- **Verificación:** test (hereda AC-IA-02/03 de v2).
- **Etiqueta:** [Decisión Gerente] (G1 v2)

#### REQ-IA-14 — Sin APIs de pago nuevas
- **Criterio:** EL SISTEMA NO DEBE requerir ninguna API de pago nueva para entrenar, evaluar ni ejecutar v3; los textos de IA de entrenamiento DEBEN ser los **ya generados** (`eval_ia/ia_api`, `eval_ia/ia`). Los únicos textos nuevos son los de `no_vistos`, generados a mano en chats gratuitos y solo para prueba (RN-IA-11).
- **Verificación:** revisión de dependencias y variables de entorno en design/PR. No verificable automáticamente (revisión manual).
- **Etiqueta:** [Decisión Gerente]

#### REQ-IA-15 — Entrenamiento en Colab gratuito T4
- **Criterio:** EL SISTEMA DEBE poder entrenar y calibrar v3 con un notebook que corra en Colab gratuito con GPU T4 dentro de una sesión, guardando puntos de control para reanudar si la sesión se corta.
- **Verificación:** ejecución documentada del notebook (tiempo y GPU) en el informe de entrenamiento.
- **Etiqueta:** [Decisión Gerente]. Duración máxima de sesión utilizable (~3–4 h) → [Supuesto].

#### REQ-IA-16 — Confidencialidad del banco
- **Criterio:** EL SISTEMA NO DEBE subir a Colab ni a terceros tesis del instituto; en Colab solo se usan textos públicos (UPeU, Wikipedia u otros repositorios abiertos) y textos de IA generados.
- **Verificación:** el manifiesto de entrenamiento marca la institución de cada texto; test: un texto del instituto en el manifiesto de Colab → aborta. 
- **Etiqueta:** [Propuesto] (hereda RN-IA-04 v2)

#### REQ-IA-17 — Prueba sellada con textos de chat
- **Criterio:** EL SISTEMA DEBE mantener los **60 textos de chat** (30 ChatGPT + 30 Gemini) fuera de entrenamiento, calibración y selección de umbral, y evaluarlos **una sola vez** sobre el modelo candidato final, registrando la fecha de apertura en el informe; SI ya existe un informe sellado para ese conjunto, ENTONCES una nueva corrida DEBE marcarse "no sellada".
- **Verificación:** test del comando: segunda corrida sobre el conjunto sellado queda marcada.
- **Etiqueta:** [Decisión Gerente]. Si la verificación V-01 confirma que esos 60 se usaron para elegir el umbral de v2, siguen como prueba, pero el informe los marca "vistos" → [Decisión Gerente] (Decisión 8). El resultado de V-01 sigue siendo [Supuesto] hasta que se ejecute.

## 6. Reglas de negocio

| ID | Regla | Etiqueta |
|---|---|---|
| RN-IA-01 | El % de IA no afecta riesgo ni certificado | [Decisión Gerente] D-03/D-04 |
| RN-IA-03 | Toda salida del % lleva la advertencia de estimación no concluyente | [Validado con usuario] |
| RN-IA-04 | Tesis del instituto no salen del servidor/PC del Gerente | [Propuesto] |
| RN-IA-06 | Meta FPR ≤ 2 % (cota superior IC 95 %) con ≥150 humanos | [Decisión Gerente] D-10 |
| RN-IA-07 | IA visible con "IA estimada" y nota de uso responsable | [Decisión Gerente] D-08 |
| RN-IA-09 | Sin APIs de pago nuevas; Colab gratuito T4 para entrenar | [Decisión Gerente] |
| RN-IA-10 | Claude se usa solo para programar: no genera textos de entrenamiento nuevos, no etiqueta ni actúa como detector | [Decisión Gerente] |
| RN-IA-11 | No se generan textos nuevos con APIs de pago; se reutilizan los textos de IA ya generados. Única excepción, decidida por el Gerente (2026-11-13): la partición `no_vistos` (≥60 documentos de ≥3 familias de LLM), generada por él a mano en chats gratuitos, solo para prueba (nunca entrenamiento, validación ni Colab) | [Decisión Gerente] |
| RN-IA-12 | Los 60 textos de chat son prueba sellada | [Decisión Gerente] |
| RN-IA-13 | Humanos 2022+ solo para diagnóstico | [Decisión Gerente] |
| RN-IA-14 | v3 entra primero en modo sombra; activarlo requiere decisión del Gerente | [Decisión Gerente] |
| RN-IA-02 | Rollback: `AI_DETECTION_MODE` vuelve al motor vigente | [Decisión Gerente] D-02 |

## 7. Supuestos y riesgos

| # | Riesgo / supuesto | Impacto | Etiqueta |
|---|---|---|---|
| R1 | **Dominio:** la mayoría de humanos ≤2021 son UPeU (educación); el instituto tiene otro estilo y no tiene tesis digitales ≤2021. Un FPR bueno en UPeU puede no trasladarse al instituto | FPR real mayor al medido | [Supuesto] |
| R2 | **Wikipedia como humano:** en el informe v2 (2026-11-11) Wikipedia da 11–42 % de IA; puede tener ediciones post-2022 o estilo enciclopédico parecido a IA | Contamina humanos o infla FPR | [Supuesto] |
| R3 | **Etiquetas:** "≤2021" no garantiza texto sin ayuda automática (parafraseadores, plantillas); los mixtos sintéticos no imitan la reescritura real de un estudiante | MAE optimista en sintéticos | [Supuesto] |
| R4 | **Volumen humano de prueba:** el split actual tiene 87 documentos humanos de prueba + 20 de control (`dataset/resumen.json`), menos de 150. Con 0 FP hacen falta 183 (bilateral) | Meta no evaluable sin ampliar banco | [Supuesto] (lectura del repo) |
| R5 | **Pocas familias de LLM:** sin APIs de pago, las familias no vistas dependen de los chats gratuitos disponibles (excepción de RN-IA-11); con ≥60 documentos de ≥3 familias, alguna familia puede quedar con <15 documentos | Recall por familia con n chico ("n bajo") | [Supuesto] |
| R6 | **RAM 2 GB:** el modelo v3 debe convivir con la app, la extracción de PDF y el motor de similitud; en sombra se ejecutan dos detectores | Caídas por falta de memoria | [Validado con usuario] (2 GB) / [Supuesto] (consumo) |
| R7 | **Sello de los 60 textos de chat:** aparecen como `test_chat` en `dataset/resumen.json` y podrían haberse mirado al ajustar v2 | La prueba sellada pierde valor | [Supuesto] |
| R8 | **Colab gratuito:** cortes de sesión, cuota de GPU variable | Entrenamiento no reproducible | [Supuesto] |
| R9 | **PDF vs DOCX:** el mismo texto daba % muy distinto según formato (saltos y nº de página) | MAE alto por extracción, no por el modelo | [Supuesto] (memoria de auditorías previas) |

## 8. Decisiones del Gerente (G1 v3, 2026-11-13) — "OK a todo"

| # | Decisión | Resuelto | Etiqueta |
|---|---|---|---|
| 1 | Método del IC 95 % y umbral de documento | Clopper-Pearson bilateral; FP / acierto = documento con % IA ≥ 20 % | [Decisión Gerente] |
| 2 | "Humano verificado ≤2021" | Tesis de repositorios públicos peruanos con fecha de depósito ≤2021, ≥3 instituciones, sin Wikipedia en la meta; ≥150 (183 con 0 FP) | [Decisión Gerente] |
| 3 | Mixtos y meta de 10 puntos | Bloques de párrafos intercalados al 0/10/…/100 %; meta = MAE ≤10; p90 se reporta | [Decisión Gerente] |
| 4 | Recall en LLM no vistos | ≥70 % de documentos IA puros sobre el umbral, medido sobre `no_vistos` (Decisión 9; reemplaza "no vista = ChatGPT") | [Decisión Gerente] |
| 5 | Calibración | ECE ≤ 0,05 por segmento, curva de 10 tramos | [Decisión Gerente] |
| 6 | Modo sombra | ≥4 semanas o ≥100 documentos reales, lo que ocurra después; activar solo con informe de comparación aprobado | [Decisión Gerente] |
| 7 | RAM y latencia en producción | Pico ≤1,5 GB; ≤30 s por 1000 palabras | [Decisión Gerente] |
| 8 | 60 textos de chat | Siguen como prueba sellada; si V-01 confirma que se usaron para el umbral v2, el informe los marca "vistos" | [Decisión Gerente] |
| 9 | Excepción a RN-IA-11 (G2, design §1.3) | Partición `no_vistos`: ≥60 documentos de ≥3 familias de LLM, generados por el Gerente a mano en chats gratuitos (sin API, sin Claude); solo prueba | [Decisión Gerente] |

## 9. Verificaciones pendientes

| ID | Tarea | Resultado esperado | Responsable | Etiqueta |
|---|---|---|---|---|
| V-01 | Verificar si el umbral de v2 (`umbral.json` del modelo `clasificador_ia_v2`, notebook `entrenar_clasificador_ia.ipynb`, comandos `exportar_dataset_ia*`/`evaluate_ai_classifier`) se eligió usando los 60 textos de chat (`chatgpt_chat`/`gemini_chat`, partición `test_chat`) | Informe breve: "usados" o "no usados", con la evidencia (código/celda/fecha). Fija si REQ-IA-17 los marca "vistos" | Claude PM, revisa el Gerente | [Supuesto] hasta ejecutarse |

## Historial
| Versión | Fecha | Autor | Cambio |
|---|---|---|---|
| v0 | 2026-11-06 | Claude PM | Plantilla |
| v3-borrador | 2026-11-13 | Claude PM | Alcance v3: detector calibrado con textos mixtos; REQ-IA-01…17; 8 decisiones para G1 |
| v3 | 2026-11-13 | Claude PM | G1 aprobado por el Gerente ("OK a todo"): decisiones 1–8 como [Decisión Gerente]; añadida verificación V-01 |
| v3.1 | 2026-11-13 | Claude PM | RN-IA-11 corregida: sin textos nuevos por APIs de pago, excepción `no_vistos` (Decisión 9); REQ-IA-07, REQ-IA-14, alcance, R5 y Decisión 4 alineados con design §1.3 |
