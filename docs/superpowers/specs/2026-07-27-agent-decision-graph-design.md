# Grafo de decisión del agente — Diseño

**Fecha:** 2026-07-27  
**Estado:** aprobado con enmiendas  
**Alcance:** `apps/agent`, una migración aditiva y documentación. Quedan fuera
`retrieve_context`, `human_approval` y las tablas de proveedores/modelos.

## Objetivo

Reemplazar la elección aleatoria por un grafo LangGraph que, en cada punto de
decisión, toma una fotografía observable del combate, calcula daño de forma
determinista, pide una acción estructurada a un LLM y garantiza que la acción
final pertenece a la máscara legal. La grabación conserva las invariantes de
D22 y registra por separado quién decidió y qué camino interno produjo la
acción.

## Línea de base

Se midió antes de construir el grafo con `LudexPlayer`, cuyo `choose_move`
delegaba en `RandomPlayer`, en `gen6randombattle`, 300 batallas por rival y
Wilson 95%:

| Rival | Victorias | Derrotas | Empates | Winrate | IC 95% |
|---|---:|---:|---:|---:|---:|
| `RandomPlayer` | 143 | 157 | 0 | 47,67% | 42,08–53,31% |
| `MaxBasePowerPlayer` | 35 | 265 | 0 | 11,67% | 8,51–15,79% |
| `SimpleHeuristicsPlayer` | 9 | 291 | 0 | 3,00% | 1,59–5,60% |

Un comando reusable volverá a ejecutar esta evaluación con generación/formato,
rival, cantidad y concurrencia como parámetros y reportará siempre el intervalo,
no solo el porcentaje.

## Arquitectura

Se usa un `StateGraph` async real, con tres nodos:

```text
parse_state → calc_damage → decide → END
```

`GraphState` es un `TypedDict`. Las dependencias externas se inyectan al
construir el grafo:

- `DamageCalculator`: cliente async de `packages/calc`;
- `DecisionProvider`: proveedor async de salida estructurada.

Los tests reemplazan ambas interfaces por fakes deterministas. LangGraph
orquesta el flujo; no es un wrapper decorativo sobre otro orquestador.

## `parse_state`

Recibe exclusivamente el `dict` producido por `serialize_battle`. Construye una
vista nueva mediante lista blanca de claves conocidas:

- `schema_version`, `turn`, `player_role`, `format`, `gen`;
- `field`;
- `me.pokemon`;
- `opponent.pokemon`;
- `legal_actions`.

No acepta un `Battle`, frames de protocolo, logs ni mensajes de chat. El payload
del prompt se arma nuevamente con campos nombrados de esa vista. No existe una
ruta de datos desde el stream de chat hasta el proveedor.

La generación se lee de `state["gen"]`; ninguna lógica del grafo contiene
`gen6` ni otro literal de generación.

## `calc_damage`

El cliente comprueba `GET /health` y llama a `POST /calc` con el contrato real
del servicio. Traduce solo datos observables del estado:

- especie, nivel, HP, estado, tipos, boosts y stats conocidos;
- movimiento por `showdown_id`;
- clima, efectos de campo y condiciones de ambos lados.

Calcula:

1. cada movimiento propio presente en `legal_actions` contra el rival activo;
2. cada movimiento revelado del rival activo contra el activo propio;
3. para un cambio forzado, cada movimiento revelado del rival activo contra
   cada reemplazo legal.

Cada resultado conserva rolls, daño mínimo/máximo, daño esperado y probabilidad
de KO provista por calc. Un error individual queda asociado a ese matchup y no
inventa un valor numérico; si todos fallan, `decide` todavía puede usar el
fallback determinista sin daño.

### Ranking determinista de movimientos

Sea `remaining_hp` el HP actual del rival en puntos, obtenido aplicando
`hp_fraction` al `defender_hp.max` que devuelve calc:

1. primero, movimientos con KO garantizado:
   `min_damage >= remaining_hp`;
2. dentro de cada grupo, mayor daño esperado capado:
   `mean(min(total_roll, remaining_hp))`;
3. empate: orden original de `legal_actions`.

El cap evita premiar overkill: 200 de daño contra 50 HP no vale más que 60.
Para movimientos multigolpe, `total_roll` es la suma de los rolls de todos los
golpes en la misma posición; el mínimo garantizado suma el mínimo de cada
golpe.

