# REVIEW PACKET — MON-38, Fase 3 Task 8 / F3-08 (ladder fail-closed)

**Issue:** MON-38 / F3-08 — "Add fail-closed ladder sessions"
(`docs/superpowers/plans/2026-08-22-phase-3-implementation.md`, Task 8).
**Decisión asociada:** `docs/DECISIONS.md` D65 (sección 6.2 ladder), ya
registrada en Task 1. Este brief NO autoriza tocar `docs/DECISIONS.md`, así
que la decisión nueva queda documentada en este packet y en el código (ver
"Decisiones agregadas").
**Spec vinculante:** `docs/superpowers/specs/2026-08-22-phase-3-design.md`
§6.2 (interlock quíntuple), §7.1 (superficie REST) y canario 30.
**Worktree:** `phase3-s8-ladder-ws`, branch `migueljh/phase3-s8-ladder-ws`.
**Base SHA:** `6958bea74f0a18aa27edd61a26dbd9b1c054cc55`
(`origin/integration/phase-3-accepted`).
**Head SHA:** `0dac3535918be8ef41f2a5a488c28fc833d5153a` (`0dac353`).
**Estado:** implementado, worktree limpio, push inmediato hecho → `In Review`.
**No marcar `Completed`** — el veredicto es exclusivo del tech lead.

---

## Corrección R2 (cierre de T-01) — MON-38 R2

**Revisión independiente:** Tasos / Grok 4.6, read-only, `CHANGES_REQUESTED`
sobre `6958bea..a6c9411`. Findings: **T-01 IMPORTANT** (TOCTOU en
`POST /sessions`), **T-02 MINOR** (deferido, ver abajo). Esta corrección
cierra **únicamente T-01**. Rango de R2: `a6c9411..<head-R2>`.

**Finding T-01.** `apps/agent/src/ludex_agent/api/routes.py`: entre el check
`if _session_state["active"]` y el `_session_state.update(active=True)` había
dos `await _read_durable_flag(...)`. Dos `POST /sessions` concurrentes
observaban `active=False`, ambos pasaban interlocks y ambos hacían
`asyncio.create_task(_run_ladder_session(...))` → dos `Player.ladder(1)` y dos
`/search` simultáneos sobre la cuenta de testing. `SessionRunner` no podía
salvarlo: cada request construye su propio runner (el `_active` es por
instancia). Los canarios seriales de T-04 (`TestClient`) no ejercen el
interleaving.

**Corrección aplicada.** `routes.py` reserva el slot **atómicamente**: el check
de ocupación y el `_session_state["active"] = True` quedan sin `await` entre
sí (exclusión mutua en un único paso del event loop). Todo el cuerpo posterior
(load_settings, lectura de flags, interlock, player) queda dentro de un `try`
cuyo `except BaseException` revierte la reserva
(`active=False, id=None, stop_requested=False`) y re-lanza, de modo que un
fallo de configuración/flag/interlock/player (o una cancelación) libera el
slot SIN abrir socket ni enviar `/search`. `source`/`stop-after-current`/
off-after-session quedan intactos.

### RED antes de la corrección (canario concurrente nuevo)

Se agregó `test_two_concurrent_session_requests_reserve_the_slot_atomically`
(`tests/api/test_app.py`): `httpx.ASGITransport` (la app corre en el MISMO
loop del test) + `asyncio.gather` de dos `POST /sessions` con gates abiertos y
`_SlowFlagSettingsSession.execute` que hace un `await asyncio.sleep(0.05)`
(ventana de interleaving determinística). Contra el head `a6c9411`:

```
$ ... -m pytest tests/api/test_app.py -q -k concurrent_session
FAILED ... assert [200, 200] == [200, 409]   # el bug exacto de T-01
1 failed, 34 deselected in 1.26s
```

### GREEN después de la corrección

```
$ ... -m pytest tests/api/test_app.py -q -k concurrent_session
1 passed in 1.46s
```

El canario asevera: `statuses == [200, 409]`, `ladder_calls == [1]`,
`socket_opens == 1`, `search_sends == ["/search"]` — exactamente un 200, un
409, un `ladder(1)`, un socket y un `/search`.

### Mutación deliberada (reserva movida de vuelta detrás del await)

Se revirtió la reserva atómica al patrón original (borrar
`_session_state["active"] = True` y devolver `active=True` al
`_session_state.update(...)` final), dejando el `await _read_durable_flag` entre
el check y la reserva:

```
$ ... -m pytest tests/api/test_app.py -q -k concurrent_session
FAILED ... assert [200, 200] == [200, 409]   # RED
1 failed in 1.19s
```

