# MON-39 — aceptación offline completa de Fase 3

Fecha: 2026-08-31  
Issue: MON-39  
Base de fase: `5868c087aee0bee0ec14c004fe06ed5cbdbd697b`  
Head de código verificado: `e945512514613a799f3b976fdfea360c9f56a9d4`  
Rama: `migueljh/phase3-integration`

Linear no estaba conectado durante esta aceptación (`linear_not_connected`).
Por eso se eligió este path estable bajo `docs/evidence/phase3/`, se dejó el
estado operativo sin inventar y el veredicto deberá aplicarse en Linear cuando
vuelva la conexión.

## REVIEW PACKET

**Issue:** MON-39  
**Commit(s):** rango completo de fase desde la Base indicada; el packet se
versiona en el commit que contiene este archivo.  
**Archivos modificados por MON-39:**

- `apps/agent/tests/hitl/test_gate.py`: cierre test-only del menor T-02
  diferido desde MON-32.
- `docs/DECISIONS.md`: D71 y saneamiento de un path personal histórico.
- `.superpowers/sdd/2026-08-22-phase-3-implementation/task-9-report.md`:
  saneamiento de paths personales, sin cambiar resultados.
- Este packet.

**Causa raíz:** el gate offline final no tenía todavía un artefacto durable
que reuniera suites, auditorías globales, queries D38/D44, scans y todas las
pruebas de mutación. Además, el canario de fuente de MON-32 buscaba substrings
y no cerraba las evasiones por alias/`getattr` que Tasos había diferido a
MON-39.

**Solución aplicada:** se verificó el rango aceptado de Tasks 1–9 contra un
clon no vacío, se migró la base canónica únicamente después de backup para que
los tests read-only vieran el esquema aceptado, se reemplazó el canario frágil
por análisis AST con no-vacuidad y se consolidó la evidencia en este packet.
No cambió código de producción en MON-39.

## Entorno y frontera de datos

- Servicios Ludex verificados: Postgres en `127.0.0.1:15432`, Showdown en
  `127.0.0.1:8100` y calc en `127.0.0.1:8200`.
- Backup nuevo antes del DDL canónico: `/tmp/backup-mon39-post-recreate-20260831.dump`
  dentro de `ludex-postgres-1` (13 MB).
- Clon intencional: `ludex_test_mon39_gate_20260831`, restaurado con 731
  batallas, 729 trayectorias y 44.949 pasos; migración máxima
  `20260822000001`.
- La base canónica pasó de migración máxima `20260804000001` a
  `20260822000001`; los conteos 731/729/44.949 quedaron idénticos.
- Toda suite con escrituras usó helpers `ludex_test_*` o el clon; las queries
  directas y los scopes del auditor leyeron el clon. No hubo juego oficial.

Incidente operativo documentado: una primera invocación de Compose desde el
worktree no recibió las variables del `.env` y recreó únicamente
`ludex-postgres-1` sobre su volumen existente. No se detuvo ni eliminó ningún
servicio y los conteos persistieron. Se tomó un backup nuevo, se restablecieron
los puertos explícitos de Ludex y toda invocación posterior usó variables
completas y `--no-deps`. El backup que estaba solo en `/tmp` del contenedor
anterior se perdió con la recreación; el backup nuevo indicado arriba lo
reemplaza.

## Verificación completa

### Python

Comando saneado:

```sh
cd apps/agent
env DATABASE_URL=<CANONICAL_READONLY_DSN> \
  TEST_DATABASE_URL=<MAINTENANCE_DSN> \
  SHOWDOWN_WS_URL=ws://127.0.0.1:8100/showdown/websocket \
  PYTHONPATH="$PWD/src" uv run pytest -q
```

Resultado final sobre `e945512`:

```text
1043 passed, 1 skipped, 3 warnings in 101.70s
```

Las tres warnings son deprecaciones de Starlette TestClient y
`ConnectionClosed.code`; no son fallos funcionales.

Durante el diagnóstico hubo dos resultados no aceptados como evidencia final:

