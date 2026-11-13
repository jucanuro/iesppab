Estado: Aprobado por Juan Carlos Núñez (Gerente), 2026-11-13

# ORI-IA · Tareas v3 — Detector calibrado con textos mixtos

> Base: `requirements.md` v3.1 y `design.md` v3.1 (ambos Aprobado por Juan Carlos Núñez (Gerente), 2026-11-13).
> Tamaño: cada tarea ≤ ~½ día. Orden = orden de dependencias. [Propuesto] salvo indicación.
> **Dónde corre:** `PC` = PC de desarrollo (código + tests con `python manage.py test`); `Colab` = Google Colab T4 gratuito (solo datos públicos, REQ-IA-16); `Gerente` = trabajo manual del Gerente; `Servidor` = producción, lo ejecuta el Gerente (Claude no toca producción).
> Puerta: **T17** aplica el criterio de parada (design §8). Si da DETENER, T18–T28 no se ejecutan y se mantiene v2. [Decisión Gerente]

## Fase A — Verificación y banco (PC / Gerente)

- [ ] T01 — Ejecutar V-01: ¿se usaron los 60 de chat para fijar el umbral v2?
  - Corre en: PC (solo lectura) · Depende de: — · REQ: REQ-IA-17, REQ-IA-07
  - Archivos: `specs/ORI-IA/V-01.md` (informe "usados"/"no usados" con evidencia: `umbral.json`, celdas del notebook v2, fechas)
  - Verificación: revisión del Gerente; el resultado fija `visto` de los 60 en el manifiesto
  - Rama: `ORI-IA/T01-verificacion-v01`

- [ ] T02 — Cargador y validador del manifiesto (C1)
  - Corre en: PC · Depende de: — · REQ: REQ-IA-01, REQ-IA-04, REQ-IA-07, REQ-IA-13, REQ-IA-16
  - Archivos: `apps/analysis/ia_v3/__init__.py`, `apps/analysis/ia_v3/manifest.py`, `apps/analysis/ia_v3/exceptions.py` (`IABankError`), `apps/analysis/tests_ia_v3_manifest.py`
  - Verificación: tests — SHA-256 ausente/distinto → `IABankError` con id; fuente en dos particiones → `IABankError`; test_fpr 149/150/183 entradas y 2/3 instituciones → "insuficiente"/OK; no_vistos <60 docs o <3 familias → "insuficiente"; ninguna institución >50 % de test_fpr
  - Rama: `ORI-IA/T02-manifiesto`

- [ ] T03 — Comando `construir_manifiesto_ia_v3` (carpetas + CSV de metadatos → manifiesto con particiones por semilla `IA_V3_SEED`)
  - Corre en: PC · Depende de: T02 · REQ: REQ-IA-01, REQ-IA-04, REQ-IA-13
  - Archivos: `apps/analysis/management/commands/construir_manifiesto_ia_v3.py`, `apps/analysis/evaluation/ia_v3/metadatos_plantilla.csv`, tests en `tests_ia_v3_manifest.py`
  - Verificación: test — misma semilla → manifiesto idéntico (hash); Wikipedia nunca en test_fpr; ≥2022 → `diagnostico`; 60 de chat → `sellado` con `visto` según T01; carpeta `no_vistos_v3` → `no_vistos`
  - Rama: `ORI-IA/T03-construir-manifiesto`

- [ ] T04 — Obtener humanos públicos ≤2021 de ≥3 instituciones (design §1.4)
  - Corre en: Gerente (descargas manuales) + PC (registro) · Depende de: T03 para registrar · REQ: REQ-IA-01
  - Archivos: textos en `eval_ia/humanos_v3/<institucion>/` (fuera de git); filas en el CSV de metadatos con `fuente_url` y `licencia`
  - Verificación: manual — solo acceso abierto, `robots.txt` respetado, hosts que bloquean descartados; T02 sobre el manifiesto real da OK (≥183, ≥3 instituciones)
  - Rama: — (sin código; el CSV va en T03 o en una rama `ORI-IA/T04-metadatos-humanos`)

