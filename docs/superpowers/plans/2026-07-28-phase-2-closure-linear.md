# Phase 2 Closure — Linear Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cerrar formalmente la Fase 2 de Ludex corrigiendo los defectos de
integridad conocidos, completando el grafo definido en `docs/PLAN.md` y
demostrando con evidencia independiente que el agente CLI juega batallas
completas y persiste un dataset confiable.

**Architecture:** El cierre se divide en un carril de estado/persistencia y
otro de grafo/decisión, unidos por contratos revisados antes de integrarse.
Linear conserva el estado operativo; el repositorio conserva decisiones,
planes, métricas y evidencia ejecutable. Ningún agente implementador puede
autoaprobar su trabajo.

**Tech Stack:** Python 3.12, uv, poke-env 0.15.0, LangGraph 1.2.9, LangChain,
Pydantic 2.13.4, SQLAlchemy 2.0.36, asyncpg 0.30.0, PostgreSQL 16 + pgvector,
dbmate 2.21, Node.js 22+, pnpm 11.1.2, TypeScript 5.7.2,
`@smogon/calc@0.11.0`, Vitest 2.1.8 y pytest 8.3.4.

## Global Constraints

- `docs/PLAN.md` es la fuente de verdad del alcance y el orden de fases.
- Leer `AGENTS.md` y `docs/AGENT_GOVERNANCE.md` antes de tomar una tarea.
- Leer la skill de `.claude/` correspondiente antes de tocar recorder, datos,
  calc, migraciones o tests.
- La generación es siempre un parámetro. No introducir `gen6` fuera de
  configuración, fixtures y comandos de verificación explícitos.
- El protocolo crudo es la fuente de verdad; nunca corregir el rival releyendo
  un objeto `Battle` mutable después de la decisión.
- La captura de la máscara legal y del mapa acción→`BattleOrder` permanece
  síncrona antes del primer `await`.
- Una decisión se identifica por `decision_index`, no por `turn_number`.
- Del rival se verifican las once claves observables: `species`, `hp_fraction`,
  `active`, `fainted`, `status`, `level`, `item`, `ability`, `types`, `boosts`
  y `moves`.
- Cero requests a internet durante una batalla salvo Showdown y APIs de LLM.
- API keys únicamente en variables de entorno; nunca en DB, logs, Linear,
  artefactos de benchmark o commits.
- El esquema real está en `db/migrations/`; los modelos SQLAlchemy son espejo.
- Toda migración tiene `migrate:down` correcto y una decisión documentada.
- Antes de borrar o modificar datos existentes se requiere backup y aprobación
  explícita del usuario.
- Nunca ejecutar `docker compose down`, `down -v`, `docker stop`, `docker rm`
  ni `brew services stop`.
- PostgreSQL de Ludex está en `127.0.0.1:15432`, Showdown en `8100` y calc en
  `8200`.
- No ejecutar dos suites de integración de Showdown en paralelo.
- Cada test nuevo debe fallar al romper deliberadamente el arreglo y volver a
  pasar al restaurarlo.
- Commits en inglés, un cambio conceptual por commit y rutas explícitas.
- Ignorar completamente `.worktrees/` ajenos y cambios no relacionados.

---

## 1. Instrucciones para el chat operador de Linear

Copiar esta sección junto con las doce tareas al chat que administra Linear.

### Mensaje de importación

```text
Crea en Linear el proyecto, milestone, estados, etiquetas y doce issues
definidos en este documento. Conserva literalmente los alias F2-00…F2-11,
descripciones, dependencias, criterios de aceptación y estados iniciales. No
resumas, combines, dividas ni autoasignes tareas. No marques ninguna issue
Completed: los implementadores solo pueden llegar a In Review y el tech lead
publicará el LINEAR_VERDICT. Al terminar, devuelve una tabla alias → Linear ID
→ URL → estado → bloqueadores y reporta cualquier campo que Linear no haya
podido representar.
```

### Proyecto

- **Nombre:** `Ludex — Phase 2 Stabilization and Closure`
- **Resumen:** Corregir integridad, completar el agente CLI según
  `docs/PLAN.md` y obtener la aprobación independiente del tech lead.
- **Milestone:** `Phase 2 accepted`
- **Documento canónico:** `docs/superpowers/plans/2026-07-28-phase-2-closure-linear.md`
- **Definición de cierre:** la tarea `F2-11` recibe un
  `LINEAR_VERDICT.status: Completed`.

### Estados

Crear o reutilizar:

1. `Backlog`
2. `Ready`
3. `In Progress`
4. `In Review`
5. `Changes Requested`
6. `On Hold`
7. `Completed`
8. `Rejected`

`Rejected` no significa “la implementación tiene errores”: para eso se usa
`Changes Requested`. `Rejected` se reserva para enfoques descartados,
duplicados o incompatibles con el plan.

### Etiquetas

- `phase-2`
- `blocker`
- `correctness`
- `dataset`
- `showdown-protocol`
- `graph`
- `calc`
- `database`
- `provider`
- `verification`
- `destructive-approval`
- `claude-lane`
- `sol-lane`
- `tech-lead`

### Reglas de creación

- Crear exactamente una issue por cada sección `F2-00` a `F2-11`.
- Conservar el alias `F2-XX` al comienzo del título aunque Linear asigne otro
  identificador.
- Copiar objetivo, evidencia, alcance, exclusiones, archivos, pasos, aceptación
  y review packet en la descripción de cada issue.
- Configurar las dependencias indicadas en el grafo; no reemplazarlas por texto.
- Estado inicial:
  - `F2-00`: `Ready`.
  - Las demás: `Backlog`, bloqueadas por sus dependencias.
- Un agente puede mover su issue hasta `In Review`, nunca a `Completed`.
- Aplicar estados finales únicamente a partir de un `LINEAR_VERDICT` del tech
  lead.
- Los comentarios de progreso no reemplazan el `REVIEW PACKET`.

## 2. Carriles y exclusión de archivos

### Carril Claude — estado, persistencia y auditor

Propietario recomendado de `F2-01` a `F2-05`.

- `apps/agent/src/ludex_agent/showdown/`
- `apps/agent/src/ludex_agent/state/`
- `apps/agent/src/ludex_agent/db/`
- `apps/agent/tests/showdown/`
- `apps/agent/tests/state/`
- `apps/agent/tests/db/`
- `apps/agent/tests/integration/test_play.py`
- `packages/dataset-audit/`
- Migraciones de identidad, dataset o persistencia.

### Carril GPT-5.6 Sol — contexto, calc, decisión y proveedores

Propietario recomendado de `F2-06` a `F2-10`.

- `apps/agent/src/ludex_agent/graph/`
- `apps/agent/tests/graph/`
- `apps/agent/src/ludex_agent/config.py`
- `apps/agent/src/ludex_agent/benchmark.py`
- `apps/agent/src/ludex_agent/eval_*`
- `apps/agent/tests/test_config.py`
- `apps/agent/tests/test_benchmark.py`
- `apps/agent/tests/test_eval_*`
- `packages/calc/`
- Migraciones de providers, models o metadata de decisión, solo después de que
  el carril Claude libere `db/migrations/`.

### Carril tech lead

Propietario de `F2-00` y `F2-11`. Revisa todos los `REVIEW PACKET`.

### Regla de exclusión

- Nadie modifica `showdown/client.py` mientras `F2-01` o `F2-02` estén
  `In Progress`.
- Solo una tarea con migraciones puede estar `In Progress`.
- El contrato de snapshot aceptado en `F2-01` se integra antes de que `F2-07`
  modifique el consumidor de calc.
- Máximo dos issues en `In Progress`, siempre con conjuntos de archivos
  disjuntos.

## 3. Grafo de dependencias