Restaurado byte a byte; `shasum -a 256 apps/agent/src/ludex_agent/api/routes.py`
== `1bb86418dd2110bf367121ad81235641a599bf6edfd3425b2cb7828599296f1b`
(idéntico al hash pre-mutación). `git diff` de `routes.py` tras restaurar: solo
el cambio T-01.

### Suites tras la corrección

```
$ ... -m pytest tests/runner/test_session.py tests/api/test_app.py -q
46 passed, 3 skipped, 1 warning   # 45 → 46 (+1 canario concurrente)

$ ... -m pytest -q --ignore=tests/integration/test_langgraph_battle.py
850 passed, 174 skipped, 0 failed  # 849 → 850
```

0 failed en ambas.

### T-02 (MINOR, deferido)

`canonical-db` responde `LADDER_INTERLOCK` con `missing: []` (la tupla de
`missing_interlocks` no incluye el DSN canónico, que lo rechaza
`_reject_unsafe_official_database`). El rechazo existe y no abre red. Queda
**explícitamente fuera** de este ciclo de corrección, registrado como minor
para la triage final de rama, sin tocar.

### Alcance de la corrección R2

Solo `apps/agent/src/ludex_agent/api/routes.py` y
`apps/agent/tests/api/test_app.py` (+ este packet versionado). NO se tocaron
`runner/session.py` ni `schemas.py`. Sin scope creep. Sin red, DB, Docker,
providers ni Linear.

---

## Archivos modificados (`git diff --stat 6958bea..0dac353`)

## Archivos modificados (`git diff --stat 6958bea..0dac353`)

```
 apps/agent/src/ludex_agent/runner/session.py      |  99 +++++-
 apps/agent/src/ludex_agent/api/routes.py          | 145 ++++++++--
 apps/agent/src/ludex_agent/api/schemas.py         |  13 +
 apps/agent/tests/runner/test_session.py           | 142 +++++++-
 apps/agent/tests/api/test_app.py                  | 293 +++++++++++--
 5 files changed, 655 insertions(+), 37 deletions(-)
```

Exactamente las cinco rutas autorizadas por el brief. Un único commit
(`0dac353`). `git status --short` vacío tras el push.

---

## Causa raíz

Antes de esta rebanada `POST /sessions` era un stub (D66/Task 6) que
escribía estado en memoria sin validar nada ni invocar un `SessionRunner`:
`runner/session.py` llamaba `player.ladder(1)` sin ningún guardia. El ladder
es matchmaking público y ranqueado; un bot ahí arriesga el baneo de la cuenta
de testing, y el dataset de aceptación solo contempla `gen6randombattle`.
La spec §6.2 exige cinco condiciones simultáneas (y el brief suma el formato
de aceptación como sexta) con rechazo ANTES de abrir socket o enviar
`/search`. No existía tal rechazo.

---

## Solución aplicada

**`runner/session.py`.** Gana `SessionKind(str, Enum)` (único miembro
`LADDER`), `LadderInterlockError(RuntimeError)`, el dataclass congelado
`LadderGates` (las seis condiciones) y `check_ladder_interlocks(gates)`, que
falla cerrado (`LadderInterlockError`) si falta cualquier interlock o si
`gates is None`. En modo `official` reusa el MISMO guardarraíl de Task 1
(`config._reject_unsafe_official_database`) en vez de reimplementar la
prohibición de DB canónica (mismo patrón que `connection.py` T-05): no hay
política duplicada. `SessionRunner` gana `kind` (default `LADDER`) y
`gates`; `start()` evalúa `check_ladder_interlocks` ANTES de cualquier
`player.ladder(1)`, por cada arranque (nunca cacheado al construir). Propiedad
`source` → `"ladder"` para `SessionKind.LADDER`.

**`api/schemas.py`.** `SessionRequest`: `n_battles: int = Field(default=1, ge=1)`
y `confirm: bool = False` (interlock por llamada, fail-closed: sin default
que lo habilite).

**`api/routes.py`.** `POST /sessions` ahora es la máquina de ladder
fail-closed: (1) 409 `ACTIVE_MATCHMAKING` si el slot está ocupado; (2)
`load_settings()` → `RuntimeError` mapeado a 422 `UNSAFE_OFFICIAL_DATABASE`
(igual que `/connection/connect` T-03); (3) lee los flags durables
`ladder_enabled`/`testing_account_confirmed` de la tabla `settings` (store
F2-09, misma key/value que `approval_mode`), ausencia = `False` (fail-closed),
releídos POR REQUEST (no cacheados); (4) arma `LadderGates` con el formato de
aceptación leído de la env `LADDER_ACCEPTANCE_FORMAT` (sin literal de
generación en producción) y `check_ladder_interlocks` → 422 `LADDER_INTERLOCK`
con `missing`+`reason`; (5) exige `app.state.ladder_player` cableado
(`LadderGates` no es suficiente sin player: fail-closed); (6) despacha
`SessionRunner.start(n)` como task de fondo y devuelve `_session_response()`
con `source="ladder"`. `DELETE /sessions/{id}` hace stop-after-current REAL:
`await runner.stop()` (nunca cancela la batalla en curso); el slot lo libera
el runner al terminar la corrida, no la ruta.