- Al apuntar `DATABASE_URL` al clon, dos guardias que exigen reconocer la DB
  canónica no levantaron; el entorno del test era incorrecto, no el código.
- Un test de batalla random excedió una vez 45 s y otro observó una vez un
  turno desalineado. El primero está documentado como variabilidad previa en
  D46. El segundo pasó tres reproducciones consecutivas (11.46 s, 11.20 s y
  11.57 s) y luego la suite completa final. Se conservan como riesgo conocido,
  no como prueba descartada silenciosamente.

### Monorepo TypeScript

Comando saneado, con Node `v22.16.0`:

```sh
env DATABASE_URL=<CLONE_DSN> TEST_DATABASE_URL=<MAINTENANCE_DSN> \
  LUDEX_SHOWDOWN_DEX_DIR=<POKE_ENV_DEX_DIR> pnpm test
```

Resultado:

| paquete | resultado |
|---|---:|
| calc | 50 passed |
| teams | 15 passed |
| seed | 122 passed |
| dataset-audit | 215 passed |

Total: 402 tests, cero fallos. El primer intento con Node 18 se rechazó antes
de ejecutar tests porque el repo exige Node `>=22.13`; no fue un fallo del
código.

### Auditoría no vacua del dataset

Comando correcto desde `packages/dataset-audit`:

```sh
pnpm exec tsx src/cli.ts audit --scope all
pnpm exec tsx src/cli.ts audit --scope training
```

`scope training` terminó con exit 0:

- 16 batallas, 2 trayectorias y 82 pasos.
- 82 filas `v2` y cero violaciones.
- Cero para los 11 campos de hidden information y para
  `action_in_mask`, `action_turn`, `decision_index`, `state_rederivable`,
  `reward_propagation`, `schema_version` y `orphans`.
- 6 queries, 1.144 líneas de protocolo, 418 entradas rivales y 4.598 checks.

`scope all` terminó con exit 1, esperado por el corpus histórico:

- 731 batallas, 729 trayectorias y 44.949 pasos.
- 47.481 violaciones de hidden information y 47 de `decision_index`.
- Cero para máscara, turno de acción, estado rederivable, reward, schema y
  orfandad.
- Split: `v1` 31.206 filas/46.341 violaciones; `v2` 13.743 filas/1.187
  violaciones.
- 6 queries, 610.751 líneas, 236.435 entradas rivales y 2.600.785 checks.

El scope global no se filtró ni se reinterpretó como verde: conserva la deuda
histórica conocida. El gate de entrenamiento es limpio y no vacuo.

### Queries directas D38 y D44

Resultado D38 sobre el clon:

```text
d38_human_override_metadata_nonnull       0
d38_agent_outcome_metadata_incomplete     0
d38_action_source_outcome_incoherent      0
```

Resultado D44:

```text
d44_eligible_trajectories                  2
d44_eligible_steps                        82
d44_selected_non_v2_violations             0
d44_selected_zero_step_violations          0
d44_selected_test_source_violations        0
d44_selected_unfinished_violations         0
```

## Auditoría de mutaciones Tasks 1–9

Se cruzaron los registros versionados, D65–D71 y los resultados durables de
Orca. Cada grupo siguiente tiene path pineado, canario nombrado, RED observado
y restauración GREEN; no se aceptó un mero baseline rojo.

