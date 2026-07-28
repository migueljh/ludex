# Agent Benchmark Cost Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capturar usage y costo real por corrida, soportar los protocolos de los modelos candidatos y ejecutar benchmarks fijos sin mezclar proveedores.

**Architecture:** El backend conserva la respuesta cruda del SDK para extraer usage, mientras `KeyRotatingProvider` registra ese usage y sigue entregando a `decide` el mismo `dict` estructurado. Rutas de modelo y precios viven en JSON versionado; un módulo puro calcula costos y construye artefactos, y la CLI agrega el JSON de la corrida y una fila a `docs/BENCHMARKS.md`.

**Tech Stack:** Python 3.12, LangChain, Typer, Pydantic, JSON, pytest, poke-env, Showdown local.

## Global Constraints

- Una corrida de benchmark usa exactamente un proveedor y un modelo; nunca activa cadena entre proveedores.
- Usage viene de la respuesta de la API; no se estima ni tokeniza localmente.
- Los precios no se hardcodean en Python y cada entrada incluye fecha y fuente.
- Costo desconocido se representa como `null`, nunca como cero.
- Las batallas no se persisten salvo `--persist`; el ledger de evaluación sí se escribe por defecto.
- No tocar `showdown/client.py`, el estado serializado ni las tablas de dataset.
- Leer y respetar `.claude/agent-recording/SKILL.md` y `.claude/verification/SKILL.md`.
- Cada test nuevo se valida con rotura deliberada antes de commitear.
- Commits en inglés, rutas explícitas, `-m` antes de `--`, nunca `git add -A`.
- Ignorar `.worktrees/`.

---

### Task 1: Usage real en el borde del proveedor

**Files:**
- Modify: `apps/agent/src/ludex_agent/graph/provider.py`
- Modify: `apps/agent/tests/graph/test_provider.py`

**Interfaces:**
- Produces: `CompletionUsage`, `ProviderCompletion`, `DecisionMetrics.usage()`.
- Preserves: `DecisionProvider.complete(...) -> dict[str, Any]`.

- [ ] **Step 1: escribir el test rojo para una respuesta exitosa**

Agregar un backend que devuelve:

```python
ProviderCompletion(
    payload={"action": {"kind": "move", "id": "tackle"}, "reasoning": "ok"},
    usage=CompletionUsage(
        input_tokens=120,
        output_tokens=30,
        cached_input_tokens=20,
        reasoning_tokens=10,
        model="minimax-m2.7",
    ),
)
```

Ejecutar dos completions y afirmar literales:

```python
assert metrics.snapshot() | {
    "calls_total": 2,
    "input_tokens": 240,
    "output_tokens": 60,
    "cached_input_tokens": 40,
    "reasoning_tokens": 20,
}
```

- [ ] **Step 2: verificar RED**

Run:

```bash
cd apps/agent
uv run pytest tests/graph/test_provider.py::test_usage_se_suma_desde_respuesta_real -q
```

Expected: FAIL porque `ProviderCompletion`/contadores no existen.

- [ ] **Step 3: implementar el sobre interno y mantener el contrato público**

Agregar:

```python
@dataclass(frozen=True)
class CompletionUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    model: str | None = None


@dataclass(frozen=True)
class ProviderCompletion:
    payload: dict[str, Any]
    usage: CompletionUsage
```

`ProviderBackend.complete` devuelve `ProviderCompletion`. Tras una respuesta
exitosa, `KeyRotatingProvider.complete` llama `metrics.usage(completion.usage)`
y devuelve `completion.payload`.

- [ ] **Step 4: probar que errores de infraestructura no inventan usage**

Test parametrizado con `QuotaExceeded`, `TransientProviderError` y timeout.
Después de propagar/reintentar, afirmar que solo las respuestas exitosas
incrementan `calls_total` y tokens.

- [ ] **Step 5: extraer usage desde `include_raw=True`**

Cambiar:

```python
structured = model.with_structured_output(
    self.response_schema,
    method="json_schema",
    include_raw=True,
)
```

Extraer del `AIMessage` crudo:

