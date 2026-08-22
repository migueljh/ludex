# Traspaso a tech lead

Este documento asume que ya leíste `PLAN.md`, `DECISIONS.md` y `AGENTS.md`.
No repite nada de eso. Lo que hay acá es lo que **no** está escrito en
ningún otro lado: en qué estado real está el proyecto, qué se midió y qué
se supuso, qué problemas están abiertos, y qué trampas ya nos costaron
caro. Fecha de corte: 2026-07-28.

---

## 1. Dónde estamos de verdad

Fases 0 y 1 cerradas. **Fase 2 está sustancialmente completa pero no
declarada cerrada**, y esa declaración es una de las primeras decisiones
que te tocan (ver §6).

Volumen actual en Postgres:

| tabla | filas |
|---|---:|
| `battles` | 502 |
| `battle_turns` | 28.950 |
| `trajectories` | 501 |
| `trajectory_steps` | 30.930 |
| `pokemon` (todas las gens sembradas) | 1.708 |
| `learnsets` | 127.840 |

149 commits, 200 tests, 30 decisiones registradas (última: D30).

Lo que existe y funciona:

- **Grabador**: el agente juega contra el server local de Showdown y
  persiste protocolo crudo por turno + estado derivado + acciones legales.
- **Grafo de decisión** (LangGraph): nodos de cálculo de daño y decisión,
  con rotación de claves, respaldo determinista, y validación de legalidad.
- **Servicio de cálculo** (`packages/calc`, @smogon/calc por HTTP).
- **Auditor de corpus** (`packages/dataset-audit`) y validador de equipos
  (`packages/teams`).
- **Runner de benchmarks** con costo real por usage y reportes en
  `apps/agent/evals/runs/*.json`.

Lo que NO existe todavía: nada de fase 3 en adelante. No hay FastAPI, ni
WebSocket propio, ni aprobación humana, ni conexión al Showdown oficial, ni
web, ni torneo, ni análisis.

---

## 2. El principio que gobierna todo lo hecho hasta acá

> **El entregable no es que juegue bien, es que grabe bien.**

Casi todo lo raro que vas a ver en el código sale de esto. Un dataset
corrupto obliga a regrabar todo y a tirar cualquier modelo entrenado con
él; que el agente juegue mal es reversible con un prompt distinto. Por eso
la fase 2 atacó primero el serializador de estado y la persistencia, con el
agente más tonto posible (elegía al azar), y recién después metimos un LLM.

Los cuatro invariantes que el dataset tiene que cumplir, cada uno con test:

1. **Cero fuga de información oculta.** Para cada turno *N*, ningún pokémon
   del rival puede estar en el estado si el protocolo no lo reveló hasta
   ese turno. El protocolo persistido es el juez.
2. **La acción tomada está dentro de su propia `legal_actions`.**
3. **Una fila pertenece al turno en el que su decisión se resolvió**, no al
   turno en el que se pidió.
4. **Una fila por decisión** (`decision_index`), no por turno.

Los tres primeros ya fallaron en producción y se arreglaron. Ver §5.

---

## 3. Hechos medidos que no son obvios

Están medidos, no supuestos. Si algo acá te suena raro, el experimento está
descrito en `.claude/agent-recording/SKILL.md`.

**Sobre el protocolo de Showdown:**

- El `|request|` llega **ya resuelto** antes de la narración del turno.
- **La narración es la respuesta del servidor a tu elección**, no un evento
  que venga solo. Experimento causal: demorar la respuesta 500 ms demora la
  narración exactamente 500 ms. Consecuencia práctica: *esperar la
  narración antes de responder es un deadlock*. Yo mismo saqué la
  conclusión inversa al principio y me equivoqué.
- `|turn|N` cierra el bloque anterior, no abre el siguiente.
- poke-env despacha una task por mensaje (`asyncio.create_task`).
- **El reloj de Showdown es opt-in**: si ningún jugador lo activa, un turno
  puede durar indefinidamente. Esto habilita modelos lentos que de otro
  modo quedarían descartados.

