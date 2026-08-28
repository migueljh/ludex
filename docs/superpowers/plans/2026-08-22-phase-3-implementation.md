# Phase 3 API, HITL, and Official Play Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Entregar la API local de Ludex, decisiones HITL/autónomas exact-once y juego oficial seguro mediante challenge y ladder `gen6randombattle`.

**Architecture:** El gate vive fuera de LangGraph, entre `decision_graph.ainvoke` y `execute_action`, y coordina los loops de FastAPI y poke-env mediante un `concurrent.futures.Future`. `POKE_LOOP` es el único escritor de la auditoría; FastAPI opera el registro vivo en memoria. Challenge y ladder son máquinas independientes, fail-closed y verificadas por aceptaciones live separadas.

**Tech Stack:** Python 3.12, asyncio, concurrent.futures, FastAPI 0.141.1, uvicorn 0.52.1, poke-env 0.15.0, SQLAlchemy 2/asyncpg, PostgreSQL/dbmate, pytest/pytest-asyncio, TypeScript 5.7, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-22-phase-3-design.md`

## Global Constraints

- Baseline inicial: `629af7813f6c6972a7e1fc9c76eaba15114f56c2`.
- Formato de aceptación: `gen6randombattle`; la generación siempre es parámetro.
- Gate fuera de LangGraph; `interrupt()` no es el mecanismo HITL.
- Outcomes exactos: `human_approved`, `human_override`, `timeout_auto`.
- Defaults: decisión `240s`, aprobación `10s`, envío `5s`, inactividad `300s`, login `15s`.
- Prohibidos `asyncio.wait_for`, `asyncio.timeout`, `wrap_future` bajo timeout y `cancel()` sobre el Future del CAS.
- La API no escribe `pending_decisions`; `POKE_LOOP` es el único writer.
- `official` exige DB marcada `acceptance` y rechaza `127.0.0.1:15432/ludex` antes de abrir red. No hay override en F3.
- Cuenta de testing obligatoria; cuenta real del torneo fuera de F3.
- Ladder off por defecto y con triple interlock.
- Nunca `docker compose down`, `docker stop`, `docker rm` ni puertos 5432/5433.
- Migraciones up/down sólo en DB descartable; `pg_dump` antes de riesgo sobre la DB canónica.
- Tests nuevos: rojo, verde y mutación deliberada que restaure el rojo.
- Commits en inglés y rutas explícitas; nunca `git add .` ni `git add -A`.
- Cada commit se pushea inmediatamente a la rama remota de su tarea.
- Ninguna rama de implementador se pushea directamente a `main`.
- Latwan integra sólo trabajo aceptado en `integration/phase-3-accepted` y pushea tras cada integración.
- Tasos revisa read-only Base SHA..Head SHA; sólo Latwan emite `LINEAR_VERDICT`.
- Cero providers/login/live antes de S9; S9 usa Gemini free tier o un modelo chino expresamente autorizado, nunca `gpt-5.6-luna`.

---

## File and ownership map

| Área | Archivos principales | Owner recomendado |
|---|---|---|
| decisiones/configuración | `docs/DECISIONS.md`, `docs/PLAN.md`, `docs/HANDOFF_CLAUDE.md`, `.env.example`, `config.py` | Neoblex / Sonnet 5 |
| gate/política/eventos | `apps/agent/src/ludex_agent/hitl/*.py` | Neoblex / Sonnet 5 |
| persistencia/auditor | migración, repositorios, `packages/dataset-audit/*` | Neoblex / Sonnet 5 |
| API/WS | `apps/agent/src/ludex_agent/api/*.py` | Nebula, tras aceptar interfaces |
| integración poke-env | `showdown/client.py`, `showdown/protocol.py` | Neoblex / Sonnet 5 |
| conexión/sesiones | `showdown/connection.py`, `showdown/lobby.py`, `runner/session.py` | Neoblex / Sonnet 5 |
| challenges/ladder | lobby, session runner y rutas API | Nebula, después de S4 |
| revisión | ningún archivo | Tasos / Grok 4.6, read-only |
| integración/live | rama de integración y evidencia | Latwan / Codex |

No hay dos implementadores sobre `showdown/client.py`, `config.py`, una migración o un mismo worktree.

---

## Execution protocol

1. Latwan crea el issue paraguas y un issue por task/rebanada en Linear. Todos
   empiezan en Backlog; sólo el issue activado pasa a In Progress.
2. Latwan crea y pushea `integration/phase-3-accepted` desde el SHA aceptado de
   `main`. Cada worktree implementador nace desde el HEAD aceptado vigente de
   esa rama, nunca desde otro worktree en movimiento.
3. Branches sugeridas: `phase3/s0-runtime-contracts`, `phase3/s1-hitl-domain`,
   `phase3/s2-decision-audit`, `phase3/s3-control-api`,
   `phase3/s4-official-connection`, `phase3/s5-challenges`,
   `phase3/s6-ladder` y `phase3/s7-provenance`.
4. Neoblex usa Sonnet 5 para la ruta compleja. Nebula toma superficies
   acotadas; su modelo efectivo se verifica al despachar y no se usa una sesión
   de OpenCode sin cuota. Tasos permanece en Grok 4.6 read-only.
5. El implementador hace commits pequeños y ejecuta `git push` inmediatamente
   después de cada uno. Su entrega final incluye Base/Head, lista de commits,
   comandos RED/GREEN/mutaciones, suites, diff-check, scans y rutas explícitas.
6. Tasos revisa el rango exacto. Latwan reproduce los riesgos, adjudica cada
   finding, emite el único veredicto, integra lo aceptado y pushea la rama de
   integración. El implementador nunca se autoaprueba.
7. Ningún issue pasa a Completed hasta que los gates se repitan sobre el commit
   integrado, no sólo sobre la rama del implementador.

---

### Task 1: Lock documentation, configuration, budgets, and canonical DB guard

**Files:**
- Modify: `docs/DECISIONS.md`
- Modify: `docs/PLAN.md:119-169`
- Modify: `docs/HANDOFF_CLAUDE.md:198-202`
- Modify: `.env.example`
- Modify: `apps/agent/src/ludex_agent/config.py:12-140`
- Modify: `apps/agent/tests/test_config.py`

**Interfaces:**
- Consumes: approved Phase 3 spec.
- Produces: `ConnectionMode`, `DatabaseRole`, coherent budget settings, and fail-closed official DB validation.

- [ ] **Step 1: Write the binding decision entry**

Record gate location, non-use of `interrupt()`, restart limits, three metadata axes, D42 clock rule, explicit challenges, ladder, override cost accounting and canonical DB prohibition.

- [ ] **Step 2: Correct PLAN and handoff narrowly**

Add a PLAN §6 note pointing to the new decision. Replace the obsolete real-account/exported-team blockers: F3 needs a testing account; exported teams belong to F5.

- [ ] **Step 3: Write failing configuration tests**

```python
def test_official_requires_acceptance_database_role():
    with pytest.raises(RuntimeError, match="DATABASE_ROLE=acceptance"):
        load_settings(official_env(DATABASE_ROLE="canonical"))

def test_official_rejects_canonical_ludex_dsn():
    env = official_env(
        DATABASE_ROLE="acceptance",
        DATABASE_URL="postgres://ludex:x@127.0.0.1:15432/ludex",
    )
    with pytest.raises(RuntimeError, match="base canónica"):
        load_settings(env)

def test_phase3_budget_defaults_are_coherent():
    settings = load_settings(local_env())
    assert settings.decision_budget_seconds == 240
    assert settings.approval_timeout_seconds == 10
    assert settings.send_margin_seconds == 5
    assert settings.battle_timeout_seconds == 300
    assert 240 + 10 + 5 < 300
```

- [ ] **Step 4: Run RED**

Run: `cd apps/agent && uv run pytest tests/test_config.py -q`

Expected: missing fields/guards and old `180` default.

- [ ] **Step 5: Implement and run GREEN**

Parse the original DSN before `_to_asyncpg`. Reject unsafe official configuration before constructing any connection. Mutate the role and DSN checks separately and confirm RED before restoring.

- [ ] **Step 6: Commit and push**

Run `git diff --check`; stage the six explicit files; commit `Define Phase 3 runtime safety contracts`; run `git push -u origin HEAD`.

---

### Task 2: Implement the pure exact-once HITL domain

**Files:**
- Create: `apps/agent/src/ludex_agent/hitl/__init__.py`
- Create: `apps/agent/src/ludex_agent/hitl/gate.py`
- Create: `apps/agent/src/ludex_agent/hitl/policy.py`
- Create: `apps/agent/src/ludex_agent/hitl/events.py`
- Create: `apps/agent/tests/hitl/test_gate.py`
- Create: `apps/agent/tests/hitl/test_policy.py`
- Create: `apps/agent/tests/hitl/test_events.py`

**Interfaces:**
- Consumes: Task 1 settings and injected D42 clock.
- Produces: `ApprovalKey`, `ApprovalProposal`, `ApprovalResolution`, `PendingApproval`, `AlreadyResolved`, policies, `EventHub`, `ReplayGapError`.

- [ ] **Step 1: Define records through failing tests**

```python
@dataclass(frozen=True)
class ApprovalKey:
    battle_tag: str
    decision_index: int
    attempt_index: int

@dataclass(frozen=True)
class ApprovalProposal:
    action: dict[str, object]
    legal_actions: Sequence[dict[str, object]]
    model_envelope: dict[str, object]

@dataclass(frozen=True)
class ApprovalResolution:
    outcome: Literal["human_approved", "human_override", "timeout_auto"]
    action: dict[str, object]
    resolved_by: Literal["operator", "timer", "system"]
    resolved_reason: str | None = None
```

Test concurrent resolvers, illegal override, fake-clock timeout, skip mode, autonomous toggle, monotonic event sequence, exact replay and `ReplayGapError`.

- [ ] **Step 2: Run RED**

Run: `cd apps/agent && uv run pytest tests/hitl -q`

- [ ] **Step 3: Implement the waiter**

```python
async def await_resolution(self) -> ApprovalResolution:
    while True:
        if self._future.done():
            return self._future.result()
        if self._clock() >= self._deadline:
            self._try_resolve_timeout()
            return self._future.result()
        self._was_pending = True
        await self._tick()
```

Validate overrides before CAS. Translate `InvalidStateError` to `AlreadyResolved(winner)`.

- [ ] **Step 4: Implement policies and replay**

Production policy skips local and official/autonomous. EventHub owns per-stream sequence and bounded ring buffer; an old cursor raises `ReplayGapError`.

- [ ] **Step 5: Run GREEN and mutations**

Mutate CAS winner, deadline, `_was_pending`, illegal override and replay-gap detection separately. Add a source canary rejecting `wait_for`, `asyncio.timeout`, `wrap_future` and `.cancel(` in `gate.py`.

- [ ] **Step 6: Commit and push**

Stage only the seven listed files; commit `Add exact-once human approval domain`; push immediately.

---

### Task 3: Add durable decision audit and dataset authorship

**Files:**
- Create: `db/migrations/20260822000001_phase3_hitl.sql`
- Modify: `db/schema.sql`
- Modify: `apps/agent/src/ludex_agent/db/models.py:102-144`
- Create: `apps/agent/src/ludex_agent/db/pending_repository.py`
- Modify: `apps/agent/src/ludex_agent/db/repository.py:139-220`
- Create: `apps/agent/tests/db/test_pending_repository.py`
- Modify: `apps/agent/tests/db/test_models.py`
- Modify: `apps/agent/tests/db/test_repository.py`
- Modify: `packages/dataset-audit/src/types.ts`
- Modify: `packages/dataset-audit/src/db.ts`
- Modify: `packages/dataset-audit/src/render.ts`
- Create: `packages/dataset-audit/test/authorship.test.ts`

**Interfaces:**
- Consumes: Task 2 JSON-safe records.
- Produces: `PendingDecisionRecord`, `PendingDecisionRepository.insert_awaiting/resolve/abort_stale`, the new `approval_outcome` keyword on `save_step`, and authorship mix reporting.

- [ ] **Step 1: Write migration and failing DB tests**

Create `pending_decisions` with unique `(battle_tag, decision_index, attempt_index)`, closed status checks, proposal/resolution fields, usage, `approval_wait_ms`, timestamps and down section. Add nullable checked `approval_outcome`.

- [ ] **Step 2: Run RED on disposable DB**

Run: `cd apps/agent && uv run pytest tests/db/test_pending_repository.py tests/db/test_models.py tests/db/test_repository.py -q`

- [ ] **Step 3: Implement repository ownership**

Use these exact public signatures: `insert_awaiting(self, proposal:
PendingDecisionRecord) -> None`, `resolve(self, key: ApprovalKey, resolution:
ApprovalResolution, approval_wait_ms: int) -> None`, and
`abort_stale(self, reason: str = "process_restart") -> int` (all async).
`PendingDecisionRecord` is an immutable record containing `key`, `proposal`,
`status="awaiting"` and the D38 model envelope.

Create engine lazily in POKE_LOOP with `NullPool`. API never receives this repository.

- [ ] **Step 4: Extend save-step and auditor**

Persist `approval_outcome`; `human_override` writes all eleven D38 fields NULL as a group. Add audit counts for agent/human and all outcomes. Synthetic tests prove human rows remain training-eligible.

- [ ] **Step 5: Run GREEN and mutations**

Run Python DB tests and `pnpm --filter @ludex/dataset-audit test`. Remove each CHECK and global D38 query in a copy; require RED. Verify migration up/down.

- [ ] **Step 6: Commit and push**

Stage only listed files; commit `Persist human approval audit metadata`; push immediately.

---

### Task 4: Build registry and loopback API/WebSockets

**Files:**
- Modify: `apps/agent/pyproject.toml`
- Modify: `apps/agent/uv.lock`
- Create: `apps/agent/src/ludex_agent/hitl/registry.py`
- Create: `apps/agent/src/ludex_agent/api/__init__.py`
- Create: `apps/agent/src/ludex_agent/api/app.py`
- Create: `apps/agent/src/ludex_agent/api/schemas.py`
- Create: `apps/agent/src/ludex_agent/api/routes.py`
- Create: `apps/agent/src/ludex_agent/api/websockets.py`
- Create: `apps/agent/src/ludex_agent/db/api_read_repository.py`
- Create: `apps/agent/tests/api/test_app.py`
- Create: `apps/agent/tests/api/test_decisions.py`
- Create: `apps/agent/tests/api/test_websockets.py`
- Create: `apps/agent/tests/hitl/test_registry.py`

**Interfaces:**
- Consumes: Task 2 gate/events and Task 3 historical schema; never `PendingDecisionRepository`.
- Produces: `ApprovalRegistry`, `create_router() -> APIRouter`,
  `register_websocket_routes(app: FastAPI) -> None`, and `create_app` returning
  `FastAPI`.

- [ ] **Step 1: Add pinned dependencies**

Use `uv add --project apps/agent fastapi==0.141.1 uvicorn==0.52.1`; do not hand-edit `uv.lock`.

- [ ] **Step 2: Write RED registry/REST/WS tests**

Exercise approve, override, illegal `422`, stale `409`, winning outcome, settings/model validation, Origin, monotonic sequence, exact resume, `REPLAY_GAP` and disconnect-independent timeout.

- [ ] **Step 3: Implement minimal API**

```python
def create_app(*, registry: ApprovalRegistry, event_hub: EventHub,
               settings_repo: ModelRepository,
               historical_repo_factory: Callable[[], ApiReadRepository]) -> FastAPI:
    app = FastAPI()
    app.state.registry = registry
    app.state.event_hub = event_hub
    app.state.settings_repo = settings_repo
    app.state.historical_repo_factory = historical_repo_factory
    app.include_router(create_router())
    register_websocket_routes(app)
    return app
```

Bind only `127.0.0.1`. Pending state comes from registry; historical reads use a FastAPI-loop engine.

- [ ] **Step 4: Run GREEN and security mutations**

Run `cd apps/agent && uv run pytest tests/api tests/hitl/test_registry.py -q`. Plant a password sentinel and assert absence from events/logs/rows. Force a cross-loop repository use and require typed failure.

- [ ] **Step 5: Commit and push**

Stage explicit dependency/source/test files; commit `Expose Phase 3 control API and live events`; push immediately.

---

### Task 5: Integrate the gate into the live decision path

**Files:**
- Modify: `apps/agent/src/ludex_agent/showdown/client.py:675-900,1547-1633`
- Modify: `apps/agent/src/ludex_agent/cli.py:191-348`
- Modify: `apps/agent/tests/showdown/test_client.py`
- Modify: `apps/agent/tests/showdown/test_decision_observability.py`
- Modify: `apps/agent/tests/integration/test_play.py`
- Modify: `apps/agent/tests/test_cli.py`
- Modify: `apps/agent/tests/test_benchmark.py`
- Modify: `apps/agent/tests/test_matrix.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: gate between `ainvoke` and `execute_action`, coherent step authorship, bounded drain.

- [ ] **Step 1: Write RED integration canaries**

Assert gate placement, approve/override/timeout, exactly one `/choose`, no gate after projection failure, new attempt after rejection, and shutdown resolving `aborted` without a step.

- [ ] **Step 2: Prove offline commands cannot block**

Tests for `run`, `benchmark` and `matrix-run` set DB HITL mode and assert that production local policy creates no Future.

- [ ] **Step 3: Insert the minimal gate call**

Persist `awaiting`, publish, await, persist outcome, then call `execute_action` once. Do not modify graph, `PendingChoice`, recorder or raw inbox.

- [ ] **Step 4: Wire coherent persistence**

`cli.py` stops hardcoding `source="agent"`. It reads `action_source` and `approval_outcome` from the step. Override nulls all eleven D38 fields while retaining the proposal in `pending_decisions`.

- [ ] **Step 5: Run GREEN and mutations**

Mutate gate position, double execution, action source and one D38 field separately. Run:

```bash
cd apps/agent && uv run pytest tests/hitl tests/showdown tests/integration/test_play.py tests/test_cli.py tests/test_benchmark.py tests/test_matrix.py -q
```

- [ ] **Step 6: Commit and push**

Commit `Integrate human approval into battle decisions` with explicit paths and push immediately.

---

### Task 6: Add official connection management and sequential sessions

**Files:**
- Create: `apps/agent/src/ludex_agent/showdown/connection.py`
- Create: `apps/agent/src/ludex_agent/showdown/lobby.py`
- Create: `apps/agent/src/ludex_agent/runner/__init__.py`
- Create: `apps/agent/src/ludex_agent/runner/session.py`
- Modify: `apps/agent/src/ludex_agent/showdown/client.py`
- Modify: `apps/agent/src/ludex_agent/cli.py:117-242`
- Modify: `apps/agent/src/ludex_agent/api/routes.py` (connection/session endpoints; D66)
- Create: `apps/agent/tests/showdown/test_connection.py`
- Create: `apps/agent/tests/showdown/test_lobby.py`
- Create: `apps/agent/tests/runner/test_session.py`

**Interfaces:**
- Consumes: Task 1 safe settings and Task 4 event hub.
- Produces: `ConnectionManager`, `LoginWatchdog`, `LobbyInbox`, `SessionRunner`.

- [ ] **Step 1: Write RED fake-client tests**

Cover mode-aware configuration, DB rejection before socket construction, invalid login becoming typed failure within fake 15s, forbidden login during `choose_move`, sequential `N`, and stop-after-current.

- [ ] **Step 2: Construct login without retaining password**

Read credentials only where `AccountConfiguration` is created. Never place password in Settings, events or persistent objects.

- [ ] **Step 3: Add watchdog and mode-aware preflight**

Observe `logged_in` and `_background_failure`. Local error guidance mentions Docker only in local mode. Login finishes before the first battle.

- [ ] **Step 4: Implement sequential sessions**

Only one matchmaking request may be active. Deleting a session sets stop-after-current and never cancels a live battle.

- [ ] **Step 5: Run GREEN and mutations**

Remove watchdog observation, permit concurrency and move DB validation after connection separately; each canary must fail.

- [ ] **Step 6: Commit and push**

Commit `Add safe official connection sessions`; push immediately.

---

### Task 7: Make challenge acceptance explicit for both producers

**Files:**
- Modify: `apps/agent/src/ludex_agent/showdown/client.py:675-800`
- Modify: `apps/agent/src/ludex_agent/showdown/lobby.py`
- Modify: `apps/agent/src/ludex_agent/api/routes.py`
- Modify: `apps/agent/tests/showdown/test_pokeenv_contract.py`
- Modify: `apps/agent/tests/showdown/test_lobby.py`
- Modify: `apps/agent/tests/api/test_app.py`

**Interfaces:**
- Consumes: Task 6 lobby/session runner.
- Produces: both `LudexPlayer` callback overrides and explicit accept/reject/outgoing routes.

- [ ] **Step 1: Write five RED canaries**

Verify callback identity, no enqueue from `|updatechallenges|`, no enqueue from PM `/challenge`, visibility of other formats, and one explicit queue insert from accept.

- [ ] **Step 2: Override both callbacks**

Publish normalized lobby events and never call either parent implementation. Do not touch `PSClient.listen()` or call `PSClient.accept_challenge` directly.

- [ ] **Step 3: Implement explicit actions**

Accept validates a displayed challenge and enqueues only that username for the waiting `Player.accept_challenges` flow. Reject removes only the displayed item; outgoing uses SessionRunner.

- [ ] **Step 4: Mutate both producers**

Restore each parent call separately and require its canary to fail.

- [ ] **Step 5: Commit and push**

Commit `Require explicit Showdown challenge acceptance`; push immediately.

---

### Task 8: Add fail-closed ladder sessions

**Files:**
- Modify: `apps/agent/src/ludex_agent/runner/session.py`
- Modify: `apps/agent/src/ludex_agent/api/routes.py`
- Modify: `apps/agent/src/ludex_agent/api/schemas.py`
- Modify: `apps/agent/tests/runner/test_session.py`
- Modify: `apps/agent/tests/api/test_app.py`

**Interfaces:**
- Consumes: Task 6 connection/session runner.
- Produces: `SessionKind.LADDER`, triple interlock and poke-env ladder call.

- [ ] **Step 1: Write zero-call tests for every missing interlock**

For local mode, disabled ladder, missing call confirmation, unconfirmed testing account, canonical DB and wrong format, assert socket and `/search` calls both equal zero.

- [ ] **Step 2: Implement the contract**

Only official + `gen6randombattle` + acceptance DB + enabled + confirmed call + confirmed testing account may call `Player.ladder(1)`. Persist `source="ladder"`.

- [ ] **Step 3: Verify off-after-session**

Disable ladder after acceptance; a subsequent request without re-enable fails before network.

- [ ] **Step 4: Mutate every interlock**

Bypass each guard separately and require the zero-call canary to fail.

- [ ] **Step 5: Commit and push**

Commit `Add guarded official ladder sessions`; push immediately.

---

### Task 9: Persist narrow Phase 6 hooks and finish audit reporting

**Files:**
- Modify: `apps/agent/src/ludex_agent/showdown/protocol.py`
- Modify: `apps/agent/src/ludex_agent/showdown/client.py`
- Modify: `apps/agent/src/ludex_agent/cli.py:267-346`
- Modify: `apps/agent/src/ludex_agent/db/repository.py`
- Modify: `apps/agent/tests/showdown/test_protocol.py`
- Modify: `apps/agent/tests/db/test_repository.py`
- Modify: `packages/dataset-audit/src/render.ts`
- Modify: `packages/dataset-audit/test/authorship.test.ts`

**Interfaces:**
- Consumes: completed battle/session metadata.
- Produces: opponent username, optional replay URL/rating bucket, authorship report.

- [ ] **Step 1: Write RED protocol/persistence tests**

Assert username follows p1/p2 role, rating is parsed only when public, challenge rating stays NULL, replay URL is sanitized, and no bucket is invented.

- [ ] **Step 2: Implement only existing hooks**

Persist normalized identity, `replay_url` and optional `elo_bucket`. Do not create profiles, replay imports, analyses or coaching tables.

- [ ] **Step 3: Run GREEN and mutations**

Mutate role selection and NULL-rating behavior. Run focal Python tests plus `pnpm --filter @ludex/dataset-audit test`.

- [ ] **Step 4: Commit and push**

Commit `Record official battle provenance hooks`; push immediately.

---

### Task 10: Run the complete offline gate and independent review

**Files:**
- Create: sanitized evidence at the repository path selected by the active Linear issue.
- Modify: no production files unless a source task receives Changes Requested.

**Interfaces:**
- Consumes: Tasks 1–9 on `integration/phase-3-accepted`.
- Produces: complete offline REVIEW PACKET and tech-lead verdict.

- [ ] **Step 1: Integrate only accepted commits**

After each task packet and Tasos review, Latwan adjudicates, integrates into `integration/phase-3-accepted`, reruns affected gates and pushes the integration branch.

- [ ] **Step 2: Run full offline verification**

```bash
cd apps/agent && uv run pytest -q
pnpm test
git diff --check
```

Parse changed JSON, scan secrets and absolute paths, run both auditor scopes on non-empty disposable data, and query D38/D44 invariants directly.

- [ ] **Step 3: Audit every mutation record**

Require named canary, mutation, RED command/output, restored GREEN command/output and pinned source path. Missing RED means Changes Requested.

- [ ] **Step 4: Request exact final offline review**

Tasos reviews Base..Head read-only. Latwan adjudicates all findings; reviewer never edits or changes state.

---

### Task 11: Execute challenge acceptance S9a

**Files:**
- Create: sanitized challenge evidence at the active issue path.
- Modify: no code unless a reproducible defect returns to its source task.

**Interfaces:**
- Consumes: accepted offline SHA, disposable DB and user-confirmed testing account.
- Produces: official challenge evidence and invariant report.

- [ ] **Step 1: Prepare isolated DB**

Confirm database name is not `ludex`, set role `acceptance`, and migrate. Never print credentials. Back up the canonical DB before any operation that could target it.

- [ ] **Step 2: Select an allowed decision provider**

Use Gemini free tier unless the user gives a new explicit cap for a paid Chinese model. Never use `gpt-5.6-luna`.

- [ ] **Step 3: Play one complete challenge**

Accept explicitly through the API. Exercise one `human_approved`, one legal `human_override`, and one unanswered `timeout_auto`.

- [ ] **Step 4: Verify and sanitize**

Require `source=challenge`, `played_by=bot`, all outcomes, zero human/provider incoherence, no partial trajectory and no secret/assertion/path leak.

---

### Task 12: Execute ladder acceptance S9b and close Phase 3

**Files:**
- Create: sanitized ladder evidence at the active issue path.
- Modify: no code unless a reproducible defect returns to its source task.

**Interfaces:**
- Consumes: Task 11 passing, triple interlock and testing account.
- Produces: independent ranked evidence and final integrated verdict.

- [ ] **Step 1: Enable all ladder interlocks**

Confirm official, `gen6randombattle`, ladder enabled, per-call confirm, testing account confirmed, acceptance role and non-canonical DSN.

- [ ] **Step 2: Play exactly one ranked battle**

Observe at least one `human_approved` or `timeout_auto`. Require `source=ladder`; persist rating only if public protocol supplies it.

- [ ] **Step 3: Disable ladder and prove zero-call**

Disable it, issue a second harness request, and verify zero `/search` calls.

- [ ] **Step 4: Repeat complete integrated gates**

Run Python/TypeScript suites, both auditor scopes, direct invariants, JSON parsing, scans and `git diff --check` on the exact SHA inspected by Tasos.

- [ ] **Step 5: Emit the only completion verdict**

Latwan emits `LINEAR_VERDICT`, pushes accepted integration, advances and pushes `main` only after all gates pass, then marks the final issue Completed.

---

## Execution order and concurrency

```text
Task 1 → Task 2
             ├─ Task 3 (Neoblex: DB/auditor)
             └─ Task 4 (Nebula: API/WS)        [parallel]
                    ↓ both accepted
                 Task 5 → Task 6
             ├─ Task 7 → Task 8 (Nebula)
             └─ Task 9 (Neoblex)               [parallel sólo sin client.py]
                    ↓ all accepted
                 Task 10 → Task 11 → Task 12
```

Task 6 owns `showdown/client.py` while active. Task 9 may advance its TypeScript portion but cannot commit Python overlap until Task 6 is integrated. Tasks 7 and 8 remain serial because they share routes and session runner.

## Estimate

| tramo | elapsed realista |
|---|---:|
| Tasks 1–2: contratos + dominio puro | 8–12 h |
| Tasks 3–4 en paralelo | 10–16 h |
| Tasks 5–6: integración + conexión | 10–16 h |
| Tasks 7–9 con paralelismo acotado | 8–14 h |
| Tasks 10–12: gates, reviews y live | 8–14 h |
| **Total realista de reloj** | **44–72 h** |

Suelo si cada review entra a la primera: **32–40 horas**. Techo si login, timers o la primera ranked revelan una clase nueva: **80–100 horas**. Trabajo agregado: aproximadamente **90–130 agent-hours**. Con cuenta testing disponible y trabajo sostenido: **5–8 días de calendario** realistas.

El cuello no es escribir FastAPI: son el gate cross-loop, D32/D38 y las revisiones seriales antes del servidor oficial.
