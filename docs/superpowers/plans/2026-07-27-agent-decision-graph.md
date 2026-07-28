# Agent Decision Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir un grafo LangGraph por decisión que usa daño determinista, valida una salida LLM estructurada, separa fallos de infraestructura de fallos del modelo y nunca elige una acción ilegal.

**Architecture:** Un `StateGraph` async coordina `parse_state → calc_damage → decide`. El grafo consume únicamente una fotografía allowlisted y dependencias inyectadas (`DamageCalculator`, `DecisionProvider`); `LudexPlayer` captura fotografía y órdenes antes del primer `await`. Autoría (`action_source`) y camino interno (`action_path`) se persisten por separado.

**Tech Stack:** Python 3.12, uv, poke-env 0.15.0, LangGraph 1.2.9, langchain-google-genai 4.2.7, HTTPX 0.28.1, Pydantic 2.13.4, PostgreSQL 16/dbmate 2.21, pytest 8.3.4.

## Global Constraints

- Leer y respetar `AGENTS.md`, `.claude/agent-recording/SKILL.md`, `.claude/migrations/SKILL.md` y `.claude/verification/SKILL.md`.
- No tocar `.worktrees/`, no detener contenedores y conectar por `127.0.0.1`.
- No modificar `showdown/client.py` hasta confirmación de que el otro agente terminó.
- `state/` permanece puro: no importa `poke_env`, HTTP ni DB.
- `db/` no importa `poke_env`.
- La fotografía y el mapa acción → `BattleOrder` se capturan síncronamente antes del primer `await`.
- El prompt usa solo campos nombrados de `serialize_battle`; chat y protocolo crudo no entran.
- La generación siempre es parámetro.
- Tests primero, RED observado, implementación mínima, GREEN y rotura deliberada de cada canario nuevo.
- Commits en inglés, `-m` antes de `--`, rutas explícitas, nunca `git add -A`.
- Sin clave real, las métricas LLM se reportan pendientes, no como cero.

---

### Task 1: Dependencias y configuración segura

**Files:**
- Modify: `apps/agent/pyproject.toml`
- Modify: `apps/agent/uv.lock`
- Modify: `apps/agent/src/ludex_agent/config.py`
- Modify: `apps/agent/tests/test_config.py`

**Interfaces:**
- Produces: `Settings.llm_provider`, `llm_model`, `llm_api_key_env`, `llm_api_keys_env`, `llm_request_timeout_seconds`, `decision_budget_seconds`, `showdown_turn_limit_seconds`.

- [ ] **Step 1: escribir tests RED de configuración**

Agregar casos que:

```python
settings = load_settings({
    "DATABASE_URL": "postgres://ludex:ludex@127.0.0.1:15432/ludex",
    "LUDEX_PROVIDER": "google",
    "LUDEX_MODEL": "gemini-test",
    "LUDEX_API_KEY_ENV": "GOOGLE_API_KEY",
    "LUDEX_API_KEYS_ENV": "GOOGLE_API_KEYS",
    "LUDEX_LLM_REQUEST_TIMEOUT_SECONDS": "30",
    "LUDEX_DECISION_BUDGET_SECONDS": "240",
    "LUDEX_SHOWDOWN_TURN_LIMIT_SECONDS": "300",
})
assert settings.decision_budget_seconds == 240
```

Verificar también que `decision_budget >= showdown_turn_limit`, timeout no
positivo, proveedor/modelo vacíos y nombres de variables vacíos fallen con
mensajes accionables, sin leer ni exponer el valor de la clave.

- [ ] **Step 2: confirmar RED**

Run:

```bash
cd apps/agent
uv run pytest tests/test_config.py -q
```

Expected: falla porque `Settings` todavía no contiene los campos LLM.

- [ ] **Step 3: agregar dependencias pineadas**

Run:

```bash
cd apps/agent
uv add 'langgraph==1.2.9' 'langchain-google-genai==4.2.7' \
  'httpx==0.28.1' 'pydantic==2.13.4'
```

Implementar `load_settings(environ: Mapping[str, str] | None = None)` para que
los tests inyecten variables sin mutar el proceso.

- [ ] **Step 4: confirmar GREEN y lock consistente**

Run:

```bash
cd apps/agent
uv lock --check
uv run pytest tests/test_config.py -q
```

- [ ] **Step 5: romper deliberadamente el guardrail temporal**