**Sobre las tres/cuatro formas en que Showdown narra que una acción no se
ejecutó:** `|cant|`, un `|faint|` propio sin `|move|` previo, y el
autogolpe por confusión (`|-activate|...|confusion`). Más Encore del rival,
que fuerza repetir otro movimiento. Y el caso aparte de Illusion, donde el
switch se narra con el nombre de otro pokémon y la revelación puede tardar
14 turnos.

**Sobre el juego:**

| agente | winrate vs SimpleHeuristics | n |
|---|---|---:|
| aleatorio (baseline frío) | 3,00% [1,59 – 5,60] | 300 |
| DeepSeek V4 Flash | 26,67% [10,90 – 51,95] | 15 |
| MiMo V2.5 (gratuito) | 40,00% [11,76 – 76,93] | 5 |

Los intervalos son Wilson 95%. El LLM le gana al baseline con separación
limpia de intervalos, que era la pregunta de la fase 2. Los tamaños de
muestra son chicos: sirven para responder "¿el LLM aporta algo?", no para
rankear modelos entre sí.

Costo: DeepSeek sale USD 0,028 por batalla → **USD 282 cada 10.000
batallas**. MiMo es gratis pero comete 21,9% de acciones ilegales que hay
que reintentar (DeepSeek: 0,19%).

---

## 4. Problemas abiertos, por orden de importancia

**1. Fallos de transporte con algunos proveedores.** Kimi abortó dos
corridas de 10 batallas sin completar ninguna. Gemini quemó sus 11 claves
en 2 batallas — esa causa **sí** está encontrada y arreglada (D30: las
claves con 429 ahora se enfrían en vez de descartarse para siempre; el
límite de Gemini es por minuto, no diario). La causa de Kimi sigue sin
identificar. La hipótesis de fuga de conexiones está **descartada con
medición**: 300 llamadas del backend real contra un servidor local dieron 1
conexión TCP y 0 errores (ver `BENCHMARKS.md`). Lo que sí cambió: el tipo
de excepción original ahora sobrevive a la clasificación, así que el
próximo aborto va a decir si fue `ConnectError`, `ReadTimeout` o
`PoolTimeout`. **Diagnóstico destrabado, no resuelto.**

**2. El test de fuga de información no escala.** Tarda 178 s sobre 484
batallas por consultas N+1, y crece linealmente con un dataset pensado para
crecer 100×. Hay que vectorizarlo. Es el test más importante del proyecto y
va camino a volverse impagable de correr.

**3. Dos decisiones de alcance de la fase 2 sin registrar**: el nodo
`retrieve_context` quedó fuera de alcance, y el switch de modelos vive en
configuración en vez de en tablas `providers`/`models`. Ninguna está en
`DECISIONS.md`.

**4. Falta escribir en `DECISIONS.md`** la regla de que el chat de la
batalla nunca entra al prompt, y dónde vive la validación de legalidad.

**5. Limpieza**: el worktree `.worktrees/feat-agent` está mergeado y sin
cambios propios, y quedó un contenedor huérfano `feat-agent-postgres-1`.

**6. Un detalle menor de calidad**: en `KeyRotatingProvider` el reloj
inyectable convive con `time.monotonic()` en la comparación del deadline.
En producción son el mismo reloj, así que no muerde; puede confundir a
quien escriba un test futuro con reloj falso.

---

## 5. Trampas que ya pagamos (lee esto antes de tocar el grabador)

Cada una de estas fue un defecto **silencioso**: el sistema no se caía, sólo
grababa mal.

- **Materialización diferida.** El snapshot del estado se armaba en una
  task de fondo; para cuando corría, la batalla podía estar en la decisión
  siguiente. 7 de 6.625 filas tenían la acción fuera de su propia máscara.
  Se arregló haciendo el snapshot sincrónico dentro de `choose_move`.
- **`save_step` hacía upsert por `(trajectory_id, turn_number)`**, pero los
  cambios forzados no avanzan el turno. Se perdieron 265 de 1.684 cambios,
  y —lo peor— **con sesgo**: eran sistemáticamente los reemplazos después
  de un debilitamiento. Se arregló con `decision_index` como parte de la
  clave.