### Ranking determinista de cambios forzados

Para cada reemplazo se calcula el daño esperado de cada movimiento rival
revelado, capado a su HP restante y expresado como fracción de ese HP. Se elige
el reemplazo cuyo peor matchup tenga la menor fracción esperada. Los empates
respetan el orden de `legal_actions`.

Si el rival no tiene movimientos revelados o calc no produce ningún matchup
utilizable, se elige el primer switch legal. Ese caso queda expuesto en el
resultado como fallback sin evaluación, no oculto como una elección calculada.

## `decide`

El proveedor recibe:

- instrucciones estables;
- el estado allowlisted;
- resultados de daño;
- la máscara `legal_actions` exacta.

Devuelve JSON estructurado:

```json
{
  "action": {"kind": "move", "id": "icebeam"},
  "reasoning": "texto",
  "alternatives": [
    {"action": {"kind": "switch", "species": "garchomp"}, "reason": "texto"}
  ]
}
```

Antes de comparar se normalizan únicamente flags booleanos de mecánicas:
`mega`, `z_move`, `dynamax` y `terastallize`. Un flag `false` se elimina, por
lo que ausente y falso son equivalentes. No se normalizan ids, especies,
claves desconocidas ni valores verdaderos. Después de esa normalización, la
igualdad es estructural exacta.

Flujo:

1. respuesta válida: `action_path="llm"`;
2. inválida o malformada: un único reintento con el error, la acción recibida
   y la máscara exacta;
3. reintento válido: `action_path="llm_retry"`;
4. segunda respuesta inválida: ranking determinista,
   `action_path="fallback"`.

Nunca se elige al azar. El resultado del grafo incluye `action`, `reasoning`,
`alternatives`, `action_path` y diagnósticos de los intentos.
Los fallos de infraestructura siguen el protocolo separado de la sección
siguiente y nunca se convierten en fallback.

## Proveedores y secretos

Configuración:

- nombre del proveedor;
- nombre del modelo;
- nombre de la variable de entorno que contiene la clave primaria;
- para proveedores que lo soportan, nombre separado de la variable que
  contiene el pool de claves;
- `base_url` opcional para proveedores OpenAI-compatible con endpoint propio;
- timeout por request y presupuesto total de decisión.

El adapter de producción usa la inicialización por `model_provider` y `model`
de LangChain; la integración concreta del proveedor debe estar instalada. La
configuración guarda solo el nombre de la variable. La clave se resuelve en
memoria al construir el adapter y nunca se escribe en código, logs, prompts o
PostgreSQL. Un proveedor desconocido, una integración ausente o una variable
sin valor falla antes de jugar con un mensaje accionable.

La suite usa `FakeDecisionProvider`, capaz de devolver una secuencia programada
de respuestas o errores. No hay claves configuradas en el entorno actual; por
eso el porcentaje real de propuestas ilegales del LLM queda pendiente. Los
resultados de fakes se reportan como pruebas de control de flujo, nunca como
métrica de modelo.

### Clasificación de fallos y rotación

El protocolo del proveedor distingue tres resultados:

1. respuesta estructurada, que luego puede ser válida o inválida;
2. error de infraestructura recuperable;
3. error fatal de configuración o autenticación.

Para Gemini, `GOOGLE_API_KEY` es la clave primaria y `GOOGLE_API_KEYS` es un
pool separado por comas. No se concatenan como una sola variable conceptual:
la primaria se intenta primero y el pool aporta las siguientes. Los valores se
deduplican en memoria y nunca se imprimen.

La máquina de reintentos separa transporte de semántica:

- **429/cuota:** marca el turno como afectado por cuota, rota a la siguiente
  clave y repite exactamente el mismo prompt. No consume el reintento del
  contrato de acción.
- **5xx o timeout de red:** marca el turno como afectado por infraestructura y
  repite el mismo prompt con backoff acotado. Tampoco consume el reintento del
  contrato.
- **JSON inválido o acción ilegal:** cuenta como fallo del modelo. Recién ahí
  se construye el segundo prompt con el error.