Cambiar temporalmente la comparación para aceptar `240 >= 200`, ejecutar el
test que exige rechazo y observar RED. Restaurar y ejecutar GREEN.

- [ ] **Step 6: commit**

```bash
git add -- apps/agent/pyproject.toml apps/agent/uv.lock \
  apps/agent/src/ludex_agent/config.py apps/agent/tests/test_config.py
git commit -m "feat(agent): configure decision graph providers" -- \
  apps/agent/pyproject.toml apps/agent/uv.lock \
  apps/agent/src/ludex_agent/config.py apps/agent/tests/test_config.py
```

---

### Task 2: Migración nullable `action_path`

**Files:**
- Create: `db/migrations/20260727000008_trajectory_action_path.sql`
- Modify: `apps/agent/src/ludex_agent/db/models.py`
- Modify: `apps/agent/src/ludex_agent/db/repository.py`
- Modify: `apps/agent/tests/db/test_models.py`
- Modify: `apps/agent/tests/db/test_repository.py`

**Interfaces:**
- Changes: `BattleRepository.save_step(..., source: str, action_path: str | None = None)`.
- Produces: `trajectory_steps.action_path text NULL` con CHECK.

- [ ] **Step 1: tomar y validar backup**

```bash
docker exec ludex-postgres-1 pg_dump -U ludex -d ludex \
  --format=custom --compress=9 -f /tmp/antes-grafo.dump
docker exec ludex-postgres-1 sh -lc \
  'test -s /tmp/antes-grafo.dump && pg_restore -l /tmp/antes-grafo.dump >/dev/null'
```

- [ ] **Step 2: escribir tests RED**

El test de esquema debe consultar `information_schema.columns` y
`pg_get_constraintdef`, comprobando:

```python
assert column.is_nullable == "YES"
assert column.data_type == "text"
assert "llm" in check_definition
assert "llm_retry" in check_definition
assert "fallback" in check_definition
```

El test de repositorio guarda `source="agent", action_path="llm_retry"` y
verifica ambos ejes por separado. Otro inserta `action_path=None`.

- [ ] **Step 3: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/db/test_models.py tests/db/test_repository.py -q
```

- [ ] **Step 4: escribir migración**

```sql
-- migrate:up
ALTER TABLE trajectory_steps
  ADD COLUMN action_path text NULL
  CONSTRAINT trajectory_steps_action_path_check
  CHECK (action_path IN ('llm', 'llm_retry', 'fallback'));

-- migrate:down
ALTER TABLE trajectory_steps DROP COLUMN action_path;
```

Actualizar modelo y `INSERT ... ON CONFLICT` para escribir/actualizar la
columna sin cambiar `action_source`.

- [ ] **Step 5: aplicar por la ruta real y confirmar GREEN**

```bash
docker compose run --rm migrate up
cd apps/agent
uv run pytest tests/db/test_models.py tests/db/test_repository.py -q
```

- [ ] **Step 6: probar el CHECK de verdad**

Insertar dentro de una transacción de test `action_path='random'` y verificar
`CheckViolation`, luego rollback. Romper temporalmente el CHECK del fixture no
cuenta: el test debe ejercer PostgreSQL real.

- [ ] **Step 7: commit**

```bash
git add -- db/migrations/20260727000008_trajectory_action_path.sql \
  apps/agent/src/ludex_agent/db/models.py \
  apps/agent/src/ludex_agent/db/repository.py \
  apps/agent/tests/db/test_models.py apps/agent/tests/db/test_repository.py
git commit -m "feat(agent): record internal action path" -- \
  db/migrations/20260727000008_trajectory_action_path.sql \
  apps/agent/src/ludex_agent/db/models.py \
  apps/agent/src/ludex_agent/db/repository.py \
  apps/agent/tests/db/test_models.py apps/agent/tests/db/test_repository.py
```

---

### Task 3: Estado del grafo y payload allowlisted

**Files:**
- Create: `apps/agent/src/ludex_agent/graph/__init__.py`
- Create: `apps/agent/src/ludex_agent/graph/state.py`
- Create: `apps/agent/tests/graph/test_state.py`

**Interfaces:**
- Produces: `GraphInput`, `GraphState`, `allowlisted_state(raw: dict) -> dict`.

- [ ] **Step 1: escribir tests RED**

Construir un estado válido con una clave centinela `"chat": "rendite"` y
atributos anidados extra. Verificar que el resultado contiene únicamente:

```python
{
    "schema_version", "turn", "player_role", "format", "gen",
    "field", "me", "opponent", "legal_actions",
}
```

y que el texto `"rendite"` no aparece en `json.dumps(result)`.

- [ ] **Step 2: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/graph/test_state.py -q
```

