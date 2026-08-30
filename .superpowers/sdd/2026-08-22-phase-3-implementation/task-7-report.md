# REVIEW PACKET R2 — MON-37, Fase 3 Task 7 / F3-07 (aceptación explícita de challenges)

**Issue:** MON-37 / F3-07 — "Implement explicit challenge acceptance"
(`docs/superpowers/plans/2026-08-22-phase-3-implementation.md`, Task 7).
**Decisión asociada:** `docs/DECISIONS.md` D69 (original) + D69 (corrección R2).
**Spec vinculante:** `docs/superpowers/specs/2026-08-22-phase-3-design.md`,
sección 6.1 (challenges) y §7.1 (superficie REST); canarios 10-13.
**Worktree:** `phase3-s7-challenge-ws`, branch `migueljh/phase3-s7-challenge-ws`.
**Base SHA:** `dce0cfd40ae15ac7108ecfd5e641e1c44ed35cde`
(`origin/integration/phase-3-accepted`).
**Head SHA (packet original):** `356438571756626cef8e3ff801949242acdc770f` (`3564385`).
**Head SHA (packet R1, evidencia versionada):** `900d302`.
**Head SHA (corregido, este documento, R2):** `PENDING` — completado en
un segundo commit inmediato de esta misma corrección (ver nota al pie del
documento).
**Revisión independiente:** Tasos, read-only, `CHANGES_REQUESTED` sobre
`dce0cfd..3564385` — T-01 (cobertura del seam de challenges), T-02, T-03.
Esta corrección cierra **únicamente T-01**; T-02/T-03 y S9a quedan fuera de
alcance de este ciclo (ver sección "Corrección R2").
**Estado:** T-01 cerrado, worktree limpio, listo para re-revisión → `In Review`.
**No marcar `Completed`** — el veredicto es exclusivo del tech lead.

---

## Corrección R2 (cierre de T-01)

**Finding T-01.** `tests/showdown/test_challenges.py` prueba el CUERPO de
`_update_challenges`/`_handle_challenge_request` llamándolos directo sobre
la instancia (`await player._update_challenges(...)`), pero nunca ejerce
el camino productivo real (`PSClient._handle_message`, el dispatcher que
usa el socket) ni asevera que los callbacks bindeados en `PSClient`
(`_on_update_challenges`, `_on_challenge_request`) apunten efectivamente a
las sobrescrituras de `LudexPlayer` y no a las de `Player` — D65 canario
10 sin cubrir. Un futuro cambio que rompiera esa atadura (p.ej. un rename
accidental de las sobrescrituras) pasaría la suite existente en verde
mientras un challenge real se auto-aceptaría en producción.

**Causa raíz.** Hueco de cobertura, no defecto de comportamiento:
`Player.__init__` (poke-env, `player.py:156-157`) pasa
`on_update_challenges=self._update_challenges` y
`on_challenge_request=self._handle_challenge_request` al construir
`PSClient`; como `type(self)` ya es `LudexPlayer` durante toda la
construcción, la resolución MRO real siempre encuentra las sobrescrituras
de `LudexPlayer` — pero nada en la suite lo comprobaba ejerciendo el seam
real, así que una regresión futura ahí sería invisible hasta producción.

**Corrección aplicada.** `tests/showdown/test_pokeenv_contract.py` (el
archivo ya dedicado a contratos con seams privados de poke-env, ver su
docstring) gana tres tests:

1. `test_los_callbacks_de_challenge_de_psclient_apuntan_a_ludexplayer` —
   `player.ps_client._on_update_challenges.__func__ is
   LudexPlayer._update_challenges` (mismo para `_on_challenge_request`).
2. `test_updatechallenges_productivo_nunca_auto_encola` — llama
   `player.ps_client._handle_message("|updatechallenges|...")` (el
   dispatcher real) y asevera `_challenge_queue.empty()`.
3. `test_pm_challenge_productivo_nunca_auto_encola` — mismo canario para
   el PM `/challenge` real (`split_message[5]` es el formato, ver
   `player.py:356-363` y `showdown/client.py::_handle_challenge_request`).

