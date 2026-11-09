---
description: SDD paso 2 — con requirements aprobado, redacta specs/ORI-<MOD>/design.md
argument-hint: <SIM|IA|CER|DOC|INFRA>
---
Módulo: $ARGUMENTS

1. Lee `.claude/commands/_sdd-comun.md` y aplica sus reglas.
2. Comprueba `specs/ORI-<MOD>/requirements.md`: debe estar `Estado: Aprobado por …`. Si no, NIÉGATE y di qué falta (archivo, estado actual).
3. Si `design.md` ya está aprobado, no lo sobrescribas; explica y detente.
4. Escribe `specs/ORI-<MOD>/design.md` (primera línea `Estado: Borrador`) con: modelo de datos (modelos, campos, migraciones), vistas/URLs, componentes/servicios, flags y constantes de configuración, riesgos y alternativas descartadas, y una matriz REQ → elemento de diseño (todo REQ debe quedar cubierto). Etiqueta cada hecho.
5. Si existe un borrador "as-is", úsalo como base y distingue lo existente de lo nuevo.
6. DETENTE y pide revisión del Gerente TI. No escribas tasks.md ni código.