- [ ] **Step 3: implementar lista blanca explícita**

Definir `TypedDict` y copiar campo por campo, incluidas las claves conocidas de
Pokémon (`species`, `hp_fraction`, `active`, `fainted`, `status`, `level`,
`item`, `ability`, `types`, `boosts`, `moves` y `stats` solo cuando existe).
No usar introspección ni `dict(raw)`.

- [ ] **Step 4: confirmar GREEN y romper el canario**

Agregar temporalmente `chat` a la salida, observar que el test falla, restaurar
y confirmar GREEN.

- [ ] **Step 5: commit**

```bash
git add -- apps/agent/src/ludex_agent/graph/__init__.py \
  apps/agent/src/ludex_agent/graph/state.py apps/agent/tests/graph/test_state.py
git commit -m "feat(agent): define allowlisted graph state" -- \
  apps/agent/src/ludex_agent/graph/__init__.py \
  apps/agent/src/ludex_agent/graph/state.py apps/agent/tests/graph/test_state.py
```

---

### Task 4: Cliente HTTP de calc y nodo determinista

**Files:**
- Create: `apps/agent/src/ludex_agent/graph/calc.py`
- Create: `apps/agent/tests/graph/test_calc.py`
- Create: `apps/agent/tests/graph/test_calc_integration.py`

**Interfaces:**
- Produces:
  - `CalcClient(base_url: str, timeout_seconds: float)`.
  - `async calculate(request: dict) -> CalcResult`.
  - `async calc_damage(state: GraphState, calculator: DamageCalculator) -> dict`.
  - `rank_move_fallback(...) -> dict`.
  - `rank_switch_fallback(...) -> dict`.

- [ ] **Step 1: tests RED de ranking**

Casos obligatorios:

```python
# Ice Beam garantiza KO aunque Thunderbolt tenga promedio bruto mayor.
assert chosen == {"kind": "move", "id": "icebeam"}

# mega false se trata en decide, no crea un score distinto acá.
# Overkill queda capado al HP restante.
# Empates preservan el orden de legal_actions.
# Cambio forzado elige el menor peor daño esperado relativo al HP.
```

Para multigolpe, verificar que el total suma posiciones de todos los golpes.

- [ ] **Step 2: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/graph/test_calc.py -q
```

- [ ] **Step 3: implementar protocolo y traducción**

`DamageCalculator` es un `Protocol`. `CalcClient` usa `httpx.AsyncClient`,
comprueba `/health`, envía `gen` del estado y nunca consulta poke-env.
`calc_damage` trata errores por matchup como datos diagnósticos, no como cero.

- [ ] **Step 4: GREEN unitario**

```bash
cd apps/agent
uv run pytest tests/graph/test_calc.py -q
```

- [ ] **Step 5: integración real parametrizada**

Contra `http://127.0.0.1:8200`, verificar una llamada con `gen=6` pasada por
fixture y comparar el resultado con la respuesta directa de `/calc`. El test
debe fallar con mensaje claro si health no responde.

```bash
cd apps/agent
uv run pytest tests/graph/test_calc_integration.py -q
```

- [ ] **Step 6: rotura deliberada**

Cambiar temporalmente el ranking para ordenar solo por promedio, confirmar que
falla el canario de KO garantizado, restaurar y ejecutar GREEN.

- [ ] **Step 7: commit**

```bash
git add -- apps/agent/src/ludex_agent/graph/calc.py \
  apps/agent/tests/graph/test_calc.py \
  apps/agent/tests/graph/test_calc_integration.py
git commit -m "feat(agent): calculate and rank battle damage" -- \
  apps/agent/src/ludex_agent/graph/calc.py \
  apps/agent/tests/graph/test_calc.py \
  apps/agent/tests/graph/test_calc_integration.py
```

---

### Task 5: Proveedor Gemini, pool de claves y métricas

**Files:**
- Create: `apps/agent/src/ludex_agent/graph/provider.py`
- Create: `apps/agent/tests/graph/test_provider.py`