| tarea | mutaciones auditadas y RED | restauración / fuente durable |
|---|---|---|
| 1 / MON-31 | guard de `DATABASE_ROLE=acceptance` y rechazo de DSN canónico; cada guard quitado produjo `DID NOT RAISE` en su canario | SHA-256 idéntico; D65, `test_config.py`; 18 focales y 696/138 offline |
| 2 / MON-32 | ganador CAS, clamp de deadline, `_was_pending`, override ilegal y replay-gap; T-02 final agregó alias `wait_for`, `getattr(wait_for)` y `self._future.cancel()`, todos RED nombrados | `gate.py` SHA-256 `70d3709b…9cd349d` restaurado; `test_gate.py`; 38/38 HITL |
| 3 / MON-33 | nueve CHECKs nuevos, query D38 global, autoría/global-query y teardown descartable: barrier, fail-open, connection errors y double-drop/catch-all | commits `e14d3e6`, `6b47796`, `f646b1c`, `3d35813`; resultados Orca R1–R5; 203/203 auditor en clon y tres corridas 209/209 en R5 |
| 4 / MON-34 | ocho categorías iniciales de API/registry/WS más remoción de tres mecanismos de tests; R2: colisión cross-loop por `id()`, tragar `ReplayGapError` y quitar fan-out | commits `8fbd752`, `e13f6df`; D66; restauración byte a byte; focales integrados verdes |
| 5 / MON-35 | gate después de execute, doble execute, source hardcodeado, grupo D38 incompleto, políticas offline cambiadas, registry sin discard y matrix sin delegación | D67; cada mutación RED y SHA restaurado; focales de gate/observabilidad verdes |
| 6 / MON-36 | sin guard de DB, sin deadline, sin exclusión de sesión, sin stop y sin replay-gap; R2 revirtió cada finding T-01–T-05 | Task 6 packet + D68; 5 mutaciones iniciales y 5 correcciones RED→GREEN; 809/174 offline |
| 7 / MON-37 | reintroducir auto-enqueue y aceptar challenge desconocido; R2 renombró ambos seams productivos para volver a callbacks de poke-env | Task 7 packet + D69; 2/3 canarios productivos RED en seam-rebind; 20/20 focal y 829/174 offline |
| 8 / MON-38 | ocho interlocks de ladder; R2 movió la reserva después del primer `await`, reproduciendo `[200,200]` en vez de `[200,409]` | Task 8 packet; hashes restaurados; 46 passed/3 skipped focal y 850/174 offline |
| 9 / MON-40 | selección p1/p2, NULL rating, challenge con rating, ambos órdenes COALESCE, tipo `|raw|`, y R4 NULL→valor para replay/rating | Task 9 packet + D70; mutaciones independientes RED, hashes restaurados; protocolo 9/9, repo 46/46, autoría 14/14 |

La única brecha diferida en las revisiones previas era MON-32 T-02; quedó
cerrada en `e945512`. No queda registro de mutación sin RED en Tasks 1–9.

## Scans y formato

- `git diff --check` sobre el rango completo: limpio.
- JSON cambiados en el rango: 0; parseo no aplica.
- `gitleaks`: no instalado en el entorno.
- Scan dirigido del diff para private keys, tokens OpenAI/Google/GitHub,
  bearer tokens y access keys AWS: cero matches.
- Scan de paths personales absolutos: cero matches después de sanear el packet
  de Task 9 y D70.
- No se versionan DSNs reales, claves ni valores de `.env` en este packet.

## Integraciones ejecutadas

- Tasks 1–9 integradas en orden sobre `migueljh/phase3-integration`.
- Ramas remotas `origin/integration/phase-3-accepted` y
  `origin/migueljh/phase3-integration` se actualizarán al commit de este packet
  después del veredicto técnico offline.
- Postgres real se usó solo mediante clon/read-only salvo el DDL aditivo ya
  aceptado y respaldado; Showdown local se usó para la suite, nunca para juego
  oficial.

## Limitaciones y riesgos conocidos

- Linear desconectado: MON-39 no puede recibir el packet ni cambiar de estado
  todavía.
- El corpus `scope all` mantiene deuda histórica; `scope training` es el
  conjunto elegible limpio y no vacío.
- Queda variabilidad preexistente en batallas random largas; la suite final y
  las reproducciones focales fueron verdes.
- MON-39 es únicamente el gate offline. Aun con veredicto PASS, Fase 3 no está
  completa hasta MON-41 (challenge oficial controlado) y MON-42 (una batalla
  ranked con cuenta de testing, DB descartable y presupuesto autorizado).

## Riesgos o dudas pendientes

No hay finding técnico offline abierto al emitir este packet. Tasos debe
revisar read-only el rango exacto Base..Head y Latwan debe adjudicar. Las
ejecuciones live posteriores siguen sujetas a credenciales, cuenta de testing,
DB de aceptación no canónica y las restricciones de modelos/costo de AGENTS.md.
