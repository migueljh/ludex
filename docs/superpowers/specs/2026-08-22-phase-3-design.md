# Fase 3 — Diseño aprobado de API, juego oficial y aprobación humana

**Estado:** aprobado por el usuario y por el tech lead
**Baseline:** `1ad425aa38f3abd29f14b269d65cc4cbe0b85f96` (`main` limpio)
**Formato de aceptación:** `gen6randombattle`
**Revisiones:** Opus 5 R1/R2 + Tasos/Grok 4.6 R1/final (`PASS`)
**Fecha:** 2026-08-22

Este documento es el contrato vigente para implementar Fase 3. Reemplaza los
detalles incompatibles de `docs/PLAN.md` §6 sobre `interrupt()` y
checkpointing; la implementación debe registrar esa desviación en
`docs/DECISIONS.md` y añadir una nota puntual al PLAN antes de S1.

La fase agrega una API local, eventos en vivo, aprobación humana opcional,
modo autónomo y conexión al servidor oficial de Pokémon Showdown. No incluye
la UI completa, equipos de torneo, perfiles de rivales, coaching ni
entrenamiento de modelos.

---

## 1. Resultado de producto

Ludex podrá:

1. Conectarse a Showdown local u oficial.
2. Recibir y enviar challenges sin aceptar ninguno automáticamente.
3. Buscar partidas de ladder de forma explícita, secuencial y limitada.
4. Publicar por WebSocket el estado observable y la recomendación del agente.
5. Esperar aprobación, aceptar un override legal o ejecutar automáticamente la
   recomendación al vencer el plazo.
6. Jugar sin intervención en modo autónomo.
7. Persistir la autoría y la autorización de cada acción sin atribuir al modelo
   una jugada humana.

### 1.1 Modos

- `local`: usa el Showdown local y saltea siempre el gate humano en producción.
- `official`: usa el servidor oficial y permite `hitl` o `autonomous`.
- `hitl`: publica la propuesta y espera hasta 10 segundos por defecto.
- `autonomous`: ejecuta la recomendación sin crear un gate.

La política de aprobación es inyectable. Los tests pueden habilitar HITL
contra Showdown local, pero `run`, `benchmark` y `matrix-run` nunca deben
bloquear por un setting de HITL.

### 1.2 Semántica del plazo

- Si el operador aprueba: se ejecuta la propuesta (`human_approved`).
- Si la reemplaza: se valida contra la misma máscara y se ejecuta el override
  (`human_override`).
- Si no responde: se ejecuta la propuesta (`timeout_auto`).
- Si el navegador se desconecta: el plazo continúa y termina igual.
- Si se cambia a autónomo con un gate pendiente: la propuesta se ejecuta de
  inmediato como `timeout_auto`, con `resolved_by=system` y
  `resolved_reason=autonomous_toggle`.
- Una acción ilegal devuelve un error tipado y no consume el gate.
- Nunca se reabre una decisión ni se emite un segundo `/choose`.

---

## 2. Alcance

### 2.1 Incluido

- FastAPI y WebSockets, ligados exclusivamente a `127.0.0.1`.
- API de salud, settings, providers/modelos, conexión, challenges, sesiones,
  batallas y decisiones pendientes.
- Eventos de lobby y batalla con replay acotado por secuencia.
- Gate HITL cross-loop, exact-once y testeable con reloj falso.
- Persistencia de propuestas y outcomes.
- Conexión oficial, login con watchdog y cuenta de testing.
- Challenges entrantes/salientes con aceptación explícita.
- Ladder `gen6randombattle`, apagado por defecto y con triple interlock.
- Sesiones secuenciales con `N` configurable y `stop-after-current`.
- `replay_url`, rating/`elo_bucket` cuando exista e identidad del rival como
  ganchos estrechos para Fase 6.
- Una aceptación live de challenge y otra independiente de ladder.

### 2.2 Fuera de alcance

- Aplicación web y viewer completos (Fase 4).
- Equipos custom, rondas y operación del torneo (Fase 5).
- Perfiles canónicos de rivales, importación de replays, coaching y análisis
  del estilo manual del usuario (Fase 6).
- Evals, entrenamiento y playbook (Fase 7).
- Recuperar una batalla viva tras reiniciar el proceso.
- Auto-accept de challenges.
- Ladder con la cuenta real del torneo.

---

## 3. Decisiones arquitectónicas vinculantes

### 3.1 El gate vive fuera de LangGraph

El punto de inserción es dentro de `run_graph`, exactamente entre:

