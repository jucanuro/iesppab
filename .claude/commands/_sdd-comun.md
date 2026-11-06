Reglas comunes SDD (leer antes de actuar; este archivo no es un comando).

- Módulos válidos: SIM, IA, CER, DOC, INFRA. $ARGUMENTS = módulo (acepta "SIM" o "ORI-SIM"). Si es inválido o falta, detente y dilo.
- Carpeta: specs/ORI-<MOD>/. Estado = primera línea de cada archivo: `Estado: Borrador` o `Estado: Aprobado por <quién> <fecha>`.
- Un archivo está APROBADO solo si su primera línea empieza con `Estado: Aprobado por` y no contiene "BLOQUEADO". Nunca cambies ese campo a Aprobado: solo el Gerente TI.
- Etiquetas de hechos: [Validado con usuario] [Propuesto] [Decisión Gerente] [Supuesto]. Etiqueta todo hecho que escribas.
- EARS: CUANDO / MIENTRAS / SI…ENTONCES / DONDE + "EL SISTEMA DEBE". IDs REQ-<MOD>-NN trazados a la HU. Lo no comprobable: "No verificable".
- Prohibido: git commit, git push, tocar producción, imprimir secretos (.env*, claves, contraseñas en docs/).
- Si te niegas por un paso previo sin aprobar, di exactamente qué archivo y qué estado tiene y qué falta.
