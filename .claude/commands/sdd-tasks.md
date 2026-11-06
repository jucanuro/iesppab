---
description: SDD paso 3 — con design aprobado, redacta specs/ORI-<MOD>/tasks.md
argument-hint: <SIM|IA|CER|DOC|INFRA>
---
Módulo: $ARGUMENTS

1. Lee `.claude/commands/_sdd-comun.md` y aplica sus reglas.
2. Comprueba que `requirements.md` y `design.md` de `specs/ORI-<MOD>/` estén `Estado: Aprobado por …`. Si alguno no, NIÉGATE y di cuál y su estado.
3. Si `tasks.md` ya está aprobado, no lo sobrescribas; explica y detente.
4. Escribe `tasks.md` (primera línea `Estado: Borrador`): tareas pequeñas (≤ ~½ día), ordenadas por dependencia, con casilla `- [ ] T<NN>`, y por tarea: REQ que cubre, archivos previstos, cómo se verifica (test concreto o comprobación manual), rama sugerida `ORI-<MOD>/T<NN>-<slug>`. Todo REQ debe estar cubierto por ≥1 tarea; lista los "No verificable" aparte.
5. DETENTE y pide revisión del Gerente TI.
