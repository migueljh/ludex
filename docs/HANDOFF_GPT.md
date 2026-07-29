Te dejo un handoff completo para copiarle al nuevo tech lead. Está orientado a que pueda revisar el código, reconstruir las decisiones y ordenar la continuación sin repetir experimentos.

---

# Handoff técnico — Ludex, estado al 28/07/2026

Vas a asumir como tech lead de la fase actual de Ludex. Ya conocés `docs/PLAN.md` y `docs/DECISIONS.md`; este mensaje explica qué se implementó, qué verificamos realmente, qué benchmarks corrimos, qué quedó inconcluso y cuál considero que debe ser el próximo orden de trabajo.

## 1. Estado del repositorio

- Repo: `/Users/miguelhernandez/Documents/ludex`
- Rama: `main`
- HEAD local/remoto verificado: `1d2e121`
- `origin/main` y `origin/HEAD` apuntaban al mismo commit.
- El worktree estaba limpio en la última inspección.
- `.worktrees/` pertenece a otros agentes: ignorarlo completamente.

Últimos commits relevantes, del más nuevo hacia atrás:

```text
1d2e121 docs(agent): rule out the connection-leak hypothesis for transport failures
2a58439 fix(provider): redact API keys from the classification log
bfbf2d6 docs(agent): record the Gemini, Kimi and MiMo benchmark runs
e8b7c2d fix(provider): cool down quota-exceeded keys instead of excluding forever
2e5dba4 fix(provider): preserve original exception in classified transport errors
2918ca7 docs(agent): record DeepSeek benchmark
78f9699 fix(agent): resolve unrevealed own Illusion switches
0556115 docs(agent): record DeepSeek V4 Flash control
5667871 fix(agent): keep failure monitor reusable across battles
ac5e0a5 feat(agent): configure DeepSeek V4 Flash
d6d600c fix(agent): propagate background decision failures
60c38d4 docs(agent): record Qwen timeout control
9b9c980 fix(agent): give Qwen a bounded longer timeout
995f4bd docs(agent): preserve interrupted Qwen controls
0d06f70 fix(agent): retry provider SDK timeouts
4652e5c docs(agent): record MiMo smoke result
c4c7907 feat(agent): checkpoint paid benchmark progress
9d2ce9b fix(agent): retry malformed structured decisions
```

Commits anteriores centrales de la rebanada del grafo:

```text
8d65607
30da4ea
e129bfd
ae98517
3298be9
1df390b
6a8c04b
931ffda
b6cdd65
7045828
```

Documentos principales:

- `docs/superpowers/specs/2026-07-27-agent-decision-graph-design.md`
- `docs/superpowers/plans/2026-07-27-agent-decision-graph.md`
- `docs/superpowers/specs/2026-07-28-agent-benchmark-cost-design.md`
- `docs/superpowers/plans/2026-07-28-agent-benchmark-cost.md`
- `docs/BENCHMARKS.md`
- `.superpowers/sdd/gpt-grafo.md`
- `.superpowers/sdd/gpt-d14.md`

Advertencia: `.superpowers/sdd/gpt-grafo.md` está desactualizado. Todavía dice que faltan métricas reales por falta de claves. Hay que actualizarlo con los benchmarks detallados más abajo.

## 2. Reglas operativas importantes

Leer antes de tocar las áreas correspondientes:

- `.claude/migrations/SKILL.md`
- `.claude/showdown-data/SKILL.md`
- `.claude/agent-recording/SKILL.md`
- `.claude/verification/SKILL.md`

Reglas duras:

- El esquema real manda sobre el prompt. Ante cualquier nombre de tabla o columna discrepante, parar y consultar.
- Commits en inglés.
- Siempre rutas explícitas.
- `git commit -m "..." -- rutas`.
- Nunca `git add -A` ni `git add .`.
- Nunca tocar `.worktrees/`.
- Nunca `docker compose down`, `down -v`, `docker stop`, `docker rm` ni `brew services stop`.
- PostgreSQL real de Ludex: `127.0.0.1:15432`, base/usuario/clave `ludex`.
- Showdown: puerto `8100`.
- Calc: puerto `8200`.
- No correr tests de integración de Showdown simultáneamente con otro `pytest`: ya hubo bloqueos falsos por dos suites peleando por el mismo server.

## 3. D14 y validador de equipos: cerrado

D14 ya quedó resuelto y verificado.

Commit:

