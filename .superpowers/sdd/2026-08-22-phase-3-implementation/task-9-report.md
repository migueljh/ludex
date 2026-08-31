# REVIEW PACKET — MON-40, Fase 3 Task 9 / F3-09 (provenance hooks)

**Issue:** MON-40 / F3-09 — "Persist narrow Phase 6 hooks and finish audit
reporting" (`.superpowers/sdd/2026-08-22-phase-3-implementation/task-9-brief.md`).
**Ruling vinculante del tech lead:** `docs/DECISIONS.md` D70 — `replay_url`
nunca se deriva de `battle_tag` (NULL salvo una línea de tipo `|raw|` con
URL `https` explícita, host exacto `replay.pokemonshowdown.com`, slug
estricto, sin `/savereplay`, `uploadreplay` ni red — el requisito de tipo
`|raw|` se agregó en R3); `elo_bucket` usa solo `battle.opponent_rating`
público del rival, `str` decimal canónico, nunca rating propio/rangos/labels/
redondeo, NULL si ausente O si `source == "challenge"` (gate explícito por
`source` agregado en R3, T-01).
**Worktree:** `phase3-s9-provenance-ws`, branch `migueljh/phase3-s9-provenance-ws`.
**Base SHA aceptada:** `7abda93a2b7b9db4d7cd85a8877674479efbbf20`.
**Continuación de sesión:** la sesión anterior (misma tarea) terminó por
agotamiento de contexto, sin `worker_done` ni commit. Este packet retoma el
diff no commiteado que dejó, lo audita completo contra D70/el brief, lo
completa y lo cierra.
**R3:** corrección sobre `TASOS REVIEW PACKET` (Grok 4.6, read-only,
`/tmp/ludex-coordination/tasos-mon40-review.md`, recomendación `FAIL`) más
un hallazgo adicional del tech lead sobre `|raw|`. T-01/T-02 IMPORTANT
(BLOCKING, aceptados), T-03/T-04 MINOR — los cuatro cerrados en esta ronda.
Ver sección "Corrección R3" abajo.
**Estado:** implementado, worktree limpio tras commit + push → `In Review`.
**No marcar `Completed`** — el veredicto es exclusivo del tech lead.

---

## Corrección R2 (pre-review) — C-01 IMPORTANT

**Contexto R2.** El tech lead levantó Postgres Ludex (`ludex-postgres-1`) sin
detener nada ajeno, tomó backup canónico
`/tmp/backup-phase3-mon40-20260831.dump` (dentro del contenedor) y corrió
`tests/db/test_repository.py` completo SOLO vía `TEST_DATABASE_URL` +
helpers `ludex_test_*` (nunca `DATABASE_URL` ni la base `ludex`): **39
passed / 2 failed**. `docs/AGENT_GOVERNANCE.md` documenta esto como un
`REVIEW PACKET` normal que vuelve a `Changes Requested`, no `Rejected`.

**Hallazgo C-01 (IMPORTANT).**
`test_dos_conexiones_reales_se_serializan_por_metadata_incompatible` y
`test_dos_conexiones_reales_se_serializan_por_winner_incompatible`
(`apps/agent/tests/db/test_repository.py:392` y `:417`) ejecutan
`_SAVE_BATTLE_SQL` **directamente**, con `dict`s de parámetros armados a
mano ANTES de que Task 9 agregara el bind `:replay_url` a esa sentencia
(`db/repository.py`). Los dos `dict`s (`datos1`/`datos2` en cada test) no
incluían `"replay_url"`, así que SQLAlchemy revienta
`InvalidRequestError: A value is required for bind parameter 'replay_url'`
**antes** de que la sentencia llegue a Postgres — ninguna de las dos
pruebas llegaba a ejercer la serialización que dicen probar.

**Búsqueda de otros contratos viejos.** `grep -rn "_SAVE_BATTLE_SQL"
apps/agent/` confirma que estos son los ÚNICOS dos callers de test que
arman el `dict` a mano contra la sentencia real (el resto del árbol usa
`repo.save_battle(...)`, que ya tenía `replay_url: str | None = None` desde
el commit `fc78282`). No quedó ningún otro contrato viejo por corregir.

**Corrección aplicada.** Se agregó `"replay_url": None` a los 4 `dict`s
(`datos1`/`datos2` de ambos tests) — el mismo valor default que
`save_battle` ya usa para cualquier caller que no pase el gancho. Sin
cambios de producción: `db/repository.py`, `cli.py`, `client.py`,
`protocol.py` y `render.ts` quedan intactos, tal como pedía el alcance R2.

