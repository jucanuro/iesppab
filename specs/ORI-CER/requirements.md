Estado: Aprobado por Gerente TI 2026-11-08

# ORI-CER · Requisitos v1 (fuente: docs/proc/CER.md, G1 aprobado por Gerente TI 2026-11-08)

> Etiquetas: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto]. Las respuestas a las preguntas bloqueantes 1–5 provienen de "Respuestas propuestas" de la HU y se etiquetan [Decisión Gerente] según la instrucción del Gerente TI. Pendiente de revisión: este archivo sigue en Borrador.

## Contexto y alcance
- **Objetivo:** entregar dos informes PDF imprimibles (similitud; IA estimada) y un certificado cuyos datos coinciden con la verificación pública por QR. [Decisión Gerente]
- **Actores:** Docente/staff, Estudiante, Tercero verificador, Sistema.
- **Entra:** PDF de similitud (reemplaza "Documento señalado", `reports:download_highlighted`); PDF de IA estimada; "IA estimada" + banda + fecha + versión del motor + nota en certificado y QR; control de permisos de descarga; cálculo del % total; reemisión opcional (D-11); validez de los 13 certificados previos. [Decisión Gerente]
- **No entra:** diagnóstico del error 500 del QR (#1) hasta contar con la URL real (HU-CER-04); cambios en los motores de similitud/IA; bloqueo de certificado por IA (RN-CER-01 solo por similitud). [Decisión Gerente]
- **Contradicciones detectadas en código (no son REQ):**
  - [Supuesto] `AI_ESTIMATE_DISCLAIMER` (`apps/reports/models.py:19`) dice "Estimación indicativa. La detección de IA no es concluyente…", distinto del texto aprobado (R3). Habrá que unificarlo; decisión de diseño, no de requisitos.
  - [Supuesto] El certificado actual usa `report.AI_CLASSIFIER_NOTE` cuando hay clasificador (`services.py:416`), lo que podría diferir de RN-CER-03.
  - [Supuesto] `engine_version` existe en el modelo de reportes (`reports/models.py:183`); falta confirmar que sea la "versión del motor" de la HU.
  - [Supuesto] La generación actual filtra por propietario si el emisor es estudiante (`certificates/services.py:133`); verificar compatibilidad con R1.

## Trazabilidad HU → REQ
| HU | REQ | Etiqueta |
|---|---|---|
| HU-CER-01 | (vigente en prod; sin REQ nuevo) | [Supuesto] |
| HU-CER-02 | REQ-CER-09 | [Supuesto] |
| HU-CER-03 | REQ-CER-01, REQ-CER-02 | [Supuesto] |
| HU-CER-04 | REQ-CER-02 (500 original fuera de alcance) | [Supuesto] |
| HU-CER-05 | Absorbida en HU-CER-06/07 | [Supuesto] |
| HU-CER-06 | REQ-CER-03, REQ-CER-04, REQ-CER-07, REQ-CER-10 | [Decisión Gerente] |
| HU-CER-07 | REQ-CER-05, REQ-CER-07, REQ-CER-10 | [Decisión Gerente] |
| HU-CER-08 | REQ-CER-06 | [Decisión Gerente] |
| HU-CER-09 | REQ-CER-01, REQ-CER-06, REQ-CER-08 | [Decisión Gerente] |
| HU-CER-10 | REQ-CER-11 | [Decisión Gerente] (opcional) |

## Requisitos (EARS)

### REQ-CER-01 · Verificación pública por QR
- **HU:** HU-CER-03, HU-CER-09 · **AC:** AC-CER-01
- **Criterio:** CUANDO un tercero abre la verificación de un certificado activo, EL SISTEMA DEBE responder 200 con código, fecha, versión del motor, % de similitud, "IA estimada" con banda y la nota de uso responsable.
- **Verificación:** test + Playwright.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-02 · Certificado inexistente o inactivo
- **HU:** HU-CER-03, HU-CER-04 · **AC:** AC-CER-02
- **Criterio:** SI el hash no existe o el certificado está inactivo, ENTONCES EL SISTEMA DEBE mostrar "no válido" sin error 500.
- **Verificación:** test.
- **Etiqueta:** [Supuesto] (HU-CER-04 sin URL real; el 500 original es No verificable)

### REQ-CER-03 · PDF de similitud: contenido
- **HU:** HU-CER-06 · **AC:** AC-CER-03
- **Criterio:** CUANDO se genera el PDF de similitud, EL SISTEMA DEBE incluir cabecera institucional, % total, % interno, % web, tabla de fuentes con su % y texto con segmentos señalados.
- **Verificación:** test de contenido PDF.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-04 · PDF de similitud: nota de cálculo
- **HU:** HU-CER-06 · **AC:** AC-CER-04
- **Criterio:** CUANDO se genera el PDF de similitud, EL SISTEMA DEBE incluir la nota de cálculo de RN-CER-06.
- **Verificación:** test de contenido PDF.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-05 · PDF de IA estimada
- **HU:** HU-CER-07 · **AC:** AC-CER-05
- **Criterio:** CUANDO se genera el PDF de IA, EL SISTEMA DEBE incluir el % rotulado "IA estimada", banda, fecha, versión del motor, segmentos señalados con probabilidad, método (modelo, versión), limitaciones y la nota de uso responsable.
- **Verificación:** test de contenido PDF.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-06 · Coherencia certificado ↔ QR
- **HU:** HU-CER-08, HU-CER-09 · **AC:** AC-CER-06
- **Criterio:** CUANDO se muestran el certificado y su verificación QR, EL SISTEMA DEBE mostrar valores idénticos de % de IA, banda, fecha, versión del motor y nota (y los mismos que el PDF de IA, RN-CER-03).
- **Verificación:** Playwright + test de contenido PDF.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-07 · Permisos de descarga
- **HU:** HU-CER-06, HU-CER-07 · **AC:** AC-CER-07
- **Criterio:** SI el usuario no es docente/staff de la institución del documento ni el estudiante dueño, ENTONCES EL SISTEMA DEBE negar la descarga de ambos PDF. (Respuesta 1: descargan el docente/staff y el estudiante dueño; nadie más.)
- **Verificación:** test.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-08 · Confidencialidad en verificación
- **HU:** HU-CER-09 · **RN:** RN-CER-04
- **Criterio:** EL SISTEMA NO DEBE exponer en la verificación pública el texto del documento ni sus segmentos. La verificación mantiene el nombre del estudiante (Respuesta 4).
- **Verificación:** test (la respuesta no contiene texto del documento).
- **Etiqueta:** [Supuesto] (confidencialidad) / [Decisión Gerente] (nombre del estudiante)

### REQ-CER-09 · Bloqueo de certificado por riesgo
- **HU:** HU-CER-02 · **RN:** RN-CER-01
- **Criterio:** SI el riesgo del reporte es Alto o Crítico por similitud, ENTONCES EL SISTEMA DEBE impedir la emisión del certificado.
- **Verificación:** test.
- **Etiqueta:** [Decisión Gerente]

### REQ-CER-10 · Reporte no terminado
- **HU:** HU-CER-06, HU-CER-07 · **CU:** CU-CER-03/04 (alterno)
- **Criterio:** SI el reporte no está terminado, ENTONCES EL SISTEMA DEBE mostrar un mensaje en lugar de generar el PDF.
- **Verificación:** test.
- **Etiqueta:** [Propuesto] (CU alterno de la HU)

### REQ-CER-11 · Reemisión (opcional, D-11)
- **HU:** HU-CER-10 · **CU:** CU-CER-06
- **Criterio:** CUANDO un estudiante con certificado previo solicita su reemisión, EL SISTEMA DEBE inactivar el certificado anterior y emitir uno nuevo que incluya la nota de uso responsable.
- **Verificación:** test.
- **Etiqueta:** [Decisión Gerente] (opcional)

### REQ-CER-12 · Diseño para impresión
- **HU:** HU-CER-06, HU-CER-07 · **AC:** AC-CER-08
- **Criterio:** DONDE el documento sea un PDF de informe, EL SISTEMA DEBE usar formato A4 con márgenes, paginación "Página X de N" y legibilidad sin depender del color (RN-CER-07).
- **Verificación:** que "se vea bien impreso" es **No verificable**; sí son verificables A4, paginación "Página X de N" y la presencia de rótulos de texto además del color.
- **Etiqueta:** [Propuesto]

## Reglas de negocio
| ID | Regla | Etiqueta |
|---|---|---|
| RN-CER-01 | Riesgo Alto o Crítico bloquea el certificado (solo por similitud) | [Decisión Gerente] D-03/D-04 |
| RN-CER-02 | Todo % de IA se rotula "IA estimada" con la nota de uso responsable: estimación, no prueba, requiere revisión humana | [Decisión Gerente] D-08 |
| RN-CER-03 | Certificado, PDF de IA y QR muestran el mismo valor, banda, fecha, versión del motor y nota | [Decisión Gerente] D-08 |
| RN-CER-04 | La verificación pública no expone el texto del documento ni sus segmentos | [Supuesto] |
| RN-CER-05 | Los 13 certificados emitidos siguen válidos | [Decisión Gerente] D-11 |
| RN-CER-06 | El % total es la unión de segmentos sin doble conteo; la suma de % por fuente puede superarlo porque un segmento coincide con varias fuentes. La spec debe documentarlo | [Decisión Gerente] D-09 |
| RN-CER-07 | PDF para impresión A4, paginación "Página X de N", sin depender del color | [Propuesto] |
| RN-CER-08 | Texto único de la nota de uso responsable: "IA estimada: valor probabilístico calculado por un modelo automático. No constituye prueba del uso de inteligencia artificial y requiere la revisión de un docente antes de cualquier decisión académica." | [Decisión Gerente] (Respuesta 3) |
| RN-CER-09 | El PDF de similitud reemplaza al "Documento señalado" (`download_highlighted`), reutilizando su resaltado | [Decisión Gerente] (Respuesta 2) |

## Preguntas abiertas
| # | Pregunta | Estado |
|---|---|---|
| 1 | ¿Quién descarga cada PDF? | Resuelta: docente/staff y estudiante dueño [Decisión Gerente] → REQ-CER-07 |
| 2 | ¿PDF de similitud reemplaza "Documento señalado"? | Resuelta: reemplaza [Decisión Gerente] → RN-CER-09 |
| 3 | Texto de la nota de uso responsable | Resuelta [Decisión Gerente] → RN-CER-08 |
| 4 | ¿QR muestra el nombre del estudiante? | Resuelta: sí [Decisión Gerente] → REQ-CER-08 |
| 5 | URL real del QR (#1) | Abierta: fuera de alcance hasta tenerla; REQ-CER-02 cubre solo el caso no válido sin 500 |
| 6 | ¿Qué ocurre con los certificados previos en el QR (sin IA estimada)? ¿Se muestran sin esos campos o se regeneran? (RN-CER-05 exige que sigan válidos) | Abierta [Supuesto] — a decidir en design |
| 7 | ¿Se unifica `AI_ESTIMATE_DISCLAIMER` actual con RN-CER-08? | Abierta [Supuesto] — a decidir en design |