- [ ] T05 — Generar ≥60 documentos IA en chats gratuitos de ≥3 familias no vistas (design §1.3)
  - Corre en: Gerente (chats) · Depende de: — (en paralelo; bloquea solo T19) · REQ: REQ-IA-07, REQ-IA-14 · RN: RN-IA-11 (única excepción: a mano en chats gratuitos, sin API ni Claude)
  - Archivos: `eval_ia/no_vistos_v3/<familia>/<id>.txt` + consigna, chat, versión y fecha por documento (fuera de git)
  - Verificación: T02 sobre el manifiesto real da OK en `no_vistos` (≥60, ≥3 familias; familias con <15 → "n bajo")
  - Rama: —

- [ ] T06 — Constructor de mixtos por bloques de párrafos (C2)
  - Corre en: PC · Depende de: T02 · REQ: REQ-IA-03, REQ-IA-04
  - Archivos: `apps/analysis/ia_v3/mixtos.py`, `apps/analysis/tests_ia_v3_mixtos.py`
  - Verificación: tests — determinismo (misma semilla → mismo sha256); `pct_real_ia` tras `AcademicTextFilter` coincide con los tramos; 0 % y 100 % exactos; nunca mezcla fuentes de particiones distintas
  - Rama: `ORI-IA/T06-mixtos`

- [ ] T07 — Métrica FPR con Clopper-Pearson bilateral (C3, parte 1)
  - Corre en: PC · Depende de: — · REQ: REQ-IA-06
  - Archivos: `apps/analysis/ia_v3/metrics.py`, `apps/analysis/tests_ia_v3_metrics.py`
  - Verificación: tests — 0/183 → CUMPLE; 1/183 → NO CUMPLE; cota comparada con valores de referencia tabulados (sin scipy)
  - Rama: `ORI-IA/T07-fpr-clopper-pearson`

- [ ] T08 — Métricas MAE/p90, recall por familia con IC y curva de fiabilidad/ECE (C3, parte 2)
  - Corre en: PC · Depende de: T07 · REQ: REQ-IA-05, REQ-IA-07, REQ-IA-08
  - Archivos: `apps/analysis/ia_v3/metrics.py`, `apps/analysis/tests_ia_v3_metrics.py`
  - Verificación: tests — MAE 9,9 → CUMPLE, 10,1 → NO CUMPLE; recall separa vistas/no vistas; predicciones calibradas → ECE ≈0, sesgadas → ECE >0,05
  - Rama: `ORI-IA/T08-metricas-mae-recall-ece`

- [ ] T09 — Comando `construir_banco_ia_v3 [--para-colab]`
  - Corre en: PC · Depende de: T03, T06 (código); T04 (datos reales) · REQ: REQ-IA-03, REQ-IA-04, REQ-IA-16
  - Archivos: `apps/analysis/management/commands/construir_banco_ia_v3.py`, tests
  - Verificación: tests — `--para-colab` excluye `sellado`/`no_vistos`/`diagnostico`; texto con `publico=false` → aborta; JSONL por partición con hash reproducible
  - Rama: `ORI-IA/T09-construir-banco`

## Fase B — Motor y entrenamiento (PC / Colab)

- [ ] T10 — `ai_classifier.py`: escalado de temperatura + estimador congelado desde `calibracion.json` (C4)
  - Corre en: PC · Depende de: — · REQ: REQ-IA-08
  - Archivos: `apps/analysis/engines/ai_classifier.py`, `apps/analysis/tests_ai_classifier.py`
  - Verificación: tests — sin `calibracion.json` el resultado v2 es idéntico al actual (regresión); con T=2 los logits se escalan; E1/E2 dan el % esperado en un caso sintético
  - Rama: `ORI-IA/T10-calibracion-temperatura`