**RED exacto (antes de la corrección, contra Postgres real).**
```
$ TEST_DATABASE_URL=postgresql://ludex:ludex@127.0.0.1:15432/postgres \
  pytest -q tests/db/test_repository.py
FAILED tests/db/test_repository.py::test_dos_conexiones_reales_se_serializan_por_metadata_incompatible
FAILED tests/db/test_repository.py::test_dos_conexiones_reales_se_serializan_por_winner_incompatible
2 failed, 39 passed in 9.83s
```
Reproducido byte a byte contra el archivo revertido a HEAD (`git show
HEAD:...| sha256sum` idéntico al del worktree revertido:
`3d1b22351548e7df9874c971d5e29ee6885b664fe9f78687d6b097141926e042`) antes de
volver a aplicar la corrección — el mismo procedimiento de mutación
in-place + SHA-256 de la Task 9 original, esta vez para reproducir un RED
que ya existía en vez de inducirlo.

**GREEN exacto (después de la corrección, contra Postgres real, mismos 41
tests, 0 skips).**
```
$ TEST_DATABASE_URL=postgresql://ludex:ludex@127.0.0.1:15432/postgres \
  pytest -q tests/db/test_repository.py
41 passed in 9.99s
```

**`pnpm --filter @ludex/dataset-audit test` con DB (0 failed, incluidos los
scopes DB).** `db.ts` conecta el CLI real vía `process.env.DATABASE_URL`
(no `TEST_DATABASE_URL`), y `test/cli.test.ts` fija conteos reales del
dataset (16 batallas/2 trayectorias/82 pasos en `scope training`) — una DB
vacía o `TEST_DATABASE_URL` no alcanzan para esos tests. Sin editar
`db.ts`/`cli.test.ts` (fuera de alcance R2), el tech lead autorizó vía `ask`
restaurar un **clon descartable, no la base canónica**, para que
`DATABASE_URL` apunte SOLO a él:

1. Verificado que `ludex_test_mon40_audit_20260831` NO existía
   (`psql -tAc "SELECT 1 FROM pg_database WHERE datname=...'"` → vacío).
2. `CREATE DATABASE ludex_test_mon40_audit_20260831 OWNER ludex` +
   `pg_restore -U ludex -d ludex_test_mon40_audit_20260831 --no-owner
   --no-privileges /tmp/backup-phase3-mon40-20260831.dump` (dentro de
   `ludex-postgres-1`) — 0 errores.
3. Verificado `SELECT current_database()` = el nombre del clon y conteos no
   vacíos: 731 `battles`, 729 `trajectories`, 44949 `trajectory_steps`.
4. El backup canónico no tenía aplicada la migración
   `db/migrations/20260822000001_phase3_hitl.sql` (`trajectory_steps.
   approval_outcome` no existía — `pg_stat_activity`/`schema_migrations`
   confirmaron el gap: última versión aplicada `20260804000001`). Se aplicó
   el bloque `migrate:up` de esa migración **solo en el clon** (`CREATE
   TABLE pending_decisions`, `ALTER TABLE trajectory_steps ADD COLUMN
   approval_outcome` + sus 3 constraints) vía `psql -v ON_ERROR_STOP=1` y se
   registró en `schema_migrations` del clon. Es DDL sobre una base
   descartable propia, no sobre `ludex` — no toca la base canónica ni su
   esquema.
5. Corrida completa con `DATABASE_URL` apuntando SOLO al clon y
   `TEST_DATABASE_URL` apuntando al host de mantenimiento (para que
   `createDisposableDatabase` siga creando sus propios `ludex_test_<uuid>`):
   ```
   $ DATABASE_URL=postgresql://ludex:ludex@127.0.0.1:15432/ludex_test_mon40_audit_20260831 \
     TEST_DATABASE_URL=postgresql://ludex:ludex@127.0.0.1:15432/postgres \
     LUDEX_SHOWDOWN_DEX_DIR=/Users/miguelhernandez/Documents/ludex/apps/agent/.venv/lib/python3.12/site-packages/poke_env/data/static/pokedex \
     pnpm --filter @ludex/dataset-audit test
   Test Files  13 passed (13)
        Tests  214 passed (214)
   ```
   0 failed, incluidos `test/cli.test.ts` (6/6, con los conteos reales
   16/2/82 de `scope training`) y `test/db.test.ts` (9/9, contra el clon
   real con el esquema al día).
