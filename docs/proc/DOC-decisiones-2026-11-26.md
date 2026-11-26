# ORI-DOC · Decisiones del Gerente TI sobre autorización de envíos

Fecha: 2026-11-26. Fuente: mensaje del Gerente TI (Juan Carlos) en el chat del proyecto. Etiqueta general: [Decisión Gerente]. Estas decisiones responden en parte las preguntas abiertas 1, 2, 3, 4 y 9 de `docs/proc/DOC.md`. El Gerente pidió implementarlas.

## Reglas

1. **Alumno en formación (STUDENT):** al subir debe elegir asesor o profesor (rol TEACHER o DIRECTOR). Ese asesor debe autorizar cada análisis. Siempre con autorización. [Decisión Gerente]
2. **Egresado:** el admin o el asesor puede marcar al alumno como egresado; desde entonces sube y analiza sin autorización. [Decisión Gerente]
3. **Docentes y asesores (TEACHER):** pueden probar el sistema con un máximo de 5 envíos. [Decisión Gerente]
4. **Admin y director:** sin límite. [Decisión Gerente]
5. **Registro público:** opción para marcar "Soy alumno" o "Soy exalumno". [Decisión Gerente]
6. **Exalumno (valida su tesis):** hasta 3 envíos. La primera solicitud debe ser validada por un asesor o docente, que ve un mensaje de primer envío de exalumno. Tras validar, ya no requiere autorización. [Decisión Gerente]

## Interpretaciones aplicadas por el PM (por confirmar)

- [Supuesto] El límite cuenta documentos subidos por la persona (no fallidos), con un campo para que el admin sume envíos extra por usuario.
- [Supuesto] El alumno en formación no tiene límite de número; cada envío requiere autorización.
- [Supuesto] "Asesor" es el campo `Document.advisor` ya existente.
- [Supuesto] "Publicar" se entiende como subir y analizar el documento (pregunta abierta 1).
- [Supuesto] Sin notificación por correo: no hay SMTP; el aviso es dentro del sistema.
- [Supuesto] Los documentos existentes quedan sin autorización requerida para no afectar nada de producción.

## Preguntas que siguen abiertas

- Versiones del mismo trabajo y exclusión entre ellas (REQ-DOC-01, 02, 08): no decididas.
- Visibilidad del informe al alumno (REQ-DOC-07): no decidida.
- ¿Los exalumnos validados pueden volver a subir más allá de 3 si el admin lo habilita? Se resuelve con el campo de envíos extra.