```text
0c1c448 fix(seed): resolver D14 con herencia aditiva
```

Validador/auditor:

```text
37141c7 feat(teams): validar equipos y auditar learnsets
```

Solución:

- Las formas heredan el learnset de su base.
- La resolución es aditiva: learnset propio + heredado.
- No se usa una selección excluyente como `prevo || baseSpecies`.
- Se preserva `learn_methods` completo como JSONB.

Resultados:

```text
Gen 6: 62.198 → 62.198, delta 0
Gen 9: 65.624 → 65.642, delta +18
```

Verificaciones:

- Los 15 movimientos faltantes quedaron presentes.
- `ninetalesalola/moonblast` quedó presente.
- Las cuatro formas de Gourgeist conservan 66 movimientos cada una.
- `db_missing`: 15 → 0.
- `db_extra`: 3.547 → 3.550.
- Re-seed ejecutado dos veces: resultado idempotente.
- No depende de una tabla vacía; pipeline upsert-only.

Los tres extras nuevos son movimientos de evento:

- Ninetales-Alola / Celebrate.
- Lycanroc-Dusk / Happy Hour.
- Polteageist-Antique / Celebrate.

Se intentó una regla estrecha para no heredar eventos de generaciones anteriores. Fue incorrecta:

- Gen 6 cayó de 62.198 a 61.918.
- Eliminó 280 learnsets legítimos.
- Afectó, entre otros, a Charizard-Mega-X.

La variante se revirtió. Los tres eventos quedaron documentados como límite conocido. No reabrir D14 salvo evidencia nueva.

## 4. Arquitectura del agente implementada

Se implementó un grafo local con LangGraph:

```text
parse_state → calc_damage → decide
```

Características:

- Estado y prompt con lista blanca; no se entrega al modelo información oculta.
- Cliente determinista contra `packages/calc`.
- Salida estructurada del proveedor.
- Normalización semántica antes de comparar:
  - `mega` ausente y `mega: false` son equivalentes.
- Validación contra `legal_actions`.
- Una respuesta inválida produce un reintento semántico con feedback.
- Segundo fallo semántico produce respaldo determinista.
- Errores de infraestructura no gastan el reintento semántico y no terminan como fallback.

Ranking de respaldo para movimientos:

1. Movimientos que garantizan KO usando `min_damage >= HP restante`.
2. Entre los demás, daño esperado.
3. Todo daño se acota al HP restante para no premiar overkill.

Cambios forzados:

- Ranking minimax.
- Se selecciona el reemplazo cuyo peor matchup defensivo contra el rival activo sea el menos malo.

Esto es mejor que “primer cambio legal” y quedó cubierto con tests.

## 5. Autoría de acciones frente a camino de decisión

No se amplió `action_source`.

Son dos ejes distintos:

- `action_source`: quién decidió (`agent`, `human`, rival, etc.).
- `action_path`: cómo resolvió internamente el agente.

Se agregó `trajectory_steps.action_path`, nullable, con:

```text
llm
llm_retry
fallback
```

Los tres mantienen `action_source='agent'`.

Es nullable intencionalmente: las trayectorias históricas eligieron al azar y no puede inventarse retrospectivamente un camino de decisión.

Se eligió `text + CHECK` deliberadamente, no enum nativo, porque el eje crecerá y los enums de PostgreSQL son difíciles de contraer. Está documentado como excepción que no sienta precedente general.

## 6. Dependencias y override de websockets

Conflicto real:

```text
poke-env 0.15.0 → websockets==16.0
langgraph 1.2.9 → langgraph-sdk → websockets>=14,<16
```

No hay combinación de versiones que resuelva el conflicto.

Se agregó un override de uv a `websockets==16.0`.

Justificación:

- Usamos `StateGraph` local, en proceso.
- No usamos LangGraph Platform ni `langgraph-sdk`.
- Se verificó import conjunto de poke-env y LangGraph, compilación de grafo y batalla.
- Si en el futuro se usa LangGraph Platform, hay que revisar/eliminar el override.

No bajar poke-env: el grabador depende del comportamiento de despacho de `0.15.0`.

## 7. Clasificación de errores del proveedor

Jerarquía:

```text
ProviderError(RuntimeError)
├── QuotaExceeded
├── TransientProviderError
├── FatalProviderError
├── ProviderPoolExhausted
└── DecisionDeadlineExceeded
```

