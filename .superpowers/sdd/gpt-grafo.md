# Grafo de decisión del agente — reporte

Fecha: 2026-07-28  
Rama: `main`

## Estado

Implementado:

- `StateGraph` local `parse_state → calc_damage → decide`.
- Estado y prompt con lista blanca explícita.
- Cliente HTTP de `packages/calc`.
- Ranking determinista: KO garantizado, daño esperado acotado y cambio
  forzado minimax.
- Salida estructurada, normalización `false == ausente` para flags,
  validación exacta contra máscara, reintento semántico y fallback.
- Rotación de claves, reintentos transitorios y cadena de proveedores para
  juego; cadena prohibida en benchmark.
- `trajectory_steps.action_path` nullable, separado de `action_source`.
- Cableado en `LudexPlayer` con fotografía y mapa de órdenes capturados antes
  del primer `await`.
- Runner de benchmark sin persistencia por defecto y baseline versionado.

## Commits

- `8d65607` — dependencias, configuración y override de websockets.
- `30da4ea` — migración `action_path`.
- `e129bfd` — estado allowlisted.
- `ae98517` — calc y ranking.
- `3298be9` — decisiones D24/D25.
- `1df390b` — clasificación y rotación de proveedores.
- `6a8c04b` — validación, reintento y fallback.
- `931ffda` — workflow LangGraph.
- `b6cdd65` — benchmark reusable.
- `7045828` — cableado seguro en Showdown.

## Evidencia

- Override `websockets==16.0`: poke-env y LangGraph importan juntos, un grafo
  local compila/ejecuta y una batalla real termina.
- Calc real: cliente y `/calc` entregan la misma respuesta en gen 6.
- Diez batallas completas con grafo falso legal: todas las acciones dentro de
  su propia máscara; `action_source='agent'`, `action_path='llm'`.
- Una batalla completa forzando dos respuestas ilegales por turno:
  `action_path='fallback'` y acciones persistidas dentro de máscara.
- Auditoría SQL global: 0 acciones fuera de su propia máscara.
- Filas históricas: `action_path=NULL`, sin default inventado.
- Suite completa: 150 tests pasan en 265,95 s.
- Roturas deliberadas detectadas: promedio bruto en lugar de KO seguro,
  acción fuera de máscara, `mega=false` distinto de ausente, 429 degradado a
  fallback, arista de calc ausente y captura movida dentro de coroutine.

## Baseline

`gen6randombattle`, 300 batallas por rival, concurrencia 20:

| Rival | W-L-T | Winrate | Wilson 95% |
|---|---:|---:|---:|
| RandomPlayer | 143-157-0 | 47,67% | 42,08–53,31% |
| MaxBasePowerPlayer | 35-265-0 | 11,67% | 8,51–15,79% |
| SimpleHeuristicsPlayer | 9-291-0 | 3,00% | 1,59–5,60% |

Fuente versionada:
`apps/agent/evals/random-baseline.json`.

## Métricas

Los proveedores falsos verifican por separado:

- rotación por 429 sin reconstruir el prompt;
- reintento de timeout/5xx sin gastar intento semántico;
- respuesta inválida recuperada como `llm_retry`;
- segunda respuesta inválida convertida en `fallback`;
- pool agotado propagado como error, nunca como fallback.

Métricas LLM reales: **pendientes por falta de API key**.

## Límites y tareas pendientes

- Ejecutar el benchmark real cuando el dueño configure una clave.
- El auditor global encontró 39 violaciones históricas `action_turn`, todas
  en seis batallas `source='test'` (`-266`, `-267`, `-269`, `-271`, `-272`,
  `-276`, grabadas el 2026-07-27 antes de este grafo). Son residuo conocido
  de pruebas anteriores a los arreglos del corrector y están excluidas del
  dataset de entrenamiento por el contrato `source='test'`: no son un
  defecto abierto ni contaminan datos de entrenamiento. Las otras cinco
  invariantes pasan: 0 fuga, 0 re-derivación, 0 reward, 0 versión y 0
  huérfanos. No se reescribió ni borró data en este encargo.
- `vectorize-information-leak-audit`: reemplazar el N+1 de la prueba de fuga,
  conservar la suite rápida sobre batallas nuevas y mover la auditoría global
  deliberada a `packages/dataset-audit`.
- Si se adopta LangGraph Platform, reevaluar el override de websockets y
  probar sus transports remotos.
- Un `pnpm audit` invocado accidentalmente al intentar ejecutar el script
  homónimo reportó 33 advisories transitivos existentes (4 critical, 12
  high, 16 moderate, 1 low), principalmente desde pokemon-showdown y Vitest.
  No se cambiaron dependencias Node dentro de esta tarea.