```text
F2-00 Baseline
 ├─→ F2-01 Estado fresco ─→ F2-02 Acción rechazada ─→ F2-03 Identidad
 │                                              │              │
 ├─→ F2-04 Auditor ─→ F2-05 Corpus             └──────┬───────┘
 └─→ F2-06 Retrieve context ────────────→ F2-07 Calc │
                                                      │
F2-02 + F2-03 ─→ F2-08 Decisión y metadata
F2-06 + F2-07 + F2-08 ─→ F2-09 Switch y paridad del grafo
F2-09 ─→ F2-10 Proveedores y operación
F2-01…F2-10 ─→ F2-11 Cierre independiente
```

Paralelismo permitido después de `F2-00`:

- `F2-01` y `F2-04` no se ejecutan simultáneamente si ambos necesitan cambiar
  fixtures de protocolo.
- `F2-04` y `F2-06` sí pueden ejecutarse en paralelo.
- `F2-02` y `F2-03` son secuenciales porque comparten persistencia y tests DB.

---

## F2-00 — Capturar baseline y matriz de cumplimiento

**Linear**

- **Título:** `F2-00 — Capture Phase 2 baseline and PLAN traceability`
- **Prioridad:** Urgent
- **Estado inicial:** Ready
- **Propietario:** Tech lead
- **Etiquetas:** `phase-2`, `blocker`, `verification`, `tech-lead`
- **Bloquea:** F2-01, F2-04, F2-06

**Objetivo:** Fijar el commit, los resultados y las discrepancias desde los que
se medirá todo el cierre, sin modificar código ni datos.

**Evidencia conocida que debe reconfirmarse:** el grafo actual contiene
`parse_state → calc_damage → decide`; el auditor independiente omite la máscara
legal y solo verifica `species`; existen discrepancias históricas de
`source='test'` —39 inconsistencias acción/turno en la última medición—;
`retrieve_context` no está implementado.

**Archivos:**

- Read: `docs/PLAN.md`
- Read: `docs/DECISIONS.md`
- Read: `docs/AGENT_GOVERNANCE.md`
- Read: `docs/HANDOFF_CLAUDE.md`
- Read: `docs/HANDOFF_GPT.md`
- Read: `apps/agent/src/ludex_agent/graph/workflow.py`
- Read: `packages/dataset-audit/src/invariants.ts`
- Update only if facts changed: issue description and comments in Linear

**Fuera de alcance:** corregir cualquier fallo encontrado.

- [ ] Registrar `git rev-parse HEAD`, `git status --short --branch` y la lista
  exacta de cambios ajenos que no pertenecen a esta fase.
- [ ] Confirmar servicios con `docker compose ps`; no reiniciar servicios sanos.
- [ ] Ejecutar la suite TypeScript desde la raíz:

```bash
pnpm test
```

- [ ] Ejecutar la suite Python una sola vez, sin otro pytest concurrente:

```bash
cd apps/agent
uv run pytest -q
```

- [ ] Ejecutar el auditor global:

```bash
pnpm --filter @ludex/dataset-audit run audit
```

- [ ] Registrar volúmenes sin modificar la DB:

```bash
docker exec ludex-postgres-1 psql -U ludex -d ludex -c \
  "SELECT source, count(*) FROM battles GROUP BY source ORDER BY source;"
docker exec ludex-postgres-1 psql -U ludex -d ludex -c \
  "SELECT count(*) AS trajectories FROM trajectories;
   SELECT count(*) AS steps FROM trajectory_steps;
   SELECT count(*) AS turns FROM battle_turns;"
```

- [ ] Ejecutar la búsqueda transversal de hardcodes:

```bash
git grep -n -i "gen6" -- apps packages db \
  ':!**/tests/**' ':!**/test/**' ':!**/evals/**'
```

- [ ] Crear en Linear una tabla con cada requisito de
  `docs/PLAN.md:137-162`, `docs/PLAN.md:216-218` y
  `docs/PLAN.md:268-276`, indicando `implemented`, `partial` o `missing` y
  enlazando la issue que lo resolverá.
- [ ] Publicar el `REVIEW PACKET`; para esta tarea la prueba de regresión dice
  `No aplica: baseline read-only`.

**Criterios de aceptación:**

- Queda registrado el commit exacto y el resultado completo de cada comando.
- Ningún test saltado se presenta como ejecutado.
- Cada discrepancia de Fase 2 está asignada a F2-01…F2-10.
- No se modificaron código, esquema, servicios ni datos.

**Commit:** No aplica.

---

## F2-01 — Dar al grafo el estado rival observable y actualizado

**Linear**

- **Título:** `F2-01 — Make decision snapshots fresh without hidden-information leakage`
- **Prioridad:** Urgent
- **Propietario:** Claude lane
- **Etiquetas:** `phase-2`, `blocker`, `correctness`,
  `showdown-protocol`, `dataset`, `claude-lane`
- **Depende de:** F2-00
- **Bloquea:** F2-02, F2-07, F2-11

**Objetivo:** La decisión del LLM y la fila persistida deben usar la misma
proyección observable correcta, sin esperar una narración que depende de
responder y sin releer el `Battle` mutable después de decidir.

**Síntoma actual:** `_choose_move_with_graph` captura `serialize_battle(battle)`
antes de invocar el grafo, mientras el rival puede estar atrasado respecto del
protocolo. `_finalize_pending_steps` refresca después, pero ese estado no
corrige lo que recibió el proveedor.

**Archivos probables:**

- Modify: `apps/agent/src/ludex_agent/showdown/client.py`
- Modify or create pure helper: `apps/agent/src/ludex_agent/showdown/protocol.py`
- Modify only if schema changes: `apps/agent/src/ludex_agent/state/serializer.py`
- Test: `apps/agent/tests/showdown/test_client.py`
- Test: `apps/agent/tests/showdown/test_protocol.py`
- Test: `apps/agent/tests/integration/test_play.py`
- Document: `docs/DECISIONS.md`
- Update: `.claude/agent-recording/SKILL.md` si aparece un hecho nuevo medido

**Interfaces:**

- Consumes: frame de protocolo crudo, snapshot propio y máscara capturados
  síncronamente.
- Produces: un snapshot inmutable cuya vista rival se deriva únicamente de
  evidencia pública previa o disponible al momento de la elección.
- Preserves: mapa acción→`BattleOrder` capturado antes del primer `await`.

**Fuera de alcance:** cambiar estrategia, prompt, calc, provider o esquema de
respuesta del modelo.

- [ ] Reproducir el desfase con un frame o secuencia real guardada, mostrando
  en el comentario de Linear el `|request|`, las líneas públicas relevantes,
  el estado enviado al grafo y el estado persistido.
- [ ] Formular una sola causa raíz y obtener aprobación del tech lead antes
  de implementar.
- [ ] Crear un test que demuestre simultáneamente:
  - el grafo recibe el HP/status/especie rival ya públicamente revelado;
  - no recibe ningún movimiento, objeto o habilidad rival todavía oculto;
  - `legal_actions` pertenece a la decisión actual.
- [ ] Ejecutar el test nuevo y confirmar que falla sobre el código anterior:

```bash
cd apps/agent
uv run pytest tests/showdown/test_client.py -q
```

- [ ] Implementar la mínima proyección derivada del protocolo. Está prohibido
  esperar narración antes de responder o reserializar desde `Battle` después.
- [ ] Ejecutar unitarios de protocolo, cliente y serializador:

```bash
cd apps/agent
uv run pytest tests/showdown/test_protocol.py \
  tests/showdown/test_client.py tests/state/test_serializer.py -q
```

- [ ] Ejecutar la integración de grabación una sola vez:

```bash
cd apps/agent
uv run pytest tests/integration/test_play.py -q
```

- [ ] Romper deliberadamente la aplicación de la proyección rival, comprobar
  que el test nuevo falla, restaurar el arreglo y repetir la suite.
