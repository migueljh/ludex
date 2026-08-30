# REVIEW PACKET R2 — MON-36, Fase 3 Task 6 (conexión oficial y sesiones secuenciales)

**Issue:** MON-36 — "Add official connection management and sequential sessions"
(`docs/superpowers/plans/2026-08-22-phase-3-implementation.md`, Task 6).
**Decisión asociada:** `docs/DECISIONS.md` D68.
**Worktree:** `phase3-s6-connection-ws`, branch `migueljh/phase3-s6-connection-ws`.
**Base SHA:** `a5c7de8bc2d8e64d7430dbcaf3732c4ff53c4ca4`
**Head SHA:** `8c0c276`
**Estado:** listo para `In Review`. **No marcar `Completed`** — el veredicto es
exclusivo del tech lead.

---

## Archivos modificados (`git diff --stat a5c7de8..8c0c276`)

```
 apps/agent/src/ludex_agent/api/routes.py           |  48 +++++
 apps/agent/src/ludex_agent/runner/__init__.py      |   5 + (nuevo)
 apps/agent/src/ludex_agent/runner/session.py       |  53 + (nuevo)
 apps/agent/src/ludex_agent/showdown/connection.py  | 128 + (nuevo)
 apps/agent/src/ludex_agent/showdown/lobby.py       |  57 + (nuevo)
 apps/agent/tests/runner/test_session.py            |  76 + (nuevo)
 apps/agent/tests/showdown/test_connection.py       | 180 + (nuevo)
 apps/agent/tests/showdown/test_lobby.py            |  48 + (nuevo)
 docs/DECISIONS.md                                  |  75 +
 9 files changed, 709 insertions(+)
```

Ningún archivo del `create`/`modify` list asignado quedó fuera; `cli.py` y
`showdown/client.py` (ambos en la lista de `modify` del plan) **no se
tocaron** — ver limitación al final. `app.py` y `schemas.py` no se tocaron,
tal como exigió el alcance. No se agregaron tests de API.

---

## RED antes de implementar

Los tres archivos de test nuevos fallaban en la fase de collection, sin
implementación:

```
ImportError: No module named 'ludex_agent.showdown.connection'
ImportError: No module named 'ludex_agent.showdown.lobby'
ImportError: No module named 'ludex_agent.runner'
3 errors in 0.14s
```

## GREEN después de implementar

```
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q \
  tests/showdown/test_connection.py tests/showdown/test_lobby.py tests/runner/test_session.py
...............
15 passed in 0.07s
```

Cobertura de los 15 casos:

- **Conexión mode-aware** (`test_connection.py`, 7 casos): `local` construye
  `ServerConfiguration` local; `official` con `database_role != acceptance`
  levanta `UnsafeOfficialDatabaseError` **antes** de construir cualquier
  socket; `official` con `acceptance` lo permite; el watchdog convierte un
  login que nunca resuelve en `LoginFailedError` dentro de 15s falsos (reloj
  inyectado, sin tiempo de pared real); el watchdog republica un fallo de
  background de poke-env como `LoginFailedError` tipado; `choose_move` está
  prohibido antes de que el login resuelva; la contraseña nunca queda
  retenida en ningún atributo del manager salvo el `environ` inyectado.
- **Lobby inbox** (`test_lobby.py`, 4 casos): orden de publicación
  preservado; `seq` monótono; `wait_for_next` resuelve sin polling; `resume`
  sobre un cursor ya rotado levanta `ReplayGapError`.
- **Sesiones secuenciales** (`test_session.py`, 4 casos): `N` configurable
  juega una batalla a la vez; una segunda solicitud de matchmaking mientras
  hay una activa levanta `ActiveMatchmakingError`; `stop()` nunca cancela la
  batalla en curso; `stop()` impide que arranque la siguiente.

---

## Mutaciones deliberadas: RED → restaurado → GREEN

Cada mutación se aplicó **in-place** sobre el worktree real (no una copia),
se verificó RED en el canario correspondiente, se restauró **byte a byte**
y se confirmó con `sha256sum` contra el hash pre-mutación.

| # | Mutación | Archivo | Resultado RED | Restaurado (sha256) |
|---|---|---|---|---|
| 1 | Se quitó la guardia `database_role != "acceptance"` de `ConnectionManager.build_server_configuration` | `showdown/connection.py` | `Failed: DID NOT RAISE UnsafeOfficialDatabaseError` | ✅ idéntico al original |
| 2 | Se quitó el chequeo de deadline del loop de `LoginWatchdog.wait_for_login` | `showdown/connection.py` | La suite **cuelga**; matada por SIGKILL a los 10s con un wrapper de timeout duro (`exit=137`) — confirma que el mecanismo de deadline, no un mock, evita el cuelgue infinito | ✅ idéntico al original |
| 3 | Se quitó la guardia `if self._active: raise ActiveMatchmakingError` de `SessionRunner.start` | `runner/session.py` | La suite **cuelga** (dos `start()` concurrentes esperan el mismo evento que nunca se dispara sin la guardia); matada por SIGKILL a los 10s (`exit=137`) | ✅ idéntico al original |
| 4 | Se quitó el chequeo `if self._stop_requested: break` del loop de `SessionRunner.start` | `runner/session.py` | `assert [1, 1, 1, 1, 1] == [1]` — jugó las 5 batallas en vez de detenerse tras la primera | ✅ idéntico al original |
| 5 | Se quitó la guardia `last_seq < self._oldest_dropped_seq` de `LobbyInbox.resume` | `showdown/lobby.py` | `Failed: DID NOT RAISE ReplayGapError` | ✅ idéntico al original |

