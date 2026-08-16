# Gobernanza de tareas y revisiones de Ludex

Este documento define cómo se coordinan el usuario, el tech lead, el chat que
opera Linear y los agentes implementadores. Se lee al asignar trabajo, al pedir
una revisión y antes de cerrar una fase.

`docs/PLAN.md` sigue siendo la fuente de verdad del alcance y del orden del
proyecto. Este documento no reemplaza el plan: define cómo se ejecuta y se
juzga.

## Responsabilidades

### Tech lead

- Traduce el plan en paquetes de tareas con alcance y aceptación verificables.
- Revisa diagnósticos antes de autorizar arreglos de alto riesgo.
- Inspecciona el repositorio y ejecuta verificaciones independientes.
- Emite el único veredicto técnico que permite cerrar o rechazar una tarea.
- No escribe código salvo que sea estrictamente necesario y el usuario lo
  autorice.

### Chat operador de Linear

- Crea proyectos, milestones, tareas, subtareas, relaciones y etiquetas a
  partir del paquete entregado por el tech lead.
- Mantiene en Linear el estado, responsable, prioridad, dependencias y
  comentarios.
- Aplica literalmente los veredictos del tech lead.
- No decide por su cuenta si una implementación es correcta.

### Agente implementador

- Inspecciona antes de diseñar y respeta `AGENTS.md` y las skills de `.claude/`.
- Publica diagnóstico, hipótesis, progreso y bloqueos en la tarea.
- Implementa únicamente el alcance autorizado.
- Entrega evidencia reproducible y verifica que el test detecte la regresión.
- Puede mover una tarea a `In Review`; nunca puede autoaprobarla.

### Code reviewer independiente (Tasos)

- Revisa en modo read-only el rango exacto `Base SHA..Head SHA`.
- Contrasta código, tests y evidencia contra el plan y las decisiones del repo.
- Publica findings reproducibles con severidad calibrada y una recomendación.
- No modifica código, no mueve tareas y no emite el veredicto final.
- Sigue el contrato completo de `code_review_best_practices.md`.

### Usuario

- Conserva la autoridad de producto y resuelve cambios de alcance.
- Pasa al tech lead el issue o `REVIEW PACKET` cuando no haya acceso directo a
  Linear.
- Pasa el `LINEAR_VERDICT` al chat operador cuando el tech lead no tenga acceso
  de escritura a Linear.

## Fuentes de verdad

| Información | Fuente de verdad |
|---|---|
| Alcance y orden de fases | `docs/PLAN.md` |
| Estado, responsable, prioridad y dependencias | Linear |
| Diagnóstico y progreso temporal | Comentarios de Linear |
| Decisiones arquitectónicas permanentes | `docs/DECISIONS.md` |
| Diseño aprobado | `docs/superpowers/specs/` |
| Plan de implementación | `docs/superpowers/plans/` |
| Métricas de modelos | `docs/BENCHMARKS.md` |
| Evidencia ejecutable | Código, tests y migraciones |
| Protocolo del reviewer independiente | `code_review_best_practices.md` |
| Veredicto técnico | Revisión del tech lead, reflejada después en Linear |

## Selección de modelos para workers

- La capacidad máxima de razonamiento no es el valor por defecto: se elige por
  riesgo y complejidad, considerando también latencia y consumo.
- Neoblex y Andromeda usan por defecto el modelo suficiente más económico y
  escalan sólo ante los riesgos definidos en `code_review_best_practices.md`.
- **Galileo** no recibe trabajo ordinario: se reserva para problemas
  extremadamente difíciles y sólo por autorización explícita del tech lead.
- Tasos usa `Terra High` como reviewer habitual y `Sol High` para protocolo,
  dataset, migraciones, seguridad o concurrencia; `xhigh` no es el default.
- Una vez resuelto el checkpoint difícil, la implementación puede bajar de
  esfuerzo si el contrato ya quedó cerrado.

No duplicar documentos completos entre Linear y el repo. Linear enlaza los
artefactos permanentes del repo y el repo menciona el identificador de Linear
cuando haga falta trazabilidad.

## Estados de una tarea

| Estado | Significado |
|---|---|
| `Backlog` | Todavía no autorizada o no refinada. |
| `Ready` | Alcance, aceptación y dependencias están definidos. |
| `In Progress` | Diagnóstico o implementación activos. |
| `In Review` | El agente entregó un `REVIEW PACKET` completo. |
| `Changes Requested` | La revisión encontró defectos corregibles. |
| `On Hold` | Existe un bloqueo externo o falta una decisión del usuario. |
| `Completed` | El tech lead verificó y aceptó el trabajo. |
| `Rejected` | Enfoque descartado, duplicado o incompatible con el plan. |