- [ ] Documentar la relación entre snapshot de decisión y snapshot persistido
  en `docs/DECISIONS.md`, incluida la regla de que el chat de batalla nunca
  entra al prompt.
- [ ] Commit con rutas explícitas:

```bash
git commit -m "fix(agent): keep decision snapshots protocol-consistent" -- \
  apps/agent/src/ludex_agent/showdown/client.py \
  apps/agent/src/ludex_agent/showdown/protocol.py \
  apps/agent/src/ludex_agent/state/serializer.py \
  apps/agent/tests/showdown/test_client.py \
  apps/agent/tests/showdown/test_protocol.py \
  apps/agent/tests/state/test_serializer.py \
  apps/agent/tests/integration/test_play.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- El proveedor decide con el estado observable correcto.
- La máscara nunca se captura después de un `await`.
- El rival no se actualiza desde request privado ni desde una lista manual.
- El snapshot de decisión y la fila persistida no se contradicen.
- El test de regresión falla al retirar el arreglo.
- Integración real y auditor de fuga permanecen verdes para filas nuevas.

---

## F2-02 — Resolver acciones rechazadas sin pasos fantasma

**Linear**

- **Título:** `F2-02 — Reconcile rejected Showdown choices without losing decisions`
- **Prioridad:** Urgent
- **Propietario:** Claude lane
- **Etiquetas:** `phase-2`, `blocker`, `correctness`,
  `showdown-protocol`, `dataset`, `claude-lane`
- **Depende de:** F2-01
- **Bloquea:** F2-03, F2-08, F2-11

**Objetivo:** Una elección que Showdown rechaza debe quedar reconciliada de
forma explícita; nunca puede persistirse como acción ejecutada, dejar un paso
fantasma o perderse mediante un `continue` silencioso.

**Archivos probables:**

- Modify: `apps/agent/src/ludex_agent/showdown/client.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Modify if persistence contract changes:
  `apps/agent/src/ludex_agent/db/repository.py`
- Test: `apps/agent/tests/showdown/test_client.py`
- Test: `apps/agent/tests/test_cli.py`
- Test: `apps/agent/tests/integration/test_play.py`
- Document: `docs/DECISIONS.md`

**Interfaces:**

- Consumes: acción reservada, respuesta `|error|` real y decisión siguiente.
- Produces: política documentada para marcar, reemplazar o excluir la elección
  rechazada sin romper `decision_index`.

**Fuera de alcance:** errores semánticos del JSON del proveedor, que pertenecen
a `decide`.

- [ ] Capturar cómo llega realmente un `|error|` de elección no disponible o
  inválida al handler de poke-env; no asumir que usa el mismo canal que la
  narración.
- [ ] Medir si `_discard_last_step` se ejecuta y qué sucede con la siguiente
  llamada a `choose_move`.
- [ ] Publicar causa raíz y política de persistencia propuesta; esperar
  aprobación del tech lead.
- [ ] Crear tests para elección no disponible, elección inválida y error no
  relacionado. El canario debe demostrar que al menos una elección fue
  rechazada.
- [ ] Ejecutar primero en rojo:

```bash
cd apps/agent
uv run pytest tests/showdown/test_client.py tests/test_cli.py -q
```

- [ ] Implementar una reconciliación atómica: la siguiente decisión no puede
  heredar acción, máscara, reasoning ni `action_path` de la rechazada.
- [ ] Asegurar que `lost_step_count` distinto de cero sea fallo consultable en
  verificación, no solo un warning.
- [ ] Ejecutar integración:

```bash
cd apps/agent
uv run pytest tests/integration/test_play.py \
  tests/integration/test_graph_play.py -q
```

- [ ] Retirar deliberadamente el manejo de `|error|`, verificar que el test
  falla, restaurar y repetir.
- [ ] Documentar el contrato en `docs/DECISIONS.md`.
- [ ] Commit:

```bash
git commit -m "fix(agent): reconcile rejected battle choices" -- \
  apps/agent/src/ludex_agent/showdown/client.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/src/ludex_agent/db/repository.py \
  apps/agent/tests/showdown/test_client.py \
  apps/agent/tests/test_cli.py \
  apps/agent/tests/integration/test_play.py \
  apps/agent/tests/integration/test_graph_play.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- Cero pasos fantasma y cero decisiones perdidas silenciosamente.
- `decision_index` mantiene orden y semántica documentados.
- Una acción rechazada no aparece como ejecutada ni recibe reward de política.
- La regresión es detectable al retirar el arreglo.

---

## F2-03 — Hacer restart-safe la identidad de batalla

**Linear**

- **Título:** `F2-03 — Make persisted battle identity safe across Showdown restarts`
- **Prioridad:** Urgent
- **Propietario:** Claude lane
- **Etiquetas:** `phase-2`, `blocker`, `database`, `dataset`, `claude-lane`
- **Depende de:** F2-02
- **Bloquea:** F2-08, F2-11

**Objetivo:** Dos batallas distintas nunca se fusionan aunque Showdown reutilice
el mismo `battle_tag`, formato y nombres después de reiniciar su contador.

**Síntoma actual:** `BattleTagCollisionError` solo detecta la colisión si cambia
p1, p2 o formato. La misma pareja y el mismo formato pueden pasar por el
upsert como si fueran la misma batalla.

**Archivos probables:**

- Create: `db/migrations/20260729000001_battle_identity.sql`
- Modify: `apps/agent/src/ludex_agent/db/repository.py`
- Modify: `apps/agent/src/ludex_agent/db/models.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Test: `apps/agent/tests/db/test_repository.py`
- Test: `apps/agent/tests/db/test_models.py`
- Test: `apps/agent/tests/integration/test_play.py`
- Update generated schema: `db/schema.sql`
- Document: `docs/DECISIONS.md`

**Interfaces:**

- Consumes: identidad de sesión/origen y `battle_tag` del protocolo.
- Produces: clave persistente que distingue ejecuciones del servidor y conserva
  idempotencia al reintentar la misma persistencia.

**Fuera de alcance:** identidad de challenges oficiales de Fase 3.

- [ ] Crear una reproducción DB con dos batallas distintas que comparten tag,
  p1, p2 y formato. Confirmar que hoy se fusionan.
- [ ] Comparar al menos dos diseños: namespace de sesión persistido y
  fingerprint del protocolo. Publicar trade-offs y esperar aprobación.
- [ ] Crear el test rojo que exige:
  - re-persistir la misma batalla devuelve el mismo `battle_id`;
  - persistir una batalla distinta con identidad externa repetida no reutiliza
    la fila;
  - nunca se compara el protocolo mediante substring ambiguo.
- [ ] Tomar backup antes de correr cualquier migración sobre la DB real:

```bash
docker exec ludex-postgres-1 pg_dump -U ludex -d ludex \
  --format=custom --compress=9 \
  -f /tmp/ludex-phase2-battle-identity.dump
```

- [ ] Crear una migración aditiva con `migrate:down`, índices y constraints
  junto a la identidad.
- [ ] Actualizar repository y modelos desde el esquema real.
- [ ] Ejecutar:

```bash
docker compose run --rm migrate up
cd apps/agent
uv run pytest tests/db/test_models.py tests/db/test_repository.py -q
```

- [ ] Probar el `migrate:down` y posterior `up` únicamente en una base
  desechable o restaurable, nunca destruyendo la DB de Ludex.
- [ ] Romper la nueva identidad para que vuelva a usar solo `battle_tag`,
  confirmar que el test falla y restaurar.
- [ ] Ejecutar una integración con dos sesiones independientes del agente.
- [ ] Documentar semántica e idempotencia en `docs/DECISIONS.md`.
- [ ] Commit:

