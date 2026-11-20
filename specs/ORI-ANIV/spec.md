# ORI-ANIV — Votación Miss y Mister del aniversario

Estado: v2 · 2026-11-20 (v1 en PR #8; v2: hasta 5 fotos en carrusel y video subido o enlazado)
Referencia visual: `docs/aniversario/maqueta.html`

## 1. Objetivo y límites

Web pública de votación servida en `https://aniversario.iesp-alfonsobarranteslingan.edu.pe`, dentro del mismo
proyecto Django y servidor que originalidad. Es temporal: se retira al terminar el concurso.

- La app vive en `apps/aniversario/` (convención del repo: todas las apps bajo `apps/`).
- **No se tocan** modelos, vistas, URLs, plantillas, tareas ni migraciones de otras apps.
- Cambios fuera de la app (todos en `config/settings.py` y `.env.example`):

| Cambio | Por qué |
|---|---|
| `"apps.aniversario"` en `INSTALLED_APPS` | Registrar modelos, admin, plantillas y estáticos. |
| `HostUrlconfMiddleware` justo después de `SecurityMiddleware` | Enrutar el subdominio a sus propias URLs. Para cualquier otro host no hace nada. |
| `CACHES`: `default` idéntico al implícito actual (LocMem) + alias nuevo `aniversario` | Conteo y rate limit compartidos entre workers en Redis sin cambiar la caché de originalidad. |
| Lectura de `ANIVERSARIO_*` del `.env` | Host, Redis, proxies de confianza, TTL del conteo y rate limit: nada hardcodeado. |
| `CSRF_FAILURE_VIEW` → despachador `apps.aniversario.views.csrf_failure` | La vista actual de originalidad usa URLs de cuentas que no existen en el subdominio (daba 500). El despachador delega en ella para cualquier otro host: originalidad no cambia. |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Sin cambio de código: ya se leen del `.env`; solo se agrega el subdominio allí. |

## 2. Ruteo por host

- `ANIVERSARIO_HOST` (env). Si `request.get_host()` (sin puerto) coincide, el middleware asigna
  `request.urlconf = "apps.aniversario.urls_publicas"`.
- Rutas en ese host: `/`, `/candidato/<id>/`, `/votar/`, `/votar/mister/`, `/votar/confirmar/`, `/api/conteo/`.
- Nada de originalidad se resuelve en ese host, `/admin/` incluido → 404 con página propia.
- En ese host el middleware fija `Referrer-Policy: strict-origin-when-cross-origin` (el global `same-origin`
  hace que YouTube rechace el video embebido). Originalidad conserva su política.
- La gestión (candidatos, fotos, videos, resultados) se hace en el admin de originalidad, en su dominio.
- No existe middleware de login global en originalidad (auditoría F0), así que no hay nada que excluir.

## 3. Modelos

- `Concurso`: `nombre`, `fecha_inicio`, `fecha_cierre`, `video_archivo` (opcional), `video_url`, `mostrar_conteo` (bool),
  `max_votos_por_ip` (int, default 1), `activo`, `bases_pdf` (opcional). Se usa el más reciente con `activo=True`.
- `Candidato`: FK `concurso`, `categoria` (`miss`/`mister`), `numero`, `nombres`, `ciclo`, `carrera`, `edad`,
  `procedencia`, `biografia`, `frase`, `video_archivo` (opcional), `video_url` (opcional), `orden`, `activo`.
  Único (`concurso`, `categoria`, `numero`).
- `FotoCandidato`: FK `candidato`, `imagen`, `orden`. Máximo 5 por candidato (validado en el modelo y en el
  formulario en línea del admin). La de menor `orden` es la principal (tarjeta de voto, Open Graph). Al borrar
  una foto o un candidato se borra el archivo.
- `Voto`: FK `concurso`, `miss` y `mister` (FK `Candidato`, PROTECT), `codigo` único (`AN-XXXX-XXXX`),
  `ip`, `token_dispositivo` único, `user_agent_hash` (SHA-256), `creado`. Índices en `ip` y `creado`.
  `clean()` valida que `miss` sea categoría miss, `mister` sea mister, ambos activos y del mismo concurso.
- Conteo con `Count` agregado (sin contadores mutables), cacheado en el alias `aniversario`
  `ANIVERSARIO_CONTEO_TTL` segundos (default 20).

## 4. Un voto por equipo

1. **Token de dispositivo.** Cookie `aniv_disp` firmada (`django.core.signing`), httpOnly, Secure (si HTTPS),
   SameSite=Lax, vence en el cierre + 7 días. Se crea al entrar al flujo de voto. Es único en `Voto`: si ya
   existe un voto con ese token, cualquier ruta de votación muestra su comprobante.
2. **Límite por IP.** Máximo `Concurso.max_votos_por_ip` votos por IP en el concurso.
   - IP real: si `REMOTE_ADDR` está en `ANIVERSARIO_PROXIES_CONFIANZA` (default `127.0.0.1,::1`) o viene vacío
     (Gunicorn por socket Unix), se usa `X-Real-IP` y, si falta, el último valor de `X-Forwarded-For`.
     Nginx sobrescribe ambos con `$remote_addr`, así que el cliente no puede falsificarlos. En otro caso, `REMOTE_ADDR`.
   - Nota: en el WiFi del campus o con datos móviles (CGNAT) muchos equipos comparten IP. Por eso
     `max_votos_por_ip` se edita en el admin: subirlo (p. ej. 50) si se reportan rechazos legítimos.
3. **Rate limit.** `limit_req` de Nginx en `/votar/confirmar/` + límite propio por IP en el POST
   (`ANIVERSARIO_RATE_POR_MINUTO`, default 10) con la caché Redis. Sin dependencias nuevas.
4. **Atomicidad.** El POST corre en `transaction.atomic()` con `select_for_update()` sobre la fila del concurso
   (serializa votos y hace exacto el conteo por IP). La unicidad de `token_dispositivo` y `codigo` es la última
   barrera: el `IntegrityError` se atrapa y se muestra el comprobante existente (doble clic).
5. **Periodo.** Antes de `fecha_inicio` o desde `fecha_cierre` el POST se rechaza en el servidor y la UI lo indica.

## 5. Flujo de voto (sin JS obligatorio)

1. `/votar/` — grilla de Miss con radios (form GET → `/votar/mister/?miss=<id>`).
2. `/votar/mister/?miss=<id>` — grilla de Mister (form GET → `/votar/confirmar/?miss=..&mister=..`).
3. `/votar/confirmar/` — resumen + POST con CSRF. Éxito → comprobante y "¡Gracias por votar!".

Los pasos llevan la selección en la URL (sin sesión). "Elegir" en un perfil precarga su categoría.
Un voto = 1 Miss + 1 Mister, ambos obligatorios.

## 6. Frontend

- Plantillas propias en `apps/aniversario/templates/aniversario/` con `base.html` propio (no hereda originalidad).
- CSS de la maqueta en `static/aniversario/css/aniversario.css`; JS en `static/aniversario/js/aniversario.js`
  solo para cuenta regresiva, galería, resaltado de selección y refresco del conteo (25 s, `/api/conteo/`).
- Mobile-first, breakpoints 860/560 px, áreas táctiles ≥44 px, `safe-area-inset`, modo oscuro de la maqueta.
- Video: los encargados lo **suben** (MP4/WebM, máx. 60 MB, tipo real verificado con python-magic) y se
  reproduce con `<video>` nativo servido por Nginx desde `media/aniversario/videos/`; o pegan un enlace de
  YouTube/Facebook (`youtube-nocookie`). Si hay archivo, tiene prioridad. Recomendado subir a YouTube cuando el
  tráfico sea alto: no consume ancho de banda ni disco del EC2.
- Fotos en **carrusel** (scroll-snap: se desliza con el dedo incluso sin JS) en las tarjetas de inicio, en la
  grilla de voto y en el perfil (con miniaturas, flechas y puntos). Las flechas de la tarjeta no abren el perfil.
- Fotos: JPEG/PNG/WebP validados con Pillow; al guardar se reescalan a 1200 px de lado largo y se guardan como
  JPEG calidad 80 en `media/aniversario/candidatos/`. `loading="lazy"`. Avatar SVG solo si falta la foto.
- Open Graph por candidato (nombre + foto_1) para WhatsApp/Facebook.
- Ley 29733: las bases indican que se guarda IP, token de dispositivo y huella del navegador (hash), sin DNI,
  solo para controlar el voto único, y que se eliminan al cerrar el concurso.
- Se elimina de la maqueta: datos de ejemplo, `BASE` de votos falsos, `setInterval` aleatorio, textos y botón de demostración.

## 6b. Cambios v3 (2026-11-20)

- **Contador:** total de votos emitidos en vivo en el inicio, además del conteo por candidato. El voto recién
  emitido se suma al conteo cacheado en el acto (sin recalcular en la BD), así quien vota lo ve reflejado;
  el TTL corrige cualquier desfase entre workers. Refresco por JS cada 20 s.
- **Estado "ya votaste" fijo:** con la cookie del equipo, todas las páginas muestran la franja con el
  comprobante, las tarjetas marcan "Tu voto ✓", el inicio cambia a "Ver mi comprobante" y el perfil reemplaza
  "Elegir" por "Ya votaste" / "tiene tu voto".
- **Fotos ajustables:** por foto, `ajuste` (rellenar recortando o foto completa con fondo difuminado) y
  `encuadre` (arriba, medio-alto, centro, abajo), editables en el admin con vista previa.
- **Video vertical 9:16** (formato reel) en el inicio (columna derecha del héroe) y en el perfil. Si no hay
  video se muestra un diseño de reserva vertical ("Muy pronto"), nunca el 16:9 de la maqueta.

## 7. Admin

- `ConcursoAdmin`, `CandidatoAdmin` (filtros categoría/ciclo, `orden` editable en lista, columnas fotos n/5 y
  video, fotos en línea con vista previa y orden, máximo 5).
- `VotoAdmin` solo lectura (sin agregar/editar; borrar solo superusuario para el voto de prueba), búsqueda por código.
- Acción "Exportar resultados a CSV" en `ConcursoAdmin` (categoría, puesto, número, nombre, votos, %).

## 8. Pruebas

Voto válido · token repetido · límite por IP · fuera de fecha (antes y después) · categorías cruzadas ·
host de aniversario sin URLs de originalidad (`/admin/` 404) · host de originalidad sin cambios ·
IP desde proxy de confianza vs. no confiable · conteo agregado.

## 9. Despliegue y retiro (Fase 3, con OK paso a paso)

Ver `docs/aniversario/despliegue.md`.