- `usage_metadata.input_tokens`;
- `usage_metadata.output_tokens`;
- `usage_metadata.input_token_details.cache_read`;
- `usage_metadata.output_token_details.reasoning`;
- `response_metadata.model_name` o equivalente.

La ausencia de un detalle vale cero; la ausencia total de usage en una
respuesta exitosa es `FatalProviderError`, porque un benchmark de costo sin
usage no es válido.

- [ ] **Step 6: verificar GREEN y romper deliberadamente**

Run:

```bash
uv run pytest tests/graph/test_provider.py -q
```

Mutación: omitir `metrics.usage(...)`; el nuevo test debe fallar por
`calls_total == 0`. Restaurar y repetir GREEN.

- [ ] **Step 7: commit**

```bash
git add apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/tests/graph/test_provider.py
git commit -m "feat(agent): capture provider token usage" -- \
  apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/tests/graph/test_provider.py
```

---

### Task 2: Rutas explícitas y parámetros por modelo

**Files:**
- Create: `apps/agent/evals/model-routes.json`
- Modify: `apps/agent/src/ludex_agent/graph/provider.py`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Modify: `apps/agent/tests/graph/test_provider.py`
- Modify: `apps/agent/tests/test_cli.py`

**Interfaces:**
- Produces: `ModelRoute`, `load_model_routes(path)`, backend Anthropic con `base_url`.
- Consumes: `CompletionUsage` de Task 1.

- [ ] **Step 1: crear tests rojos del registro**

Casos literales:

```python
assert route("open_code_zen", "minimax-m2.7").protocol == "chat_completions"
assert route("open_code_zen", "deepseek-v4-pro").protocol == "chat_completions"
assert route("open_code_zen", "qwen3.5-plus").protocol == "messages"
assert route("open_code_zen", "qwen3.6-plus").protocol == "messages"
assert route("kimi", "kimi-k2.6").thinking == "enabled"
```

Un ID inexistente debe fallar antes de construir jugadores.

- [ ] **Step 2: verificar RED**

```bash
uv run pytest tests/graph/test_provider.py tests/test_cli.py -q
```

- [ ] **Step 3: agregar configuración explícita**

Contenido mínimo de `model-routes.json`:

```json
{
  "version": "2026-07-28",
  "routes": [
    {"provider":"open_code_zen","model":"mimo-v2.5-free","protocol":"chat_completions"},
    {"provider":"open_code_zen","model":"minimax-m2.7","protocol":"chat_completions"},
    {"provider":"open_code_zen","model":"deepseek-v4-pro","protocol":"chat_completions"},
    {"provider":"open_code_zen","model":"qwen3.5-plus","protocol":"messages"},
    {"provider":"open_code_zen","model":"qwen3.6-plus","protocol":"messages"},
    {"provider":"kimi","model":"kimi-k2.6","protocol":"chat_completions",
     "temperature":1.0,"thinking":"enabled","max_tokens":16000}
  ]
}
```

- [ ] **Step 4: implementar los tres caminos necesarios**

- `chat_completions`: `ChatOpenAI`, base URL de Kimi o Zen.
- `messages`: `ChatAnthropic`, con `base_url=OPEN_CODE_ZEN_BASE_URL`.
- `responses`: reconocer el valor y fallar con mensaje explícito hasta la
  extensión posterior; no enrutar silenciosamente por chat completions.

Para Kimi pasar `temperature=1.0` y
`extra_body={"thinking":{"type":"enabled"}}`. No conservar el cero genérico.

- [ ] **Step 5: verificar selección y secretos**

Los tests construyen proveedores con claves ficticias y revisan tipo/ruta sin
hacer red. `repr` y errores no incluyen claves.

- [ ] **Step 6: romper deliberadamente**

Cambiar temporalmente Qwen a `chat_completions`; el test debe fallar. Restaurar.

- [ ] **Step 7: GREEN y commit**

```bash
uv run pytest tests/graph/test_provider.py tests/test_cli.py -q
git add apps/agent/evals/model-routes.json \
  apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/graph/test_provider.py apps/agent/tests/test_cli.py
git commit -m "feat(agent): route benchmark models explicitly" -- \
  apps/agent/evals/model-routes.json \
  apps/agent/src/ludex_agent/graph/provider.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/graph/test_provider.py apps/agent/tests/test_cli.py
```