**Interfaces:**
- Produces:
  - `DecisionProvider.complete(prompt: str, *, deadline: float) -> Awaitable[dict]`.
  - `FakeDecisionProvider(responses: list[dict | Exception])`.
  - `GeminiDecisionProvider`.
  - `QuotaExceeded`, `TransientProviderError`, `FatalProviderError`,
    `ProviderPoolExhausted`, `DecisionDeadlineExceeded`.
  - `DecisionMetrics.snapshot() -> dict[str, int]`.

- [ ] **Step 1: tests RED de clasificación**

Probar estas secuencias:

```python
[QuotaExceeded(), valid]        # rota, mismo prompt
[TransientProviderError(), valid]  # reintenta, misma clave/prompt
[QuotaExceeded(), QuotaExceeded()] # pool agotado, excepción fatal
```

Verificar que `GOOGLE_API_KEY` precede a `GOOGLE_API_KEYS`, los vacíos se
descartan, duplicados se deduplican y ninguna representación/log contiene las
claves.

- [ ] **Step 2: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/graph/test_provider.py -q
```

- [ ] **Step 3: implementar adapter**

Usar `ChatGoogleGenerativeAI(...).with_structured_output(...,
method="json_schema")`. Mapear 429, 5xx y timeouts a excepciones propias; 401/403
a fatal. El bucle de infraestructura recibe un deadline monotónico y nunca
avanza el intento semántico.

- [ ] **Step 4: métricas por turno**

Implementar sets internos de ids de turno para no contar dos veces
`turns_quota_affected` ni `turns_transient_affected`; incrementar
`key_rotations` por evento.

- [ ] **Step 5: GREEN y rotura deliberada**

```bash
cd apps/agent
uv run pytest tests/graph/test_provider.py -q
```

Cambiar temporalmente 429 para devolver un resultado fallback, confirmar que
el test de pool agotado falla, restaurar y ejecutar GREEN.

- [ ] **Step 6: commit**

```bash
git add -- apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/tests/graph/test_provider.py
git commit -m "feat(agent): classify provider failures and rotate keys" -- \
  apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/tests/graph/test_provider.py
```

---

### Task 6: Nodo `decide`, reintento semántico y fallback

**Files:**
- Create: `apps/agent/src/ludex_agent/graph/decision.py`
- Create: `apps/agent/tests/graph/test_decision.py`

**Interfaces:**
- Produces:
  - `DecisionResponse` Pydantic.
  - `normalize_action(action: dict) -> dict`.
  - `validate_action(action: dict, legal: list[dict]) -> dict`.
  - `async decide(state: GraphState, provider: DecisionProvider, metrics: DecisionMetrics) -> dict`.

- [ ] **Step 1: tests RED de normalización**

```python
assert normalize_action({"kind": "move", "id": "x", "mega": False}) == {
    "kind": "move", "id": "x"
}
```

Claves desconocidas, ids distintos y `mega=True` no se normalizan.

- [ ] **Step 2: test RED obligatorio del fallback**

El fake devuelve dos acciones ilegales. Verificar:

```python
assert result["action"] == expected_damage_fallback
assert result["action_path"] == "fallback"
assert fake.prompts[1].contains("acción ilegal")
assert metrics.snapshot()["turns_model_invalid"] == 1
assert metrics.snapshot()["turns_fallback"] == 1
```

- [ ] **Step 3: tests RED de infraestructura**

Una secuencia `429 → válida` y `timeout → válida` debe terminar
`action_path="llm"`, con un único prompt semántico. Pool agotado debe propagar
excepción y nunca incrementar fallback.

- [ ] **Step 4: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/graph/test_decision.py -q
```

- [ ] **Step 5: implementar prompts y máquina semántica**

Construir payload mediante campos nombrados de `GraphState`. Máximo dos
respuestas del modelo; los reintentos de infraestructura viven dentro de cada
pedido. La segunda respuesta inválida usa ranking de calc.

- [ ] **Step 6: GREEN y roturas deliberadas**

```bash
cd apps/agent
uv run pytest tests/graph/test_decision.py -q
```

Romper por separado: aceptar acción sin máscara, contar 429 como fallback y
tratar `mega=False` como distinto. Cada mutación debe producir RED; restaurar.

- [ ] **Step 7: commit**

```bash
git add -- apps/agent/src/ludex_agent/graph/decision.py \
  apps/agent/tests/graph/test_decision.py
git commit -m "feat(agent): validate and recover model decisions" -- \
  apps/agent/src/ludex_agent/graph/decision.py \
  apps/agent/tests/graph/test_decision.py
```