6. **Cleanup.** `SELECT pid FROM pg_stat_activity WHERE datname=...` → 0
   filas (sin conexiones colgadas) antes de dropear. `DROP DATABASE
   ludex_test_mon40_audit_20260831` y reverificado `SELECT 1 FROM
   pg_database WHERE datname=...` → vacío (no existe). `SELECT datname FROM
   pg_database WHERE datname LIKE 'ludex_test%'` → vacío: tampoco quedó
   ningún `ludex_test_<uuid>` huérfano de los helpers descartables. La base
   `ludex` y cualquier otro nombre no se tocaron en ningún momento de este
   procedimiento.

**Focal protocol (sin cambios, re-confirmado tras R2):**
```
$ pytest --noconftest -q tests/showdown/test_protocol.py -k "replay_url or elo_bucket"
8 passed, 185 deselected
```

**`git diff --check` (rango R2):** limpio (exit 0). **Scan de secretos**
sobre `test_repository.py`: sin coincidencias.

**No se repitieron las mutaciones de Task 9** (role selection en `render.ts`,
NULL-rating en `protocol.py`): ninguno de esos dos archivos fue tocado en
R2, y el brief de R2 exime repetirlas salvo que se toquen sus archivos.

---

## Corrección R3 (pre-review) — TASOS REVIEW PACKET T-01/T-02/T-03/T-04 +
hallazgo adicional del tech lead

**Contexto R3.** Revisión independiente de Tasos (Grok 4.6, read-only) sobre
el rango `7abda93..d4ac3d5`: `/tmp/ludex-coordination/tasos-mon40-review.md`,
recomendación `FAIL`. Adjudicación del tech lead: T-01 y T-02 BLOCKING y
aceptados; T-03/T-04 también se cierran en esta ronda. El tech lead agregó
un quinto hallazgo propio: `extract_replay_url` no exigía que la línea fuera
de tipo `|raw|`, solo que el regex matcheara en cualquier línea.

### T-01 (IMPORTANT) — `challenge` no forzaba `elo_bucket` NULL

**Hallazgo.** `cli.py:_persist_one` no consultaba `source`: si
`battle.opponent_rating` venía poblado (poke-env lo llena desde cualquier
`|raw|` de rating que aparezca, sin filtrar por tipo de sesión), un
`challenge` terminaba con `elo_bucket` no-NULL, contradiciendo D70/el plan
("challenge rating stays NULL").

**RED (antes de la corrección, `tests/test_cli.py`):**
```
$ pytest --noconftest -q tests/test_cli.py -k persist_one_challenge_fuerza_elo_bucket_none
FAILED test_persist_one_challenge_fuerza_elo_bucket_none_aunque_haya_opponent_rating
AssertionError: challenge tiene que forzar NULL aunque opponent_rating este poblado
assert '1503' is None
1 failed, 1 passed
```

**Corrección.** `_persist_one`: `elo_bucket = None if source == "challenge"
else elo_bucket_from_rating(opponent_rating)`, evaluado ANTES de
`save_trajectory`.

**GREEN:**
```
$ pytest --noconftest -q tests/test_cli.py -k "persist_one_persiste_replay_url_y_elo_bucket_de_ladder or persist_one_challenge_fuerza_elo_bucket_none"
2 passed, 68 deselected
```

**Mutación de regresión.** SHA-256 pre-mutación de `cli.py`:
`d56b944bd0612beffdc1db24ac0d894da1ea916f3ecb4cce7fe3ffd4ff63e7d9`. Se quitó
el `if source == "challenge"` (vuelve a `elo_bucket =
elo_bucket_from_rating(opponent_rating)` sin condición): RED reproducido
exacto (mismo assert de arriba). Restaurado; SHA-256 post-restauración
idéntico.

### T-02 (IMPORTANT) — COALESCE reescribía provenance no-NULL establecida

**Hallazgo.** `COALESCE(EXCLUDED.x, tabla.x)` (orden original) protege
contra que un `NULL` entrante borre un valor conocido, pero un SEGUNDO
valor no-NULL DISTINTO seguía ganando (`EXCLUDED` primero). `elo_bucket` no
tenía ningún test que lo demostrara — quitar su COALESCE en un scratch
independiente de Tasos dejó pasar 3/3.

**RED (antes de la corrección, contra Postgres real, `TEST_DATABASE_URL`):**
```
$ pytest -q tests/db/test_repository.py -k no_se_reescribe_con_otro_distinto
FAILED test_replay_url_ya_establecido_no_se_reescribe_con_otro_distinto
  assert 'https://...-2' == 'https://...-1'
FAILED test_elo_bucket_ya_establecido_no_se_reescribe_con_otro_distinto
  assert '1600' == '1503'
2 failed, 1 passed, 42 deselected
```

