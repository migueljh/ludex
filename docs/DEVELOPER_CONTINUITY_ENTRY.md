# Developer continuity entry — MON-15

Fecha: 2026-08-08
Issue: [MON-15](https://linear.app/montsuki/issue/MON-15/f2-10-prove-sustained-provider-reliability-and-decision-latency)
Rama: `andromeda/mon-15-provider-reliability`
Worktree: `/Users/miguelhernandez/Documents/ludex-mon-15`
Base: `7b4df003f6cf0259f08be880cebcaf3f40bd64ab`
Commits:
- `61866af` — feat(agent): injectable clock, latency metrics, and controlled not-run handling (MON-15)
- `123aa20` — data(evals): add MON-15 credential-unavailable not-run artifacts

## Qué se hizo

Se cerró el trabajo de confiabilidad sostenida de proveedores y latencia de
decisión para F2-10:

- Se eliminaron las fugas de `time.monotonic()` en `KeyRotatingProvider`
  (líneas que ahora usan `self._clock()` para deadline y cooldown).
- Se hizo inyectable el reloj en `decide()`, `FakeDecisionProvider` y
  `LudexPlayer`.
- Se agregaron métricas de latencia `p50/p95/max` a `DecisionMetrics` y al
  `BenchmarkRecord`.
- Se actualizó `eval_report.py` y el ledger (`docs/BENCHMARKS.md`) para
  mostrar latencia.
- Se agregó manejo controlado de credenciales faltantes en la CLI: genera
  artefactos `status: not-run`, `reason: credential unavailable`, exit code 2,
  sin exponer secretos ni publicar winrates comparables.
- Se registró la decisión D41 en `docs/DECISIONS.md` y se actualizó
  `docs/HANDOFF.md`.

## Verificación

Comando usado:

```bash
cd /Users/miguelhernandez/Documents/ludex-mon-15/apps/agent
DATABASE_URL='postgresql+asyncpg://ludex:ludex@localhost:15432/ludex?sslmode=disable' \
SHOWDOWN_WS_URL='ws://localhost:8100/showdown/websocket' \
uv run pytest tests/graph/test_provider.py tests/graph/test_decision.py \
  tests/graph/test_workflow.py tests/showdown/ tests/test_cli.py \
  tests/test_benchmark.py tests/test_eval_report.py -q
```

Resultado: **355 passed in 2.15s**.

Se ejecutaron pruebas de regresión revertiendo el arreglo de reloj y
confirmando que los tests fallan, luego restaurándolo y confirmando verde.

## Artefactos generados

Tres controles quedaron como `not-run` por credenciales no configuradas:

- `apps/agent/evals/runs/20260808-kimi-k26-battle-1.json`
- `apps/agent/evals/runs/20260808-gemini-25flash-d30.json`
- `apps/agent/evals/runs/20260808-opencode-mimo-screen.json`

El screen de OpenCode usó `mimo-v2.5-free`, modelo disponible en
`apps/agent/evals/model-routes.json`. El modelo `claude-haiku-4-5` que aparece
en `.env.example` no está en ese catálogo.

## Estado operativo

- Linear: MON-15 está en **In Review** con el REVIEW PACKET publicado.
- No hay bloqueos activos.
- No se modificó la base de datos compartida ni se corrieron batallas reales.

## Próximos pasos sugeridos

1. Tech lead revisa el REVIEW PACKET en Linear y emite `LINEAR_VERDICT`.
2. Cuando haya credenciales disponibles, reemplazar los artefactos `not-run`
   por corridas reales de Kimi, Gemini y OpenCode para validar D30 y medir
   latencia real.
3. Verificar que el ledger histórico sin columna de latencia siga siendo
   legible; si se agregan muchas filas nuevas, considerar migrar las filas
   antiguas a `N/A` explícito.

## Notas para el siguiente desarrollador

- Nunca mezclar `time.monotonic()` con `self._clock()` en una misma rutina;
  D41 lo prohíbe explícitamente.
- Si agregás una nueva métrica de latencia, actualizá tanto
  `DecisionMetrics` como `BenchmarkRecord` y el ledger.
- Los artefactos `not-run` son evidencia válida de límite externo; no los
  borres sin registrar por qué.