```bash
git commit -m "fix(agent): namespace persisted battle identity" -- \
  db/migrations/20260729000001_battle_identity.sql \
  db/schema.sql \
  apps/agent/src/ludex_agent/db/repository.py \
  apps/agent/src/ludex_agent/db/models.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/db/test_repository.py \
  apps/agent/tests/db/test_models.py \
  apps/agent/tests/integration/test_play.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- El caso misma pareja + mismo formato + mismo tag no se fusiona.
- Reintentar la misma persistencia continúa siendo idempotente.
- El esquema y SQLAlchemy coinciden columna por columna.
- Up/down está verificado en un entorno seguro.

---

## F2-04 — Completar y vectorizar el auditor independiente

**Linear**

- **Título:** `F2-04 — Complete and scale the independent dataset auditor`
- **Prioridad:** Urgent
- **Propietario:** Claude lane
- **Etiquetas:** `phase-2`, `blocker`, `dataset`, `verification`,
  `claude-lane`
- **Depende de:** F2-00
- **Bloquea:** F2-05, F2-11

**Objetivo:** Convertir `packages/dataset-audit` en la puerta real del corpus,
con invariantes completos, semántica honesta y costo que no haga una consulta
por paso.

**Defectos que debe cubrir:**

- No verifica `action_taken ∈ legal_actions`.
- Solo verifica `species` para fuga de información.
- `state_rederivable` solo confirma que existen líneas, no que sostienen el
  estado.
- `tie` no forma parte del tipo y se interpreta como reward `-1`.
- Marca cualquier mezcla de versiones como inválida aunque el plan permite
  versiones históricas interpretables.
- El chequeo Python de fuga tarda aproximadamente 178 s sobre el corpus
  observado por un N+1.

**Archivos:**

- Modify: `packages/dataset-audit/src/types.ts`
- Modify: `packages/dataset-audit/src/db.ts`
- Modify: `packages/dataset-audit/src/invariants.ts`
- Modify: `packages/dataset-audit/src/cli.ts`
- Modify: `packages/dataset-audit/src/render.ts`
- Test: `packages/dataset-audit/test/invariants.test.ts`
- Test: `packages/dataset-audit/test/db.test.ts`
- Test: `packages/dataset-audit/test/cli.test.ts`
- Test: `packages/dataset-audit/test/render.test.ts`
- Modify after parity is proven:
  `apps/agent/tests/integration/test_play.py`
- Document: `docs/DECISIONS.md`

**Interfaces:**

- `audit --scope all`: revisa todas las filas, incluidas `source='test'`.
- `audit --scope training`: revisa solo filas elegibles para entrenamiento.
- `audit --gen N`: aplica el mismo contrato filtrado por generación.
- Ambas variantes son read-only y devuelven código 1 ante violaciones.

**Fuera de alcance:** borrar filas; eso pertenece a F2-05.

- [ ] Agregar fixtures negativos independientes para cada una de las once
  claves rivales y confirmar que cada defecto produce
  `hidden_information`.
- [ ] Agregar fixtures para acción fuera de máscara, hueco/duplicado de
  `decision_index`, empate con reward 0, versión columna/JSON distinta,
  versión soportada coexistente y paso sin protocolo suficiente.
- [ ] Ejecutar los tests y confirmar que los nuevos casos fallan con el auditor
  actual:

```bash
pnpm --filter @ludex/dataset-audit test
```

- [ ] Implementar comparación de acciones con normalización semántica limitada
  a flags especiales `false`/ausentes; no perdonar IDs distintos.
- [ ] Implementar verificación de las once claves usando protocolo línea por
  línea y dex local cuando corresponda. No concatenar protocolo.
- [ ] Representar `finalResult` como `win | loss | tie | null` y exigir reward
  `1 | -1 | 0` respectivamente.
- [ ] Sustituir “versiones mezcladas” por:
  - `trajectory_steps.state_schema_version` y `state.schema_version` coinciden;
  - cada versión presente tiene validador soportado;
  - una versión desconocida falla.
- [ ] Cargar dataset con un número constante de queries y construir índices
  acumulativos por batalla/lado/turno en una sola pasada.
- [ ] Añadir un canario de conteo de queries en tests DB y medir tiempo antes y
  después sobre el mismo corpus.
- [ ] Mantener el auditor TypeScript independiente de las funciones de
  producción Python que verifica.
- [ ] Ejecutar:

```bash
pnpm --filter @ludex/dataset-audit test
pnpm --filter @ludex/dataset-audit run audit --scope all
pnpm --filter @ludex/dataset-audit run audit --scope training
```

- [ ] Romper individualmente la verificación de máscara y una clave rival;
  ambos tests deben fallar. Restaurar y repetir.
- [ ] Solo después de demostrar paridad, retirar duplicación N+1 del test
  Python sin reducir cobertura global.
- [ ] Documentar scopes, versiones y semántica de empate.
- [ ] Commit:

```bash
git commit -m "feat(audit): enforce complete trajectory invariants" -- \
  packages/dataset-audit/src/types.ts \
  packages/dataset-audit/src/db.ts \
  packages/dataset-audit/src/invariants.ts \
  packages/dataset-audit/src/cli.ts \
  packages/dataset-audit/src/render.ts \
  packages/dataset-audit/test/invariants.test.ts \
  packages/dataset-audit/test/db.test.ts \
  packages/dataset-audit/test/cli.test.ts \
  packages/dataset-audit/test/render.test.ts \
  apps/agent/tests/integration/test_play.py \
  docs/DECISIONS.md \
  pnpm-lock.yaml
```

**Criterios de aceptación:**

- Los cuatro invariantes del recorder están enforced.
- Las once claves rivales tienen fixtures positivos y negativos.
- Empate exige reward 0.
- Versiones soportadas pueden coexistir.
- El número de queries no crece con la cantidad de pasos.
- El auditor sigue siendo estrictamente read-only.

---

## F2-05 — Sanear corpus sintético y separar scopes

**Linear**

- **Título:** `F2-05 — Quarantine historical test rows and prove training-scope isolation`
- **Prioridad:** High
- **Propietario:** Claude lane
- **Etiquetas:** `phase-2`, `dataset`, `database`,
  `destructive-approval`, `claude-lane`
- **Depende de:** F2-04
- **Bloquea:** F2-11

**Objetivo:** Resolver las discrepancias sintéticas históricas sin ocultarlas
con filtros oportunistas y demostrar que ningún consumidor de entrenamiento
incluye `source='test'`.

**Archivos probables:**

- Modify: `packages/dataset-audit/src/db.ts`
- Modify: `packages/dataset-audit/test/db.test.ts`
- Modify: `apps/agent/tests/db/test_repository.py`
- Modify: `apps/agent/tests/integration/test_play.py`
- Document: `docs/DECISIONS.md`
- Operational evidence: comentario de Linear con tags y SQL exactos

**Fuera de alcance:** borrar o modificar batallas `source <> 'test'`.

- [ ] Ejecutar `audit --scope all` y exportar la lista exacta de violaciones.
- [ ] Clasificar cada fila por `source`, batalla, decisión, fecha y posibilidad
  de rederivación desde protocolo.
- [ ] Probar que `audit --scope training` excluye únicamente por contrato
  explícito y no mediante tags hardcodeados.
- [ ] Crear tests negativos que insertan una fila `source='test'` y demuestran:
  - aparece en scope `all`;
  - no aparece en scope `training`;
  - una fila local equivalente aparece en ambos.
- [ ] Elegir entre rederivar o eliminar únicamente filas sintéticas sin valor.
  Publicar lista exacta y esperar aprobación explícita del usuario.
- [ ] Antes de cualquier limpieza aprobada, tomar backup:

```bash
docker exec ludex-postgres-1 pg_dump -U ludex -d ludex \
  --format=custom --compress=9 \
  -f /tmp/ludex-phase2-corpus-hygiene.dump