El detalle importante es que heredan de `RuntimeError`, no `ValueError`.

El bucle de decisión solo captura `ValueError` y `TypeError` como fallos semánticos del modelo. Por lo tanto:

- 429 no se cuenta como acción ilegal.
- 5xx y timeouts no se cuentan como acción ilegal.
- Pool agotado no produce fallback.
- Deadline no produce fallback.
- Un fallo de infraestructura atraviesa el grafo y aborta ruidosamente.

Además se descubrió que los SDK de OpenAI y Anthropic representan ciertos timeouts como subclases de `APIConnectionError`, no necesariamente de `TimeoutError`. La clasificación fue corregida.

## 8. Rotación de claves y D30

Configuración real:

```text
GEMINI_API_KEY
GEMINI_API_KEYS
KIMI_API_KEY
KIMI_BASE_URL
OPEN_CODE_ZEN_API_KEY
OPEN_CODE_ZEN_BASE_URL
OPEN_CODE_ZEN_MODEL
ANTHROPIC_API_KEY
```

También se aceptan `GOOGLE_API_KEY` y `GOOGLE_API_KEYS` como aliases de compatibilidad.

Hay dos conceptos separados:

- Rotación de claves dentro del mismo proveedor.
- Cadena entre proveedores.

En benchmarks:

- Proveedor y modelo fijos.
- Cadena entre proveedores prohibida.
- Si falla el proveedor fijado, la corrida aborta.
- Nunca mezclar modelos en un mismo winrate.

Defecto encontrado en Gemini:

- La implementación anterior avanzaba permanentemente `first_available_key` después de un 429.
- Trataba una cuota temporal por minuto como agotamiento permanente de la clave.
- Terminaba consumiendo el pool entero aunque las primeras claves ya se hubieran recuperado.

D30, commit `e8b7c2d`:

- Cada clave tiene `cooldown_until`.
- Usa `retry_delay` de Gemini cuando está disponible.
- Si no, default de 60 segundos.
- Si todas las claves están enfriándose y la más próxima vuelve antes del deadline interno, espera y reutiliza.
- Si no entra en el presupuesto, lanza `ProviderPoolExhausted`.
- No intenta deducir con certeza cuota diaria frente a temporal: se permite un nuevo intento después del cooldown.

Hay seis canarios de regresión para este comportamiento.

Cambios relacionados:

- `2e5dba4`: preserva la excepción original del transporte.
- `2a58439`: redacción de claves de API en logs.
- `1d2e121`: descarta experimentalmente la hipótesis de fuga de conexiones.

Prueba de la hipótesis de conexiones:

- Servidor falso OpenAI-compatible local.
- Backend real.
- 300 llamadas.
- Una sola conexión TCP.
- 300 requests.
- 0 errores.
- Aproximadamente 20 segundos.

Por lo tanto, crear wrappers en ese camino no explicó el fallo sostenido de Kimi.

## 9. Grabación, `client.py` y corrector de turnos

`showdown/client.py` es el archivo más delicado del repositorio.

Invariante D22:

- La foto del estado y el mapa `acción → BattleOrder` deben capturarse síncronamente antes del primer `await`.
- Una versión anterior serializaba desde una tarea de fondo después de que Battle hubiera avanzado y grababa acciones fuera de su propia máscara legal.

No mover esa captura sin una prueba específica de regresión.

El corrector de turnos reconoce, entre otros:

- `cant`.
- Faint propio antes de ejecutar movimiento.
- Autogolpe por confusión.
- Encore.
- Win/tie.
- Revelación de Illusion.
- Cambios de forma de Meloetta y Groudon.

### Fallos de tareas de fondo

Se encontró que una excepción dentro de la tarea de mensajes de poke-env podía no propagarse a `battle_against`. El proceso quedaba colgado, sin CPU, hasta intervención manual.

Se corrigió:

- Un `concurrent.futures.Future` thread-safe comunica el fallo.
- El runner compite entre la batalla y el monitor de fallo.
- Cancela la batalla y relanza la excepción original.
- Se usa `asyncio.shield` para que cancelar el waiter por batalla no cancele el future compartido.
- El monitor queda reutilizable entre batallas.

Esto es crítico para las futuras corridas desatendidas de miles de batallas.

### Zoroark e Illusion

Caso real:

```text
battle-gen6randombattle-1799
```

