# ORI-DOC · Entregas y versiones (HU / CU / RN)

Estado: Borrador (propuesto por el PM virtual, 2026-11-22). Nada de esto está aprobado por el Gerente TI.

Etiquetas: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto].

## Contexto

- La directora pidió (presentación 2026-11-11) un límite de 1 o 2 envíos por alumno según asesor/docente, con envíos extra habilitados por el docente, e historial de versiones. [Validado con usuario]
- El 2026-11-19 el Gerente indicó que los alumnos solo podrán publicar si el docente lo autoriza, y que solo `jucanuro` será ADMIN. [Validado con usuario]
- Hallazgo 2026-11-22: el informe de «Proyecto directora» pasó de 0,84 % a 48,3 % porque el documento «PROYECTO» de otra cuenta (`ronald`, subido 2026-11-18, .docx) contiene el mismo texto y entró al índice interno. La similitud interna compara contra todo documento activo de la institución, sin noción de «mismo trabajo». [Validado con evidencia: informe PDF y consulta a la BD de producción]
- Hoy `Document` no tiene trabajo padre, versión, límite de envíos ni autorización de docente. Sí tiene `owner`, `uploaded_by`, `advisor`, `sha256_hash`, `course_name`, `academic_period`. [Validado: apps/documents/models.py]
- Módulo: se propone ampliar ORI-DOC. Si el Gerente prefiere un módulo nuevo (ORI-ENT), hay que registrarlo como decisión, porque `_sdd-comun.md` solo admite SIM, IA, CER, DOC e INFRA. [Propuesto]

## Historias de usuario

| HU | Como… | Quiero… | Para… | Etiqueta |
|---|---|---|---|---|
| HU-DOC-01 | Docente / asesor | que las versiones de un mismo trabajo no se comparen entre sí | que reenviar una corrección no dispare un 48 % de similitud contra uno mismo | [Propuesto] |
| HU-DOC-02 | Docente / asesor | ver el historial de versiones de un trabajo con fecha, quién subió, similitud e IA | seguir la evolución y comparar versiones | [Validado con usuario] |
| HU-DOC-03 | Directora / docente | limitar los envíos por trabajo (defecto 1, hasta 2) | evitar probar el sistema hasta bajar la cifra | [Validado con usuario] |
| HU-DOC-04 | Docente / asesor | habilitar envíos extra a un alumno concreto | permitir una corrección justificada | [Validado con usuario] |
| HU-DOC-05 | Docente | autorizar que un alumno envíe o publique su trabajo | controlar qué entra al sistema | [Validado con usuario; alcance de «publicar» por confirmar] |
| HU-DOC-06 | Docente | decidir si el alumno ve similitud, IA o ambas | evitar que el alumno use la cifra para «afinar» el texto | [Propuesto] |
| HU-DOC-07 | Admin | marcar dos documentos de distinto dueño como «mismo trabajo» | excluir duplicados como el de `ronald` sin borrarlos | [Propuesto] |
| HU-DOC-08 | Gerente TI | migrar los documentos existentes sin pérdida | no perder historial ni certificados | [Propuesto] |

## Casos de uso (resumen)

- **CU-DOC-01 Subir nueva versión:** el alumno elige un trabajo existente y sube archivo. El sistema valida el límite, crea la versión N+1, analiza y excluye las versiones hermanas de la similitud interna.
- **CU-DOC-02 Habilitar envío extra:** el docente/asesor abre el trabajo del alumno, suma 1 envío y registra motivo. El sistema guarda quién, cuándo y por qué.
- **CU-DOC-03 Autorizar entrega:** el docente habilita al alumno para un trabajo/curso/periodo. Sin habilitación el alumno no puede subir.
- **CU-DOC-04 Vincular duplicado:** el admin selecciona dos documentos y los marca como «mismo trabajo». El sistema recalcula la similitud del afectado.
- **CU-DOC-05 Ver historial:** lista de versiones con cifras y enlace a cada informe.

## Reglas de negocio propuestas

- RN-DOC-01 Un trabajo tiene 1 o más versiones; la última es la vigente. [Propuesto]
- RN-DOC-02 Límite por defecto: 1 envío por trabajo; máximo configurable: 2; extras solo por docente/asesor/admin. [Validado con usuario: «1 o 2»]
- RN-DOC-03 Las versiones del mismo trabajo y los documentos vinculados como «mismo trabajo» no son candidatos de similitud interna entre sí. [Propuesto]
- RN-DOC-04 Todo cambio de límite, habilitación o vínculo queda auditado (quién, cuándo, motivo). [Propuesto]
- RN-DOC-05 Solo `jucanuro` es ADMIN; director, docente y alumno tienen permisos menores. [Decisión Gerente 2026-11-19]
- RN-DOC-06 La IA sigue siendo indicativa; este módulo no cambia cómo se calcula ni su efecto en certificados. [Validado con usuario]

## Preguntas abiertas

1. ¿«Publicar» significa subir el documento, que se le emita certificado, o que el informe sea visible? La redacción actual asume subir.
2. ¿El límite es por trabajo, por curso/tarea o por periodo?
3. ¿Quién fija el límite: directora, docente del curso o asesor?
4. ¿El alumno ve similitud, IA, ambas o ninguna? ¿Cuál es el defecto?
5. ¿Un duplicado con el mismo SHA-256 del mismo dueño se rechaza o se enlaza como versión?
6. ¿Los documentos que ya están en la base se vuelven «trabajos» automáticamente (una versión cada uno)?
7. ¿Se vincula «mismo trabajo» entre dueños distintos solo por admin, o también por docente?
8. ¿Se agrupan versiones por título, por elección explícita del alumno, o ambas?
9. ¿Un docente/director puede habilitar solo a sus alumnos asesorados (`advisor`) o a cualquiera de su institución?