**No se tocó ningún archivo de producción** — T-01 era exclusivamente un
hueco de cobertura de test. `showdown/client.py`, `challenge_gateway.py`,
`api/routes.py`, `api/schemas.py` y `api/app.py` quedan byte a byte iguales
al head `3564385`.

### Mutación deliberada ("seam rebind"), restaurada y verificada por `sha256`

Se renombraron temporalmente (`sed`, líneas 987 y 1008 de
`showdown/client.py`) `LudexPlayer._update_challenges`/
`_handle_challenge_request` a `..._MUTATED_seam_rebind`, simulando
exactamente la regresión que T-01 señaló como no cubierta: sin esas
sobrescrituras, `Player.__init__` bindea los callbacks de `PSClient` a las
implementaciones ORIGINALES de `Player`, que auto-encolan si el formato
matchea.

```
$ sha256sum src/ludex_agent/showdown/client.py   # antes de mutar
305a7b0d8971f957ecfed46b4a0ea8846b5b6222df0c0eeade387db9962201d4

$ PYTHONPATH=src DATABASE_URL='' .venv/bin/python -m pytest \
  tests/showdown/test_pokeenv_contract.py -q -k "callback or productivo"
.FF
2 failed, 1 passed, 7 deselected in 0.22s
```

- `test_updatechallenges_productivo_nunca_auto_encola` → RED:
  `_challenge_queue` terminó `['rival1']` en vez de vacía.
- `test_pm_challenge_productivo_nunca_auto_encola` → RED:
  `_challenge_queue` terminó `['rival2']` en vez de vacía.
- `test_los_callbacks_de_challenge_de_psclient_apuntan_a_ludexplayer` →
  **sigue en verde con esta mutación puntual**: es tautológico respecto a
  "lo que sea que `LudexPlayer._update_challenges` resuelva hoy" — tras el
  rename, ambos lados de la comparación (`player.ps_client.
  _on_update_challenges.__func__` y `LudexPlayer._update_challenges`)
  resuelven al mismo `Player._update_challenges` base. Documentado como
  limitación conocida de ESE test puntual (no invalida la corrección: los
  dos canarios productivos SÍ cierran T-01, que exigía ejercer el
  dispatcher real).

```
$ sed -i '' -e '987s/_MUTATED_seam_rebind//' ... # restaurado
$ sha256sum src/ludex_agent/showdown/client.py   # después de restaurar
305a7b0d8971f957ecfed46b4a0ea8846b5b6222df0c0eeade387db9962201d4
```

Idéntico al hash pre-mutación. `git diff` contra HEAD tras restaurar:
vacío para `showdown/client.py`.

### Verificación

```
$ PYTHONPATH=src DATABASE_URL='' .venv/bin/python -m pytest \
  tests/showdown/test_pokeenv_contract.py tests/showdown/test_challenges.py -q
20 passed in 0.26s

$ PYTHONPATH=src DATABASE_URL='' .venv/bin/python -m pytest -q \
  --ignore=tests/integration/test_langgraph_battle.py
829 passed, 174 skipped, 3 warnings in 15.38s
```

0 failed. 826 → 829 (+3 tests nuevos de esta corrección). 0 archivos de
producción modificados.

### Alcance de la corrección

**Solo T-01.** T-02/T-03 (otros findings de la revisión independiente) y
S9a (consumidor real de `_challenge_queue`, wiring de la superficie REST a
un `LudexPlayer` vivo) quedan **explícitamente fuera** de este ciclo de
corrección, sin tocar.

---

## Alcance de esta rebanada

Implementa S5 de la spec: **ningún challenge entrante se acepta
automáticamente**; solo un accept explícito encola. Fuera de alcance,
deliberadamente:

- S9a (consumidor real de `_challenge_queue` vía `Player.accept_challenges`
  contra un socket vivo — "aceptación live de challenge en DB descartable").
- Wiring de la superficie REST a un `LudexPlayer` vivo (mismo alcance sin
  socket real que Task 6/D66 T-02 para `/connection/*` y `/sessions`).