- **401/403, proveedor mal configurado, pool agotado o presupuesto total
  vencido:** la decisión falla ruidosamente. No produce `fallback` ni una fila
  que pueda confundirse con una decisión válida del LLM.

Una rotación puede ocurrir tanto en el primer prompt como en el prompt
semántico de reintento. En ambos casos se repite el pedido que estaba en curso;
jamás vuelve al prompt anterior ni avanza el contador semántico.

### Cadena entre proveedores

La rotación de claves y la cadena de proveedores son dos ejes distintos:

- **rotación intraproveedor:** otra credencial del mismo modelo/proveedor;
- **fallback interproveedor:** otro proveedor/modelo, por ejemplo Gemini →
  Kimi → OpenCode Zen.

Ambos responden a fallos de infraestructura y repiten el mismo pedido
semántico. Ninguno incrementa los contadores de JSON/acción inválida ni produce
`action_path="fallback"`. La cadena usa configuraciones completas, por lo que
cada eslabón conserva proveedor, modelo, variable de clave, pool opcional y
`base_url` opcional.

Los nombres reales de entorno son:

- Gemini: `GOOGLE_API_KEY`, pool `GOOGLE_API_KEYS`, endpoint nativo;
- Kimi: `KIMI_API_KEY`, `KIMI_BASE_URL`;
- OpenCode Zen: `OPEN_CODE_ZEN_API_KEY`, `OPEN_CODE_ZEN_BASE_URL`,
  `OPEN_CODE_ZEN_MODEL`;
- Anthropic: `ANTHROPIC_API_KEY`, endpoint nativo.

Kimi y OpenCode Zen usan un adapter OpenAI-compatible con su `base_url`.
Cambiar de proveedor incrementa un contador separado de las rotaciones de
clave. Si se agota la cadena completa, la decisión falla ruidosamente.

La cadena está permitida en partidas normales para no perder una batalla por
una cuota agotada. Está **prohibida en benchmark**: proveedor y modelo quedan
fijados al iniciar la corrida. Si ese proveedor agota sus claves o sufre un
fallo fatal, el benchmark aborta, informa cuántas batallas completó y no
publica el resultado parcial como winrate comparable.

### Presupuesto temporal medido

Una sonda real contra `127.0.0.1:8100`, con `/timer on`, produjo:

```text
|inactive|Time left: 300 sec this turn | 300 sec total | 60 sec grace
```

El presupuesto total por decisión será configurable y tendrá default de 240
segundos; el límite de Showdown tendrá default medido de 300 segundos. La carga
de configuración exige `decision_budget < showdown_turn_limit`. Cada request y
backoff usa el deadline total restante, de modo que una cadena de rotaciones no
puede extenderse indefinidamente. Los 60 segundos restantes absorben envío de
la orden y scheduling local.

### Contadores consultables

`DecisionMetrics` mantiene contadores monotónicos y expone un snapshot:

- turnos totales;
- turnos con respuesta válida al primer intento;
- turnos con JSON o acción inválida en el primer intento;
- turnos recuperados por el reintento semántico;
- turnos terminados en fallback;
- turnos afectados por 429;
- turnos afectados por 5xx o timeout;
- rotaciones de clave;
- cambios de proveedor;
- fallos fatales por pool agotado o presupuesto.

Los contadores “turnos afectados” cuentan cada turno una sola vez por clase,
aunque haya varios 429 o timeouts; `rotaciones de clave` sí cuenta cada evento.
El runner y el benchmark imprimen el snapshot al terminar. Así puede
distinguirse una corrida limpia de una que sobrevivió mediante rotaciones sin
contaminar `action_path`.

## Persistencia

`action_source` conserva autoría:

- `agent`: cualquier decisión producida por el grafo;
- `human`: futura intervención de UI;
- `opponent`: rival.

Una migración aditiva agrega:

```sql
action_path text NULL
  CHECK (action_path IN ('llm', 'llm_retry', 'fallback'))
```

`NULL` significa “sin camino interno registrado”: aplica a las filas históricas
elegidas al azar. No se usa default porque atribuirles un camino sería falso.

`text + CHECK` es un desvío deliberado del uso habitual de enums nativos del
proyecto. Este eje crecerá con nuevas etapas del grafo y PostgreSQL no permite
quitar valores de un enum sin recrearlo. La excepción no constituye un
precedente general para reemplazar los demás enums.

