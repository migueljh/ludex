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
  Requieren `TEST_DATABASE_URL` (MON-11/R2) — ver "Limitaciones conocidas".
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

**Focal, repository (offline → skip por diseño, MON-11/R2):**
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
con esta tarea ni con la restricción offline de este turno.

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

Ninguna contra DB/Docker/red real — restricción explícita de esta tarea
("totalmente offline; no DB, Docker, providers, live"). La capa de
integración con Postgres (`tests/db/test_repository.py` completo) queda
pendiente para una sesión con `TEST_DATABASE_URL` disponible; ver
limitaciones.

## Datos inspeccionados

`apps/agent/src/ludex_agent/db/models.py` (columnas `replay_url`/
`elo_bucket` ya existentes, tipos `Mapped[str | None]`), diff completo de
los 8 archivos originales del brief (auditado línea por línea contra D70
antes de continuar — ninguno contradecía el ruling; el trabajo de la sesión
anterior ya cumplía "nunca inventar", solo faltaba cerrar
DECISIONS/evidencia/mutaciones/commit/push), `.git` (worktree list, stash
list, `git diff --check`).

## Decisiones agregadas a DECISIONS.md

`docs/DECISIONS.md` D70 (autorizado explícitamente como noveno path por el
tech lead) — ruling completo transcripto, motivo, verificación por mutación
con SHA-256, suites ejecutadas y límite conocido de la capa DB offline.

## Limitaciones conocidas

- `tests/db/test_repository.py` (incl. los 3 tests nuevos de esta tarea)
  requiere `TEST_DATABASE_URL` y quedó en `skipped` por la restricción
  offline; falta correrlo contra Postgres real antes de cerrar la rebanada
  completa (capa de integración de `.claude/verification/SKILL.md`).
- Las 4 fallas de `test/cli.test.ts` son preexistentes (confirmadas también
  contra la base) y fuera de alcance; no se tocó ese archivo.

## Riesgos o dudas pendientes

Ninguno nuevo. El único punto abierto es la verificación de integración con
DB real, ya documentado arriba como límite conocido y acotado a
`test_repository.py`.

## Commits

Ver `git log` del branch `migueljh/phase3-s9-provenance-ws` — commit único
en inglés con rutas explícitas cubriendo los 8 archivos del brief más
`docs/DECISIONS.md` y este packet.

**Modelo efectivo:** Sonnet 5 (Neoblex), continuación de MON-40 Task 9.
Sin recomendación propia de estado: Tasos/tech lead adjudica.