---

### Task 3: Tabla de precios y cálculo puro

**Files:**
- Create: `apps/agent/evals/pricing-2026-07-28.json`
- Create: `apps/agent/src/ludex_agent/eval_cost.py`
- Create: `apps/agent/tests/test_eval_cost.py`

**Interfaces:**
- Produces: `PricingTable.load(path)`, `calculate_cost(usage, price)`.
- Cost uses `Decimal`; returns `None` when a required price is absent.

- [ ] **Step 1: escribir tests rojos con cálculo manual**

Fixture:

```python
usage = {
    "input_tokens": 1_000_000,
    "cached_input_tokens": 250_000,
    "output_tokens": 100_000,
}
price = {"input": "0.30", "cached_input": "0.06", "output": "1.20"}
assert calculate_cost(usage, price) == Decimal("0.360")
```

La entrada no cacheada es `input_tokens - cached_input_tokens`. Agregar caso
con `cached_input=None` que retorna `None`, no `0`.

- [ ] **Step 2: verificar RED**

```bash
uv run pytest tests/test_eval_cost.py -q
```

- [ ] **Step 3: crear tabla fechada**

Incluir las tarifas oficiales del 2026-07-28 para:

- Kimi directo K2.6: 0.95 input, 4.00 output, 0.16 cache;
- OpenCode MiniMax M2.7: 0.30, 1.20, 0.06;
- OpenCode DeepSeek V4 Pro: 1.74, 3.48, 0.145;
- OpenCode Qwen3.5 Plus: 0.20, 1.20, 0.02;
- OpenCode Qwen3.6 Plus: 0.50, 3.00, 0.05;
- MiMo V2.5 Free: 0, 0, 0.

Cada entrada incluye `source_url`, `checked_at` y
`pricing_table_id="2026-07-28-official"`.

- [ ] **Step 4: implementar carga/validación**

Rechazar:

- modelo duplicado;
- precio negativo;
- fuente o fecha vacía;
- provider/model no coincidente.

- [ ] **Step 5: romper deliberadamente**

Mutar el cálculo para cobrar cache como input normal; el literal `0.360` debe
fallar. Restaurar.

- [ ] **Step 6: GREEN y commit**

```bash
uv run pytest tests/test_eval_cost.py -q
git add apps/agent/evals/pricing-2026-07-28.json \
  apps/agent/src/ludex_agent/eval_cost.py \
  apps/agent/tests/test_eval_cost.py
git commit -m "feat(agent): calculate benchmark cost from pricing data" -- \
  apps/agent/evals/pricing-2026-07-28.json \
  apps/agent/src/ludex_agent/eval_cost.py \
  apps/agent/tests/test_eval_cost.py
```

---

### Task 4: Artefacto de corrida y ledger acumulativo

**Files:**
- Create: `apps/agent/src/ludex_agent/eval_report.py`
- Create: `apps/agent/tests/test_eval_report.py`
- Create: `docs/BENCHMARKS.md`
- Modify: `apps/agent/src/ludex_agent/cli.py`
- Modify: `apps/agent/tests/test_cli.py`

**Interfaces:**
- Produces: `BenchmarkRecord`, `write_run_json`, `append_ledger_row`.
- Consumes: `BenchmarkResult`, snapshot de métricas y `calculate_cost`.

- [ ] **Step 1: test rojo de registro completo**

Con `completed=15`, `wins=5`, `turns_total=300`, `turns_model_invalid=3`,
`turns_fallback=1`:

```python
assert record.calls_per_battle == Decimal("20")
assert record.invalid_recovered_pct == Decimal("0.6666666667")
assert record.fallback_pct == Decimal("0.0033333333")
assert record.projected_10k_cost == record.cost_per_battle * 10_000
```

Wilson sale de `BenchmarkResult.interval`, no de otra fórmula.

- [ ] **Step 2: test rojo de corrida abortada**

