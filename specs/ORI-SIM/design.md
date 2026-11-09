Estado: Borrador — BLOQUEADO hasta G1

# ORI-SIM · Design (borrador AS-IS, solo lectura del código) — todo [Supuesto]

Alcance asumido [Supuesto]: similitud en `analysis` (motores chunk/span, interna/OAI/web) + informe y fuentes en `reports`. La orquestación del job la comparte con ORI-IA/ORI-INFRA.

## Modelo de datos
- `analysis.AnalysisJob`: document, requested_by, analysis_type (`AnalysisType`: FULL, SIMILARITY, AI_DETECTION), status (`AnalysisJobStatus`: PENDING, QUEUED, RUNNING, SUCCESS, FAILED, CANCELLED), attempt_number, celery_task_id, delivery_count, current_step, started_at, finished_at, error_code, error_message.
- `analysis.DocumentKnowledgeChunk`: base interna (document, text_excerpt, normalized_text, offsets, word_count, content_hash, is_active).
- `analysis.OaiRecord`: repository_name/domain, oai_identifier, title, authors, source_url, text_excerpt, normalized_text, harvested_at, source_date, is_active.
- `analysis.OaiFullText` (1:1 con OaiRecord; `OaiFullTextStatus`), `OaiFullTextChunk` (record, position, text).
- `analysis.DocumentFingerprint`: hash BigInt indexado, source_type, source_id (Winnowing).
- `analysis.WebSearchCacheEntry`, `WebPageCacheEntry` (cachés de Brave y páginas).
- `reports.AnalysisReport` (1:1 document y analysis_job): similarity_percent, web_similarity_percent, internal_similarity_percent, original_similarity_percent, ai_probability_percent, risk_level (`ReportRiskLevel`: LOW, MEDIUM, HIGH, CRITICAL), summary(JSON), analyzed_content, span_alignment(JSON), exclude_quotes, exclude_bibliography, engine_version, ai_detector, generated_at, is_final.
- `reports.ReportSource`: report, source_type (`SourceType`: WEB, INTERNAL, REPOSITORY), title, url, domain, matched_percent, snippet, metadata, source_key, word_count, is_excluded, excluded_by, excluded_at, exclusion_reason.
- `reports.ReportFinding`: report, source, finding_type (SIMILARITY | AI_GENERATED), start_offset, end_offset, text_excerpt, confidence_percent, metadata.

## Vistas / URLs
| URL | Vista | name |
|---|---|---|
| `/documentos/<uuid:pk>/analizar/` | DocumentAnalyzeView | analysis:analyze |
| `/documentos/<uuid:pk>/reporte/` | ReportDetailView | reports:detail |
| `.../reporte/estado/` | ReportStatusView | reports:status |
| `.../reporte/descargar-senalado/` | DownloadHighlightedDocumentView | reports:download_highlighted |
| `.../reporte/fuentes/<uuid:source_pk>/excluir/` | ExcludeReportSourceView | reports:exclude_source |
| `.../reporte/fuentes/<uuid:source_pk>/restaurar/` | RestoreReportSourceView | reports:restore_source |

## Componentes
- `DocumentAnalysisService.execute` (`analysis/services.py`): pasos `_run_similarity`, `_get_fingerprint_candidates`, `_save_report`, `_replace_internal/web_similarity_findings`, `_calculate_total_similarity_percent`, `_resolve_risk_level`.
- Motores (`analysis/engines/`): `fingerprint.py` (Winnowing), `similarity.py` (chunk), `span_similarity.py` (span), `web_search.py` + `web_similarity.py` (Brave), `text_filters.py`, `exclusions.py` (citas/bibliografía), `oai_harvester.py`, `oai_fulltext.py`; `common_text.py`, `retrieval.py`, `indexers.py` (`DocumentKnowledgeIndexer`), `web_cache.py`.
- `reports/services.py`: `resolve_risk_level`, `SpanReportWriter`, `ReportSourceExclusionService` (excluir/restaurar y recalcular; revoca certificado), `build_highlighted_document_pdf`, `resubmission_warning_sources`.
- Herramientas de medición: `analysis/banco/`, `analysis/evaluation/`, comando `benchmark_similarity`.

## Flags y constantes (valores por defecto en settings.py / reports)
- `SIMILARITY_ENGINE` = "chunk" (valores chunk|span); `SIMILARITY_RETRIEVAL` = "vieja"; `SIMILARITY_MIN_MATCH_WORDS`=8; `SIMILARITY_MAX_GAP_WORDS`=2; `SIMILARITY_MIN_SHORT_RUN`=4; `SIMILARITY_MIN_FUZZY_SPAN`=15; `SIMILARITY_MIN_FUZZY_RATIO`=0.7; `SIMILARITY_MAX_CANDIDATES`=1000; `SIMILARITY_COMMON_TEXT_MIN_DOCS`=5; `..._SHORT_RUN_WORDS`=15; `..._SHORT_MIN_DOCS`=4; `SIMILARITY_EXCLUDE_TEMPLATES`=True.
- Web: `WEB_ANALYSIS_ENABLED`, `BRAVE_SEARCH_API_KEY` (secreto), `BRAVE_SEARCH_COUNTRY` (debe ser ALL), `WEB_QUERY_MODE`="parrafos", `WEB_ANALYSIS_MAX_*`, `WEB_FOLLOW_PDF`, `WEB_PDF_MAX_MB`=20, `WEB_*_CACHE_DAYS`=30.
- Riesgo (solo similitud): ≥15 MEDIUM, ≥30 HIGH, ≥50 CRITICAL; `RESUBMISSION_COVERAGE_PERCENT`=95.

## Riesgos observados [Supuesto]
- Motor por defecto sigue "chunk"; "span" está implementado pero no activado.
- Servicios muy grandes (analysis/services.py ~1540 líneas, reports/services.py ~1510).