---

### Task 7: Compilar el `StateGraph`

**Files:**
- Create: `apps/agent/src/ludex_agent/graph/workflow.py`
- Create: `apps/agent/tests/graph/test_workflow.py`

**Interfaces:**
- Produces: `build_decision_graph(calculator, provider, metrics)` y
  `await graph.ainvoke({"raw_state": snapshot, "turn_id": id})`.

- [ ] **Step 1: test RED del orden real**

Fakes registran eventos y verifican:

```python
assert events == ["parse_state", "calc_damage", "decide"]
```

El resultado debe incluir `action` y `action_path`.

- [ ] **Step 2: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/graph/test_workflow.py -q
```

- [ ] **Step 3: implementar `StateGraph`**

Usar `StateGraph(GraphState)`, `START`, tres nodos async y `END`; compilar una
vez al construir, no por turno.

- [ ] **Step 4: GREEN y prueba anti-decoración**

```bash
cd apps/agent
uv run pytest tests/graph/test_workflow.py -q
```

Eliminar temporalmente la arista a `calc_damage` y confirmar RED.

- [ ] **Step 5: commit**

```bash
git add -- apps/agent/src/ludex_agent/graph/workflow.py \
  apps/agent/tests/graph/test_workflow.py
git commit -m "feat(agent): orchestrate decisions with LangGraph" -- \
  apps/agent/src/ludex_agent/graph/workflow.py \
  apps/agent/tests/graph/test_workflow.py
```

---

### Task 8: Benchmark reusable y baseline versionado

**Files:**
- Create: `apps/agent/src/ludex_agent/benchmark.py`
- Create: `apps/agent/tests/test_benchmark.py`
- Create: `apps/agent/evals/random-baseline.json`
- Modify: `apps/agent/src/ludex_agent/cli.py`

**Interfaces:**
- Produces: `wilson_interval(wins, n, confidence=0.95)`, comando
  `benchmark --opponent ... --n ... --concurrency ... [--persist] [--json ...]`.

- [ ] **Step 1: tests RED de Wilson y persistencia opt-in**

Fijar los intervalos medidos:

```python
assert rounded(wilson_interval(143, 300)) == (0.4208, 0.5331)
```

Verificar que sin `--persist` no se construye `BattleRepository`; con
`--persist`, usa el mismo helper de persistencia del runner.

- [ ] **Step 2: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/test_benchmark.py -q
```

- [ ] **Step 3: implementar comando**

Mapear nombres exactos a `RandomPlayer`, `MaxBasePowerPlayer` y
`SimpleHeuristicsPlayer`. Generación/formato vienen de argumentos/config.
Imprimir métricas de decisión además del resultado deportivo.

- [ ] **Step 4: guardar baseline inspeccionado**

`random-baseline.json` contiene los 900 resultados ya medidos, fecha, formato,
concurrencia 20 y commit pre-grafo. No recalcularlos ni redondearlos a partir
del texto.

- [ ] **Step 5: GREEN y smoke real**

```bash
cd apps/agent
uv run pytest tests/test_benchmark.py -q
uv run python -m ludex_agent.cli benchmark --opponent random --n 5 \
  --concurrency 5
```

Confirmar que no se agregaron filas a `battles` en el smoke sin `--persist`.

- [ ] **Step 6: commit**

```bash
git add -- apps/agent/src/ludex_agent/benchmark.py \
  apps/agent/src/ludex_agent/cli.py apps/agent/tests/test_benchmark.py \
  apps/agent/evals/random-baseline.json
git commit -m "feat(agent): add reusable win-rate benchmark" -- \
  apps/agent/src/ludex_agent/benchmark.py \
  apps/agent/src/ludex_agent/cli.py apps/agent/tests/test_benchmark.py \
  apps/agent/evals/random-baseline.json
```

---

### Task 9: Cableado seguro en `LudexPlayer`

**Gate:** ejecutar solo después de confirmación del usuario y con
`git status` mostrando que los cambios del otro agente ya están commiteados.