```

- [ ] Ejecutar SQL con `WHERE source='test'` y tags/IDs exactos; está prohibido
  borrar por rango, fecha amplia, prefijo no revisado o cascade desde una tabla
  no verificada.
- [ ] Ejecutar auditoría global y de entrenamiento después de la limpieza.
- [ ] Demostrar que tests nuevos limpian sus fixtures o producen filas que
  cumplen invariantes incluso si el proceso se interrumpe.
- [ ] Documentar el backup, la lista afectada, la recuperabilidad y la regla de
  scopes en `docs/DECISIONS.md`.
- [ ] Commit solo si hubo cambios de código/documentación:

```bash
git commit -m "test(agent): isolate synthetic battle data" -- \
  packages/dataset-audit/src/db.ts \
  packages/dataset-audit/test/db.test.ts \
  apps/agent/tests/db/test_repository.py \
  apps/agent/tests/integration/test_play.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- `audit --scope all` no tiene violaciones conocidas excusadas.
- `audit --scope training` contiene únicamente datos entrenables.
- Cero datos reales borrados o reescritos.
- La limpieza es recuperable desde un backup identificado.
- La autorización del usuario está enlazada en Linear.

**Condición `On Hold`:** falta de aprobación para una operación destructiva.

---

## F2-06 — Implementar `retrieve_context` de Fase 2

**Linear**

- **Título:** `F2-06 — Add generation-scoped retrieve_context to the decision graph`
- **Prioridad:** Urgent
- **Propietario:** GPT-5.6 Sol lane
- **Etiquetas:** `phase-2`, `blocker`, `graph`, `database`, `sol-lane`
- **Depende de:** F2-00
- **Bloquea:** F2-07, F2-09, F2-11

**Objetivo:** Incorporar el nodo faltante que consulta la data local de juego
por generación y suministra al grafo conocimiento verificable de especies,
movimientos y learnsets.

**Alcance de Fase 2:** game data ya existente. El filtro por ronda se agrega en
Fase 5; perfiles, lecciones y playbook se agregan en Fases 6 y 7. No crear
tablas futuras.

**Archivos:**

- Create: `apps/agent/src/ludex_agent/graph/context.py`
- Create: `apps/agent/src/ludex_agent/db/context_repository.py`
- Modify: `apps/agent/src/ludex_agent/graph/state.py`
- Modify: `apps/agent/src/ludex_agent/graph/workflow.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Test: `apps/agent/tests/graph/test_context.py`
- Test: `apps/agent/tests/graph/test_workflow.py`
- Test: `apps/agent/tests/db/test_context_repository.py`
- Document: `docs/DECISIONS.md`

**Interfaces:**

```python
class ContextRepository(Protocol):
    async def load_battle_context(
        self,
        *,
        gen_number: int,
        own_species: tuple[str, ...],
        opponent_species: tuple[str, ...],
    ) -> dict[str, object]: ...


async def retrieve_context(
    state: GraphState,
    repository: ContextRepository,
) -> dict[str, object]:
    """Devuelve {'context': <dict JSON-serializable>}."""
```

`GraphState` agrega `context: dict[str, Any]`. `build_decision_graph` recibe un
`ContextRepository` y conecta:

```text
parse_state → retrieve_context → calc_damage → decide
```

**Fuera de alcance:** `round_availability`, perfiles, embeddings y acceso a
internet.

- [ ] Escribir tests puros que extraen IDs allowlisted del estado y nunca
  consultan especies rivales no reveladas.
- [ ] Escribir integración DB con gen 6 y gen 9 que prueba `(gen_id,
  showdown_id)` y una frontera real entre generaciones.
- [ ] Ejecutar en rojo:

```bash
cd apps/agent
uv run pytest tests/graph/test_context.py \
  tests/db/test_context_repository.py \
  tests/graph/test_workflow.py -q
```

- [ ] Implementar queries parametrizadas contra `generations`, `pokemon`,
  `moves` y `learnsets`, respetando `sourceSpecies` y sin aplanar métodos.
- [ ] Representar `moves.accuracy IS NULL` como “nunca falla”, no como faltante.
- [ ] Representar `power_kind` y no describir poder variable como “power 0”.
- [ ] Insertar el nodo en el grafo y asegurar que `decide` recibe `context`.
- [ ] Inyectar repository desde CLI; el nodo no abre conexiones globales ni
  conoce variables de entorno.
- [ ] Ejecutar:

```bash
cd apps/agent
uv run pytest tests/graph tests/db/test_context_repository.py -q
```

- [ ] Romper el filtro por generación, demostrar que el test de frontera falla,
  restaurar y repetir.
- [ ] Documentar el alcance progresivo del nodo y por qué no crea tablas de
  Fases 5-7.
- [ ] Commit:

```bash
git commit -m "feat(agent): retrieve generation-scoped battle context" -- \
  apps/agent/src/ludex_agent/graph/context.py \
  apps/agent/src/ludex_agent/db/context_repository.py \
  apps/agent/src/ludex_agent/graph/state.py \
  apps/agent/src/ludex_agent/graph/workflow.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/graph/test_context.py \
  apps/agent/tests/graph/test_workflow.py \
  apps/agent/tests/db/test_context_repository.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- El grafo contiene `retrieve_context`.
- Toda consulta está filtrada por generación.
- Cero información rival no revelada entra a la consulta.
- No existen fetches de game data durante runtime.
- Los campos problemáticos `accuracy` y `power_kind` conservan su semántica.

---

## F2-07 — Completar contexto y semántica del cálculo

**Linear**

- **Título:** `F2-07 — Carry complete observable battle context into damage calculation`
- **Prioridad:** Urgent
- **Propietario:** GPT-5.6 Sol lane
- **Etiquetas:** `phase-2`, `blocker`, `graph`, `calc`, `correctness`,
  `sol-lane`
- **Depende de:** F2-01, F2-06
- **Bloquea:** F2-09, F2-11

**Objetivo:** El adaptador Python debe construir requests honestos para
`packages/calc`, incluyendo condiciones observables y mecánicas especiales,
y diferenciar datos desconocidos de fallos de infraestructura.

**Archivos:**

- Modify: `apps/agent/src/ludex_agent/graph/calc.py`
- Modify: `apps/agent/src/ludex_agent/graph/state.py`
- Test: `apps/agent/tests/graph/test_calc.py`
- Test: `apps/agent/tests/graph/test_calc_integration.py`
- Modify only if contract lacks a required field: `packages/calc/src/calc.ts`
- Test if modified: `packages/calc/test/calc.test.ts`
- Test if modified: `packages/calc/test/server.test.ts`
- Document: `docs/DECISIONS.md`

**Interfaces:**

- Consumes: `battle_state`, `context` y acciones legales.
- Produces: `damage` con requests auditables y error tipado.
- El request usa el contrato existente `CalcRequest`:
  `gen`, `attacker`, `defender`, `move` y `field`.

**Casos obligatorios:** clima, terreno, Gravity, Reflect, Light Screen,
hazards relevantes, boosts, status, HP actual, Singles/Doubles, Megaevolución
de Gen 6 y movimientos rivales revelados/posibles.

**Fuera de alcance:** reimplementar fórmulas de daño en Python.

- [ ] Crear una tabla de mapeo inspeccionada entre enums/side conditions de
  poke-env y strings exactos de `packages/calc`.
- [ ] Escribir tests que capturan el request completo para Rain, Reflect,
  boosts, burn y acción `mega: true`.
- [ ] Escribir un test donde el cambio forzado usa movimientos rivales
  recuperados por `retrieve_context` sin presentarlos como revelados.
- [ ] Escribir tests separados para:
  - dato desconocido legítimo;
  - request semánticamente inválido;
  - calc HTTP no disponible.
- [ ] Ejecutar en rojo:

```bash
cd apps/agent
uv run pytest tests/graph/test_calc.py \
  tests/graph/test_calc_integration.py -q
```

- [ ] Implementar el mapeo sin inventar habilidad, item, nature, EVs o IVs
  rivales.