Una revisión fallida normal termina en `Changes Requested`, no en `Rejected`.
`Rejected` se reserva para trabajo que no debe continuar.

## Flujo de una tarea

1. El tech lead define objetivo, alcance, fuera de alcance, dependencias,
   archivos probables, riesgos y criterios de aceptación.
2. El chat operador crea la tarea en Linear sin reinterpretar el contenido.
3. El agente la mueve a `In Progress` y publica primero el diagnóstico.
4. Si encuentra una discrepancia con el esquema, el plan o la realidad, detiene
   la implementación y mueve la tarea a `On Hold` con evidencia.
5. Antes del arreglo debe existir una reproducción y, cuando corresponda, un
   test que falle.
6. El agente implementa un solo cambio conceptual por ciclo de revisión.
7. Verifica el arreglo, rompe deliberadamente la función corregida y demuestra
   que el test de regresión falla.
8. Publica el `REVIEW PACKET` y mueve la tarea a `In Review`.
9. Tasos revisa todo cambio de código, datos, migraciones o protocolo y publica
   su `TASOS REVIEW PACKET`. Documentación y cambios mecánicos pueden omitirlo
   sólo si el tech lead registra el motivo.
10. El implementador corrige los findings que el tech lead valide y Tasos
    verifica la nueva revisión.
11. El tech lead inspecciona código, decisiones, tests, datos y resultados de
    forma independiente.
12. El tech lead emite un `LINEAR_VERDICT`.
13. El chat operador aplica el estado indicado y publica el veredicto completo
    en la tarea.

## REVIEW PACKET obligatorio

Ninguna tarea entra a revisión sin este comentario completo. Las secciones que
no correspondan deben decir `No aplica` y explicar por qué.

```text
REVIEW PACKET

Issue:
Commit(s):
Archivos modificados:

Causa raíz:
Solución aplicada:

Tests agregados:
Comando de verificación:
Resultado completo:

Prueba de regresión:
- Cómo se rompió deliberadamente el arreglo:
- Qué test falló:
- Resultado después de restaurarlo:

Integraciones ejecutadas:
Datos inspeccionados:

Decisiones agregadas a DECISIONS.md:
Limitaciones conocidas:
Riesgos o dudas pendientes:
```

Si falta evidencia necesaria, el tech lead puede devolver la tarea a
`Changes Requested` sin completar la revisión profunda.

## Formato del veredicto

El tech lead responde con un bloque que el chat operador puede ejecutar sin
interpretación:

```yaml
LINEAR_VERDICT:
  issue: LUD-123
  status: Completed
  reviewed_commit: abc1234
  summary: "Cumple los criterios de aceptación y la regresión está ejercida."
  required_changes: []
  follow_up_issues: []
```

Ejemplo de correcciones solicitadas:

```yaml
LINEAR_VERDICT:
  issue: LUD-123
  status: Changes Requested
  reviewed_commit: abc1234
  summary: "El test puede pasar sin ejercer el protocolo actualizado."
  required_changes:
    - "Agregar un fixture donde request preceda la narración."
    - "Demostrar que al revertir el arreglo el test falla."
  follow_up_issues: []
```

## Criterios de revisión

El tech lead no acepta una tarea solo porque su agente informó éxito o porque
una suite terminó en verde. La revisión verifica, según corresponda:

- Fidelidad a `docs/PLAN.md` y a las decisiones existentes.
- Causa raíz demostrada, no inferida.
- Test rojo antes del arreglo y verde después.
- Prueba deliberada de que el test detecta la regresión.
- Suites relacionadas e integración real.
- Invariantes sobre todo el dataset, no solo la corrida nueva.
- Ausencia de fugas de información y de acciones fuera de su máscara.
- Generación parametrizada.
- Migraciones reversibles y esquema real inspeccionado.
- Decisiones no triviales registradas.
- Limitaciones conocidas explícitas y con impacto acotado.

## Cadencia de consulta

El tech lead relee este documento:

- al comenzar una sesión de planificación o auditoría;
- antes de emitir nuevas tareas;
- al recibir un `REVIEW PACKET`;
- antes de aceptar una tarea;
- antes de declarar cerrada una fase.

Al asumir contexto resumido o después de una pausa larga, consulta también
`docs/PLAN.md`, `docs/DECISIONS.md`, `AGENTS.md` y los handoffs vigentes antes
de emitir un juicio.