- [ ] T11 — Plazo cooperativo (`deadline`) entre lotes en `SegmentAIClassifier` (C4)
  - Corre en: PC · Depende de: T10 · REQ: REQ-IA-10
  - Archivos: `apps/analysis/engines/ai_classifier.py`, `apps/analysis/tests_ai_classifier.py`
  - Verificación: test — plazo vencido tras el primer lote → excepción de plazo; sin plazo, comportamiento intacto
  - Rama: `ORI-IA/T11-plazo-cooperativo`

- [ ] T12 — Notebook de entrenamiento y calibración v3 (C5)
  - Corre en: PC (redacción) · Depende de: T09, T10 · REQ: REQ-IA-15, REQ-IA-16, REQ-IA-08, REQ-IA-13
  - Archivos: `notebooks/entrenar_clasificador_ia_v3.ipynb`
  - Verificación: manual — celda inicial aborta si hay `publico=false`; puntos de control por época en Drive; exporta `calibracion.json` con temperatura, estimador, `tau`, `hash_banco`, semilla, commit; prueba en seco con 10 textos en CPU
  - Rama: `ORI-IA/T12-notebook-v3`

- [ ] T13 — Entrenar BETO v3 en Colab T4
  - Corre en: Colab · Depende de: T12, T09 (banco real `--para-colab`), T04 · REQ: REQ-IA-15
  - Archivos: pesos en Drive → `~/models/clasificador_ia_v3/` en la PC (fuera de git)
  - Verificación: registro de ejecución (épocas, pérdida, reanudaciones) en el informe de entrenamiento
  - Rama: —

- [ ] T14 — Calibrar en val: temperatura (NLL), elección E1/E2 y `tau` (design §2.1), congelar
  - Corre en: Colab · Depende de: T13 · REQ: REQ-IA-08, REQ-IA-06
  - Archivos: `calibracion.json` del modelo v3
  - Verificación: el notebook muestra FPR de documento en val ≤1 % y el MAE de ambos estimadores; la regla de empate se aplica tal cual
  - Rama: —

## Fase C — Evaluación y criterio de parada (PC)

- [ ] T15 — Comando `evaluar_ia_v3 --modelo DIR --particion val|test` (pipeline de producción)
  - Corre en: PC · Depende de: T08, T09, T10 · REQ: REQ-IA-02, REQ-IA-05, REQ-IA-06, REQ-IA-07, REQ-IA-08, REQ-IA-13
  - Archivos: `apps/analysis/management/commands/evaluar_ia_v3.py`, tests
  - Verificación: tests — dos corridas → informe con métricas idénticas; un humano 2022 no cambia el FPR de la meta (sección diagnóstico aparte); funciona con un modelo sin `calibracion.json` (v2); informe incluye commit, hash banco, hash modelo, semilla
  - Rama: `ORI-IA/T15-evaluar-ia-v3`

- [ ] T16 — Comando `comparar_v2_v3` (criterio de parada, design §8)
  - Corre en: PC · Depende de: T15 · REQ: — (design §8, [Decisión Gerente])
  - Archivos: `apps/analysis/management/commands/comparar_v2_v3.py`, tests
  - Verificación: tests — FP v3 ≤ v2 y MAE v3 < v2 → CONTINUAR; MAE igual → DETENER; FP v3 > v2 → DETENER; rechaza informes que no sean de `val`
  - Rama: `ORI-IA/T16-comparar-v2-v3`

- [ ] T17 — **PUERTA:** evaluar v2 y v3 en val y aplicar el criterio de parada
  - Corre en: PC · Depende de: T14, T16 · REQ: — (design §8)
  - Archivos: `eval_ia/_informes/ia_v3_val_<fecha>_<commit>.{json,txt}` y el de v2 (fuera de git)
  - Verificación: decisión del Gerente sobre el informe de `comparar_v2_v3`. DETENER → fin de v3, se mantiene v2
  - Rama: —

## Fase D — Prueba final (PC) — solo si T17 = CONTINUAR

