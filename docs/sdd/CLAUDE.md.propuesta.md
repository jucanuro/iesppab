# Propuesta de cambios a CLAUDE.md (pendiente de OK del Gerente TI)

Estado: Borrador — NO aplicada. `CLAUDE.md` actual queda intacto hasta que se apruebe.
Propuesta: **añadir** la sección siguiente al final de `CLAUDE.md` (no se borra nada de lo existente).

Corrección sugerida a texto existente [Propuesto]: la sección `apps.certificates` dice "(WeasyPrint)", pero
`apps/certificates/services.py` genera el PDF con **ReportLab** (WeasyPrint no se importa). Cambiar a "ReportLab".

---

## Metodología VQL + SDD (Spec-Driven Development)

Proyecto **ORI** (Originalidad IESPP). DLF / aprobador único: Gerente TI.

### Módulos ORI
| Código | Módulo | Alcance (propuesto) |
|---|---|---|
| ORI-SIM | Similitud | análisis de similitud (interna/OAI/web), fuentes, informe de similitud, exclusiones |
| ORI-IA | Detección de IA | perplejidad, segmentos, clasificador, indicadores de IA en el informe |
| ORI-CER | Certificados | emisión, verificación pública, descarga, revocación |
| ORI-DOC | Documentos y usuarios | carga de documentos, extracción de texto, cuentas, roles, instituciones |
| ORI-INFRA | Infraestructura | Celery/Redis, settings/flags, cosecha OAI, cachés, despliegue, logging |

### Estructura de specs
`specs/ORI-<MOD>/requirements.md` · `design.md` · `tasks.md`.
Cada archivo lleva en su **primera línea** `Estado: Borrador | Aprobado por <quién> <fecha>`.
**Solo el Gerente TI cambia ese campo.** Claude nunca lo edita a "Aprobado".

### Ramas
`main` ← `dev` ← `ORI-<MOD>/<tarea>` (p. ej. `ORI-SIM/T03-umbral-span`).

### Reglas de la metodología
1. Sin `requirements.md` aprobado no hay código. Sin `design.md` aprobado no hay `tasks.md`. Sin `tasks.md` aprobado no hay implementación.
2. Un cambio de alcance vuelve a `requirements.md` (estado vuelve a Borrador).
3. Criterios en **EARS**: `CUANDO …` / `MIENTRAS …` / `SI … ENTONCES …` / `DONDE …` + `EL SISTEMA DEBE …`.
4. IDs `REQ-<MOD>-NN` (p. ej. `REQ-SIM-01`), cada uno trazado a su HU. Lo que no se pueda comprobar se marca **No verificable**.
5. Etiquetas de hechos: `[Validado con usuario]` `[Propuesto]` `[Decisión Gerente]` `[Supuesto]`.
6. Comandos: `/sdd-requirements <MOD>`, `/sdd-design <MOD>`, `/sdd-tasks <MOD>`, `/sdd-implement <MOD>` (ver `.claude/commands/`).

### Reglas de trabajo de Claude en este repo
- **No hacer `git commit` ni `git push`**: los hace el Gerente TI. Claude solo propone el mensaje de commit.
- **No tocar producción** (ni servidores, ni `.env` de producción, ni comandos contra BD de producción).
- **No imprimir secretos** (claves, tokens, contraseñas, contenido de `.env*`, ni credenciales que aparezcan en `docs/`).