Una corrida `completed < requested` o con `failure` escribe JSON/ledger con
`status=aborted`, pero deja `win_rate`, `cost_per_battle` y proyección como
`null`.

- [ ] **Step 3: verificar RED**

```bash
uv run pytest tests/test_eval_report.py tests/test_cli.py -q
```

- [ ] **Step 4: implementar escritura atómica**

Artefactos:

```text
apps/agent/evals/runs/YYYYMMDDTHHMMSSZ-provider-model.json
docs/BENCHMARKS.md
```

Escribir JSON a archivo temporal dentro del mismo directorio y renombrar. El
ledger tiene encabezado fijo y una fila Markdown por corrida con link relativo
al JSON.

- [ ] **Step 5: integrar CLI**

Agregar opciones:

```text
--pricing apps/agent/evals/pricing-2026-07-28.json
--ledger docs/BENCHMARKS.md
--run-id ID_EXPLICITO
--record/--no-record  (default: record)
```

`--run-id` acepta solo `[a-z0-9-]+`, no puede sobrescribir un artefacto y
determina `apps/agent/evals/runs/<run-id>.json`.

La CLI imprime siempre usage y costo. `--no-record` existe para tests/manual,
no para las corridas oficiales.

- [ ] **Step 6: romper deliberadamente**

Mutar el estado abortado para publicar winrate; el test debe fallar. Restaurar.

- [ ] **Step 7: GREEN y commit**

```bash
uv run pytest tests/test_eval_report.py tests/test_benchmark.py tests/test_cli.py -q
git add apps/agent/src/ludex_agent/eval_report.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/test_eval_report.py apps/agent/tests/test_cli.py \
  docs/BENCHMARKS.md
git commit -m "feat(agent): record benchmark usage and cost" -- \
  apps/agent/src/ludex_agent/eval_report.py \
  apps/agent/src/ludex_agent/cli.py \
  apps/agent/tests/test_eval_report.py apps/agent/tests/test_cli.py \
  docs/BENCHMARKS.md
```

---

### Task 5: Verificación de integración antes de gastar

**Files:**
- Modify only if a verified incompatibility requires it:
  `apps/agent/src/ludex_agent/graph/provider.py`
- Test: `apps/agent/tests/graph/test_provider.py`
- Update: `docs/DECISIONS.md`

**Interfaces:**
- Verifies real structured output and usage for each route.

- [ ] **Step 1: ejecutar una completion mínima por modelo**

Con `.env` cargado y sin iniciar Showdown:

```bash
uv run python -m ludex_agent.cli provider-smoke \
  --provider open_code_zen --model mimo-v2.5-free
uv run python -m ludex_agent.cli provider-smoke \
  --provider open_code_zen --model minimax-m2.7
uv run python -m ludex_agent.cli provider-smoke \
  --provider kimi --model kimi-k2.6
uv run python -m ludex_agent.cli provider-smoke \
  --provider open_code_zen --model deepseek-v4-pro
uv run python -m ludex_agent.cli provider-smoke \
  --provider open_code_zen --model qwen3.5-plus
```

Cada salida debe tener payload válido, input/output usage mayor que cero y no
mostrar la clave.

- [ ] **Step 2: detenerse ante discrepancias**

Si un SDK no conserva usage o un endpoint rechaza structured output, aplicar
`systematic-debugging`: capturar tipo/status/respuesta sanitizada, escribir un
test rojo que reproduzca el borde y corregir solo esa ruta. No iniciar
benchmarks hasta que las cinco rutas dejen evidencia.

- [ ] **Step 3: suite rápida y completa**

```bash
uv run pytest \
  tests/graph/test_provider.py tests/graph/test_decision.py \
  tests/test_eval_cost.py tests/test_eval_report.py \
  tests/test_benchmark.py tests/test_cli.py -q
uv run pytest -q
```

- [ ] **Step 4: registrar decisión**

Agregar D28 en `docs/DECISIONS.md` con:

- usage crudo como fuente de costo;
- tabla externa fechada;
- protocolos explícitos;
- Kimi thinking como configuración medida;
- prohibición de mezclar modelos en benchmark.

- [ ] **Step 5: commit del checkpoint**