Nuestro Zoroark entró disfrazado de Barbaracle, salió antes de revelar Illusion y “Zoroark” nunca apareció en el protocolo público. El corrector no podía encontrar el switch real.

Solución D29, commit `78f9699`:

- Solo para nuestro lado, se permite usar el `|request|` propio.
- Se parsea el JSON.
- Se comprueba que `request.side.id` sea nuestro lado.
- Se exige exactamente el Pokémon con `active=true`.
- No se busca por substring.
- Nunca se usa un request para resolver el lado rival.
- El rival debe seguir pudiendo engañarnos con Illusion; filtrar eso sería fuga de información oculta.

## 10. Verificación y deuda del auditor de fuga

Una suite completa posterior a D29 y al arreglo del ledger dio:

```text
191 passed in 218.17s
```

Sin embargo, eso fue antes de los últimos commits de proveedor/D30. Los commits nuevos tienen tests propios, pero no afirmaría que la suite completa en el HEAD actual `1d2e121` está verificada hasta correrla otra vez.

Importante: el test global de fuga de información no está colgado. En el corpus observado:

```text
484 batallas
29.880 pasos
1 passed in 178s
```

Es lento por un N+1:

- Una consulta por cada paso.
- Cada consulta vuelve a recorrer `battle_turns` hasta el turno N.

Deuda registrada:

```text
vectorize-information-leak-audit
```

Dirección acordada:

- Reemplazar el bucle por una consulta con JOIN/agregación.
- La suite rápida debe verificar siempre las batallas recién generadas.
- `packages/dataset-audit` debe revisar deliberadamente todo el corpus antes de merge.
- No eliminar la cobertura global: ya escapó un defecto cuando se filtró solamente por la corrida corriente.

Hay 39 discrepancias históricas de `action_turn`:

- Todas pertenecen a batallas viejas `source='test'`.
- Son anteriores a los arreglos del corrector.
- Están excluidas del dataset por contrato.
- Residuo conocido, no defecto abierto ni corrupción de datos.

## 11. Infraestructura de benchmark y ledger

El runner:

- Escribe snapshots JSON atómicos después de cada batalla.
- Conserva usage ya pagado aunque la corrida se interrumpa.
- Reporta progreso por batalla.
- Las corridas abortadas conservan datos parciales, pero no se les atribuye winrate comparable.
- No persiste batallas por defecto, intencionalmente.
- Proveedor y modelo quedan fijos por corrida.
- Usage se toma de la respuesta de la API, no se estima.
- Costos se calculan desde un archivo de precios fechado y con fuente.
- Si el precio no está disponible, el costo queda vacío.

El ledger es:

```text
docs/BENCHMARKS.md
```

Los artefactos están en:

```text
apps/agent/evals/runs/
```

El ledger ahora inserta las filas dentro de la tabla Markdown principal, incluso si hay notas después de ella, e incluye una columna de transitorios.

Las tres preguntas separadas son:

1. ¿Sirve para generar dataset?
   - Winrate, acciones ilegales, fallback, costo por batalla.

2. ¿Sirve para jugar en vivo?
   - Latencia por turno y tolerancia a que el rival active el reloj.

3. ¿Cuánto cuesta?
   - Por llamada, por batalla y proyección a 10.000 batallas.

El reloj de Showdown no corre por defecto. En bot contra bot local, una respuesta de 60 segundos alarga la corrida pero no pierde automáticamente la partida. Para juego humano sigue siendo un riesgo porque el rival puede activar el timer.

## 12. Baselines

Archivo:

```text
apps/agent/evals/random-baseline.json
```

300 batallas por rival, `gen6randombattle`:

| Rival | Resultado | Winrate | Wilson 95% |
|---|---:|---:|---:|
| RandomPlayer | 143–157 | 47,67% | 42,08–53,31 |
| MaxBasePowerPlayer | 35–265 | 11,67% | 8,51–15,79 |
| SimpleHeuristicsPlayer | 9–291 | 3,00% | 1,59–5,60 |

Para una pantalla de 15 batallas contra SimpleHeuristics:

- 5 o más victorias: señal fuerte.
- 0 o 1: no hay mejora útil.
- 2–4: interpretar con Wilson, validez, costo y latencia.

No combinar corridas parciales ni modelos diferentes.

## 13. Benchmarks realizados

### DeepSeek V4 Flash

Run:

```text
20260728-deepseek-v4-flash-15
```

Resultado:

```text
15/15 completas
4 victorias, 11 derrotas
26,6667%
Wilson 95%: 10,8975%–51,9504%
```

Uso:

```text
516 turnos
519 llamadas
1.335.731 tokens de entrada
899.469 tokens de salida
142.900 tokens cacheados
```

Validez:

```text
1 turno con respuesta inválida
0,1938% recuperado por reintento
0 fallback
52 turnos afectados por transitorios
10,08%
0 deadlines
0 rotaciones
```

Costo:

```text
USD 0,42284886 total
USD 0,028189924 por batalla
USD 281,89924 proyectados a 10.000 batallas
```

Lectura:

- Muy por encima del baseline del 3%.
- No alcanzó el umbral predeclarado de 5 victorias; quedó en 4.
- El límite inferior de Wilson sí queda por encima del baseline.
- Evidencia positiva, pero no un cierre incontestable bajo el criterio original.
- Muy buena legalidad.
- Corrida extremadamente lenta: aproximadamente 2,5 horas; algunas batallas duraron 20–29 minutos.
- No hay instrumentación detallada de latencia por llamada todavía.
- Puede ser candidato para dataset, pero hoy es débil para juego en vivo.

### MiMo V2.5 Free

Run:

```text
20260728-mimo-v25-5
```

Resultado:

```text
5/5 completas
2 victorias, 3 derrotas
40%
Wilson 95%: 11,7621%–76,9276%
```

Uso:

```text
169 turnos
206 llamadas
503.090 entrada
23.413 salida
47.168 cacheados
```

Validez:

```text
37 turnos con respuesta inicial inválida
21,8935% recuperados mediante reintento
0 fallback
0 transitorios
0 deadlines
0 rotaciones
```

Costo:

```text
USD 0
```

Un smoke anterior también dio aproximadamente 20,4% de respuestas inválidas. Es un patrón, no un accidente aislado.

Lectura:

- Rápido, gratuito y aparentemente competitivo en una muestra pequeña.
- Pero una de cada cinco decisiones iniciales es imposible.
- El winrate todavía no basta para elegirlo.
- No lo usaría como generador principal de dataset sin entender o reducir esa tasa de invalidez, aunque el reintento la recupere.

### Gemini 2.5 Flash

Run:

```text
20260728-gemini25flash-5
```

Estado:

```text
Abortado después de 1/5
La única batalla fue victoria
No corresponde reportar winrate comparable
```

Uso parcial:

```text
40 turnos
39 llamadas exitosas
82.359 entrada
34.459 salida
32.115 reasoning
1.460 cacheados
```

Validez:

```text
0 respuestas ilegales
0 fallback
0 transitorios reportados
0 deadlines
10 rotaciones
10 turnos afectados por cuota
```

Terminó en `ProviderPoolExhausted`.

La causa fue el defecto de rotación permanente corregido por D30. Por lo tanto Gemini es la prioridad para una repetición limpia con un run id nuevo.

Todavía falta precio oficial de Gemini en la configuración. Hasta agregarlo con fecha y fuente oficial, el costo debe seguir en blanco.

### Kimi K2.6

Runs:

```text
20260728-kimi-k26-10
20260728-kimi-k26-10-retry1
```

Primer intento:

```text
Abortado 0/10
2 turnos
1 llamada exitosa
1.310 entrada
1.146 salida
1.065 reasoning
1.310 cacheados
USD 0,0047936
2 turnos afectados por transitorios
```

Segundo intento:

```text
Abortado en la primera decisión
0 llamadas exitosas
1 turno transitorio
```

Un `provider-smoke` aislado sí pasó:

```text
~15 segundos
acción legal
52 entrada
492 salida
446 reasoning
sin transitorios
```

Lectura:

- Kimi funciona para llamadas aisladas.
- Falla bajo la secuencia sostenida de una batalla.
- Antes se colapsaba la causa como `provider transport failed`.
- Ahora se conserva y registra la excepción original.
- Las claves quedan redactadas.
- La hipótesis de fuga de conexiones fue descartada.

Próximo paso correcto: un diagnóstico controlado de una sola batalla o smoke sostenido, capturar si la excepción real es `ConnectError`, `ReadTimeout`, `PoolTimeout`, etc., y corregir la raíz. No seguir lanzando benchmarks de 10 a ciegas.

### DeepSeek: controles previos

Hubo dos controles exploratorios:

- Uno interrumpido tras una batalla perdida:
  - 26 llamadas.
  - 65.909 entrada.
  - 39.876 salida.
  - USD 0,02039254.

