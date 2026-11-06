---
description: SDD paso 4 — implementa UNA tarea de tasks.md con tests y propone el commit
argument-hint: <SIM|IA|CER|DOC|INFRA> [T<NN>]
---
Argumentos: $ARGUMENTS (módulo, y opcionalmente el ID de tarea; si falta, la primera `- [ ]` en orden)

1. Lee `.claude/commands/_sdd-comun.md` y aplica sus reglas.
2. Comprueba que `requirements.md`, `design.md` y `tasks.md` del módulo estén `Estado: Aprobado por …`. Si no, NIÉGATE y di cuál falta.
3. Verifica que la tarea no tenga dependencias sin marcar `[x]`. Si las tiene, niégate y dilo.
4. Verifica la rama actual: debe ser `ORI-<MOD>/<tarea>`; si no, avisa y no hagas cambios (no cambies de rama por tu cuenta).
5. Implementa SOLO esa tarea, siguiendo design.md y los patrones de CLAUDE.md (servicios sobre forms, textos en español). Si descubres que falta alcance, detente: eso vuelve a requirements.md.
6. Escribe/ajusta tests y córrelos (`python manage.py test <ruta>`). Si fallan, repórtalo tal cual, no marques `[x]`.
7. Marca la tarea `[x]` en tasks.md (único cambio permitido en ese archivo; no toques su línea Estado).
8. Propón el mensaje de commit (en español, con REQ cubiertos) como texto. NO ejecutes git add/commit/push. Detente: UNA tarea por vez.
