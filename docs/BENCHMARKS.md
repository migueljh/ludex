# Benchmarks de modelos

Registro acumulativo. El costo se calcula con usage real; una celda vacía
significa desconocido o no comparable, nunca cero implícito.

| Fecha | Run | Proveedor/modelo | Batallas | W-L-T | Winrate | Wilson 95% | Llamadas/batalla | Tokens in/out | Costo total | Costo/batalla | 10.000 batallas | Ilegales retry/fallback | Transitorios | Deadlines | Rotaciones | Completion p50/p95/max (ms) | Decision p50/p95/max (ms) | Precios |
|---|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|---:|---|
| 2026-07-28 | [20260728-mimo-smoke](../apps/agent/evals/runs/20260728-mimo-smoke.json) | open_code_zen/mimo-v2.5-free | 2/2 | 0-2-0 | 0.0000% | 0.0000%–65.7620% | 32.5000 | 156087/7177 | 0.0000 | 0.0000 | 0.0000 | 20.3704%/0.0000% | 0 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-07-28 | [20260728-deepseek-v4-flash-control-2](../apps/agent/evals/runs/20260728-deepseek-v4-flash-control-2.json) | open_code_zen/deepseek-v4-flash | 1/1 | 1-0-0 | 100.0000% | 20.6549%–100.0000% | 25.0000 | 64305/45962 | 0.0215 | 0.0215 | 215.4681 | 0.0000%/0.0000% | 1 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-07-28 | [20260728-deepseek-v4-flash-15](../apps/agent/evals/runs/20260728-deepseek-v4-flash-15.json) | open_code_zen/deepseek-v4-flash | 15/15 | 4-11-0 | 26.6667% | 10.8975%–51.9504% | 34.6000 | 1335731/899469 | 0.4228 | 0.0282 | 281.8992 | 0.1938%/0.0000% | 52 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-07-28 | [20260728-kimi-k26-10](../apps/agent/evals/runs/20260728-kimi-k26-10.json) | kimi/kimi-k2.6 | 0/10 | 0-0-0 |  |  |  | 1310/1146 | 0.0048 |  |  | 0.0000%/0.0000% | 2 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-07-28 | [20260728-kimi-k26-10-retry1](../apps/agent/evals/runs/20260728-kimi-k26-10-retry1.json) | kimi/kimi-k2.6 | 0/10 | 0-0-0 |  |  |  | 0/0 | 0.0000 |  |  | 0.0000%/0.0000% | 1 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-07-28 | [20260728-gemini25flash-5](../apps/agent/evals/runs/20260728-gemini25flash-5.json) | google/gemini-2.5-flash | 1/5 | 1-0-0 |  |  | 39.0000 | 82359/34459 |  |  |  | 0.0000%/0.0000% | 0 | 0 | 10 |  |  | 2026-07-28-official |
| 2026-07-28 | [20260728-mimo-v25-5](../apps/agent/evals/runs/20260728-mimo-v25-5.json) | open_code_zen/mimo-v2.5-free | 5/5 | 2-3-0 | 40.0000% | 11.7621%–76.9276% | 41.2000 | 503090/23413 | 0.0000 | 0.0000 | 0.0000 | 21.8935%/0.0000% | 0 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-08-08 | [20260808-kimi-k26-battle-1](../apps/agent/evals/runs/20260808-kimi-k26-battle-1.json) | kimi/kimi-k2.6 | 0/1 | 0-0-0 |  |  |  | 0/0 | 0.0000 |  |  | 0.0000%/0.0000% | 1 | 0 | 0 |  |  | 2026-07-28-official |
| 2026-08-08 | [20260808-gemini-25flash-d30](../apps/agent/evals/runs/20260808-gemini-25flash-d30.json) | google/gemini-2.5-flash | 0/1 | 0-0-0 |  |  |  | 439144/24515 |  |  |  | 0.0000%/0.0000% | 0 | 0 | 1 |  |  | 2026-07-28-official |
| 2026-08-08 | [20260808-opencode-claude-haiku-4-5-screen](../apps/agent/evals/runs/20260808-opencode-claude-haiku-4-5-screen.json) | open_code_zen/claude-haiku-4-5 | 1/1 | 1-0-0 | 100.0000% | 20.6549%–100.0000% | 52.0000 | 156/7975 |  |  |  | 39.3939%/18.1818% | 0 | 0 | 0 | 3007/4304/7253 | 5466/7465/8885 | 2026-07-28-official |

## Controles parciales e incidencias