```text
decision_graph.ainvoke(...)
        ↓
approval_gate.await_resolution(...)
        ↓
execute_action(action, action_orders)
```

El grafo conserva sus cinco nodos, su `compile()` actual y la resolución de
provider por invocación. No se modifican `graph/workflow.py`,
`graph/state.py`, `graph/decision.py` ni `graph/execute.py` para implementar el
gate.

`interrupt()` no se usa como gate. Un checkpointer es una rebanada ortogonal y
descartable: si obliga a cambiar D39 o la semántica del grafo, se elimina de
Fase 3.

Motivo: poke-env necesita devolver un `BattleOrder` en la corrutina viva de
`choose_move`; un checkpoint no conserva socket, lock, ventana del servidor ni
el mapa de objetos `BattleOrder`. Prometer recuperación de la batalla tras un
reinicio sería falso.

### 3.2 El mapa de acciones sigue siendo la frontera de seguridad

La máscara legal y el mapa `acción → BattleOrder` se capturan sincrónicamente
antes del primer `await`. Tanto la propuesta como un override se validan contra
ese mismo mapa. Por construcción:

```text
action_taken ∈ legal_actions
```

El gate no relee el objeto `Battle` después de la decisión y no modifica
`PendingChoice` ni su máquina D34.

### 3.3 Un proceso, dos loops, ownership explícito

```text
FastAPI loop
  REST / WebSocket / EventHub / ApprovalRegistry
                   │
                   │ concurrent.futures.Future (CAS)
                   ▼
POKE_LOOP
  LudexPlayer → run_graph → gate → execute_action
  PendingDecisionRepository (único escritor)
```

- El API resuelve el CAS pero no escribe `pending_decisions`.
- El estado vivo se lee del registry en memoria.
- Las lecturas históricas usan un engine propio del loop de FastAPI.
- El repositorio del listener crea su engine perezosamente dentro de
  `POKE_LOOP`, con `NullPool`, siguiendo D32.
- Compartir un `AsyncEngine` entre loops está prohibido y tiene canario.

Agent y API corren en el host. Fase 3 no amplía `docker-compose.yml`.

---

## 4. Gate exact-once

Cada intento usa una clave única:

```text
(battle_tag, decision_index, attempt_index)
```

`PendingApproval` posee un `concurrent.futures.Future`. El primer
`set_result()` gana; el segundo recibe `InvalidStateError` y recupera el
outcome ganador. El override se valida antes del CAS, para que una acción
inválida no cierre la decisión.

### 4.1 Regla de reloj

El waiter usa únicamente el `clock()` inyectado de D42:

```text
gate_start = clock()
approval_deadline = min(gate_start + approval_timeout, decision_deadline)

while Future sigue pending:
    si clock() >= approval_deadline:
        CAS(timeout_auto, proposed_action)
    si no:
        await tick()
```

Está prohibido aplicar al Future del CAS, directa o indirectamente:

- `asyncio.wait_for`;
- `asyncio.timeout`;
- `asyncio.wrap_future` bajo un timeout;
- `cancel()`;
- `shield` como sustituto del reloj inyectado.

La razón está verificada en CPython 3.12: `wait_for(wrap_future(fut))`
cancela el Future fuente y vuelve imposible escribir `timeout_auto`.

Producción usa un tick de 50 ms; tests usan un tick falso o
`asyncio.sleep(0)`. El test de timeout debe demostrar que el Future estuvo
`pending` antes de resolverse.

### 4.2 Presupuestos iniciales

Fase 3 fija estos defaults, sujetos a configuración:

| presupuesto | default |
|---|---:|
| decisión del modelo | 240 s |
| aprobación humana | 10 s |
| margen de envío | 5 s |
| timeout/inactividad de batalla | 300 s |
| fallback del turno sin `|inactive|` | 300 s |
| watchdog de login | 15 s |

Debe cumplirse `240 + 10 + 5 < 300`. En juego oficial, el timeout de batalla
es de inactividad; en benchmark/matrix permanece como timeout de pared.

Cada `|inactive|` puede acortar el deadline:

```text
deadline = min(deadline, clock() + segundos_anunciados - margen_de_envío)
```

Nunca puede extenderlo y nunca mezcla `clock()` con `loop.time()` o
`time.monotonic()`. Cada batalla oficial activa el timer del servidor al
comenzar; `LUDEX_SHOWDOWN_TURN_LIMIT_SECONDS` es el fallback si el servidor no
anuncia countdown.