**Corrección.** `db/repository.py`: orden invertido a `COALESCE(tabla.x,
EXCLUDED.x)` para AMBAS columnas (`battles.replay_url`,
`trajectories.elo_bucket`) — el valor ya establecido gana, el nuevo solo
completa un NULL previo. Tests nuevos: las dos pruebas de "no reescribe con
otro distinto" (una por columna) más
`test_elo_bucket_de_una_repersistencia_posterior_no_pisa_el_ya_conocido_con_null`
(paridad con el test ya existente de `replay_url`).

**GREEN (suite completa `test_repository.py`, Postgres real):**
```
$ TEST_DATABASE_URL=postgresql://ludex:ludex@127.0.0.1:15432/postgres pytest -q tests/db/test_repository.py
44 passed in 10.69-10.93s
```
(41 de R2 + 3 nuevos de T-02.)

**Mutación de regresión (dos, una por columna).** SHA-256 pre-mutación de
`db/repository.py`:
`672475af77c106981fd7e2d70cfd1e6c682f41d3813e9e53433fd91de4a51aaf`.
(a) Revertir SOLO el orden de `replay_url` a
`COALESCE(EXCLUDED.replay_url, battles.replay_url)`: RED reproducido exacto
en `test_replay_url_ya_establecido_no_se_reescribe_con_otro_distinto`
(1 failed, 1 passed — el de `elo_bucket` no se entera, confirma que las dos
mutaciones son independientes). Restaurado. (b) Revertir SOLO el orden de
`elo_bucket` a `COALESCE(EXCLUDED.elo_bucket, trajectories.elo_bucket)`:
RED reproducido exacto en
`test_elo_bucket_ya_establecido_no_se_reescribe_con_otro_distinto`
(1 failed, 1 passed). Restaurado. SHA-256 post-restauración de
`db/repository.py`: idéntico al pre-mutación.

### T-03 (MINOR) — cableado real y mapeo p1/p2 sin cobertura end-to-end

**Hallazgo.** Los focales de `test_protocol.py` prueban las funciones
puras aisladas; ninguno atraviesa `_persist_one` (borrar `replay_url=`/
`elo_bucket=` de `cli.py` los habría dejado en verde). Los 4 tests DB de
`authorship.test.ts` cargan el dataset global pero ninguno asertaba la
identidad del rival.

**Corrección (cobertura nueva, sin cambios de producción).**
`tests/test_cli.py`:
`test_persist_one_persiste_replay_url_y_elo_bucket_de_ladder` (composición
real: `|raw|` grabado en el recorder + `opponent_rating` → llegan sin
transformación a los kwargs de `save_battle`/`save_trajectory`).
`packages/dataset-audit/test/authorship.test.ts`: nuevo `describe` con una
base descartable real, UNA batalla con DOS trayectorias (`player_side='p1'`
y `'p2'`), `loadDataset(scope: "all")` real, y assert sobre
`renderAuthorshipReport`/`opponentUsername` con las filas REALES devueltas
por Postgres — el primer test que ejercita el mapeo p1/p2 contra un dataset
que de verdad pasó por `loadDataset`.

**GREEN:**
```
$ TEST_DATABASE_URL=... LUDEX_SHOWDOWN_DEX_DIR=... vitest run test/authorship.test.ts
✓ test/authorship.test.ts (14 tests)   14 passed
```
(13 previos + 1 DB nuevo.)

### T-04 (MINOR) — D70/packet stale

**Hallazgo.** El "Límite conocido" de D70 ("repository skipped, offline")
quedaba falso desde R2 (41/41 GREEN real). La mutación de rol de D70/el
packet contaba "4 tests"; remedido en R3: son 3 (el test de `p3`/fail-closed
NO cae con esa mutación — invertir `p1`/`p2` no toca la rama `throw`, es
otro contrato). La frase "sin rama especial" para `elo_bucket` contradecía
el resumen "NULL si ausente o en challenge" del header de este mismo
packet.

**Corrección.** `docs/DECISIONS.md` D70 editado en el cuerpo original (fecha
y autoría de la entrada se preservan) para: (a) reemplazar "sin rama
especial" por la descripción del gate explícito de T-01; (b) corregir el
conteo de la mutación de rol a 3 tests, con la aclaración de por qué el
`p3` no cae; (c) reemplazar el "Límite conocido" stale por los resultados
reales de R2 (41/41) y R3 (44/44, 14/14); y se agregó una entrada nueva
`## D70 (corrección R3)` con el resumen completo de T-01–T-04 y el hallazgo
del `|raw|`. Ver también el header de este packet, ya corregido: ya no
resume "sin rama especial".