- 2026-08-08 (MON-15): controles de proveedores de F2-10 con credenciales
  reales. **Kimi y Gemini quedaron `aborted`** (no comparables, sin winrate
  ni latencia publicados en el ledger); **OpenCode Zen quedó `complete`
  1/1**, el único control comparable de la fecha:
  - **Kimi** (`20260808-kimi-k26-battle-1`, pin `kimi/kimi-k2.6`): `aborted`
    0/1. Fallo clasificado `TransientProviderError` con causa original
    `APITimeoutError` (timeout de transporte del endpoint de Kimi con
    thinking + 16k max_tokens; límite externo del proveedor, el smoke pasa).
    El artefacto conserva ambos tipos (`failure_type` /
    `failure_cause_type`), el progreso real (1 turno, 1 transitorio) y
    latencias `null`.
  - **Gemini** (`20260808-gemini-25flash-d30`, pin `google/gemini-2.5-flash`,
    sin chain): `aborted` 0/1 por `BenchmarkDeadlineExceeded` a los 180 s
    (límite externo: el modelo tarda 15–21 s por completion y no cierra una
    batalla en el deadline). Ejercitó D30 en vivo: 1 rotación de clave y 1
    turno afectado por cuota real (429), 11 completions, 12 turnos.
  - **OpenCode Zen** (`20260808-opencode-claude-haiku-4-5-screen`, pin
    `open_code_zen/claude-haiku-4-5`, sin chain): **`complete` 1/1** con
    winrate 100.0000% (Wilson 95% 20.6549%–100.0000%). 52 completions en 33
    decisiones; latencia comparable en el ledger: completion p50/p95/max
    3007/4304/7253 ms y decision 5466/7465/8885 ms. El modelo respondió con
    alta tasa de respuestas inválidas (19 ilegales, 6 fallbacks) pero el
    screen cerró comparable; el modelo `claude-haiku-4-5` fue validado contra
    el catálogo `/models` real (existe) y se le agregó su ruta local en
    `model-routes.json`.

    **Sobre el usage de OpenCode (156 input / 7.975 output tokens, 52
    completions):** es el usage reportado por el gateway OpenCode Zen, no
    validado como conteo semántico — el modelo respondió 19 respuestas
    inválidas y 6 fallbacks dentro del screen, y el gateway no expone
    facturación por completion. Como `claude-haiku-4-5` no tiene precio
    aplicable en la tabla `2026-07-28-official`, **no se publica costo
    comparable** para esta corrida (celdas de costo vacías).
- `20260728-deepseek-v4-flash-control` completó 1 de 2 batallas antes de
  interrumpirse por la cancelación accidental del monitor reutilizable de
  errores de fondo. La batalla completada fue una derrota con 26 llamadas,
  65.909 tokens de entrada, 39.876 de salida y USD 0,02039254. El defecto quedó
  corregido antes del control siguiente.
- Tomando únicamente las dos batallas efectivamente completadas de ambos
  controles de DeepSeek V4 Flash, el resultado exploratorio es 1-1: 51
  llamadas, 130.214 tokens de entrada, 85.838 de salida y USD 0,041939352.
  Hubo 0 acciones ilegales, 0 fallbacks, 0 deadlines, 0 rotaciones y un turno
  afectado por un error transitorio que se recuperó. No es una medición de
  winrate: dos batallas solo verifican funcionamiento y costo aproximado.

## Fallos de transporte: qué se descartó

Las dos corridas de Kimi abortaron sin completar una sola batalla, y el
mensaje que quedó grabado era el fijo de la clasificación (`provider
transport failed`), sin el tipo de excepción original. La hipótesis en
estudio era una fuga de conexiones: `_LangChainBackend.complete` construye
un cliente nuevo (`ChatOpenAI` / `ChatGoogleGenerativeAI` / `ChatAnthropic`)
en CADA llamada y nunca lo cierra, así que parecía razonable que cientos de
llamadas dejaran cientos de pools abiertos hasta agotar descriptores —
encajaba con el síntoma (una llamada de humo pasa, el uso sostenido cae).

**Está descartada, medida.** Un servidor local que imita un endpoint
OpenAI-compatible y cuenta conexiones TCP, llamado por el código REAL del
backend 300 veces seguidas: **1 conexión TCP total, 300 requests, 0
errores, 20s**. El cliente subyacente se reutiliza, así que construir el
wrapper por llamada no abre sockets nuevos. Sea lo que sea el fallo de
Kimi, no es esto.

**Medido en vivo (MON-15 R2/R3, 2026-08-08).** El primer aborto real de Kimi
con credenciales dejó la clase original en el log: `APITimeoutError:
Request timed out` (timeout de transporte, límite externo del proveedor),
clasificada como `TransientProviderError`. No es una fuga de sockets: es un
timeout del endpoint de Kimi con `thinking` habilitado y `max_tokens`
16.000. La batalla quedó versionada como `aborted` (0/1, sin winrate ni
latencia comparable), el diagnóstico quedó desbloqueado y resuelto. Desde
R3 el artefacto persiste ambos tipos de forma durable y sanitizada:
`failure_type=TransientProviderError`, `failure_cause_type=APITimeoutError`.