---

## RED antes de implementar

```
$ env -i ... PYTHONPATH=$PWD/src DATABASE_URL='' .venv/bin/python -m pytest \
    tests/runner/test_session.py tests/api/test_app.py -q
ImportError: cannot import name 'LadderGates' from 'ludex_agent.runner.session'
1 error in 0.44s
```

## GREEN después de implementar

```
$ env -i ... PYTHONPATH=$PWD/src DATABASE_URL='' .venv/bin/python -m pytest \
    tests/runner/test_session.py tests/api/test_app.py -q
45 passed, 3 skipped, 1 warning in 0.91s
```

Cobertura de los tests nuevos (+20, de 25 a 45):

- **`test_session.py`** (runner): `SessionKind.LADDER.value == "ladder"`; un
  canario parametrizado de CERO llamadas para los seis interlocks
  (`local-mode`, `disabled-ladder`, `missing-call-confirmation`,
  `unconfirmed-testing-account`, `canonical-db`, `wrong-format`) que asevera
  `ladder_calls == []`, `socket_opens == 0` y `search_sends == []`; fail-closed
  sin `gates`; interlocks abiertos → exactamente una llamada `ladder(1)` con
  socket y `/search`; y off-after-session: re-evalúa por `start()`, deshabilitar
  ladder bloquea el arranque siguiente. Los 4 tests de Task 6 se conservan
  pasando `gates=_open_gates()` (semántica de secuencia intacta).
- **`test_app.py`** (ruta): seis canarios HTTP de cero llamadas (mismos ids);
  fail-closed sin player cableado; `load_settings()` → 422
  `UNSAFE_OFFICIAL_DATABASE` (nunca 500); interlocks abiertos → 200,
  `source="ladder"`, disparo real de `Player.ladder(1)`; off-after-session
  (deshabilitar flag → 422, sin segunda llamada). El canario T-04 de Task 6 se
  ADAPTÓ a la semántica real de stop-after-current (ver "Limitaciones").

---

## Mutaciones deliberadas: RED → restaurado → GREEN

Aplicadas **in-place** sobre el worktree real (con `PYTHONPATH` pineado, según
`.claude/verification/SKILL.md`), verificadas RED en el canario correspondiente
y restauradas **byte a byte**, confirmado por `sha256` contra el hash
pre-mutación.

Hashes pre-mutación (y post-restauración, idénticos):

```
6160f5b0f16790a7c7dfb6b93722624c255f0d9ae8ee3a79841ba576d009ea01  runner/session.py
6904696fa4becc471284309ed02eace187d8d5d6450c1e1c2b5b49e285454d01  api/routes.py
8f36c703538e3f7ba2e5b791a1d080df7819d1e7aec2e65bb95aa13f2e908f07  api/schemas.py
```

| # | Mutación | Archivo | Resultado RED | Restaurado |
|---|---|---|---|---|
| 1 | `connection_mode != "official"` → `if False` | `runner/session.py` | `test_ladder_interlock_fails_closed_with_zero_network_calls[local-mode]` + `test_ladder_session_interlock_fails_closed_with_zero_network_calls[local-mode]`: 2 failed | ✅ idéntico |
| 2 | `not self.ladder_enabled` → `if False` | `runner/session.py` | `[disabled-ladder]` runner + ruta: 2 failed | ✅ idéntico |
| 3 | `not self.confirm` → `if False` | `runner/session.py` | `[missing-call-confirmation]` runner + ruta: 2 failed | ✅ idéntico |
| 4 | `not self.testing_account_confirmed` → `if False` | `runner/session.py` | `[unconfirmed-testing-account]` runner + ruta: 2 failed | ✅ idéntico |
| 5 | `battle_format != required_format` → `if False` | `runner/session.py` | `[wrong-format]` runner + ruta: 2 failed | ✅ idéntico |
| 6 | `raise LadderInterlockError(str(exc))` → `pass` (silencia el guard de DB canónica) | `runner/session.py` | `[canonical-db]` runner + ruta: 2 failed | ✅ idéntico |
| 7 | `if player is None` → `if False` | `api/routes.py` | `test_ladder_session_without_a_wired_player_fails_closed`: failed | ✅ idéntico |
| 8 | `ladder_enabled=await _read_durable_flag(...)` → `ladder_enabled=True` | `api/routes.py` | `[disabled-ladder]` ruta: failed | ✅ idéntico |

