# Benchmarks de modelos

Registro acumulativo. El costo se calcula con usage real; una celda vacía
significa desconocido o no comparable, nunca cero implícito.

| Fecha | Run | Proveedor/modelo | Batallas | W-L-T | Winrate | Wilson 95% | Llamadas/batalla | Tokens in/out | Costo total | Costo/batalla | 10.000 batallas | Ilegales retry/fallback | Transitorios | Deadlines | Rotaciones | Precios |
|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| 2026-07-28 | [20260728-mimo-smoke](../apps/agent/evals/runs/20260728-mimo-smoke.json) | open_code_zen/mimo-v2.5-free | 2/2 | 0-2-0 | 0.0000% | 0.0000%–65.7620% | 32.5000 | 156087/7177 | 0.0000 | 0.0000 | 0.0000 | 20.3704%/0.0000% | 0 | 0 | 0 | 2026-07-28-official |
| 2026-07-28 | [20260728-deepseek-v4-flash-control-2](../apps/agent/evals/runs/20260728-deepseek-v4-flash-control-2.json) | open_code_zen/deepseek-v4-flash | 1/1 | 1-0-0 | 100.0000% | 20.6549%–100.0000% | 25.0000 | 64305/45962 | 0.0215 | 0.0215 | 215.4681 | 0.0000%/0.0000% | 1 | 0 | 0 | 2026-07-28-official |
| 2026-07-28 | [20260728-deepseek-v4-flash-15](../apps/agent/evals/runs/20260728-deepseek-v4-flash-15.json) | open_code_zen/deepseek-v4-flash | 15/15 | 4-11-0 | 26.6667% | 10.8975%–51.9504% | 34.6000 | 1335731/899469 | 0.4228 | 0.0282 | 281.8992 | 0.1938%/0.0000% | 52 | 0 | 0 | 2026-07-28-official |
| 2026-07-28 | [20260728-kimi-k26-10](../apps/agent/evals/runs/20260728-kimi-k26-10.json) | kimi/kimi-k2.6 | 0/10 | 0-0-0 |  |  |  | 1310/1146 | 0.0048 |  |  | 0.0000%/0.0000% | 2 | 0 | 0 | 2026-07-28-official |
| 2026-07-28 | [20260728-kimi-k26-10-retry1](../apps/agent/evals/runs/20260728-kimi-k26-10-retry1.json) | kimi/kimi-k2.6 | 0/10 | 0-0-0 |  |  |  | 0/0 | 0.0000 |  |  | 0.0000%/0.0000% | 1 | 0 | 0 | 2026-07-28-official |
| 2026-07-28 | [20260728-gemini25flash-5](../apps/agent/evals/runs/20260728-gemini25flash-5.json) | google/gemini-2.5-flash | 1/5 | 1-0-0 |  |  | 39.0000 | 82359/34459 |  |  |  | 0.0000%/0.0000% | 0 | 0 | 10 | 2026-07-28-official |
| 2026-07-28 | [20260728-mimo-v25-5](../apps/agent/evals/runs/20260728-mimo-v25-5.json) | open_code_zen/mimo-v2.5-free | 5/5 | 2-3-0 | 40.0000% | 11.7621%–76.9276% | 41.2000 | 503090/23413 | 0.0000 | 0.0000 | 0.0000 | 21.8935%/0.0000% | 0 | 0 | 0 | 2026-07-28-official |

## Controles parciales e incidencias

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