- `send_challenges` real (envío saliente) contra poke-env.

No se tocó ningún archivo de conexión oficial, ladder, batallas, base de
datos ni `.env`/secrets. No se abrió socket, no se conectó a Postgres, no
se invocó Docker en ningún momento de esta rebanada.

---

## Archivos modificados (`git diff --stat dce0cfd..3564385`)

```
 apps/agent/src/ludex_agent/api/app.py                       |   5 +
 apps/agent/src/ludex_agent/api/routes.py                    |  52 +++++-
 apps/agent/src/ludex_agent/api/schemas.py                   |  17 ++
 apps/agent/src/ludex_agent/showdown/challenge_gateway.py    |  51 + (nuevo)
 apps/agent/src/ludex_agent/showdown/client.py                | 104 ++++++++++++
 apps/agent/tests/api/test_app.py                             |  85 ++++++++++
 apps/agent/tests/showdown/test_challenges.py                 | 178 + (nuevo)
 docs/DECISIONS.md                                             |  86 ++++++++++
 8 files changed, 577 insertions(+), 1 deletion(-)
```

Un único commit (`3564385`) contiene la implementación completa de esta
rebanada.

---

## Causa raíz

poke-env 0.15.0 auto-encola cualquier challenge entrante que matchee
`self._format`, vía dos productores de `_challenge_queue`:
`Player._update_challenges` (desde `|updatechallenges|`) y
`Player._handle_challenge_request` (desde PM `/challenge`). D65 sección 6.1
prohíbe ese auto-accept. `api/routes.py` documentaba explícitamente
"challenges a S5/Task 7" como pendiente antes de esta rebanada.

**Inspección de la librería real** (`.venv/lib/python3.12/site-packages/
poke_env/`, versión instalada en el worktree, no memoria): confirmado que
ambos métodos llaman `self._challenge_queue.put(user)` directo sin publicar
nada observable antes; que `PSClient._update_challenges`/
`_handle_challenge_request` son wrappers finos que delegan a callbacks
apuntados por `Player` a sus propios métodos (el punto de intercepción
correcto es sobrescribir esos métodos en `LudexPlayer`, ya el patrón usado
en el resto de la clase); y que `Player._accept_challenges` corre un loop
`while True` que **descarta** cualquier item de la cola que no matchee el
filtro `opponent` — esto descarta un diseño de consumidor real por-usuario
(dos accepts casi simultáneos de usuarios distintos podrían descartarse
entre sí) y motivó dejar el consumidor real fuera de esta rebanada (S9a).

## Solución aplicada

**`showdown/client.py` (`LudexPlayer`).** Gana `lobby_inbox: LobbyInbox`
(reutiliza el `LobbyInbox` de Task 6, sin usar hasta ahora) e
`incoming_challenges: dict[str, str]`. Las dos sobrescrituras nunca llaman
al original ni tocan `_challenge_queue`: solo publican al lobby y
actualizan `incoming_challenges`, **sin filtrar por formato** (canario 12
— challenges de otro formato siguen visibles). `_update_challenges` trata
el mapa `challengesFrom` como snapshot completo del server: un usuario
ausente en el mapa nuevo publica `challenge_withdrawn`.
`accept_incoming_challenge(username)`/`reject_incoming_challenge(username)`
normalizan el username (`normalize_id`, mismo criterio que `to_id_str` de
poke-env) y fallan cerrado con `UnknownChallengeError` si no hay challenge
conocido — no existe accept "a ciegas". **Solo** `accept_incoming_challenge`
inserta en `_challenge_queue`; nunca se llama `PSClient.accept_challenge`
directo (D65: saltearía la contabilidad de poke-env).