**Files:**
- Modify: `apps/agent/src/ludex_agent/showdown/client.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Modify: `apps/agent/tests/showdown/test_client.py`
- Modify: `apps/agent/tests/integration/test_play.py`

**Interfaces:**
- `LudexPlayer(..., decision_graph=None)`.
- Cada step agrega `action_path`.

- [ ] **Step 1: re-leer skill y diff vigente**

```bash
sed -n '1,360p' .claude/agent-recording/SKILL.md
git status --short
git log -3 --oneline -- apps/agent/src/ludex_agent/showdown/client.py
```

- [ ] **Step 2: tests RED de captura antes del await**

Crear un grafo fake cuyo await muta el `battle`. Verificar que recibe el
snapshot y mapa anteriores y que `action_taken in legal_actions` sigue cierto.
Forzar movimiento mega y cambio.

- [ ] **Step 3: confirmar RED**

```bash
cd apps/agent
uv run pytest tests/showdown/test_client.py -q
```

- [ ] **Step 4: implementar mínimo cableado**

En `choose_move`, antes de devolver coroutine:

```python
snapshot = serialize_battle(battle)
action_orders = build_action_order_map(battle)
```

Reservar el step con esos datos. La coroutine llama `graph.ainvoke`, busca la
acción normalizada en `action_orders` y completa `action_taken/action_path`.
No releer `battle` dentro de la coroutine.

- [ ] **Step 5: persistir `action_path`**

`_persist_one` pasa `step.get("action_path")`; el modo random legacy conserva
`None`. `action_source` permanece `"agent"`.

- [ ] **Step 6: GREEN y roturas deliberadas**

```bash
cd apps/agent
uv run pytest tests/showdown/test_client.py tests/integration/test_play.py -q
```

Mover temporalmente `serialize_battle` después del primer await y confirmar que
falla el canario D22. Romper temporalmente uno de los rastros del corrector de
turnos y confirmar que la suite existente lo detecta. Restaurar ambos.

- [ ] **Step 7: batallas completas con fake**

Jugar al menos 10 batallas con un fake legal y una batalla con fake
`ilegal → ilegal` para ejercer fallback. Persistir con `source="test"`.

- [ ] **Step 8: commit**

```bash
git add -- apps/agent/src/ludex_agent/showdown/client.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/showdown/test_client.py \
  apps/agent/tests/integration/test_play.py
git commit -m "feat(agent): execute graph decisions safely" -- \
  apps/agent/src/ludex_agent/showdown/client.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/showdown/test_client.py \
  apps/agent/tests/integration/test_play.py
```

---

### Task 10: Decisiones, auditoría y reporte

**Files:**
- Modify: `docs/DECISIONS.md`
- Create: `.superpowers/sdd/gpt-grafo.md`

**Interfaces:**
- Documents: action_path, `text + CHECK`, D22 wiring, retry taxonomy, timer
  measurement, baseline and known rejected-order limitation.

- [ ] **Step 1: esperar que `docs/DECISIONS.md` quede libre**

Confirmar que el trabajo del otro agente está commiteado; releer las nuevas
decisiones antes de anexar una entrada, sin sobrescribirlas.

- [ ] **Step 2: auditoría SQL sobre todo el dataset**

Ejecutar queries sin filtro por tags:

```sql
SELECT count(*) FROM trajectory_steps ts
WHERE NOT EXISTS (
  SELECT 1 FROM jsonb_array_elements(ts.legal_actions) la
  WHERE la = ts.action_taken
);
```

Ejecutar además el auditor existente de etiquetado contra todo el dataset y
agrupar `action_source`, `action_path`.

- [ ] **Step 3: verificar métricas fake y declarar pendiente real**

Reportar número de intentos inválidos, recuperados y fallback de las batallas
fake. Escribir “métrica LLM real: pendiente por falta de API key”, nunca 0%.
Incluir rotaciones/infraestructura del fake como control de flujo.

- [ ] **Step 4: verificación completa**

```bash
cd apps/agent
uv lock --check
uv run pytest -q
grep -ri "gen6" src/ || true
cd ../..
git diff --check
```

Confirmar el total real de tests en la salida; no fijar “95” si la rama del
otro agente agregó tests.

- [ ] **Step 5: escribir documentación y reporte**

Incluir hashes, baseline 300×3, timer medido 300 s, presupuesto 240 s, pruebas
RED/GREEN/roturas, migración, batallas completas, auditoría global, métricas y
concerns.

- [ ] **Step 6: commit final explícito**

```bash
git add -- docs/DECISIONS.md
git add -f -- .superpowers/sdd/gpt-grafo.md
git commit -m "docs(agent): record decision graph guarantees" -- \
  docs/DECISIONS.md .superpowers/sdd/gpt-grafo.md
```