- **Un test normalizaba Hidden Power y el código no.** El test perdonaba
  exactamente lo que el código seguía haciendo mal.
- **El puerto 5433 tenía un Postgres de Homebrew** que una vez tapó el
  puerto y casi hace que el pipeline escribiera en la base equivocada **sin
  fallar**.
- **Se mergeó con 5 corridas en verde** cuando un agente ya había avisado
  que un test era intermitente. Falló en la primera corrida post-merge.

La regla que salió de todo esto y que está en `AGENTS.md`: *un test que
puede pasar sin ejercer lo que dice ejercer es peor que no tenerlo*, y
*verificá que un test nuevo detecta la regresión* (revertí el arreglo y
mirá que falle).

---

## 6. Decisiones que te tocan a vos

**a) ¿Se declara cerrada la fase 2?** Mi lectura: sí. La pregunta de la
fase era si el grafo con LLM juega mejor que el baseline, y los intervalos
de Wilson no se superponen (10,90% contra 5,60%). El criterio informal que
veníamos usando era "5 de 15 victorias"; con 4 de 15 la separación
estadística ya existe. Moverlo a 5 sería correr el arco después del tiro.
Contra: las muestras son chicas y dos proveedores todavía abortan.

**b) ¿Qué modelo se adopta?** DeepSeek es el más confiable medido (0,19%
ilegales) pero cuesta USD 282 por 10.000 batallas. MiMo es gratis y ganó
más en 5 batallas, pero con 21,9% de acciones ilegales. La muestra no
alcanza para decidir por winrate; probablemente haya que decidir por costo
y confiabilidad, y volver a medir con n más grande.

**c) Fase 3 necesita una cuenta de testing de Showdown, no la cuenta real
del torneo** (D65, `docs/superpowers/specs/2026-08-22-phase-3-design.md`
§6.3, §11 y §12). Jugar oficial con la cuenta real queda prohibido durante
Fase 3 — el riesgo de baneo lo paga el torneo. El equipo exportado del
usuario no es un blocker de Fase 3: pertenece a Fase 5 (equipos de
torneo). Fase 3 sigue usando `gen6randombattle`, donde el server genera
los equipos, igual que la fase 2.

**d) El orden acordado era 3 → 4 → 5**, pero nunca se validó contra
dependencias reales.

---

## 7. Cómo se viene trabajando

- **Varios agentes en paralelo sobre territorio disjunto** (Claude con
  subagentes, Kimi, GPT), cada uno con directorios declarados, y revisión
  de todo antes de mergear. Los choques que tuvimos —una rama pisada, dos
  commits que se llevaron trabajo ajeno— salieron de un plan que asumía un
  solo escritor.
- **Commits en inglés**, con rutas explícitas (`git commit -m "..." --
  <rutas>`), nunca `git add -A`.
- Hay skills del proyecto en `.claude/`; `agent-recording` condensa todo lo
  aprendido sobre el grabador y conviene leerla antes de tocar
  `showdown/client.py`, que es el archivo más peligroso del repo.
- La regla que más rinde: **inspeccionar antes de diseñar**. Las fases que
  salieron bien empezaron leyendo el código de `pokemon-showdown` o de
  `poke-env`; los errores más caros salieron de suponer un esquema. Cinco
  veces le pasé a un agente un nombre de tabla o columna equivocado y las
  cinco las cazó porque frenó a preguntar en vez de adaptarse.

## 8. Restricciones operativas de la máquina

No son negociables y no están todas en el repo:

- **Nunca** `docker compose down`, `down -v`, `docker stop`, `docker rm` ni
  `brew services stop`: hay contenedores de **otro proyecto** (`jets`)
  corriendo hace días en la misma máquina.
- Postgres de Ludex en `127.0.0.1:15432` (5432 es de `jets`, 5433 es un
  Homebrew que ya causó un incidente). Showdown local en `8100`, calc en
  `8200`.
- `.env` está en `.gitignore` y nunca se commiteó (verificado: 0 commits lo
  tocaron). Las claves no se pegan en un chat ni se imprimen; la base
  guarda el **nombre** de la variable, nunca el valor.