- [ ] T18 — Opción `--sellado` en `evaluar_ia_v3` (sello de `sellado` + `no_vistos`)
  - Corre en: PC · Depende de: T15, T01 · REQ: REQ-IA-17, REQ-IA-07
  - Archivos: `evaluar_ia_v3.py`, `eval_ia/_informes/sello_chat_v3.json` (generado), tests
  - Verificación: tests — primera corrida crea el sello; segunda → "no sellada"; los 60 existentes salen como "vistos" según T01 y no cuentan para la meta de recall
  - Rama: `ORI-IA/T18-sellado`

- [ ] T19 — Ejecutar test + sellado (una sola vez) e informe final de métricas
  - Corre en: PC · Depende de: T17 (CONTINUAR), T18, T05 · REQ: REQ-IA-01, REQ-IA-05, REQ-IA-06, REQ-IA-07, REQ-IA-08, REQ-IA-17
  - Archivos: `eval_ia/_informes/ia_v3_test_<fecha>_<commit>.{json,txt}`
  - Verificación: revisión del Gerente de los veredictos (FPR cota ≤2 %, MAE ≤10, recall no vistos ≥70 %, ECE ≤0,05)
  - Rama: —

## Fase E — Modo sombra (PC → Servidor) — solo si T17 = CONTINUAR

- [ ] T20 — Modelo `AIShadowEstimate` + migración reversible + admin solo superusuario
  - Corre en: PC · Depende de: T17 · REQ: REQ-IA-09
  - Archivos: `apps/reports/models.py`, `apps/reports/migrations/00NN_aishadowestimate.py`, `apps/reports/admin.py`, `apps/reports/tests.py`
  - Verificación: tests — `unique_together` (report, detector); migrate/migrate inverso en SQLite; admin no visible para no superusuario
  - Rama: `ORI-IA/T20-aishadowestimate`

- [ ] T21 — Sombra en `DocumentAnalysisService._estimate_ai_probability` + flags `AI_SHADOW_*` (C6)
  - Corre en: PC · Depende de: T20, T11 · REQ: REQ-IA-09, REQ-IA-10, REQ-IA-12
  - Archivos: `apps/analysis/services.py`, `config/settings.py`, `.env.example`, `apps/analysis/tests.py`
  - Verificación: tests — flag apagado → no hay filas; v3 lanza excepción o supera plazo → reporte idéntico + fila con `error` + `logger.warning`; `AI_SHADOW_UNLOAD_PRIMARY` descarga v2 antes de cargar v3; se guardan `duration_ms` y `peak_rss_mb`
  - Rama: `ORI-IA/T21-sombra-servicio`

- [ ] T22 — Test de no exposición del % v3 (detalle, PDF de IA, certificado, verificación pública)
  - Corre en: PC · Depende de: T21 · REQ: REQ-IA-09
  - Archivos: `apps/reports/tests.py`, `apps/certificates/tests.py`
  - Verificación: tests — con una fila de sombra de valor distintivo, el valor no aparece en `reports:detail`, PDF de IA, certificado ni `certificates:verify`
  - Rama: `ORI-IA/T22-no-exposicion-v3`

- [ ] T23 — Comando `medir_recursos_ia --modelo DIR docs…`
  - Corre en: PC · Depende de: T10 · REQ: REQ-IA-12
  - Archivos: `apps/analysis/management/commands/medir_recursos_ia.py`, tests
  - Verificación: test del veredicto con mediciones simuladas (≤1536 MB / ≤30 s por 1000 palabras); prueba manual en PC con 5 documentos
  - Rama: `ORI-IA/T23-medir-recursos`

- [ ] T24 — Comando `comparar_ia_sombra [--desde FECHA]`
  - Corre en: PC · Depende de: T20 · REQ: REQ-IA-11
  - Archivos: `apps/reports/management/commands/comparar_ia_sombra.py`, tests
  - Verificación: tests sobre filas sintéticas — distribución, diferencias >20 pts, conteo de errores, criterio de salida (≥4 semanas **y** ≥100 documentos)
  - Rama: `ORI-IA/T24-comparar-sombra`