### Hallazgo adicional del tech lead — `extract_replay_url` sin filtro de
tipo de línea `|raw|`

**Hallazgo.** El regex de host/slug se aplicaba a CUALQUIER línea del
protocolo, no solo a las de tipo `|raw|` — Showdown es la única fuente que
emite `|raw|` con el link de replay, pero un jugador podía escribir la
misma URL en un mensaje de chat (`|c|...`) y `extract_replay_url` la habría
aceptado igual.

**RED (antes de la corrección):**
```
$ pytest --noconftest -q tests/showdown/test_protocol.py -k extract_replay_url
FAILED test_extract_replay_url_ignora_una_url_identica_fuera_de_una_linea_raw
assert 'https://replay.pokemonshowdown.com/gen6randombattle-386' is None
1 failed, 5 passed, 188 deselected
```

**Corrección.** `protocol.py::extract_replay_url`: `if not
line.startswith("|raw|"): continue` antes de aplicar el regex.

**GREEN:**
```
$ pytest --noconftest -q tests/showdown/test_protocol.py -k "extract_replay_url or elo_bucket"
9 passed, 185 deselected
```

**Mutación de regresión.** SHA-256 pre-mutación de `protocol.py`:
`acc6d2429108caac083f355033a7920127388a467d0a441de1fac0a08cfddb89`. Se quitó
el `if not line.startswith("|raw|"): continue`: RED reproducido exacto
(mismo assert de arriba, 1 failed/5 passed/188 deselected). Restaurado;
SHA-256 post-restauración idéntico.

### Verificación conjunta R3

**`git diff --check` (rango R3):** limpio (exit 0). **Scan de secretos**
sobre los 6 archivos tocados en R3: sin coincidencias. **`grep -ri
"gen6"`:** sin coincidencias nuevas fuera de fixtures/ejemplos ya
preexistentes.

**Suites focales, todas GREEN tras R3:**
- `tests/showdown/test_protocol.py -k "extract_replay_url or elo_bucket"`:
  9 passed.
- `tests/test_cli.py -k persist_one`: incluye los 2 nuevos de T-01/T-03,
  sin regresiones en los ~20 focales de `_persist_one` preexistentes.
- `tests/db/test_repository.py` (Postgres real, `TEST_DATABASE_URL`): 44
  passed.
- `packages/dataset-audit/test/authorship.test.ts` (con `TEST_DATABASE_URL`
  + `LUDEX_SHOWDOWN_DEX_DIR`): 14 passed.

---

## Causa raíz / alcance

No hay bug que corregir: es una feature nueva (ganchos S9). El "problema" que
resuelve esta tarea es que Fase 6 (identidad del rival) necesita
`battles.replay_url` y `trajectories.elo_bucket` poblados de forma que nunca
mientan cuando el dato no está disponible offline. `db/models.py` ya traía
las dos columnas (`replay_url: str | None`, `elo_bucket: str | None`) de una
tarea previa de esquema; esta tarea es exclusivamente el gancho de
extracción + persistencia + reporte.

## Solución aplicada

- **`apps/agent/src/ludex_agent/showdown/protocol.py`**: `extract_replay_url`
  (regex `href="https://replay\.pokemonshowdown\.com/[a-zA-Z0-9][a-zA-Z0-9-]*"`,
  ancla el host exacto vía el propio `"` que cierra el atributo, así que un
  typosquat/subdominio no matchea) y `elo_bucket_from_rating`
  (`str(rating)` o `None`, sin bucketing).
- **`apps/agent/src/ludex_agent/showdown/client.py`**: `LudexPlayer.replay_url(tag)`,
  gancho pasivo que delega en `extract_replay_url` sobre
  `recorder.all_lines` (D17: el protocolo crudo es el juez) — nunca
  construye nada a partir de `tag`.
- **`apps/agent/src/ludex_agent/cli.py`** (`_persist_one`, líneas ~298-341):
  calcula `replay_url`/`elo_bucket` y los pasa a `save_battle`/`save_trajectory`.
  `opponent_rating` sale de `getattr(battle, "opponent_rating", None)` porque
  los dobles de test no siempre lo definen; el atributo real de poke-env es
  `Optional[int]` con default `None`, así que el `getattr` representa el
  mismo estado, no una excusa sin diagnosticar.