- [ ] Para `mega: true`, calcular con la forma y habilidad post-Mega
  correctas de la generación; derivarlas del dex local, no de una lista.
- [ ] Un error de una acción individual puede quedar diagnosticado; una caída
  completa de calc no puede degradarse silenciosamente a “sin cálculos”.
- [ ] Comparar al menos tres requests generados por Python contra
  `runCalc` directo en Node con resultados exactos.
- [ ] Ejecutar:

```bash
pnpm --filter @ludex/calc test
cd apps/agent
uv run pytest tests/graph/test_calc.py \
  tests/graph/test_calc_integration.py -q
```

- [ ] Retirar deliberadamente el field y la transformación Mega; los tests
  correspondientes deben fallar. Restaurar y repetir.
- [ ] Documentar mapeo, datos desconocidos y semántica de fallos.
- [ ] Commit:

```bash
git commit -m "fix(agent): preserve battle mechanics in damage requests" -- \
  apps/agent/src/ludex_agent/graph/calc.py \
  apps/agent/src/ludex_agent/graph/state.py \
  apps/agent/tests/graph/test_calc.py \
  apps/agent/tests/graph/test_calc_integration.py \
  packages/calc/src/calc.ts \
  packages/calc/test/calc.test.ts \
  packages/calc/test/server.test.ts \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- Field y side conditions cambian el resultado exactamente como
  `@smogon/calc`.
- Mega usa la forma correcta.
- Lo desconocido permanece desconocido; no se completa con defaults falsos.
- Una caída del servicio es ruidosa y clasificable.
- Fallback consume cálculos válidos, no errores disfrazados de daño cero.

---

## F2-08 — Completar contrato de decisión y metadata persistida

**Linear**

- **Título:** `F2-08 — Persist the complete decision contract and ML quality metadata`
- **Prioridad:** Urgent
- **Propietario:** GPT-5.6 Sol lane
- **Etiquetas:** `phase-2`, `blocker`, `graph`, `database`, `dataset`,
  `sol-lane`
- **Depende de:** F2-02, F2-03
- **Bloquea:** F2-09, F2-11

**Objetivo:** Producir y persistir la salida definida por el plan —acción,
target, rationale, confidence y alternativas— junto con proveedor/modelo
efectivos y latencia por decisión.

**Regla de seguridad:** `reasoning` significa justificación breve destinada al
usuario, no cadena de pensamiento privada.

**Archivos probables:**

- Create: `db/migrations/20260729000002_decision_metadata.sql`
- Modify: `apps/agent/src/ludex_agent/graph/decision.py`
- Modify: `apps/agent/src/ludex_agent/graph/state.py`
- Modify: `apps/agent/src/ludex_agent/showdown/client.py` solo después de que
  Claude libere el archivo
- Modify: `apps/agent/src/ludex_agent/db/repository.py`
- Modify: `apps/agent/src/ludex_agent/db/models.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Test: `apps/agent/tests/graph/test_decision.py`
- Test: `apps/agent/tests/showdown/test_client.py`
- Test: `apps/agent/tests/db/test_repository.py`
- Test: `apps/agent/tests/db/test_models.py`
- Update: `db/schema.sql`
- Document: `docs/DECISIONS.md`

**Contrato mínimo:**

```python
class DecisionAlternative(BaseModel):
    action: DecisionAction
    reasoning: str


class DecisionResponse(BaseModel):
    action: DecisionAction
    target: str | None
    reasoning: str
    confidence: float
    alternatives: list[DecisionAlternative]
```

`confidence` está en `[0, 1]`. Acción principal y alternativas se validan
contra la misma máscara capturada. Provider/model/latencia pertenecen al paso,
porque el modelo puede cambiar entre turnos. La metadata de calidad también
debe permitir filtrar quién decidió (`action_source`), origen de la batalla y
rating estimado del rival cuando sea observable; un rating desconocido queda
`NULL`, nunca inferido.

**Fuera de alcance:** UI, aprobación humana e historial de chat.

- [ ] Proponer cómo representar metadata en `trajectory_steps`, conservando
  `decision_index` como clave canónica. Esperar aprobación antes de migrar.
- [ ] Escribir tests de Pydantic para confidence fuera de rango, alternativas
  ilegales, campos faltantes y target nullable en singles.
- [ ] Escribir test DB con dos decisiones del mismo turno y metadata distinta.
- [ ] Ejecutar en rojo:

```bash
cd apps/agent
uv run pytest tests/graph/test_decision.py \
  tests/db/test_repository.py tests/db/test_models.py -q
```

- [ ] Tomar backup antes de migrar:

```bash
docker exec ludex-postgres-1 pg_dump -U ludex -d ludex \
  --format=custom --compress=9 \
  -f /tmp/ludex-phase2-decision-metadata.dump
```

- [ ] Crear migración aditiva. Metadata histórica desconocida queda `NULL`; no
  inventar proveedor/modelo retroactivo.
- [ ] Validar acción principal y cada alternativa. Si una alternativa es
  ilegal, la respuesta completa consume el reintento semántico.
- [ ] Conservar rationale y metadata en el step antes de persistir; una acción
  rechazada no puede conservar metadata como si se hubiera ejecutado.
- [ ] Resolver `battle_turns.agent_reasoning` sin dos fuentes canónicas: la
  decisión canónica vive por `decision_index`; cualquier resumen por turno es
  derivado y documentado.
- [ ] Ejecutar migración y suites:

```bash
docker compose run --rm migrate up
cd apps/agent
uv run pytest tests/graph/test_decision.py \
  tests/showdown/test_client.py \
  tests/db/test_repository.py tests/db/test_models.py -q
```

- [ ] Romper la validación de alternativas y la persistencia de model id;
  ambos tests deben fallar. Restaurar.
- [ ] Documentar metadata, rationale, semántica histórica y ubicación
  canónica de la validación de legalidad.
- [ ] Commit:

```bash
git commit -m "feat(agent): persist structured decision metadata" -- \
  db/migrations/20260729000002_decision_metadata.sql \
  db/schema.sql \
  apps/agent/src/ludex_agent/graph/decision.py \
  apps/agent/src/ludex_agent/graph/state.py \
  apps/agent/src/ludex_agent/showdown/client.py \
  apps/agent/src/ludex_agent/db/repository.py \
  apps/agent/src/ludex_agent/db/models.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/graph/test_decision.py \
  apps/agent/tests/showdown/test_client.py \
  apps/agent/tests/db/test_repository.py \
  apps/agent/tests/db/test_models.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- Contrato del plan completo y validado.
- Alternativas siempre legales.
- Provider/model/latencia consultables por decisión.
- Filas históricas no reciben metadata inventada.
- Múltiples decisiones del mismo turno no se pisan.

---

## F2-09 — Implementar switch persistido y paridad del grafo

**Linear**

- **Título:** `F2-09 — Resolve providers per turn and complete Phase 2 graph parity`
- **Prioridad:** Urgent
- **Propietario:** GPT-5.6 Sol lane
- **Etiquetas:** `phase-2`, `blocker`, `graph`, `provider`, `database`,
  `sol-lane`
- **Depende de:** F2-06, F2-07, F2-08
- **Bloquea:** F2-10, F2-11

**Objetivo:** Cumplir el switch de modelos del plan y cerrar explícitamente la
diferencia entre el grafo conceptual y la ejecución actual mediante poke-env.

**Archivos probables:**

- Create: `db/migrations/20260729000003_providers_models_settings.sql`
- Create: `apps/agent/src/ludex_agent/db/model_repository.py`
- Modify: `apps/agent/src/ludex_agent/graph/provider.py`
- Modify: `apps/agent/src/ludex_agent/graph/decision.py`
- Modify: `apps/agent/src/ludex_agent/graph/workflow.py`
- Modify: `apps/agent/src/ludex_agent/config.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Modify: `apps/agent/src/ludex_agent/showdown/client.py` solo para el borde
  execute aprobado
