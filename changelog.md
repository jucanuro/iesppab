# Changelog

Registro de cambios del proyecto. Fechas en formato [YYYY-MM-DD].

## [2026-09-08]

### Changed
- Bandeja de documentos (`templates/documents/upload.html`):
  - Eliminado el botón "Subir documento" del encabezado (el enlace `<a href="#nuevo-documento">`),
    ya que el panel de carga está siempre visible más abajo y el botón era redundante.
  - El botón de envío del formulario de carga pasa de "Enviar" a "Registrar"
    (y su estado de carga de "Enviando..." a "Registrando...").
  - El botón de acción "Analizar similitud" (icono suelto con hover rojo, y una `%`
    literal en la tarjeta móvil) se reemplaza por un botón consciente del estado del
    documento: "Analizar originalidad" (icono de documento con lupa) cuando está
    `UPLOADED`; "Analizando originalidad…" deshabilitado con spinner en `QUEUED`/`PROCESSING`;
    "Reanalizar originalidad" (icono de recarga) en `COMPLETED`/`FAILED`. El hover pasa a
    azul institucional y los botones de solo icono ganan `aria-label`.

- Página de reporte (`templates/reports/detail.html`): la cabecera gana un botón de
  acción junto a "Volver": "Analizar originalidad" cuando el documento no tiene reporte,
  "Reanalizar" cuando ya lo tiene o el último análisis falló, y un indicador
  "Analizando…" deshabilitado mientras hay un análisis en curso. Los textos de los
  estados vacíos (fallido / sin reporte) ahora apuntan a este botón en vez de "la
  bandeja documental".
- `ReportDetailView` (`apps/reports/views.py`): `analysis_in_progress` también es cierto
  cuando `Document.status` es `QUEUED` o `PROCESSING`, para cubrir el hueco entre encolar
  la tarea y que el worker cree el `AnalysisJob` (antes ese lapso no mostraba el spinner
  ni activaba el auto-refresco de la página).

### Fixed
- `DocumentAnalyzeView` (`apps/analysis/views.py`) ahora marca el documento como
  `QUEUED` antes de encolar la tarea Celery, para que la bandeja y el reporte reflejen
  el estado sin esperar a que el worker recoja el trabajo (antes quedaba en `UPLOADED`
  y la página de reporte, a la que redirige, mostraba "aún no tiene reporte").
- `DocumentAnalyzeView` ya no vuelve a encolar un documento cuyo estado es `QUEUED` o
  `PROCESSING`; en ese caso muestra un aviso y redirige al reporte. Evita jobs
  duplicados por doble clic.
- `DocumentAnalysisService.check_permission` (`apps/analysis/services.py`) ahora
  devuelve el `Document` validado (antes no devolvía nada) para que la vista pueda
  inspeccionar su estado.