- **`apps/agent/src/ludex_agent/db/repository.py`**: `save_battle`/
  `save_trajectory` aceptan los dos campos opcionales (default `None`, no
  rompen callers existentes) y el `ON CONFLICT` usa
  `COALESCE(EXCLUDED.x, tabla.x)` — una re-persistencia sin el dato (p.ej.
  antes de que Showdown emita el `|raw|` de replay al cerrar la sala) no pisa
  un valor ya conocido con `NULL`.
- **`packages/dataset-audit/src/render.ts`**: `opponentUsername(battle, playerSide)`
  resuelve el rival por rol (`p1`→`battle.p2`, `p2`→`battle.p1`), falla
  cerrado (`throw`) ante cualquier otro valor de `playerSide`, y
  `renderAuthorshipReport` agrega una sección "Identidad del rival" por
  trayectoria (batalla huérfana → mensaje explícito, nunca un rival
  inventado).

## Tests agregados

**Task 9 (original):**
- `apps/agent/tests/showdown/test_protocol.py`: 8 tests nuevos —
  `extract_replay_url` (link real, ausencia, host falso/typosquat, esquema
  no-`https`, primero-de-varios) y `elo_bucket_from_rating` (None→None,
  rating público→string exacto, canario nombrado de no-bucketing:
  `1499 != elo_bucket_from_rating(1501)` y ambos preservan su valor exacto).
- `apps/agent/tests/db/test_repository.py`: 3 tests nuevos — persistencia
  redonda (`replay_url`/`elo_bucket` presentes), NULL sin dato público
  (`challenge`), y COALESCE en re-persistencia sin pisar un valor conocido.
- `packages/dataset-audit/test/authorship.test.ts`: `opponentUsername`
  (p1→rival p2, p2→rival p1, `player_side` desconocido falla cerrado) +
  `renderAuthorshipReport` (lista rival por trayectoria normalizado,
  distingue p1/p2 sin repetir el propio nombre).

**R2:** ningún test nuevo — 4 dicts corregidos en los 2 tests concurrentes
existentes (C-01).

**R3 (ver sección "Corrección R3" arriba para RED/GREEN/mutación de cada
uno):**
- `apps/agent/tests/showdown/test_protocol.py`: 1 test nuevo —
  `test_extract_replay_url_ignora_una_url_identica_fuera_de_una_linea_raw`
  (hallazgo del tech lead: tipo de línea `|raw|` obligatorio).
- `apps/agent/tests/test_cli.py`: 2 tests nuevos —
  `test_persist_one_persiste_replay_url_y_elo_bucket_de_ladder` (T-03,
  cableado real vía `_persist_one`) y
  `test_persist_one_challenge_fuerza_elo_bucket_none_aunque_haya_opponent_rating`
  (T-01).
- `apps/agent/tests/db/test_repository.py`: 3 tests nuevos —
  `test_elo_bucket_de_una_repersistencia_posterior_no_pisa_el_ya_conocido_con_null`,
  `test_replay_url_ya_establecido_no_se_reescribe_con_otro_distinto`,
  `test_elo_bucket_ya_establecido_no_se_reescribe_con_otro_distinto` (T-02).
- `packages/dataset-audit/test/authorship.test.ts`: 1 test nuevo —
  identidad del rival contra una base descartable real, con trayectorias
  `p1` Y `p2` de la MISMA batalla, sobre el dataset completo cargado por
  `loadDataset` (T-03).

Todos verificados en R2/R3 contra Postgres real (`TEST_DATABASE_URL` +
helpers `ludex_test_*`, nunca `DATABASE_URL` ni la base `ludex`) — GREEN:
`test_repository.py` 44/44, `authorship.test.ts` 14/14.

## Comando de verificación y resultado completo

**Pin de venv verificado** (evita el falso verde de MON-20 R8 — el venv de
`/Users/miguelhernandez/Documents/ludex/apps/agent/.venv` es un install
editable):

```
$ env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin PYTHONPATH=$PWD/src \
    /Users/miguelhernandez/Documents/ludex/apps/agent/.venv/bin/python \
    -c "import ludex_agent.showdown.protocol as p; print(p.__file__)"
/Users/miguelhernandez/orca/workspaces/ludex/phase3-s9-provenance-ws/apps/agent/src/ludex_agent/showdown/protocol.py
```

**Focal, protocol:**
```
$ pytest --noconftest -q tests/showdown/test_protocol.py -k "replay_url or elo_bucket"
8 passed, 185 deselected
```