### 4.3 Shutdown e invalid choices

- Shutdown resuelve gates abiertos como `aborted` y no crea un step.
- Un rechazo de Showdown invalida el intento, lo marca `superseded`, incrementa
  `attempt_index` y crea una ronda con una máscara nueva.
- Una aprobación tardía con un `attempt_index` viejo recibe `409
  STALE_ATTEMPT`.
- Sólo la acción finalmente resuelta entra a `trajectory_steps`.

---

## 5. Persistencia y dataset

### 5.1 Tabla de auditoría

Se agrega `pending_decisions` con, como mínimo:

- identidad: `battle_tag`, `decision_index`, `attempt_index` y unicidad del
  triple;
- estado: `awaiting`, `human_approved`, `human_override`, `timeout_auto`,
  `superseded` o `aborted`;
- propuesta completa: acción, rationale, confidence, alternatives, provider,
  model y usage D38;
- resolución: acción final, `resolved_by`, `resolved_reason`, timestamps y
  `approval_wait_ms`;
- `approval_wait_ms` como tercera población, separada de
  `decision_latency_ms` y `completion_latency_ms`.

La fila `awaiting` se persiste antes de publicar la propuesta por WebSocket.
El Future sigue siendo la fuente de verdad de `/choose`; la tabla es auditoría.
Al iniciar, un sweep cambia filas huérfanas `awaiting` a
`aborted/process_restart`.

### 5.2 Tres ejes ortogonales

Se agrega `trajectory_steps.approval_outcome` (`text NULL` con `CHECK`):

| outcome | action_source | metadata D38 |
|---|---|---|
| `human_approved` | `agent` | completa |
| `human_override` | `human` | las 11 columnas NULL como grupo |
| `timeout_auto` | `agent` | completa |
| `NULL` | `agent` | comportamiento histórico |

`action_source`, `action_path` y `approval_outcome` nunca se colapsan en un
solo eje. `played_by` permanece `bot`: describe el cliente, no quién eligió la
acción.

La propuesta descartada de un override permanece completa en
`pending_decisions`. Por ello, el costo de una batalla con overrides se calcula
con `trajectory_steps` más `pending_decisions`.

### 5.3 Training y D44

- Las filas humanas entran a `scope=training`.
- El auditor reporta mezcla `agent/human` y los tres outcomes.
- La coherencia D38 se consulta sobre toda `trajectory_steps`, nunca sólo sobre
  los tags de la corrida.
- La mezcla se prueba en Fase 3 con datos sintéticos.
- Los canarios canónicos 16/2/82 permanecen sin cambios durante Fase 3.

### 5.4 Guardarraíl de la base canónica

Fase 3 prohíbe persistir juego oficial en la base canónica. La configuración
tiene un rol explícito de DB (`canonical` o `acceptance`):

- `CONNECTION_MODE=official` exige `DATABASE_ROLE=acceptance`;
- además rechaza el DSN canónico `127.0.0.1:15432/ludex`;
- S9 usa otra base y la marca `acceptance`/`disposable`;
- no existe flag de override en Fase 3;
- el rechazo ocurre antes de abrir red o autenticar.

Habilitar juego oficial sobre el corpus canónico queda diferido a una decisión
posterior, junto con repin deliberado y pruebas reales de mezcla.

---

## 6. Challenges, ladder y conexión oficial

### 6.1 Challenges

poke-env 0.15.0 tiene dos productores de `_challenge_queue`:

- `_update_challenges`, desde `|updatechallenges|`;
- `_handle_challenge_request`, desde un PM `/challenge`.

`LudexPlayer` sobrescribe ambos. Ninguno llama al original ni encola. Los dos
publican al lobby todos los formatos. El canario de cableado verifica que
`PSClient` recibió los métodos de `LudexPlayer`.

Sólo `POST /challenges/{user}/accept` inserta explícitamente al usuario en la
cola que consume `Player.accept_challenges`. Usar directamente
`PSClient.accept_challenge` está prohibido porque saltea la contabilidad de
poke-env. No existe auto-accept por usuario, formato o lista blanca.

### 6.2 Ladder

Ladder es una máquina independiente de challenges y tiene aceptación propia.
Está apagado por defecto y exige simultáneamente:

1. `connection_mode=official`;
2. `ladder_enabled=true` en settings;
3. `confirm=true` en la solicitud de sesión;
4. `testing_account_confirmed=true`;
5. `DATABASE_ROLE=acceptance` y DB no canónica.

