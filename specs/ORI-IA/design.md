Estado: Borrador — BLOQUEADO hasta G1

# ORI-IA · Design (borrador AS-IS, solo lectura del código) — todo [Supuesto]

Alcance asumido [Supuesto]: detección de IA en `analysis/engines` y su presentación en el informe.

## Modelo de datos
Sin modelos propios. Persiste en `reports.AnalysisReport`: `ai_probability_percent`, `ai_detector`, `summary`/`ai_score_breakdown` (JSON; el clasificador queda en `["classifier"]`), y `reports.ReportFinding` con `finding_type=AI_GENERATED`.

## Vistas / URLs
Sin URLs propias: se muestra en `reports:detail` y en el PDF señalado (`reports:download_highlighted`). Disparo por `analysis:analyze`.

## Componentes
- `DocumentAnalysisService`: `_estimate_ai_probability`, `_estimate_ai_probability_base`, `_estimate_ai_probability_by_segments`, `_classify_indicative`, `_replace_ai_findings`.
- `engines/ai_detector.py` (heurístico), `perplexity_detector.py` (DeepESP/gpt2-spanish, MAX_TOKENS=900), `ai_segments.py` (segmentos 150–250 palabras, umbral 50), `ai_classifier.py` (BETO afinado, solo indicativo; BATCH_SIZE=2, NUM_THREADS=2, MAX_LENGTH=384).
- Presentación: escala por rango (<20 "Sin indicios", <50 "Indicios leves", ≥50 "Posible IA: revisar"); nota "Estimación indicativa, no es prueba."
- Herramientas: comandos `evaluate_ai_*`, `generar_textos_ia`, `exportar_dataset_ia`; `eval_ia/`, `dataset/`, `notebooks/`.

## Flags y constantes
- `PERPLEXITY_AI_DETECTION_ENABLED`=False (decisión previa: no activar en producción por RAM ~2 GB).
- `AI_DETECTION_MODE` ∈ {global, segments, classifier}, por defecto "global"; `AI_CLASSIFIER_MODEL_DIR` (modelo ~420 MB fuera de git).
- `PERPLEXITY_MIDPOINT`=55, `PERPLEXITY_SCALE`=15, pesos burstiness 0 / perplejidad 1; `SEGMENT_AI_THRESHOLD`=50; `MIN_EVALUABLE_WORDS`=40; `MAX_NOISE_RATIO`=0.30.

## Reglas vigentes en código
- El % de IA **no** afecta riesgo ni certificado (`resolve_risk_level` solo usa similitud).

## Riesgos observados [Supuesto]
- Memoria/latencia de modelos en servidor de 2 GB; sesgo por tipo de documento (listas, tablas, PDF vs DOCX).
- Falsos positivos: la salida es indicativa; la decisión de uso sigue pendiente del Gerente.