Las métricas se calculan sobre filas con `action_source='agent'` y
`action_path IS NOT NULL`:

- porcentaje de primer intento ilegal;
- porcentaje recuperado por reintento;
- porcentaje que llegó al fallback.

## Integración con `LudexPlayer`

Este es el último paso y queda bloqueado hasta que `showdown/client.py` esté
libre.

`choose_move` seguirá siendo una función síncrona al entrar. Antes de devolver
un awaitable:

1. ejecuta `serialize_battle(battle)`;
2. captura `legal_actions`;
3. construye el mapa exacto entre cada acción normalizada y su `BattleOrder`;
4. reserva el paso y sus metadatos de D22.

El awaitable ejecuta el grafo usando solo esas capturas, resuelve la acción
contra el mapa y completa `action_taken`/`action_path`. No relee el `Battle`
mutable después del primer `await`.

No se modifica `_find_action_line`, `_correct_step_turns` ni sus reglas para
acciones impedidas, Encore, final repentino o Illusion. La persistencia sigue
guardando `action_source="agent"` y agrega el `action_path` del paso.

## Benchmark reusable

El comando de benchmark:

- no persiste batallas por default;
- con `--persist`, usa el mismo camino de persistencia del runner actual para
  guardar batallas y trayectorias;
- permite elegir política actual, rival, generación/formato, `n` y
  concurrencia;
- crea procesos/jugadores aislados por matchup para no acumular recorders;
- reporta wins, losses, ties, winrate y Wilson 95%;
- puede emitir JSON para comparar corridas.

Es la misma herramienta que crecerá en la fase 7 para escribir la fila
agregada de `evals` cuando esa tabla exista; no se creará un segundo runner.
El opt-in evita contaminar el dataset normal con partidas contra heurísticos.
Los tres resultados de línea de base quedan en un artefacto versionado junto al
comando y en el reporte final.

## Pruebas y aceptación

- Tests unitarios RED/GREEN para lista blanca del prompt, cliente calc, ranking
  de KO/overkill, normalización de `mega=false`, reintento y fallback.
- Test obligatorio: dos respuestas ilegales del fake producen exactamente la
  acción del ranking determinista y `action_path="fallback"`.
- Test de cambio forzado: elige el reemplazo con menor peor daño esperado.
- Test de migración/modelo/repositorio: `action_path` nullable, CHECK real y
  persistencia independiente de `action_source`.
- Integración contra `127.0.0.1:8200` con una generación pasada como parámetro.
- Tras liberar `client.py`: batallas completas con el grafo y proveedor falso,
  sin crashes, verificando fotografía anterior al primer await y acción dentro
  de su máscara.
- Auditoría sobre todo el dataset: cero acciones fuera de su propia máscara y
  cero filas mal etiquetadas.
- Suite existente de 95 tests y todos los tests nuevos en verde.
- Cada test nuevo se rompe deliberadamente para probar que detecta su regresión.
- `grep -ri "gen6" apps/agent/src/` sin resultados fuera de configuración.

## Límites conocidos

- Sin una clave real no se puede medir winrate ni tasa de ilegalidad de un LLM.
  El cableado, fakes y consultas quedan listos; esas métricas se reportan como
  pendientes, no como cero.
- El fallback de cambio forzado degrada al primer switch solo cuando no hay
  movimientos rivales revelados o ningún cálculo utilizable.
- Calc usa únicamente información observable. Los movimientos no revelados y
  sets desconocidos no se inventan ni se obtienen de data oculta.
- **Corrección F2-02 / D34:** la afirmación anterior de que esos errores no
  llegaban por el canal de batalla era falsa. Se reprodujeron ambos en frames
  reales y el corpus ya contenía `[Unavailable choice]` en `battle_turns`.
  `_discard_last_step` fue retirado: una máquina de estados correlaciona el
  `|error|` con `battle_tag`, request/frame y `/choose` outbound; los retries
  reemplazan el mismo slot y sólo la acción resuelta entra al dataset. Los
  errores auxiliares de room, como `/undo` → `nothing to cancel`, conservan el
  pending sin retry, y los casos desconocidos fallan cerrado.