(El brief pedía 4 mutaciones mínimas; se ejecutaron 5 — una por cada
componente nuevo con lógica de guardia — para cubrir también el canario de
`LobbyInbox`.)

Verificación de restauración exacta:

```
$ sha256sum src/ludex_agent/showdown/connection.py src/ludex_agent/showdown/lobby.py src/ludex_agent/runner/session.py
7145a9d9... showdown/connection.py   # idéntico al hash pre-mutación
e8da87ed... showdown/lobby.py        # idéntico al hash pre-mutación
43acb130... runner/session.py        # idéntico al hash pre-mutación
```

---

## Suite offline completa (sin DB, sin red, sin Docker)

Comando de verificación exacto:

```
DATABASE_URL='' TEST_DATABASE_URL='' PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q tests/ \
  --deselect tests/integration/test_langgraph_battle.py::test_langgraph_y_poke_env_juegan_una_batalla_en_el_mismo_proceso
```

Resultado:

```
806 passed, 174 skipped, 1 deselected, 3 warnings in 15.19s
```

- Los 174 `skipped` son los skips documentados por falta de `TEST_DATABASE_URL`/DB/Showdown reales.
- El único test deselected (`test_langgraph_battle.py`) es preexistente, llama
  `load_settings()` sin condición de skip y exige una `DATABASE_URL` real
  incluso para construir `Settings`; no pertenece al alcance de Task 6 y no
  se tocó.
- **0 failed.**

## Scans / verificaciones de higiene

```
$ grep -ri "gen6" apps/agent/src/ludex_agent/showdown/connection.py apps/agent/src/ludex_agent/showdown/lobby.py apps/agent/src/ludex_agent/runner/session.py apps/agent/src/ludex_agent/api/routes.py
(sin resultados fuera de config/fixtures)

$ git status --short
(worktree limpio tras el commit — ver abajo)
```

No se abrió ningún socket, no se invocó Docker ni se conectó a Postgres en
ningún punto de esta rebanada. El daemon de Docker estuvo apagado durante
toda la sesión (confirmado con `docker ps` → "Cannot connect to the Docker
daemon").

---

## Limitaciones conocidas

1. **`cli.py` no se modificó.** El plan asignaba `cli.py:117-242` para
   enrutar el preflight de `play()` a través de `ConnectionManager`. Se
   intentó: el cambio en sí es correcto, pero rompía 4 tests de
   `tests/test_cli.py` cuyos fakes de `Settings` (`SimpleNamespace`) no
   incluyen `connection_mode`/`database_role`. `tests/test_cli.py` **no**
   está en la lista de archivos autorizados para esta tarea, y el usuario
   indicó explícitamente durante la sesión no tocar ese archivo. El cambio
   se revirtió; `cli.py` queda byte a byte igual al head anterior
   (confirmado con `git diff --stat` vacío para ese archivo). `run` sigue
   usando `local_server_configuration` directo — comportamiento sin cambios,
   correcto hoy porque nunca corre en modo `official` (D67), pero no pasa
   todavía por el guardarraíl unificado de `ConnectionManager`.
2. **`showdown/client.py` no se modificó** por el mismo motivo: el único
   punto de integración razonable (inyectar `LoginWatchdog` en
   `LudexPlayer`) requeriría tocar `tests/showdown/test_client.py`, tampoco
   autorizado.
3. **`api/routes.py`** expone los 5 endpoints de conexión/sesiones con
   estado en memoria del router (sin tabla ni columna nueva), porque
   `app.py`/`schemas.py` están fuera de alcance explícito del brief. Las
   respuestas son `dict` planos, no `ResponseModel` de Pydantic. No hay
   wiring real a un `LudexPlayer` vivo desde las rutas: eso requiere la
   integración diferida en (1)/(2).

Ambas limitaciones están documentadas también en `docs/DECISIONS.md` D68 y
quedan como trabajo pendiente para una rebanada posterior que incluya
`cli.py`, `showdown/client.py` y sus tests en su alcance autorizado.

---

## Comando de revisión sugerido para Tasos / tech lead

```
git diff a5c7de8bc2d8e64d7430dbcaf3732c4ff53c4ca4..8c0c276 -- \
  apps/agent/src/ludex_agent/showdown/connection.py \
  apps/agent/src/ludex_agent/showdown/lobby.py \
  apps/agent/src/ludex_agent/runner/ \
  apps/agent/src/ludex_agent/api/routes.py \
  apps/agent/tests/showdown/test_connection.py \
  apps/agent/tests/showdown/test_lobby.py \
  apps/agent/tests/runner/test_session.py \
  docs/DECISIONS.md
```

**Modelo efectivo:** Claude Sonnet 5 (MON-36 Task 6). Sin recomendación
propia: Latwan adjudica.
