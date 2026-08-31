# REVIEW PACKET — MON-40, Fase 3 Task 9 / F3-09 (provenance hooks)

**Issue:** MON-40 / F3-09 — "Persist narrow Phase 6 hooks and finish audit
reporting" (`.superpowers/sdd/2026-08-22-phase-3-implementation/task-9-brief.md`).
**Ruling vinculante del tech lead:** `docs/DECISIONS.md` D70 — `replay_url`
nunca se deriva de `battle_tag` (NULL salvo URL `https` explícita, host
exacto `replay.pokemonshowdown.com`, slug estricto, sin `/savereplay`,
`uploadreplay` ni red); `elo_bucket` usa solo `battle.opponent_rating`
público del rival, `str` decimal canónico, nunca rating propio/rangos/labels/
redondeo, NULL si ausente o en `challenge`.
**Worktree:** `phase3-s9-provenance-ws`, branch `migueljh/phase3-s9-provenance-ws`.
**Base SHA aceptada:** `7abda93a2b7b9db4d7cd85a8877674479efbbf20`.
**Continuación de sesión:** la sesión anterior (misma tarea) terminó por
agotamiento de contexto, sin `worker_done` ni commit. Este packet retoma el
diff no commiteado que dejó, lo audita completo contra D70/el brief, lo
completa y lo cierra.
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

- `apps/agent/tests/showdown/test_protocol.py`: 8 tests nuevos —
  `extract_replay_url` (link real, ausencia, host falso/typosquat, esquema
  no-`https`, primero-de-varios) y `elo_bucket_from_rating` (None→None,
  rating público→string exacto, canario nombrado de no-bucketing:
  `1499 != elo_bucket_from_rating(1501)` y ambos preservan su valor exacto).
- `apps/agent/tests/db/test_repository.py`: 3 tests nuevos — persistencia
  redonda (`replay_url`/`elo_bucket` presentes), NULL sin dato público
  (`challenge`), y COALESCE en re-persistencia sin pisar un valor conocido.
  Requieren `TEST_DATABASE_URL` (MON-11/R2); verificados en R2 contra
  Postgres real (ver "Corrección R2" arriba) — GREEN, 41/41.
- `packages/dataset-audit/test/authorship.test.ts`: `opponentUsername`
  (p1→rival p2, p2→rival p1, `player_side` desconocido falla cerrado) +
  `renderAuthorshipReport` (lista rival por trayectoria normalizado,
  distingue p1/p2 sin repetir el propio nombre).

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
- Test que falló: 4 tests nombrados de `test/authorship.test.ts` (los 3 de
  `describe("opponentUsername")` + `renderAuthorshipReport > una trayectoria
  de p2 muestra a p1 como rival`).
- Restaurado (reescritura manual desde el diff capturado, **no** `git
  checkout` — ver nota abajo); SHA-256 post-restauración: idéntico
  (`d5c7f6e1f6aa4...86bb3e`); suite focal vuelve a 9 passed | 4 skipped.

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
verificación de integración que faltaba).

## Limitaciones conocidas

Ninguna. La limitación documentada en la versión anterior de este packet
("`test_repository.py` no verificado contra Postgres real") quedó resuelta
en R2: 41/41 GREEN contra Postgres real, y `pnpm --filter
@ludex/dataset-audit test` da 214/214 con los scopes DB incluidos.

## Riesgos o dudas pendientes

Ninguno.

## Commits

Ver `git log` del branch `migueljh/phase3-s9-provenance-ws`:
- Commit Task 9: rutas explícitas cubriendo los 8 archivos del brief más
  `docs/DECISIONS.md` y este packet.
- Commit R2: `apps/agent/tests/db/test_repository.py` (fix C-01) y este
  packet actualizado.

**Modelo efectivo:** Sonnet 5 (Neoblex), Task 9 + corrección pre-review R2
de MON-40. Sin recomendación propia de estado: Tasos/tech lead adjudica.
