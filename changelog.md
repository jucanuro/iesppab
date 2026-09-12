# Changelog

Registro de cambios del proyecto. Fechas en formato [YYYY-MM-DD].

## [2026-09-12]

### Changed
- Página de fallo de CSRF (`templates/403.html`, `apps/core/views.py::csrf_failure`):
  el texto deja de asumir siempre "tu sesión expiró" (confuso en páginas públicas
  como `/registro/`, donde no hay sesión que expirar) y ahora explica las causas
  reales: formulario abierto demasiado tiempo, sesión caducada, o token CSRF
  rotado por haber iniciado sesión en otra pestaña del mismo navegador. El botón
  pasa a "Volver a intentarlo" y enlaza a la página de origen (`HTTP_REFERER`,
  validado con `url_has_allowed_host_and_scheme` para no seguir un referer
  externo) para reintentar con un formulario fresco; si no hay una URL de origen
  segura, cae de vuelta al botón "Iniciar sesión" de antes.

### Added
- `docs/roles.md`: referencia del sistema de roles (ADMIN/DIRECTOR/TEACHER/STUDENT),
  la regla de alcance por institución, una tabla de qué puede ver/hacer cada rol por
  función (bandeja, reportes, análisis, certificados, panel admin) y las
  inconsistencias detectadas en el código actual (p. ej. que TEACHER/DIRECTOR pueden
  analizar o certificar documentos de su institución que no ven en su propia bandeja,
  o que el rol ADMIN no otorga acceso real a `/admin/` porque no toca `is_staff`).

## [2026-09-08]

### Changed
- El PDF descargable del reporte (`apps/reports/services.py`,
  `_HighlightedDocumentPdfBuilder`) pasa de "documento señalado" a un **informe de
  originalidad** con formato tipo Turnitin: cabecera con 4 métricas grandes (índice de
  similitud, fuentes de internet, trabajos/repositorios, IA estimada), lista
  "FUENTES PRIMARIAS" con badge numerado de color, dominio, tipo de fuente y
  "N palabras — X%", y el texto analizado completo con los resaltados y un chip
  numerado de color junto a cada pasaje que remite a su fuente. El nº de palabras por
  fuente se calcula a partir de los offsets de los hallazgos. El botón del reporte
  pasa de "Documento señalado" a "Informe en PDF" y se muestra siempre que hay
  reporte (antes exigía que hubiera resaltados); el archivo se llama
  `informe-originalidad-<id>.pdf`.
- `apps/reports/tests.py`: `test_pdf_has_the_originality_report_layout` verifica (con
  `pypdf`) que el PDF trae las secciones del nuevo formato.

### Added
- WhiteNoise (`requirements.txt`, middleware en `config/settings.py`) para servir los
  estáticos ya recogidos por `collectstatic` sin depender de `DEBUG` ni de un servidor
  web aparte. Necesario para exponer el proyecto detrás del túnel Cloudflare con
  `DEBUG=False` (antes `ManifestStaticFilesStorage` + `runserver` sin `collectstatic`
  daba 500 en toda página que usa `{% static %}`). `WHITENOISE_MANIFEST_STRICT=False` y
  `WHITENOISE_USE_FINDERS` en modo local para no reventar si no se ha corrido
  `collectstatic`.
- Navegación entre coincidencias en el visor del reporte
  (`templates/reports/detail.html`): stepper "◀ N / total ▶" con filtro
  (todas / solo similitud / solo IA) que hace scroll suave a cada fragmento
  resaltado y lo destaca un instante. Los `<span>` resaltados ganan
  `data-mark` + clase `js-mark`.
- Páginas de error con la identidad del sitio: `templates/403.html`,
  `templates/404.html` y `templates/500.html`. La de 403 distingue "sin permiso"
  de "sesión expirada".
- Vista `apps.core.views.csrf_failure` (configurada en `CSRF_FAILURE_VIEW`): un
  formulario enviado tras caducar la sesión ya no devuelve un 403 crudo, sino la
  página "Tu sesión expiró" con enlace a iniciar sesión.
- Aviso de "plataforma sin configurar": `apps/core/context_processors.py`
  (`institution_status`, registrado en `TEMPLATES`) + banner en `templates/base.html`
  cuando no hay ninguna `Institution` activa — con enlace directo a crearla para
  superusuarios/administradores, y un mensaje neutro para el resto.
- Vista previa del archivo en la zona de carga (`templates/documents/upload.html`):
  al seleccionar o arrastrar un archivo ahora se muestra su nombre, tipo (PDF/DOCX)
  y tamaño formateado, no solo el nombre.