- Uno completado con una victoria:
  - 25 llamadas.
  - 64.305 entrada.
  - 45.962 salida.
  - USD 0,021546812.
  - Un transitorio.

Combinados parecen 1–1, pero no son un benchmark comparable y no deben sumarse al run oficial de 15.

### Qwen 3.5 Plus

Controles:

- Primer parcial:
  - 8 llamadas exitosas.
  - 16.308 entrada.
  - 4.726 salida.
  - USD 0,0089328.
  - Luego `APITimeoutError`.

- Otro intento:
  - Tres timeouts de 30 segundos.
  - 0 llamadas exitosas.
  - 0 costo medible.

Se aumentó a:

```text
timeout=60s
max_tokens=1024
```

Aun así, la primera decisión volvió a agotar tres intentos de 60 segundos, sin respuesta exitosa.

Qwen no está descartado conceptualmente por el reloj de Showdown, pero el gateway de OpenCode Zen no fue suficientemente confiable. No gastar una corrida grande hasta resolver o caracterizar esto.

### MiniMax

No se ejecutó una pantalla reciente durable.

Precio configurado:

```text
USD 0,30/M input
USD 1,20/M output
USD 0,06/M cache
```

Hubo un intento anterior que consumió aproximadamente USD 0,14 y se detuvo, sin resultado incorporable al ledger.

Es un buen candidato para una pantalla fija de cinco batallas mientras se diagnostica Kimi y se espera/reprueba Gemini.

## 14. Rutas de modelos

Configuración en:

```text
apps/agent/evals/model-routes.json
```

Rutas relevantes:

- MiMo V2.5 Free: OpenCode Zen, `chat_completions`.
- MiniMax M2.7: OpenCode Zen, `chat_completions`.
- DeepSeek V4 Pro: OpenCode Zen, `chat_completions`.
- DeepSeek V4 Flash: OpenCode Zen, `chat_completions`.
- Qwen 3.5 Plus: OpenCode Zen, `messages`, `max_tokens=1024`, `timeout=60`.
- Qwen 3.6 Plus: OpenCode Zen, `messages`.
- Kimi K2.6: endpoint Kimi, `chat_completions`, temperatura 1, thinking habilitado, `max_tokens=16000`.

Tabla de precios fechada 2026-07-28:

- Kimi: 0,95 input / 4 output / 0,16 cache por millón.
- MiMo: 0.
- MiniMax: 0,30 / 1,20 / 0,06.
- DeepSeek Pro: 1,74 / 3,48 / 0,145.
- DeepSeek Flash: 0,14 / 0,28 / 0,028.
- Qwen 3.5: 0,20 / 1,20 / 0,02.
- Qwen 3.6: 0,50 / 3 / 0,05.
- Gemini: todavía sin tarifa registrada.

## 15. Orden recomendado de continuación

### Paso 1: estabilizar el HEAD actual

Correr la suite completa de `apps/agent` sola, sin otro pytest simultáneo.

- No interrumpir el test de fuga antes de unos tres minutos.
- Esperar más de 191 tests, porque D30 agregó casos.
- Revisar en particular los seis canarios del cooldown, la redacción de claves y la propagación de excepción original.

No afirmar que `1d2e121` está completamente verde hasta ejecutar esto.

### Paso 2: repetir Gemini

Hacer una corrida nueva de cinco batallas contra `SimpleHeuristicsPlayer`:

- Proveedor Google/Gemini fijo.
- Modelo `gemini-2.5-flash`.
- Sin cadena de proveedores.
- Run id nuevo.
- Verificar que D30 reutiliza claves luego del cooldown en lugar de agotar el pool.
- Si vuelve a abortar, conservar el artefacto y diagnosticar el tipo real de cuota.

Añadir precio oficial de Gemini al archivo de precios solamente con fuente oficial y fecha. Si no se encuentra, dejar el costo en blanco.

### Paso 3: pantalla de MiniMax

Cinco batallas fijas contra `SimpleHeuristicsPlayer`.

Objetivo:

- Tener una comparación barata adicional.
- No saltar directamente a 15 hasta ver validez, latencia y costo.
- Si el tech lead prefiere respetar el criterio original estricto, ampliar después a 15; no mezclar la pantalla de cinco con otro modelo.

### Paso 4: diagnosticar Kimi, no benchmarkearlo aún

