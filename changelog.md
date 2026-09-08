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

### Added
- Recuperación de contraseña ("¿Olvidaste tu contraseña?"): flujo completo con las
  vistas integradas de Django, subclasadas en `apps/accounts/views.py`
  (`PasswordResetView` y las otras tres) para inyectar el nombre de la institución.
  Rutas bajo `apps/accounts/urls.py` (`/clave/recuperar/`, `/clave/recuperar/enviado/`,
  `/clave/nueva/<uidb64>/<token>/`, `/clave/nueva/lista/`). Plantillas nuevas en
  `templates/accounts/`: `auth_base.html` (shell común para las pantallas de acceso),
  `password_reset_form/done/confirm/complete.html`, `password_reset_email.html` y
  `password_reset_subject.txt`.
- Configuración de email en `config/settings.py` y `.env.example`: `EMAIL_HOST`,
  `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS/SSL`,
  `DEFAULT_FROM_EMAIL`, `PASSWORD_RESET_TIMEOUT` (3 días). Sin `EMAIL_HOST` los correos
  se imprimen en consola; con `EMAIL_HOST` se envían por SMTP.
- Página "Política de privacidad" (`/privacidad/`, `apps.core`): borrador completo
  alineado con el tratamiento real de datos de la plataforma y la Ley N.º 29733,
  marcado visiblemente como pendiente de revisión legal.
- Página "Centro de Ayuda" (`/ayuda/`, `apps.core`): guía de uso y preguntas
  frecuentes (acceso, registro y habilitación, recuperación de contraseña, carga,
  análisis, lectura del reporte, descargas, roles).
- `apps/core/views.py` y `apps/core/urls.py` (nuevo), montado en `config/urls.py`.
- Favicon del sitio: `templates/base.html`, `templates/accounts/login.html` y
  `templates/accounts/register.html` enlazan `img/logo-iesppabl.png` como `icon` y
  `apple-touch-icon` (antes la pestaña salía sin icono).
- Avisos (mensajes de Django) en `templates/base.html`: botón "×" para cerrarlos,
  auto-ocultado a los 6 s para los de tipo `success`, y `role="alert"` /
  `aria-live="polite"` para lectores de pantalla.
- Spinner de envío reutilizable en `templates/base.html`: cualquier
  `<form data-submit-spinner>` deshabilita su botón de envío y muestra un spinner
  (con texto opcional vía `data-loading-label`) al enviarse. Aplicado a los botones
  "Analizar"/"Reanalizar" de la bandeja (`templates/documents/upload.html`) y de la
  página de reporte (`templates/reports/detail.html`).

### Added
- Leyenda de estados en la bandeja (`templates/documents/upload.html`): un
  desplegable nativo `<details>` ("¿Qué significan los estados?") junto al título
  "Trabajos académicos registrados", cerrado por defecto, que explica cada insignia
  reutilizando el parcial `_status_badge.html` (así nunca se desincroniza del diseño
  real) y enlaza al Centro de Ayuda. Solo se muestra si hay documentos.
- Tooltips (`title`) en todas las insignias de estado, no solo en las de riesgo.
  El parcial `_status_badge.html` acepta ahora `status` y `risk_level` como
  parámetros de `{% include %}` para pintar insignias de muestra sin un documento.

### Changed
- Insignias de estado en "Trabajos académicos registrados"
  (`templates/documents/upload.html`, tabla desktop + tarjeta móvil): extraídas a
  un parcial `templates/documents/_status_badge.html`. `UPLOADED` pasa de "Subido"
  a "Sin analizar" (insignia tenue, llamada a la acción); `QUEUED` pasa de ámbar a
  gris neutro; `PROCESSING` muestra "Analizando…" con el punto latiendo; `FAILED`
  pasa a contorno rojo sin relleno ("Falló"); y `COMPLETED` deja de mostrar
  "Completado" para mostrar el nivel de riesgo del reporte ("Riesgo bajo/medio/alto",
  verde/ámbar/rojo) con un tooltip "Similitud X% · IA Y%". Fallback "Analizado" si
  el documento está completo pero sin reporte asociado.
- Tipografía base: `templates/base.html`, `login.html` y `register.html` pasan de
  `Arial, Helvetica, sans-serif` a un stack de fuente de sistema
  (`ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, …`).
- `templates/base.html`: los enlaces del pie "Política de privacidad" y
  "Centro de Ayuda" (antes `href="#"`) ahora apuntan a `core:privacy` y `core:help`.
- `templates/accounts/login.html`: "¿Olvidaste tu contraseña?" (antes `href="#"`)
  apunta a `accounts:password_reset` y "Ayuda" a `core:help`.
- `templates/accounts/login.html` y `register.html`: favicon y stack de fuente de
  sistema (tienen `<head>` propio, no heredan de `base.html`).
