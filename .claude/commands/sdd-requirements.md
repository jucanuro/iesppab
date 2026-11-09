---
description: SDD paso 1 — redacta specs/ORI-<MOD>/requirements.md en EARS desde la HU/CU/RN
argument-hint: <SIM|IA|CER|DOC|INFRA>
---
Módulo: $ARGUMENTS

1. Lee `.claude/commands/_sdd-comun.md` y aplica sus reglas.
2. Verifica que existe `specs/ORI-<MOD>/requirements.md`. Si su primera línea ya dice `Estado: Aprobado por`, NO lo sobrescribas: detente y explica que un cambio de alcance requiere que el Gerente TI lo devuelva a Borrador.
3. Fuente de HU/CU/RN: `docs/proc/<MOD>.md`; si no existe, usa lo que el Gerente haya pegado en la conversación. Si no hay ninguna de las dos, detente y pide la HU/CU/RN (no la inventes).
4. Escribe `specs/ORI-<MOD>/requirements.md` con primera línea `Estado: Borrador`, y:
   - Contexto y alcance del módulo (qué entra / qué no).
   - Tabla de trazabilidad HU → REQ.
   - Un REQ-<MOD>-NN por requisito, en EARS, con: HU de origen, criterio de aceptación, etiqueta de hecho, y "No verificable" cuando corresponda.
   - Reglas de negocio (RN) y preguntas abiertas.
5. Puedes leer el código solo para detectar contradicciones; márcalas [Supuesto], no las conviertas en REQ sin respaldo de la HU.
6. DETENTE. Resume y pide revisión del Gerente TI. No avances a design.