**Superficie REST (`api/routes.py`, `showdown/challenge_gateway.py`,
`api/app.py`, `api/schemas.py`).** `GET /challenges`, `POST
/challenges/{user}/accept`, `POST /challenges/{user}/reject`, `POST
/challenges/outgoing` (spec §7.1). `ChallengeGateway` es un `Protocol`
(`list_incoming`/`accept`/`reject`) que desacopla la ruta del productor
real; `InMemoryChallengeGateway` es el default inyectado por `create_app`
vía un parámetro nuevo **opcional** (`challenge_gateway`), que no rompe
ningún call-site existente. Un accept/reject sobre usuario desconocido
responde 404 `UNKNOWN_CHALLENGE`.

---

## RED antes de implementar

```
$ PYTHONPATH=src DATABASE_URL='' .venv/bin/python -m pytest tests/showdown/test_challenges.py -q
ImportError: cannot import name 'UnknownChallengeError' from 'ludex_agent.showdown.client'
1 error in 3.01s
```

## GREEN después de implementar

```
$ PYTHONPATH=src DATABASE_URL='' .venv/bin/python -m pytest tests/showdown/test_challenges.py tests/api/test_app.py -q -k "challenge or test_challenges"
```

Cobertura:

- **`test_challenges.py` (10 casos):** los dos productores publican sin
  encolar; formatos ajenos siguen visibles en el lobby; un snapshot de
  `|updatechallenges|` sin un usuario previo publica `challenge_withdrawn`;
  `accept_incoming_challenge`/`reject_incoming_challenge` fallan cerrado
  (`UnknownChallengeError`) sobre un usuario sin challenge conocido; accept
  encola y normaliza el username (`"rival one"` matchea `"Rival One"`);
  reject descarta sin tocar la cola; el `LobbyInbox` inyectado se respeta.
- **`test_app.py` (7 casos nuevos):** `GET /challenges` refleja el gateway
  inyectado; accept/reject conocido devuelve 200 y limpia la lista;
  accept/reject desconocido devuelve 404 `UNKNOWN_CHALLENGE`; `POST
  /challenges/outgoing` hace eco; `create_app` sin `challenge_gateway`
  explícito usa `InMemoryChallengeGateway` por defecto.

---

## Mutaciones deliberadas: RED → restaurado → GREEN

Aplicadas **in-place** sobre el worktree real, verificadas RED, restauradas
**byte a byte** y confirmadas con `sha256sum` contra el hash pre-mutación
de los tres archivos tocados (`client.py`, `challenge_gateway.py`,
`routes.py`).

| # | Mutación | Archivo | Resultado RED | Restaurado (sha256) |
|---|---|---|---|---|
| 1 | `_update_challenges` vuelve a encolar automáticamente (`await self._challenge_queue.put(user)` dentro del loop de publicación) — restaura el auto-accept original de poke-env | `showdown/client.py` | 4/10 en rojo: `test_update_challenges_publishes_without_enqueuing`, `test_accept_incoming_challenge_enqueues_known_user`, `test_accept_incoming_challenge_normalizes_username`, `test_reject_incoming_challenge_removes_without_enqueuing` (`_challenge_queue` termina con 2 items en vez de 1 / no vacía cuando debería) | ✅ idéntico al original |
| 2 | `InMemoryChallengeGateway.accept` reemplaza el chequeo `UnknownChallengeError` por `self._incoming.pop(username, None)` (acepta a ciegas) | `showdown/challenge_gateway.py` | `test_accept_unknown_challenge_returns_404`: `assert 200 == 404` | ✅ idéntico al original |

Verificación de restauración exacta:

```
$ sha256sum src/ludex_agent/showdown/client.py src/ludex_agent/showdown/challenge_gateway.py src/ludex_agent/api/routes.py
# hash idéntico antes y después de la ronda completa de mutación
```

`git diff` contra HEAD tras restaurar: vacío para los tres archivos.

---

## Suite offline completa (sin DB, sin red, sin Docker)

Comando de verificación exacto:

```
$ cd apps/agent && PYTHONPATH=src DATABASE_URL='' .venv/bin/python -m pytest -q \
  --ignore=tests/integration/test_langgraph_battle.py
```

Resultado (re-ejecutado para este packet, no reusado de memoria):

```
826 passed, 174 skipped, 3 warnings in 15.99s
```

