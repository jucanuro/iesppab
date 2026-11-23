Estado: Borrador — BLOQUEADO hasta G1

# ORI-DOC · Requisitos (v1, entregas y versiones)

> Sin este archivo aprobado no hay código. Fuente: `docs/proc/DOC.md`. Redactado por el PM virtual el 2026-11-22; no es una decisión del Gerente TI.

## Contexto y alcance

Entra: trabajos con versiones, exclusión de versiones hermanas en la similitud interna, límite de envíos, envíos extra, autorización del docente, historial, visibilidad del informe al alumno, vínculo manual de duplicados, migración de datos existentes. [Propuesto]

No entra: cambios en el cálculo de similitud o IA (ORI-SIM, ORI-IA), certificados (ORI-CER), ni el módulo de acceso a `/admin/`. [Propuesto]

Evidencia de origen: informe de «Proyecto directora» con 48,3 % contra el documento «PROYECTO» de `ronald` (id 38a0ee84…), 1.052 de 2.178 palabras. [Validado con evidencia]

## Trazabilidad HU → REQ

| HU | REQ | Etiqueta |
|---|---|---|
| HU-DOC-01 | REQ-DOC-01, REQ-DOC-02 | [Propuesto] |
| HU-DOC-02 | REQ-DOC-03 | [Validado con usuario] |
| HU-DOC-03 | REQ-DOC-04 | [Validado con usuario] |
| HU-DOC-04 | REQ-DOC-05 | [Validado con usuario] |
| HU-DOC-05 | REQ-DOC-06 | [Supuesto] |
| HU-DOC-06 | REQ-DOC-07 | [Propuesto] |
| HU-DOC-07 | REQ-DOC-08 | [Propuesto] |
| HU-DOC-08 | REQ-DOC-09 | [Propuesto] |
| RN-DOC-04 | REQ-DOC-10 | [Propuesto] |

## Requisitos (EARS)

### REQ-DOC-01
- **HU:** HU-DOC-01
- **Criterio:** EL SISTEMA DEBE representar cada trabajo como una entidad con una o más versiones ordenadas, donde la última es la vigente.
- **Verificación:** test de modelo y de servicio.
- **Etiqueta:** [Propuesto]

### REQ-DOC-02
- **HU:** HU-DOC-01
- **Criterio:** CUANDO se analiza una versión, EL SISTEMA DEBE excluir de los candidatos de similitud interna las demás versiones del mismo trabajo y los documentos vinculados como «mismo trabajo».
- **Verificación:** test con dos versiones del mismo texto: la similitud interna no debe contar la hermana.
- **Etiqueta:** [Propuesto]

### REQ-DOC-03
- **HU:** HU-DOC-02
- **Criterio:** EL SISTEMA DEBE mostrar el historial de versiones de un trabajo con fecha, quién subió, similitud, IA y enlace al informe de cada una.
- **Verificación:** test de vista por rol.
- **Etiqueta:** [Validado con usuario]

### REQ-DOC-04
- **HU:** HU-DOC-03
- **Criterio:** CUANDO un alumno sube una versión y el número de versiones del trabajo alcanzó su límite, EL SISTEMA DEBE rechazar el envío con un mensaje claro. El límite por defecto es 1 y su máximo configurable es 2.
- **Verificación:** test de servicio de subida.
- **Etiqueta:** [Validado con usuario]

### REQ-DOC-05
- **HU:** HU-DOC-04
- **Criterio:** CUANDO un docente, asesor o admin habilita un envío extra, EL SISTEMA DEBE sumar uno al límite de ese trabajo y registrar quién, cuándo y motivo.
- **Verificación:** test de servicio y de auditoría.
- **Etiqueta:** [Validado con usuario]

### REQ-DOC-06
- **HU:** HU-DOC-05
- **Criterio:** SI un alumno no tiene autorización vigente del docente para el trabajo, ENTONCES EL SISTEMA DEBE impedir que suba una versión.
- **Verificación:** test por rol. Alcance de «publicar» pendiente de la pregunta abierta 1.
- **Etiqueta:** [Supuesto]

### REQ-DOC-07
- **HU:** HU-DOC-06
- **Criterio:** DONDE el docente configure la visibilidad del informe, EL SISTEMA DEBE mostrar al alumno solo las cifras permitidas (ninguna, similitud, IA o ambas).
- **Verificación:** test de vista del alumno.
- **Etiqueta:** [Propuesto]

### REQ-DOC-08
- **HU:** HU-DOC-07
- **Criterio:** CUANDO un admin vincule dos documentos de distinto propietario como «mismo trabajo», EL SISTEMA DEBE excluirlos mutuamente de la similitud interna sin borrar ninguno, y permitir recalcular los informes afectados.
- **Verificación:** test con el caso 38a0ee84… vs «Proyecto directora».
- **Etiqueta:** [Propuesto]

### REQ-DOC-09
- **HU:** HU-DOC-08
- **Criterio:** CUANDO se aplique la migración, EL SISTEMA DEBE convertir cada documento existente en un trabajo con una versión, sin perder archivos, textos, informes ni certificados, y la migración DEBE ser reversible. Se exige respaldo `pg_dump` previo.
- **Verificación:** test de migración con datos de ejemplo; verificación manual con conteos antes y después.
- **Etiqueta:** [Propuesto]

### REQ-DOC-10
- **HU:** RN-DOC-04
- **Criterio:** EL SISTEMA DEBE auditar todo cambio de límite, habilitación, autorización o vínculo con usuario, fecha y motivo.
- **Verificación:** test de registro de auditoría.
- **Etiqueta:** [Propuesto]

### Fuera de verificación
- El efecto de estos cambios en la cercanía con Turnitin: No verificable.

## Reglas de negocio · Preguntas abiertas

Ver `docs/proc/DOC.md`: RN-DOC-01 a RN-DOC-06 y las 9 preguntas abiertas. Sin respuesta a las preguntas 1, 2, 3 y 6 no se puede pasar a diseño.