- `provider-smoke`.
- Como máximo una batalla controlada.
- Capturar la excepción original nueva.
- Diferenciar conexión, lectura, pool, proxy o respuesta.
- Corregir según evidencia.
- Recién después hacer una pantalla de cinco o el benchmark de 15.

### Paso 5: seleccionar finalistas y hacer comparación homogénea

Una vez que haya pantallas válidas:

- Misma generación.
- Mismo rival.
- Mismo número de batallas.
- Proveedor/modelo fijo por corrida.
- Sin cadena.
- No agregar resultados de corridas parciales.
- Reportar Wilson, no solamente winrate.

Orden de lectura:

1. Validez:
   - Ilegales recuperadas.
   - Fallback.
   - Transitorios.
   - Deadlines/cuota.

2. Calidad:
   - Winrate y Wilson.

3. Costo:
   - Por batalla.
   - Proyección a 10.000.

4. Uso en vivo:
   - Latencia separada del rendimiento para dataset.

## 16. Mi lectura actual de los candidatos

- DeepSeek V4 Flash es el único con una muestra completa de 15. Es claramente superior al baseline y casi nunca propone acciones ilegales, pero es lento y tuvo muchos turnos con transitorios. Buen candidato para dataset; mal candidato actual para juego interactivo.

- MiMo es gratis, rápido y prometedor, pero el 21,9% de respuestas iniciales ilegales es una señal seria. Aunque el reintento las recupere, consume llamadas y hace que el rendimiento dependa demasiado del corrector.

- Gemini mostró la mejor disciplina estructural: 0% ilegales en la muestra parcial. Su corrida fue invalidada por un defecto de nuestra rotación, ahora corregido. Es el siguiente modelo que hay que medir.

- Kimi todavía no está medido. Su endpoint funciona en aislamiento, pero no sostuvo una batalla. Es un problema de transporte pendiente, no evidencia de mala calidad del modelo.

- Qwen tampoco está medido de forma válida. El problema actual es la confiabilidad/latencia del gateway, no el razonamiento del modelo.

- MiniMax falta y merece una pantalla barata antes de decidir.

No cerraría todavía la selección de modelo basándome solamente en DeepSeek 4/15 y MiMo 2/5. Sí diría que la arquitectura de fase 2 ya demostró que un LLM puede jugar, respetar la máscara legal y superar ampliamente el 3% del agente aleatorio contra SimpleHeuristics.

## 17. Deudas concretas

- Rerun completo de tests en `1d2e121`.
- Actualizar `.superpowers/sdd/gpt-grafo.md`.
- Añadir precio oficial de Gemini o mantener costo vacío.
- Instrumentar latencia por completion y por batalla; hoy la latencia de DeepSeek fue observada por tiempo de pared, no registrada finamente.
- Resolver `vectorize-information-leak-audit`.
- Diagnosticar transporte sostenido de Kimi.
- Repetir Gemini después de D30.
- Ejecutar pantalla de MiniMax.
- Mantener visible la distinción entre:
  - Modelo para dataset.
  - Modelo para juego en vivo.
  - Modelo económicamente viable.
- No tocar casualmente la captura síncrona previa al primer `await`.
- No simplificar el corrector de turnos ni eliminar rastros de Illusion/confusión/formas.
- La acción rechazada por el servidor sigue siendo un límite conocido: `_discard_last_step` busca evidencias `|error|` que pueden no llegar por ese canal. No quedó resuelto en esta rebanada.

## 18. Resultado que debería producir la próxima etapa

Antes de lanzar cientos de batallas, deberíamos terminar con una tabla homogénea de candidatos que permita decidir:

```text
modelo
proveedor
n
victorias
winrate
Wilson 95%
% retry semántico
% fallback
% turnos transitorios
rotaciones/cuota
deadlines
latencia media/p95
tokens input/output por batalla
costo por batalla
proyección 10.000
apto para dataset
apto para vivo
```

La próxima decisión no debería ser “qué modelo parece mejor”, sino:

- Cuál genera trayectorias suficientemente buenas y legales.
- Cuál soporta corridas largas sin degradación silenciosa.
- Cuál tiene un costo sostenible para 10.000 batallas.
- Cuál responde suficientemente rápido si un humano activa el reloj.

El principio que viene gobernando el proyecto debe mantenerse: nada que degrade silenciosamente puede contaminar un número que después se presenta como resultado.

---