- 0 failed.
- Los 174 `skipped` son los skips ya documentados por falta de
  `TEST_DATABASE_URL`/DB/Showdown reales (sin cambios respecto a Task 6).
- El único `--ignore` (`test_langgraph_battle.py`) es preexistente y
  documentado en D64/Task 6: exige una `DATABASE_URL` real incluso para
  construir `Settings`; no pertenece al alcance de Task 7 y no se tocó.

---

## Scans e higiene del diff

**`git diff --check` (whitespace/conflict markers) sobre el rango
`dce0cfd..3564385`:**

```
$ git diff --check dce0cfd40ae15ac7108ecfd5e641e1c44ed35cde 356438571756626cef8e3ff801949242acdc770f
(sin salida — exit 0)
```

**Archivos JSON tocados por el diff:** ninguno (`git diff --name-only
dce0cfd..3564385 | grep -i '\.json$'` no devuelve resultados) — no aplica
validación de JSON.

**Escaneo de secretos** sobre el diff completo (patrones: `api[_-]?key`,
`secret`, `password`, `token`, `SHOWDOWN_(OFFICIAL_)?PASSWORD`, encabezados
de clave privada, patrón de AWS access key):

```
$ git diff dce0cfd40ae15ac7108ecfd5e641e1c44ed35cde 356438571756626cef8e3ff801949242acdc770f \
  | grep -nEi "api[_-]?key|secret|password|token|SHOWDOWN_(OFFICIAL_)?PASSWORD|BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|AKIA[0-9A-Z]{16}"
(sin resultados)
```

**`grep -ri "gen6"` fuera de config/fixtures** sobre los archivos de
producción tocados:

```
$ grep -ri "gen6" src/ludex_agent/showdown/client.py src/ludex_agent/showdown/challenge_gateway.py src/ludex_agent/api/routes.py src/ludex_agent/api/schemas.py src/ludex_agent/api/app.py
(sin resultados)
```

Los tests SÍ usan `gen6ou` como formato de fixture (esperado — es
configuración de test, no un valor hardcodeado en producción).

**Worktree limpio tras el commit:**

```
$ git status --short
(sin salida)
```

Docker apagado durante toda la sesión; ningún test de esta rebanada
requiere DB, Showdown local ni red.

---

## Decisiones agregadas a `docs/DECISIONS.md`

D69 — MON-37 (Fase 3 Task 7, F3-07): aceptación explícita de challenges.
Documenta causa raíz, inspección de la librería real, diseño, tests,
mutación y limitaciones conocidas con el mismo nivel de detalle que este
packet.

---

## Limitaciones conocidas

1. **No existe consumidor real de `_challenge_queue`.** `Player.
   accept_challenges` (el método de poke-env que efectivamente acepta la
   batalla vía `PSClient.accept_challenge` con la contabilidad correcta)
   no se invoca desde ningún camino nuevo de esta rebanada. Esta rebanada
   cierra "nada auto-encola" + "el accept explícito sí encola" (S5); "un
   socket real acepta la batalla" es S9a, explícitamente diferida.
2. **La superficie REST sigue sin `LudexPlayer` vivo detrás** — mismo
   alcance que Task 6 (D66 T-02) para `/connection/*` y `/sessions`.
   `GET /challenges` solo refleja lo que un test/operador siembra a mano
   en `InMemoryChallengeGateway.seed_incoming` (método de test, sin ruta
   REST que lo exponga): no hay challenges entrantes reales todavía porque
   no hay conexión real.
3. **`POST /challenges/outgoing` es un stub que hace eco** del payload
   recibido; `send_challenges` real contra poke-env queda para la rebanada
   que integre el gateway con un `LudexPlayer` vivo.

## Riesgos o dudas pendientes

Ninguno bloqueante para esta rebanada. La continuación natural (S9a:
gateway real + consumidor de cola + conexión oficial) debería asignarse
como tarea separada, siguiendo el mismo criterio de asignación vinculante
que D66 aplicó a Task 6.

---

**Modelo efectivo:** Claude Sonnet 5. Recomendación: `In Review`.