- Test: `apps/agent/tests/graph/test_provider.py`
- Test: `apps/agent/tests/graph/test_workflow.py`
- Test: `apps/agent/tests/test_config.py`
- Test: `apps/agent/tests/db/test_models.py`
- Test: `apps/agent/tests/integration/test_graph_play.py`
- Update: `db/schema.sql`
- Document: `docs/DECISIONS.md`

**Interfaces:**

- `providers` guarda nombre, base URL opcional, nombre de env var y enabled.
- `models` guarda provider, model id, label y disponibilidad/default.
- `settings` guarda la selección activa, no secretos.
- El modelo activo se resuelve al comienzo de cada decisión, no al compilar el
  grafo ni al iniciar la batalla.
- Rutas iniciales obligatorias: OpenAI nativo, Google Gemini nativo,
  Kimi/Moonshot OpenAI-compatible y OpenCode Zen OpenAI-compatible. El modelo
  local OpenAI-compatible permanece diferido a Fase 8.

**Fuera de alcance:** endpoint PATCH y UI, que pertenecen a Fase 3/4.

- [ ] Escribir test que cambia el modelo activo entre dos invocaciones del
  mismo grafo y prueba que la segunda usa el nuevo modelo sin recompilar.
- [ ] Escribir test que inspecciona DB y demuestra que no contiene un valor de
  API key.
- [ ] Escribir test de workflow con orden:

```text
parse_state → retrieve_context → calc_damage → decide → execute-adapter
```

- [ ] Ejecutar en rojo:

```bash
cd apps/agent
uv run pytest tests/graph/test_provider.py \
  tests/graph/test_workflow.py \
  tests/test_config.py tests/db/test_models.py -q
```

- [ ] Tomar backup y crear migración solo para tablas/campos consumidos en esta
  fase.
- [ ] Usar `init_chat_model` donde soporte el contrato necesario; conservar
  clientes de chat especializados únicamente donde rutas/usage lo exijan y
  documentar cada excepción.
- [ ] Mantener configuración env como bootstrap; DB gobierna selección activa.
- [ ] Resolver OpenCode `/models` al configurar, no hardcodear su catálogo.
- [ ] Diseñar `execute` como adapter explícito de poke-env. Si no puede ser un
  nodo LangGraph sin romper el contrato de `choose_move`, documentar la
  equivalencia, sus límites y su test end-to-end. No dejar la discrepancia
  silenciosa.
- [ ] Ejecutar:

```bash
docker compose run --rm migrate up
cd apps/agent
uv run pytest tests/graph tests/test_config.py \
  tests/db/test_models.py tests/integration/test_graph_play.py -q
```

- [ ] Romper la resolución por turno para cachear el provider al inicio;
  confirmar que el test de switch falla. Restaurar.
- [ ] Documentar bootstrap, selección activa y borde execute.
- [ ] Commit:

```bash
git commit -m "feat(agent): resolve the active model per decision" -- \
  db/migrations/20260729000003_providers_models_settings.sql \
  db/schema.sql \
  apps/agent/src/ludex_agent/db/model_repository.py \
  apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/src/ludex_agent/graph/decision.py \
  apps/agent/src/ludex_agent/graph/workflow.py \
  apps/agent/src/ludex_agent/config.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/src/ludex_agent/showdown/client.py \
  apps/agent/tests/graph/test_provider.py \
  apps/agent/tests/graph/test_workflow.py \
  apps/agent/tests/test_config.py \
  apps/agent/tests/db/test_models.py \
  apps/agent/tests/integration/test_graph_play.py \
  docs/DECISIONS.md
```

**Criterios de aceptación:**

- El modelo cambia entre turnos sin reiniciar batalla.
- Ningún secreto se persiste o loguea.
- Proveedores iniciales del plan tienen ruta testeada.
- El grafo y el adapter execute tienen una correspondencia documentada.
- El benchmark continúa fijando provider/model y prohíbe mezclarlos.

---

## F2-10 — Estabilizar proveedores, reloj y latencia

**Linear**

- **Título:** `F2-10 — Prove sustained provider reliability and decision latency`
- **Prioridad:** High
- **Propietario:** GPT-5.6 Sol lane
- **Etiquetas:** `phase-2`, `provider`, `verification`, `sol-lane`
- **Depende de:** F2-09
- **Bloquea:** F2-11

**Objetivo:** Resolver fallos internos reproducibles, distinguir límites
externos y producir mediciones comparables para dataset y juego en vivo.

**Archivos probables:**

- Modify: `apps/agent/src/ludex_agent/graph/provider.py`
- Modify: `apps/agent/src/ludex_agent/benchmark.py`
- Modify: `apps/agent/src/ludex_agent/eval_report.py`
- Modify: `apps/agent/src/ludex_agent/eval_cost.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Test: `apps/agent/tests/graph/test_provider.py`
- Test: `apps/agent/tests/test_benchmark.py`
- Test: `apps/agent/tests/test_eval_report.py`
- Update after valid runs: `docs/BENCHMARKS.md`
- Create/update run artifacts: `apps/agent/evals/runs/`
- Document: `docs/DECISIONS.md`

**Interfaces:**

- Métricas por completion: latencia, input/output/cache/reasoning tokens,
  retry, quota y modelo efectivo.
- Métricas por decisión: total, p50/p95/max, camino y deadline.
- Benchmarks: proveedor/modelo fijos, sin chain, snapshots atómicos.

**Fuera de alcance:** elegir ganador por una muestra incomparable o aumentar
N antes de estabilizar una pantalla corta.

- [ ] Corregir la mezcla de reloj inyectado con `time.monotonic()` mediante un
  test con fake clock.
- [ ] Agregar tests de latencia sin usar sleeps reales: backend guionado y
  reloj inyectable.
- [ ] Ejecutar:

```bash
cd apps/agent
uv run pytest tests/graph/test_provider.py \
  tests/test_benchmark.py tests/test_eval_report.py -q
```

- [ ] Para Kimi, ejecutar primero `provider-smoke` y como máximo una batalla
  controlada. Capturar tipo original: ConnectError, ReadTimeout, PoolTimeout,
  proxy o respuesta.
- [ ] No corregir Kimi hasta tener una causa raíz. Si el origen es externo,
  demostrar manejo seguro y documentar alcance.
- [ ] Repetir Gemini después de D30 con run id nuevo y proveedor/modelo fijos.
- [ ] Ejecutar una pantalla corta de un modelo OpenCode disponible para
  comprobar el tercer camino configurado.
- [ ] No ejecutar proveedores sin credencial configurada; cubrir su adapter
  con backend falso y registrar “not run: credential unavailable”.
- [ ] Verificar que el artefacto parcial nunca publica winrate comparable.
- [ ] Verificar que ningún log o artifact contiene fragmentos de claves.
- [ ] Actualizar `docs/BENCHMARKS.md` solo con corridas válidas o abortos
  clasificados.
- [ ] Romper la medición de latencia y el aislamiento provider/model; los tests
  deben fallar. Restaurar.
- [ ] Commit de código separado de artefactos de benchmark:

```bash
git commit -m "feat(agent): record decision latency metrics" -- \
  apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/src/ludex_agent/benchmark.py \
  apps/agent/src/ludex_agent/eval_report.py \
  apps/agent/src/ludex_agent/eval_cost.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/graph/test_provider.py \
  apps/agent/tests/test_benchmark.py \
  apps/agent/tests/test_eval_report.py \
  apps/agent/tests/test_eval_cost.py \
  docs/DECISIONS.md
