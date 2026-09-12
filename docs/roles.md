# Roles y control de acceso

Referencia de cómo está implementado el sistema de roles y quién puede ver o
actuar sobre los documentos, reportes y certificados de otros usuarios.

## Los 4 roles

`UserRole` (`apps/accounts/models.py`): **ADMIN, DIRECTOR, TEACHER, STUDENT**.
Cada uno tiene una propiedad `is_x_role` en el modelo `User`:

| Rol | Cómo se calcula | ¿Incluye a los superusuarios? |
|---|---|---|
| ADMIN | `role == ADMIN or is_superuser` | **Sí** |
| DIRECTOR | `role == DIRECTOR` | No |
| TEACHER | `role == TEACHER` | No |
| STUDENT | `role == STUDENT` | No |

**Solo `is_admin_role` tiene el comodín de superusuario.** Las otras tres
propiedades son una simple comparación del campo `role`, sin excepción para
superusuarios. Consecuencia práctica: un superusuario cuyo `role` sea
literalmente `STUDENT` (el valor por defecto del modelo) sería `is_admin_role
= True` **y** `is_student_role = True` a la vez.

## Alcance por institución

- Todo usuario no-superusuario debe pertenecer a una `Institution`
  (`User.clean()` en `apps/accounts/models.py` lo exige).
- Los superusuarios están exentos de esa regla y de la validación de dominio
  de correo institucional: ven todo, en todas las instituciones.
- Regla general que se repite en documentos, reportes, análisis y
  certificados (con una excepción real, ver más abajo):
  **ADMIN ve todo dentro de su institución; TEACHER/DIRECTOR/STUDENT solo ven
  lo que ellos mismos subieron o poseen.**

## Quién ve qué, función por función

| Función | Superusuario | ADMIN | TEACHER / DIRECTOR | STUDENT |
|---|---|---|---|---|
| Bandeja de documentos (`apps/documents/views.py`, `_scoped_documents`) | Todos, todas las instituciones | Todos los de su institución | Solo los propios (`owner` o `uploaded_by`) — no todos los de su institución | Solo los propios |
| Ver un reporte (`apps/reports/views.py`, `_get_allowed_document`) | Todos | Todos los de su institución | Solo los propios (misma regla que la bandeja) | Solo los propios |
| Subir un documento para otro (`apps/documents/services.py`, `_resolve_owner`) | Cualquier alumno activo, cualquier institución | Cualquier alumno activo y habilitado de su institución | Igual que ADMIN (mismo trato) | Solo para sí mismo |
| **Ejecutar/consultar el análisis** (`apps/analysis/services.py`, `_get_allowed_document`) | Todos | — | **Cualquier documento de su institución**, aunque no lo haya subido ni sea el dueño | Solo los propios (`owner`) |
| **Generar/descargar certificado** (`apps/certificates/services.py` / `views.py`) | Todos | — | **Cualquier reporte de su institución**, aunque no lo vea en su propia bandeja | Solo los propios |
| Verificar un certificado por hash (`certificates:verify`) | — | — | — | Público, sin login (es la página de verificación externa, así está pensada) |
| Habilitar alumnos autorregistrados (`apps/accounts/views.py`, `_ensure_can_manage_pending_students`) | Sí | Sí | Sí (ambos roles) | No |
| Panel `/admin/` de Django | Sí | En la práctica, no (ver siguiente sección) | No | No |

La fila que más llama la atención: para **análisis y certificados**, el corte
es "¿eres alumno?" en vez de "¿eres tú o eres admin?" — así que un profesor o
director puede analizar o certificar un documento de su institución **aunque
ese documento no le aparezca en su propia bandeja**.

## El rol ADMIN no da acceso real a `/admin/`

El campo `role=ADMIN` de la app no está conectado con el flag nativo de
Django `is_staff`, que es el que de verdad controla el acceso al panel de
administración. Nada en el código pone `is_staff=True` a partir del rol.
`templates/base.html` sí muestra el enlace "Administración" a cualquier
usuario con `is_admin_role=True`, pero si esa cuenta no tiene `is_staff=True`
puesto a mano, el enlace lleva a una pantalla de login/permiso denegado. Hoy,
en la práctica, **solo los superusuarios pueden usar `/admin/`.**

## Inconsistencias conocidas (sin corregir todavía)

1. El enlace "Administración" se muestra a usuarios ADMIN que luego no pueden
   entrar de verdad (ver sección anterior).
2. TEACHER y DIRECTOR pueden analizar/certificar documentos de toda la
   institución aunque su propia bandeja les oculte esos mismos documentos —
   asimetría real entre módulos (normalmente pasa desapercibida porque hay
   que conocer/adivinar el UUID del documento).
3. DIRECTOR (un rol que suena supervisor) no tiene ninguna visibilidad
   ampliada respecto a ADMIN — se comporta igual que TEACHER en todos lados,
   salvo la excepción del punto 2.
4. `ReportDetailView._get_allowed_document` y
   `DownloadHighlightedDocumentView._get_allowed_document`
   (`apps/reports/views.py`) son dos copias idénticas de la misma lógica en
   vez de un único helper compartido — si se corrige el alcance en una y no
   en la otra, se reabre el hueco.
5. El certificado se bloquea solo si `risk_level == HIGH`
   (`CertificateGenerationService._validate_report`); existe un nivel
   `CRITICAL` (más alto que `HIGH`) que hoy **no** está bloqueado — parece un
   descuido, no algo intencional.
6. `base.html` comprueba `is_admin_role or is_superuser` en dos sitios; como
   `is_admin_role` ya incluye a los superusuarios, el `or is_superuser` no
   hace nada (inofensivo, pero es ruido).