Si falta una condición, se rechaza antes de abrir socket o enviar `/search`.
Sólo se usa una cuenta de testing; la cuenta del torneo queda fuera de Fase 3.

### 6.3 Login y secretos

- Username y password salen del entorno; la contraseña nunca entra al
  dataclass `Settings`, tabla `settings`, logs, eventos o artefactos.
- El watchdog observa `logged_in` y publica un fallo tipado si la task
  fire-and-forget de poke-env pierde el error de login.
- Login siempre ocurre antes de cualquier `choose_move`.
- El observador outbound continúa limitado a salas `battle-`; un canario
  impide persistir `/trn` o su assertion.
- API y WebSockets validan loopback y `Origin`.

---

## 7. API y eventos

### 7.1 REST mínimo

- `GET /health`
- `GET /settings`
- `PATCH /settings/model`
- `PATCH /settings/hitl`
- `GET /providers` y `GET /models`
- `GET /connection`, `POST /connection/connect`, `POST /connection/disconnect`
- `GET /challenges`
- `POST /challenges/{user}/accept`, `/reject` y `/outgoing`
- `POST /sessions`, `DELETE /sessions/{id}`
- `GET /battles`, `/battles/{tag}`, `/battles/{tag}/pending`
- `POST /battles/{tag}/decisions/{index}/approve`
- `POST /battles/{tag}/decisions/{index}/override`

La sesión es secuencial, tiene `N` configurable y al cancelarse termina la
batalla actual antes de parar.

### 7.2 WebSockets

`/ws/battle/{battle_tag}` publica:

- `hello`;
- `protocol` desde el inbox raw pre-lock;
- `decision_proposed`;
- `decision_resolved`;
- `decision_invalidated`;
- `battle_ended`;
- errores saneados.

`/ws/lobby` publica conexión, challenges, batallas y estado de sesión.

Cada stream usa `seq` monótono y un ring buffer. `resume(last_seq)` devuelve el
sufijo exacto; si ya rotó responde `REPLAY_GAP` y obliga a recargar el estado.
La UI nunca lee `Battle` ni `ProtocolRecorder` mientras la decisión sostiene
el lock; usa exclusivamente el inbox pre-lock y el estado proyectado D31.

---

## 8. Fallos y límites honestos

- Caída del navegador: la batalla continúa y el gate vence normalmente.
- Caída del WebSocket a Showdown: fallo cerrado, batalla incompleta y sin
  trayectoria parcial.
- Reinicio del proceso: la batalla viva se pierde; las filas pendientes pasan
  a `aborted`.
- Proyección ambigua o vencida: no hay gate, acción ni fila.
- Login fallido: error tipado dentro del watchdog, nunca espera infinita.
- La UI puede reconectarse; la batalla de Showdown no puede reanudarse tras
  reiniciar el proceso.

---

## 9. Rebanadas de implementación

La entrada de `DECISIONS.md`, la nota puntual de `PLAN.md` §6 y la corrección
de `HANDOFF_CLAUDE.md` §6.c se realizan antes de S1. El handoff debe indicar
que Fase 3 requiere cuenta de testing y que el equipo exportado pertenece a
Fase 5.

| rebanada | contenido |
|---|---|
| S0 | documentación vinculante, configuración, presupuestos, guardarraíl DB, gate y política puros, eventos/replay |
| S1 | gate integrado entre `ainvoke` y `execute_action`, testeado localmente mediante política inyectable |
| S2 | migraciones, repositorio D32, tres ejes y coherencia D38 |
| S3 | FastAPI y WebSockets en loopback |
| S4 | conexión oficial, cuenta testing, watchdog, sessions/challenges salientes |
| S5 | ambos productores de challenges y aceptación explícita |
| S6 | ladder con interlocks y rechazo pre-red |
| S7 | ganchos estrechos para replay, rating e identidad |
| S8 | checkpointer opcional; descartar si toca D39 |
| S9a | aceptación live de challenge en DB descartable |
| S9b | aceptación live independiente de ladder en DB descartable |

Cada rebanada usa TDD, mutación deliberada, REVIEW PACKET completo y revisión
independiente. Ningún implementador se autoaprueba.

---

## 10. Canarios obligatorios

Como mínimo, la implementación debe defender:

1. CAS: un ganador y `AlreadyResolved` devuelve ese ganador.
2. Timeout con reloj falso: Future no cancelado y observado `pending`.
3. Prohibición de `wait_for`, `timeout`, `cancel` y `wrap_future` en el CAS.
4. Override ilegal no consume el gate ni llama `execute_action`.
5. Override legal usa exactamente un `BattleOrder` de la máscara capturada.
6. Modo skip no crea Future; `run`, `benchmark` y `matrix-run` no bloquean.
7. Toggle autónomo produce un único `/choose` y no reabre.
8. Replay WS exacto o `REPLAY_GAP`, nunca backlog parcial.
9. Señuelo de password ausente de eventos, logs y filas; `/trn` no grabado.
10. Ambos callbacks de challenge pertenecen a `LudexPlayer`.
11. Ni `|updatechallenges|` ni PM `/challenge` encolan automáticamente.
12. Challenges de otro formato siguen visibles en lobby.
13. Sólo el endpoint explícito de accept encola.
14. Lobby/chat nunca completa una ventana D31.
15. Exactamente un `/choose` por intento.
16. Rechazo de override crea intento nuevo con máscara nueva.
17. Watchdog vuelve ruidoso un login inválido y no hay login durante decisión.
18. `|inactive|` sólo acorta el deadline con el reloj D42.
19. La fila durable existe antes del evento WS.
20. Cruce de loop del repositorio falla de forma tipada.
21. Sweep elimina todo `awaiting` huérfano.
22. Matriz `action_source`/`approval_outcome` correcta.
23. Cero filas globales `human` con provider no nulo; D38 NULL como grupo.
24. Shutdown con gate abierto termina acotado y sin step.
25. Migraciones verificadas up/down en DB descartable.
26. Auditor reporta mezcla con datos sintéticos.
27. Canarios 16/2/82 permanecen verdes.
28. `official` contra `127.0.0.1:15432/ludex` falla antes de la red.
29. DB de aceptación exige marca explícita y DSN no canónico.
30. Ladder exige todos los interlocks y un test de cero llamadas al faltar uno.

Cada canario nuevo debe ponerse rojo mediante una mutación deliberada. Los
tests del dataset corren sobre el dataset completo. Las mutaciones en copias
deben pinear `PYTHONPATH` según `.claude/verification/SKILL.md`.

---

## 11. Aceptación final de Fase 3

### 11.1 Challenge oficial

- `gen6randombattle`, cuenta de testing y DB marcada `acceptance`.
- Challenge entrante aceptado explícitamente desde la API.
- Protocolo y propuestas visibles mediante un cliente WebSocket de prueba.
- Al menos un `human_approved`, un `human_override` y un `timeout_auto`.
- `source=challenge`, `played_by=bot` y cero incoherencias D38.
- Evidencia saneada sin rutas absolutas, secretos, assertions ni trazas crudas.

### 11.2 Ladder oficial

- Una partida ranked `gen6randombattle`, separada de la anterior.
- Cuenta de testing, triple interlock manual y DB descartable.
- Al menos un `human_approved` o `timeout_auto`.
- `source=ladder`; rating/`elo_bucket` sólo si el protocolo lo aporta.
- Ladder vuelve a quedar apagado al finalizar.

Estas ejecuciones live requieren aprobación operativa del tech lead, no
autorizan modelos fuera de las restricciones de `AGENTS.md`, no compran
créditos y no usan la cuenta real del torneo.

### 11.3 Gate de cierre

Fase 3 sólo puede cerrarse con:

- todas las suites proporcionales y completas offline en verde;
- cero skips o fallos inexplicados;
- migraciones up/down verificadas en DB descartable;
- challenge y ladder live independientes;
- auditor sobre ambos scopes y queries directas de invariantes;
- evidencia saneada y REVIEW PACKET completo;
- revisión independiente del rango exacto;
- veredicto final del tech lead sobre el SHA integrado.

---

## 12. Decisiones que no se reabren durante la implementación

- Gate fuera de LangGraph.
- `interrupt()` no es el mecanismo HITL.
- Checkpointer opcional y descartable.
- Outcomes exactos: `human_approved`, `human_override`, `timeout_auto`.
- Toggle autónomo resuelve inmediatamente la propuesta pendiente.
- Challenges nunca se aceptan automáticamente.
- Ladder forma parte de Fase 3 y tiene aceptación propia.
- Cuenta de testing obligatoria; cuenta del torneo prohibida en Fase 3.
- Juego oficial prohibido contra la DB canónica durante Fase 3.
- Filas humanas entran a training con metadata coherente.
- `played_by=bot` también en modo híbrido.
- Host para agent/API; compose no se amplía.
- Fases 4, 5 y 6 permanecen fuera de alcance.