- Auto-refresco del reporte por polling: nuevo endpoint `reports:status`
  (`/documentos/<uuid>/reporte/estado/`, `ReportStatusView` en `apps/reports/views.py`)
  que devuelve `{status, status_display, in_progress}` en JSON. `templates/reports/detail.html`
  deja de usar `<meta http-equiv="refresh" content="10">` (recarga completa, pierde
  scroll, parpadea) y en su lugar consulta ese endpoint cada 3 s mientras el análisis
  está en curso, recargando solo cuando pasa a `COMPLETED`/`FAILED`.
- Bandeja de documentos: filtros (estado, tipo, búsqueda por título o alumno) y
  paginación de 10 en 10 (`DocumentUploadView.get_context_data` + `_scoped_documents` /
  `_apply_filters` / `_filter_querystring` en `apps/documents/views.py`; antes cortaba en
  `[:10]` sin forma de ver el resto). El estado vacío distingue "sin documentos" de
  "sin resultados para el filtro".

### Changed
- `apps/reports/views.py`: `ReportDetailView._get_allowed_document` pasa a `@staticmethod`
  para reutilizarla desde `ReportStatusView`.
- Accesibilidad de teclado: foco visible (`outline` azul en `:focus-visible`) para
  enlaces, botones y `<summary>` en `templates/base.html` y en las plantillas de acceso
  (`auth_base.html`, `login.html`, `register.html`), que tienen `<head>` propio. El reset
  de Tailwind (CDN) atenuaba el outline nativo.
- Bandeja: el icono/botón "Ver reporte" (ojo en desktop, "Ver" en móvil) se deshabilita
  cuando el documento está `UPLOADED` (nunca analizado), con tooltip explicativo, en vez
  de llevar a una página de reporte vacía. Añadido `aria-label` al enlace del ojo.

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

### Changed
- El certificado ya no se puede emitir para reportes con nivel de riesgo alto.
  - `CertificateGenerationService._validate_report` (`apps/certificates/services.py`)
    lanza `ValidationError` si `report.risk_level == HIGH` (además de las validaciones
    de reporte final y documento completado que ya había).
  - Nuevo parcial `templates/certificates/_action_button.html`: el botón de certificado
    de la bandeja (`templates/documents/upload.html`, desktop + móvil) muestra
    "Descargar certificado" si ya está emitido; "Generar certificado" solo si el
    análisis terminó y el riesgo no es alto; y una versión deshabilitada con tooltip
    explicativo en los demás casos (riesgo alto, o análisis sin terminar).
  - Misma lógica en la cabecera de `templates/reports/detail.html`: el botón
    "Generar certificado" se sustituye por "Certificado no disponible" (deshabilitado)
    cuando el riesgo es alto o no hay reporte.
  - `DocumentUploadView._get_recent_documents` (`apps/documents/views.py`) añade
    `report__certificate` al `select_related` para no disparar consultas N+1 al
    pintar el botón por fila.

### Added
- `apps/certificates/tests.py` (antes vacío): `CertificateRiskGateTests` cubre que
  un reporte con riesgo alto no se certifica y que uno con riesgo bajo sí.
- Branding del admin de Django (`/admin/`): `templates/admin/base_site.html` (nuevo)
  pone el logo institucional en la cabecera y sobreescribe las variables de color del
  admin con la paleta azul institucional (`#123f9e` / `#236bfd`, acento `#f5b400`);
  en modo oscuro la cabecera y los botones siguen azules y el resto usa la paleta
  oscura nativa. `config/urls.py` fija `site_header`, `site_title` e `index_title`.
  Sin dependencias ni cambios en la funcionalidad del admin.

### Fixed
- `CertificateGenerateView` (`apps/certificates/views.py`) redirige a la página del
  reporte con `?descargar_certificado=1` tras generar, en vez de directo a la descarga
  del PDF. Antes, al pulsar "Generar certificado" el navegador bajaba el PDF pero no
  cambiaba de página, así que el botón se quedaba bloqueado en "Generando…". Ahora la
  página recarga (el botón pasa a "Descargar certificado") y un `<script>` en
  `templates/reports/detail.html` dispara la descarga automáticamente al detectar ese
  parámetro, limpiándolo después para que un F5 no vuelva a descargar.
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
- Iconos de certificado (`templates/certificates/_seal_icon.html` y
  `_seal_download_icon.html`, nuevos): una roseta/sello para "Generar certificado"
  y la misma roseta con flecha para "Descargar certificado", sustituyendo el icono
  de documento genérico y la flecha de descarga sueltos. Aplicados en la bandeja
  (`templates/documents/upload.html`, desktop + móvil, donde el botón móvil deja de
  mostrar el texto "PDF"), en la página de reporte (`templates/reports/detail.html`)
  y en la de verificación (`templates/certificates/verify.html`). El botón "Exportar"
  del reporte pasa a llamarse "Generar certificado".

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