**Focal, repository (offline en la sesión original → skip por diseño,
MON-11/R2; verificado GREEN contra Postgres real en R2, ver "Corrección R2"
arriba):**
```
$ pytest --noconftest -q tests/db/test_repository.py -k "replay_url or elo_bucket"
3 skipped, 38 deselected
```

**Suite Python completa offline:**
```
$ pytest -q --ignore=tests/integration/test_langgraph_battle.py --ignore=tests/api
796 passed, 174 skipped, 2 warnings
```
(`--ignore=tests/api` es preexistente: el venv compartido no tiene `fastapi`
instalado; no relacionado con este diff — confirmado corriendo la suite
completa sin tocar `tests/api`.)

**dataset-audit, focal:**
```
$ npx vitest run test/authorship.test.ts
✓ test/authorship.test.ts (13 tests | 4 skipped)   9 passed | 4 skipped
```

**dataset-audit, suite completa (`pnpm --filter @ludex/dataset-audit test`):**
```
Test Files  2 failed | 9 passed | 2 skipped (13)
     Tests  4 failed | 183 passed | 18 skipped (205)
```
Las 4 fallas son en `test/cli.test.ts`, todas por falta de `DATABASE_URL`/
Postgres/dex local — confirmado que existen **idénticas contra la base sin
este diff** (stash temporal de los 8 archivos, mismas 4 fallas, mismo
mensaje; restaurado con `git stash apply <sha>` + `git stash drop` del
mismo entry, sin tocar el stash compartido de otros agentes). No relacionado
con esta tarea ni con la restricción offline de ese turno. **Resuelto en R2**
con Postgres real disponible (ver "Corrección R2" arriba): la suite completa
da 214/214, 0 failed.

## Prueba de regresión (mutación, restaurada por SHA-256)

**Mutación 1 — selección de rol p1/p2** (`packages/dataset-audit/src/render.ts`,
`opponentUsername`): se invirtió `p1`→`battle.p2` a `p1`→`battle.p1` (y
simétrico para `p2`) in-place sobre el worktree.
- SHA-256 pre-mutación: `d5c7f6e1f6aa497d6f199a20a1f2bbf84daeaa33a82fc20696930f1a8d86bb3e`.
- Test que falló: **3** tests nombrados de `test/authorship.test.ts` (los 3
  de `describe("opponentUsername")` que ejercen `p1`/`p2`, MÁS
  `renderAuthorshipReport > una trayectoria de p2 muestra a p1 como rival`
  — corrección de conteo en R3, TASOS REVIEW PACKET T-04: esta sección
  decía "4 tests" originalmente, contados sin volver a correr la mutación.
  El cuarto test de `describe("opponentUsername")`, el de `p3` que verifica
  el fail-closed ante un `playerSide` desconocido, NO cae con esta mutación
  — invertir `p1`/`p2` no toca la rama `throw`, es un contrato distinto).
- Restaurado (reescritura manual desde el diff capturado, **no** `git
  checkout` — ver nota abajo); SHA-256 post-restauración: idéntico
  (`d5c7f6e1f6aa4...86bb3e`); suite focal vuelve a 9 passed | 4 skipped
  (offline original) / 14 passed (R3, con `TEST_DATABASE_URL`).

**Mutación 2 — NULL-rating** (`apps/agent/src/ludex_agent/showdown/protocol.py`,
`elo_bucket_from_rating`): `opponent_rating is None` pasó a devolver
`"unrated"` en vez de `None`.
- SHA-256 pre-mutación: `8ef0cb67ae935ecb97fba60dc9bf0812d15a7dfa59d4981ad087a3bc58449590`.
- Test que falló: `test_elo_bucket_from_rating_none_da_none`
  (`assert 'unrated' is None` → `AssertionError`).
- Restaurado con `Edit` puntual sobre la línea mutada; SHA-256
  post-restauración: idéntico; suite focal vuelve a 8 passed.

**Nota operativa sobre la primera restauración.** Al recuperar la mutación 1
usé por error `git checkout -- packages/dataset-audit/src/render.ts`, que
revirtió TODO el archivo a HEAD (`7abda93`) — no solo la línea mutada,
porque el diff completo de esta tarea seguía sin commitear. Se reconstruyó
el archivo entero a mano a partir del diff que ya había capturado en la
inspección inicial (import de `BattleRecord`, función `opponentUsername`,
sección del reporte), y se verificó **exactamente** contra el SHA-256
pre-mutación antes de seguir. Se documenta como aprendizaje operativo: para
restaurar una mutación in-place sobre un archivo con diff sin commitear,
usar `Edit` puntual (o `sha256sum` contra un backup explícito), nunca `git
checkout` sobre un archivo con cambios no commiteados de la propia tarea.