```bash
git add docs/DECISIONS.md
git commit -m "docs(agent): record benchmark accounting decision" -- \
  docs/DECISIONS.md
```

Parar para revisión humana antes de ejecutar batallas reales.

---

### Task 6: Escalera real de benchmarks

**Files:**
- Create per run: `apps/agent/evals/runs/*.json`
- Modify per run: `docs/BENCHMARKS.md`
- Update: `.superpowers/sdd/gpt-grafo.md`

**Interfaces:**
- Consumes the verified CLI from Tasks 1–5.

- [ ] **Step 1: humo gratuito**

```bash
set -a; source .env; set +a
cd apps/agent
uv run python -m ludex_agent.cli benchmark \
  --provider open_code_zen --model mimo-v2.5-free \
  --opponent simple_heuristics --n 2 --concurrency 1 \
  --run-id 20260728-mimo-smoke
```

Condición para seguir: `completed=2`, cero deadlines y la mayoría de acciones
no cae en fallback. Si aborta, registrar y no promover MiMo.

- [ ] **Step 2: MiniMax fijo**

```bash
uv run python -m ludex_agent.cli benchmark \
  --provider open_code_zen --model minimax-m2.7 \
  --opponent simple_heuristics --n 15 --concurrency 1 \
  --run-id 20260728-minimax-m27-15
```

No cambiar proveedor/modelo durante el proceso. Si aborta, no reanudar bajo
otro modelo en la misma fila.

- [ ] **Step 3: Kimi fijo**

```bash
uv run python -m ludex_agent.cli benchmark \
  --provider kimi --model kimi-k2.6 \
  --opponent simple_heuristics --n 15 --concurrency 1 \
  --run-id 20260728-kimi-k26-15
```

La fila debe identificar `thinking=enabled`.

- [ ] **Step 4: decidir DeepSeek**

Si MiniMax obtiene 0–1 victoria o fallback/ilegalidad alta, ejecutar:

```bash
uv run python -m ludex_agent.cli benchmark \
  --provider open_code_zen --model deepseek-v4-pro \
  --opponent simple_heuristics --n 15 --concurrency 1 \
  --run-id 20260728-deepseek-v4-pro-15
```

Si MiniMax obtiene 5 o más con corrida válida, DeepSeek queda preparado pero
no se gasta crédito todavía.

- [ ] **Step 5: verificar artefactos**

Por cada corrida, comparar manualmente:

- stdout;
- JSON;
- fila Markdown;
- suma de tokens;
- costo total y por batalla;
- proyección 10.000;
- modelo/proveedor fijo.

- [ ] **Step 6: reporte y commit**

Actualizar `.superpowers/sdd/gpt-grafo.md` con tabla comparativa y lectura de
calidad versus costo.

```bash
git add apps/agent/evals/runs/20260728-mimo-smoke.json \
  apps/agent/evals/runs/20260728-minimax-m27-15.json \
  apps/agent/evals/runs/20260728-kimi-k26-15.json \
  docs/BENCHMARKS.md .superpowers/sdd/gpt-grafo.md
git commit -m "docs(agent): record model benchmark results" -- \
  apps/agent/evals/runs/20260728-mimo-smoke.json \
  apps/agent/evals/runs/20260728-minimax-m27-15.json \
  apps/agent/evals/runs/20260728-kimi-k26-15.json \
  docs/BENCHMARKS.md .superpowers/sdd/gpt-grafo.md
```

Si DeepSeek se ejecutó, agregar
`apps/agent/evals/runs/20260728-deepseek-v4-pro-15.json` explícitamente al
mismo commit.

---

## Self-review

- La captura de usage cubre éxito, respuesta inválida e infraestructura.
- El cálculo separa entrada normal, caché, razonamiento y salida.
- Los modelos Qwen y DeepSeek quedan habilitados por su protocolo real.
- Kimi no hereda temperatura incompatible.
- El ledger distingue corrida completa de abortada.
- Cada corrida real conserva proveedor/modelo fijo.
- No hay cambios en recorder, estado, `client.py` ni Postgres.
- No hay placeholders de implementación; los nombres y rutas son consistentes
  entre tareas.