- [ ] T25 — Despliegue 1: corrección de extracción (rama `fix/extraccion-pdf-palabra-por-linea`), **sin** la migración de sombra
  - Corre en: Servidor (Gerente) · Depende de: — (fuera de v3; prerrequisito de orden, design §9) · REQ: — (design §9)
  - Archivos: —
  - Verificación: manual — servicio estable tras el despliegue; `showmigrations reports` sin `00NN_aishadowestimate`
  - Rama: —

- [ ] T26 — Despliegue 2: migración `AIShadowEstimate` con respaldo previo, `AI_SHADOW_ENABLED=False`
  - Corre en: Servidor (Gerente) · Depende de: T20, T25 · REQ: REQ-IA-09 (design §9)
  - Archivos: —
  - Verificación: manual — `pg_dump` previo restaurable y de tamaño >0; migración aplicada; reportes nuevos sin cambios
  - Rama: —

- [ ] T27 — Copiar modelo v3 al servidor y medir RAM/latencia
  - Corre en: Servidor (Gerente) · Depende de: T19, T23, T26 · REQ: REQ-IA-12
  - Archivos: `~/models/clasificador_ia_v3/` en el servidor
  - Verificación: manual — `medir_recursos_ia` sobre 5 documentos: pico ≤1,5 GB y ≤30 s/1000 palabras; si no cumple, no se activa la sombra
  - Rama: —

- [ ] T28 — Activar sombra y emitir informe de comparación al cumplir ≥4 semanas y ≥100 documentos
  - Corre en: Servidor (Gerente) · Depende de: T21, T22, T24, T27 · REQ: REQ-IA-09, REQ-IA-11
  - Archivos: informe de `comparar_ia_sombra` (fuera de git)
  - Verificación: manual — informe revisado por el Gerente; activar v3 como visible queda fuera de alcance (RN-IA-14)
  - Rama: —

## Cobertura REQ → tareas

| REQ | Tareas |
|---|---|
| REQ-IA-01 | T02, T03, T04, T19 |
| REQ-IA-02 | T15 |
| REQ-IA-03 | T06, T09 |
| REQ-IA-04 | T02, T03, T06, T09 |
| REQ-IA-05 | T08, T15, T19 |
| REQ-IA-06 | T07, T14, T15, T19 |
| REQ-IA-07 | T01, T02, T05, T08, T15, T18, T19 |
| REQ-IA-08 | T08, T10, T12, T14, T15, T19 |
| REQ-IA-09 | T20, T21, T22, T26, T28 |
| REQ-IA-10 | T11, T21 |
| REQ-IA-11 | T24, T28 |
| REQ-IA-12 | T21, T23, T27 |
| REQ-IA-13 | T02, T03, T12, T15 |
| REQ-IA-14 | T05 (excepción RN-IA-11), revisión manual en cada PR (ver abajo) |
| REQ-IA-15 | T12, T13 |
| REQ-IA-16 | T02, T09, T12 |
| REQ-IA-17 | T01, T18, T19 |
| design §8 (parada) | T16, T17 |
| design §9 (despliegue) | T25, T26 |

## No verificable (automáticamente)

- REQ-IA-14 (sin APIs de pago nuevas): revisión manual en cada PR de que no se añaden dependencias ni variables de API de IA. [Propuesto]
- REQ-IA-15 (Colab T4 gratuito): solo evidencia por registro de ejecución (T13). [Propuesto]
- Calidad de origen de T04/T05 (licencias, robots.txt, que los textos sean realmente del LLM indicado): declaración del Gerente en el CSV de metadatos. [Supuesto]

## Historial
| Versión | Fecha | Autor | Cambio |
|---|---|---|---|
| v0 | 2026-11-06 | Claude PM | Plantilla bloqueada hasta G1 |
| v3-borrador | 2026-11-13 | Claude PM | 28 tareas sobre design v3 aprobado, con puerta de parada en T17 |
| v3 | 2026-11-13 | Claude PM | Aprobado por el Gerente; base requirements/design v3.1; T05 enlazada a RN-IA-11 y REQ-IA-14 |