Los jugadores falsos de los canarios de cero-llamadas usan `hold=False` para
que una mutación falle en rojo limpio (asserción) en vez de colgarse en el
evento de batalla (lección del "falso verde por editable install" y de los
cuelgues documentados en Task 6).

---

## Suite offline completa (sin DB, sin red, sin Docker)

```
$ env -i ... PYTHONPATH=$PWD/src DATABASE_URL='' TEST_DATABASE_URL='' \
    .venv/bin/python -m pytest -q --ignore=tests/integration/test_langgraph_battle.py
849 passed, 174 skipped, 3 warnings in 17.45s
```

0 failed. 829 → 849 (+20 tests de esta rebanada). Los 174 skips son los ya
documentados por falta de `TEST_DATABASE_URL`/DB/Showdown reales. El único
`--ignore` es preexistente (D64/Task 6). Docker apagado durante toda la
sesión; no se abrió socket, no se conectó a Postgres, no se tocó la DB canónica.

---

## Scans e higiene del diff

```
$ git diff --check                     # exit 0, sin salida
$ grep -ri "gen6" runner/session.py api/routes.py api/schemas.py   # sin resultados
```

El literal `gen6` no aparece en ningún archivo de producción: el formato de
aceptación entra por la env `LADDER_ACCEPTANCE_FORMAT` (generación como
parámetro) y los `gen6*` de los tests son fixtures.

Escaneo de secretos sobre el diff: el único match es
`llm_api_key_env="GEMINI_API_KEY"` en el fixture de test — es el NOMBRE de la
variable de entorno (patrón F2-09), no un valor de credencial. Ningún secreto,
contraseña ni token real en el diff.

---

## Decisiones agregadas a DECISIONS.md

**No aplica** — el brief autoriza exactamente cinco archivos y
`docs/DECISIONS.md` no está entre ellos (a diferencia de Task 6/7, cuyos
briefs lo listaban explícitamente). La decisión no trivial (interlock
quíntuple + formato de aceptación como parámetro `LADDER_ACCEPTANCE_FORMAT`
+ source=ladder como seam del runner) queda documentada en este packet y en los
docstrings de `runner/session.py`/`api/routes.py`. Si el tech lead quiere la
entrada durable, es un follow-up de una línea.

---

## Limitaciones conocidas

1. **No hay `LudexPlayer` vivo detrás de las rutas** (misma clase que D68/D66
   T-02). El player se inyecta vía `app.state.ladder_player`; sin él, la ruta
   falla cerrado (422 `LADDER_INTERLOCK`, `missing=["player"]`). Cablear un
   `LudexPlayer` real es trabajo de una rebanada posterior (S9b, fuera del
   alcance autorizado de los cinco archivos).
2. **`source="ladder"` se persiste como seam, no como fila.** Las filas de
   `battles` las escribe el flujo de batalla (`cli.py::play` →
   `repo.save_battle(source=...)`), que está fuera de los cinco archivos
   autorizados. Esta rebanada expone `SessionRunner.source == "ladder"` y la
   respuesta de sesión `source="ladder"`; el consumidor real que persista
   `battles.source` con ese valor es la rebanada S9b.
3. **Los flags durables se escriben fuera de esta superficie.** No hay ruta
   `PATCH` para `ladder_enabled`/`testing_account_confirmed` (la spec §7.1 no
   la define); se leen por request de la tabla `settings` y un operador los
   fija por SQL/env en la aceptación (Task 12). El test de off-after-session
   muta el store directamente, simulando ese apagado out-of-band.
4. **El canario T-04 de Task 6 se ADAPTÓ** (misma intención, no empeorado):
   con un runner real, `DELETE` ya no puede liberar `active` de inmediato
   porque hay una batalla viva que proteger; `active` se libera cuando TERMINA
   la corrida. El test ahora asevera `stop_requested=True` + `active=True`
   tras el DELETE y que el slot se libera al terminar la batalla.

## Riesgos o dudas pendientes

- El nombre `LADDER_ACCEPTANCE_FORMAT` es una variable de entorno nueva que no
  está en `.env.example` (archivo fuera del alcance del brief); sin ella el
  interlock de formato falla cerrado. Confirmar con el tech lead si prefiere
  documentarla en `.env.example`/`DECISIONS.md` como follow-up.
- Los minors diferidos de Task 7 (T-02 `UnknownChallengeError` duplicado,
  T-03 aserción de cola vacía) no se tocan ni se empeoran: pertenecen al
  dominio de challenges, no a `/sessions`. No bloquean esta tarea.

---

**Modelo efectivo:** deepseek-v4-pro. Recomendación: `In Review`.