```

Para el segundo commit, ejecutar primero
`git status --short apps/agent/evals/runs`, copiar los nombres completos de
cada artefacto nuevo y pasarlos individualmente después de `--` junto con
`docs/BENCHMARKS.md`. Está prohibido commitear el directorio completo.

**Criterios de aceptación:**

- Un solo reloj gobierna deadline y cooldown en tests y producción.
- Latencia p50/p95/max queda registrada por modelo.
- Kimi tiene causa raíz interna corregida o limitación externa demostrada.
- Gemini prueba el cooldown D30 en una corrida nueva.
- Corridas parciales no contaminan winrates.
- Cero secretos en salidas versionadas.

---

## F2-11 — Ejecutar la puerta final y cerrar Fase 2

**Linear**

- **Título:** `F2-11 — Execute independent Phase 2 acceptance gate`
- **Prioridad:** Urgent
- **Propietario:** Tech lead
- **Etiquetas:** `phase-2`, `blocker`, `verification`, `tech-lead`
- **Depende de:** F2-01, F2-02, F2-03, F2-04, F2-05, F2-06, F2-07, F2-08,
  F2-09, F2-10
- **Bloquea:** inicio de Fase 3

**Objetivo:** Verificar el commit integrado sin confiar en reportes de agentes
y emitir el único veredicto que permite declarar cerrada la fase.

**Archivos:**

- Read: todo el diff desde el baseline F2-00
- Update if closure succeeds: `docs/DECISIONS.md`
- Update: handoff vigente
- No código de feature en esta issue

**Fuera de alcance:** arreglar fallos encontrados. Cada fallo abre
`Changes Requested` en su issue de origen o una nueva issue bloqueante.

- [ ] Confirmar que cada issue dependiente está `In Review` con
  `REVIEW PACKET` completo.
- [ ] Revisar diff por commit y confirmar un cambio conceptual por commit.
- [ ] Confirmar que no se absorbieron archivos de otros agentes.
- [ ] Ejecutar servicios requeridos sin detener otros proyectos:

```bash
docker compose up -d postgres calc
docker compose --profile local up -d showdown
docker compose run --rm migrate up
```

- [ ] Ejecutar toda la suite TypeScript:

```bash
pnpm test
```

- [ ] Ejecutar toda la suite Python sola:

```bash
cd apps/agent
uv run pytest -q
```

- [ ] Ejecutar ambos scopes del auditor:

```bash
pnpm --filter @ludex/dataset-audit run audit --scope all
pnpm --filter @ludex/dataset-audit run audit --scope training
```

- [ ] Consultar directamente máscara, rewards y metadata:

```bash
docker exec ludex-postgres-1 psql -U ludex -d ludex -c \
  "SELECT count(*) AS actions_outside_mask
   FROM trajectory_steps
   WHERE action_taken IS NOT NULL
     AND NOT legal_actions @> jsonb_build_array(action_taken);"
docker exec ludex-postgres-1 psql -U ludex -d ludex -c \
  "SELECT t.final_result, s.reward, count(*)
   FROM trajectories t
   JOIN trajectory_steps s ON s.trajectory_id=t.id
   GROUP BY t.final_result, s.reward
   ORDER BY t.final_result, s.reward;"
```

- [ ] Confirmar por query los campos de metadata definidos en F2-08 sobre
  todas las decisiones nuevas; los históricos pueden ser NULL si así lo
  documenta la migración.
- [ ] Ejecutar una batalla completa con provider falso determinista y
  persistencia activa.
- [ ] Ejecutar al menos una batalla completa con un proveedor real ya
  validado, sin mezclar modelos.
- [ ] Inspeccionar manualmente al menos:
  - una batalla con movimiento;
  - una con cambio forzado;
  - una con Mega;
  - una con `cant`, confusión o Encore;
  - una trayectoria con más de una decisión en el mismo turno.
- [ ] Para cada muestra, comparar protocolo crudo, snapshot, máscara, acción,
  reasoning, metadata y reward.
- [ ] Ejecutar búsqueda de hardcodes:

```bash
git grep -n -i "gen6" -- apps packages db \
  ':!**/tests/**' ':!**/test/**' ':!**/evals/**'
```

- [ ] Revisar la matriz de F2-00 y exigir `implemented` para:
  `parse_state`, `retrieve_context`, `calc_damage`, `decide`, switch de modelos,
  execute-adapter, persistencia de `battle_turns`, `trajectory_steps`,
  schema version, acciones legales, reward y metadata de calidad.
- [ ] Confirmar que `human_approval`, checkpointer, API, WebSocket y conexión
  oficial siguen explícitamente en Fase 3 y no fueron implementados a medias.
- [ ] Releer todos los límites conocidos y rechazar cualquiera sin alcance por
  generación e impacto documentados.
- [ ] Emitir el `LINEAR_VERDICT` canónico definido en
  `docs/AGENT_GOVERNANCE.md`, usando como `reviewed_commit` la salida fresca de
  `git rev-parse HEAD`. Si existe cualquier blocker, el status es
  `Changes Requested` y Fase 3 permanece bloqueada.

**Criterios de aceptación:**

- Cero fallos o skips no explicados.
- Ambos scopes del auditor terminan con código 0.
- Cero acciones fuera de máscara.
- Cero pasos perdidos o sin materializar.
- Rewards correctos para win/loss/tie.
- Estado, contexto, calc, decisión y persistencia corresponden al mismo punto
  observable.
- La selección de modelo se resuelve por decisión y queda registrada.
- `docs/PLAN.md:216-218` está demostrado, no inferido.

**Commit:** No se commitea código desde esta issue. Solo documentación de cierre
si el veredicto es `Completed`.

---

## 4. Definition of Done común para F2-01…F2-10

Antes de mover una issue a `In Review`, el agente debe:

- [ ] Cumplir todos sus criterios de aceptación.
- [ ] Publicar causa raíz con evidencia.
- [ ] Adjuntar comandos y salida completa.
- [ ] Demostrar red → green → regresión rota → green restaurado.
- [ ] Ejecutar la suite relacionada, no solo el test nuevo.
- [ ] Registrar decisiones no triviales.
- [ ] Declarar límites y riesgos restantes.
- [ ] Confirmar `git status` y listar solo sus archivos.
- [ ] Usar este formato:

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

## 5. Reglas de rechazo y hold

Mover a `On Hold` cuando:

- el esquema real contradice la tarea;
- se necesita una operación destructiva todavía no autorizada;
- falta una credencial externa para una prueba real;
- tres hipótesis de arreglo fallaron y corresponde revisar arquitectura;
- otro agente conserva el ownership de un archivo necesario.

Mover a `Rejected` únicamente cuando:

- la tarea es duplicada;
- el enfoque viola `docs/PLAN.md`;
- propone esperar narración antes de responder;
- propone reserializar el rival desde `Battle` mutable;
- reimplementa daño en Python;
- filtra auditoría global para hacerla pasar;
- inventa metadata histórica;
- persiste secretos;
- hardcodea una generación en código de runtime.

## 6. Orden de ejecución recomendado

1. F2-00.
2. F2-01 y F2-04, con coordinación de fixtures.
3. F2-02 mientras F2-06 avanza en carril disjunto.
4. F2-03.
5. F2-05, con aprobación destructiva si corresponde.
6. F2-07 después de integrar F2-01 y F2-06.
7. F2-08 cuando `client.py` y migraciones estén libres.
8. F2-09.
9. F2-10.
10. F2-11.

Estimación de planificación, no compromiso contractual:

| Ola | Issues | Duración enfocada |
|---|---|---:|
| Baseline | F2-00 | 0.5 día |
| Integridad | F2-01…F2-05 | 3–5 días |
| Grafo | F2-06…F2-09 | 3–5 días |
| Proveedores | F2-10 | 1–2 días |
| Cierre | F2-11 | 1 día |

Total esperado: 7–12 días de trabajo enfocado. Un proveedor externo puede
extender su diagnóstico, pero no habilita corrupción silenciosa ni cambia el
criterio de cierre.