## Integraciones ejecutadas

**Task 9 (sesión original):** ninguna contra DB/Docker/red real —
restricción explícita offline de ese turno.
**R2 (esta corrección):** capa de integración completa contra Postgres real
ejecutada por el tech lead (`ludex-postgres-1`, `tests/db/test_repository.py`
vía `TEST_DATABASE_URL` + helpers `ludex_test_*`, RED 2 failed/39 passed) y
por este agente (GREEN 41/41 tras el fix, más `pnpm --filter
@ludex/dataset-audit test` con `DATABASE_URL` apuntando a un clon
descartable `ludex_test_mon40_audit_20260831` restaurado del backup
canónico y dropeado al cierre — ver "Corrección R2" arriba). La base `ludex`
nunca se escribió en ningún momento de R2.

## Datos inspeccionados

`apps/agent/src/ludex_agent/db/models.py` (columnas `replay_url`/
`elo_bucket` ya existentes, tipos `Mapped[str | None]`), diff completo de
los 8 archivos originales del brief (auditado línea por línea contra D70
antes de continuar — ninguno contradecía el ruling; el trabajo de la sesión
anterior ya cumplía "nunca inventar", solo faltaba cerrar
DECISIONS/evidencia/mutaciones/commit/push), `.git` (worktree list, stash
list, `git diff --check`). En R2: `_SAVE_BATTLE_SQL` (único caller sin
`replay_url` era el par de tests de C-01, confirmado por `grep`), el
esquema real del backup canónico (`\d trajectory_steps`, `schema_migrations`)
y el diff de la migración `20260822000001_phase3_hitl.sql` antes de
aplicarla al clon.

## Decisiones agregadas a DECISIONS.md

`docs/DECISIONS.md` D70 (autorizado explícitamente como noveno path por el
tech lead) — ruling completo transcripto, motivo, verificación por mutación
con SHA-256 y suites ejecutadas. R2 no agrega una decisión nueva: es
evidencia adicional sobre el mismo D70 (cierre del hallazgo C-01 y
verificación de integración que faltaba). **R3** corrige el cuerpo de D70
(gate explícito de `elo_bucket` por `source`, conteo real de la mutación de
rol, resultados reales de R2/R3 reemplazando el límite stale) y agrega
`## D70 (corrección R3)` con el resumen de T-01–T-04 y el hallazgo del
`|raw|`.

## Limitaciones conocidas

Ninguna.

## Riesgos o dudas pendientes

Ninguno. Los tres "open questions" que el `TASOS REVIEW PACKET` dejaba
abiertas quedaron resueltas por la adjudicación del tech lead: (1) D70 se
corrigió para reflejar el gate explícito de `source == "challenge"`, no
"sin rama especial"; (2) el orden del `COALESCE` es "el valor ya
establecido gana" (`COALESCE(tabla.x, EXCLUDED.x)`), consistente en
`replay_url` y `elo_bucket`; (3) no se necesitó medir en vivo si Showdown
emite `|raw|...rating...` en challenge rateado — el gate por `source` hace
que el resultado sea NULL de cualquier forma.

## Commits

Ver `git log` del branch `migueljh/phase3-s9-provenance-ws`:
- Commit Task 9: rutas explícitas cubriendo los 8 archivos del brief más
  `docs/DECISIONS.md` y este packet.
- Commit R2: `apps/agent/tests/db/test_repository.py` (fix C-01) y este
  packet actualizado.
- Commit R3: `apps/agent/src/ludex_agent/cli.py` (T-01),
  `apps/agent/src/ludex_agent/db/repository.py` (T-02),
  `apps/agent/src/ludex_agent/showdown/protocol.py` (hallazgo `|raw|`),
  `apps/agent/tests/test_cli.py` (T-01/T-03),
  `apps/agent/tests/db/test_repository.py` (T-02),
  `apps/agent/tests/showdown/test_protocol.py` (hallazgo `|raw|`),
  `packages/dataset-audit/test/authorship.test.ts` (T-03),
  `docs/DECISIONS.md` (T-04) y este packet.

**Modelo efectivo:** Sonnet 5 (Neoblex), Task 9 + correcciones pre-review
R2/R3 de MON-40. Sin recomendación propia de estado: tech lead adjudica.
