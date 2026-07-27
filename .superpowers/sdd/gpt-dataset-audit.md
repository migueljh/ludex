# Tarea B — `packages/dataset-audit`

## Status

Completa. El paquete implementa una segunda verificación independiente en
Node/TypeScript. No importa código de los tests Python ni de `apps/agent`.
Carga el dataset completo con cuatro consultas `SELECT`, y el pool fuerza
`default_transaction_read_only=on` como defensa adicional.

Comandos:

```text
pnpm --filter @ludex/dataset-audit audit [--gen N]
pnpm --filter @ludex/dataset-audit battle <battle-tag|id> [--gen N]
```

La generación se toma de `trajectories.gen_id -> generations.gen_number` y
puede filtrarse con `--gen`. No hay conteos de batallas fijados en tests.

## Verificación

```text
Test Files  4 passed (4)
Tests       14 passed (14)
```

`tsc --noEmit` terminó con código 0. Los tests incluyen fixtures derivados de
filas reales, `Mr. Mime`, `Farfetch'd`, diacríticos, comparación de protocolo
línea por línea, múltiples decisiones por turno, las seis invariantes, loader
real read-only, renderer y CLI.

## Auditoría completa

Snapshot auditado: 112 batallas, 111 trayectorias y 6.625 pasos. El número era
móvil porque otra suite estaba grabando batallas; no se fija como expectativa.

Resultado:

- Sin fuga de información oculta.
- 41 filas donde `trajectory_steps.turn_number` no coincide con
  `state.turn`, en siete batallas.
- Todos los pasos tienen protocolo crudo correspondiente.
- Todos los rewards de trayectorias terminadas están propagados con el signo
  correcto.
- Una única versión de esquema, consistente entre columna y JSON.
- Sin trayectorias, pasos ni turnos huérfanos.

Salida verbatim:

```text
Dataset: 112 batallas · 111 trayectorias · 6625 pasos
PASS hidden_information: 0
FAIL action_turn: 41
PASS state_rederivable: 0
PASS reward_propagation: 0
PASS schema_version: 0
PASS orphans: 0

Violaciones:
  - action_turn · battle-gen6randombattle-266 · p1 · turno 68 · decisión 66: fila turn_number=68 pero state.turn=63
  - action_turn · battle-gen6randombattle-266 · p1 · turno 69 · decisión 67: fila turn_number=69 pero state.turn=63
  - action_turn · battle-gen6randombattle-266 · p1 · turno 72 · decisión 68: fila turn_number=72 pero state.turn=64
  - action_turn · battle-gen6randombattle-266 · p1 · turno 73 · decisión 69: fila turn_number=73 pero state.turn=65
  - action_turn · battle-gen6randombattle-266 · p1 · turno 75 · decisión 70: fila turn_number=75 pero state.turn=66
  - action_turn · battle-gen6randombattle-266 · p1 · turno 76 · decisión 71: fila turn_number=76 pero state.turn=67
  - action_turn · battle-gen6randombattle-266 · p1 · turno 79 · decisión 72: fila turn_number=79 pero state.turn=68
  - action_turn · battle-gen6randombattle-266 · p1 · turno 81 · decisión 73: fila turn_number=81 pero state.turn=69
  - action_turn · battle-gen6randombattle-266 · p1 · turno 82 · decisión 76: fila turn_number=82 pero state.turn=72
  - action_turn · battle-gen6randombattle-266 · p1 · turno 85 · decisión 79: fila turn_number=85 pero state.turn=75
  - action_turn · battle-gen6randombattle-266 · p1 · turno 88 · decisión 80: fila turn_number=88 pero state.turn=76
  - action_turn · battle-gen6randombattle-266 · p1 · turno 92 · decisión 82: fila turn_number=92 pero state.turn=78
  - action_turn · battle-gen6randombattle-267 · p1 · turno 28 · decisión 28: fila turn_number=28 pero state.turn=27
  - action_turn · battle-gen6randombattle-267 · p1 · turno 34 · decisión 32: fila turn_number=34 pero state.turn=31
  - action_turn · battle-gen6randombattle-269 · p1 · turno 31 · decisión 18: fila turn_number=31 pero state.turn=18
  - action_turn · battle-gen6randombattle-269 · p1 · turno 32 · decisión 21: fila turn_number=32 pero state.turn=20
  - action_turn · battle-gen6randombattle-269 · p1 · turno 38 · decisión 23: fila turn_number=38 pero state.turn=22
  - action_turn · battle-gen6randombattle-269 · p1 · turno 39 · decisión 24: fila turn_number=39 pero state.turn=23
  - action_turn · battle-gen6randombattle-269 · p1 · turno 40 · decisión 25: fila turn_number=40 pero state.turn=24
  - action_turn · battle-gen6randombattle-269 · p1 · turno 41 · decisión 30: fila turn_number=41 pero state.turn=28
  - action_turn · battle-gen6randombattle-271 · p1 · turno 54 · decisión 24: fila turn_number=54 pero state.turn=23
  - action_turn · battle-gen6randombattle-271 · p1 · turno 55 · decisión 27: fila turn_number=55 pero state.turn=26
  - action_turn · battle-gen6randombattle-271 · p1 · turno 56 · decisión 36: fila turn_number=56 pero state.turn=35
  - action_turn · battle-gen6randombattle-271 · p1 · turno 58 · decisión 37: fila turn_number=58 pero state.turn=36
  - action_turn · battle-gen6randombattle-271 · p1 · turno 59 · decisión 46: fila turn_number=59 pero state.turn=44
  - action_turn · battle-gen6randombattle-271 · p1 · turno 60 · decisión 52: fila turn_number=60 pero state.turn=49
  - action_turn · battle-gen6randombattle-271 · p1 · turno 62 · decisión 53: fila turn_number=62 pero state.turn=50
  - action_turn · battle-gen6randombattle-272 · p1 · turno 66 · decisión 41: fila turn_number=66 pero state.turn=42
  - action_turn · battle-gen6randombattle-272 · p1 · turno 83 · decisión 43: fila turn_number=83 pero state.turn=44
  - action_turn · battle-gen6randombattle-272 · p1 · turno 84 · decisión 45: fila turn_number=84 pero state.turn=46
  - action_turn · battle-gen6randombattle-272 · p1 · turno 85 · decisión 47: fila turn_number=85 pero state.turn=47
  - action_turn · battle-gen6randombattle-272 · p1 · turno 107 · decisión 48: fila turn_number=107 pero state.turn=48
  - action_turn · battle-gen6randombattle-272 · p1 · turno 110 · decisión 51: fila turn_number=110 pero state.turn=51
  - action_turn · battle-gen6randombattle-272 · p1 · turno 146 · decisión 52: fila turn_number=146 pero state.turn=52
  - action_turn · battle-gen6randombattle-272 · p1 · turno 147 · decisión 54: fila turn_number=147 pero state.turn=54
  - action_turn · battle-gen6randombattle-272 · p1 · turno 148 · decisión 79: fila turn_number=148 pero state.turn=79
  - action_turn · battle-gen6randombattle-276 · p1 · turno 125 · decisión 129: fila turn_number=125 pero state.turn=124
  - action_turn · battle-gen6randombattle-276 · p1 · turno 135 · decisión 139: fila turn_number=135 pero state.turn=133
  - action_turn · battle-gen6randombattle-276 · p1 · turno 137 · decisión 141: fila turn_number=137 pero state.turn=135
  - action_turn · battle-gen6randombattle-277 · p1 · turno 33 · decisión 35: fila turn_number=33 pero state.turn=32
  - action_turn · battle-gen6randombattle-277 · p1 · turno 34 · decisión 36: fila turn_number=34 pero state.turn=33
```

No se modificó ninguna fila.

## Batalla renderizada completa

Comando:

```text
pnpm --filter @ludex/dataset-audit battle battle-gen6randombattle-315
```

Salida verbatim:

```text
Batalla battle-gen6randombattle-315 (#335)
LudexBot6548 vs Rival6548 · formato gen6randombattle · ganador LudexBot6548

Trayectoria 187 · p1 · gen 6 · resultado win

Turno 0 · decisión 0 · p1
Acción: MOVE hiddenpowerice (agent)
Rival conocido: mothim activo HP=100%
Pasó (protocolo turno 1):
  |turn|1
  |move|p1a: Electrode|Hidden Power|p2a: Mothim
  |-supereffective|p2a: Mothim
  |-damage|p2a: Mothim|62/100
  |move|p2a: Mothim|Bug Buzz|p1a: Electrode
  |-damage|p1a: Electrode|131/249
  |-heal|p1a: Electrode|146/249|[from] item: Leftovers
  |-heal|p2a: Mothim|68/100|[from] item: Leftovers
  |upkeep

Turno 2 · decisión 1 · p1
Acción: MOVE toxic (agent)
Rival conocido: mothim activo HP=68%
Pasó:
  |turn|2
  |move|p1a: Electrode|Toxic|p2a: Mothim
  |-status|p2a: Mothim|tox
  |move|p2a: Mothim|Bug Buzz|p1a: Electrode
  |-damage|p1a: Electrode|29/249
  |-heal|p1a: Electrode|44/249|[from] item: Leftovers
  |-heal|p2a: Mothim|74/100 tox|[from] item: Leftovers
  |-damage|p2a: Mothim|68/100 tox|[from] psn
  |upkeep

Turno 3 · decisión 2 · p1
Acción: MOVE thunderbolt (agent)
Rival conocido: mothim activo HP=68% TOX
Pasó:
  |turn|3
  |switch|p2a: Galvantula|Galvantula, L79, F|100/100
  |move|p1a: Electrode|Thunderbolt|p2a: Galvantula
  |-resisted|p2a: Galvantula
  |-damage|p2a: Galvantula|72/100
  |-heal|p1a: Electrode|59/249|[from] item: Leftovers
  |upkeep

Turno 4 · decisión 3 · p1
Acción: SWITCH unown (agent)
Rival conocido: mothim HP=68% TOX, galvantula activo HP=72%
Pasó:
  |turn|4
  |switch|p1a: Unown|Unown|258/258
  |move|p2a: Galvantula|Thunder|p1a: Unown
  |-damage|p1a: Unown|71/258
  |-damage|p2a: Galvantula|62/100|[from] item: Life Orb
  |upkeep

Turno 5 · decisión 4 · p1
Acción: SWITCH electrode (agent)
Rival conocido: mothim HP=68% TOX, galvantula activo HP=62%
Pasó:
  |turn|5
  |switch|p2a: Tauros|Tauros, L83, M|100/100
  |switch|p1a: Electrode|Electrode, L88|59/249
  |-heal|p1a: Electrode|74/249|[from] item: Leftovers
  |upkeep

Turno 6 · decisión 5 · p1
Acción: MOVE thunderbolt (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros activo HP=100%
Pasó:
  |turn|6
  |switch|p2a: Wobbuffet|Wobbuffet, L90, F|100/100
  |move|p1a: Electrode|Thunderbolt|p2a: Wobbuffet
  |-damage|p2a: Wobbuffet|76/100
  |-heal|p1a: Electrode|89/249|[from] item: Leftovers
  |upkeep

Turno 7 · decisión 6 · p1
Acción: MOVE toxic (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet activo HP=76%
Pasó:
  |turn|7
  |switch|p2a: Rhydon|Rhydon, L84, M|100/100
  |move|p1a: Electrode|Toxic|p2a: Rhydon
  |-status|p2a: Rhydon|tox
  |-heal|p1a: Electrode|104/249|[from] item: Leftovers
  |-damage|p2a: Rhydon|94/100 tox|[from] psn
  |upkeep

Turno 8 · decisión 7 · p1
Acción: SWITCH hooh (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=76%, rhydon activo HP=94% TOX
Pasó:
  |turn|8
  |switch|p1a: Ho-Oh|Ho-Oh, L70|264/264
  |move|p2a: Rhydon|Swords Dance|p2a: Rhydon
  |-boost|p2a: Rhydon|atk|2
  |-damage|p2a: Rhydon|82/100 tox|[from] psn
  |upkeep

Turno 9 · decisión 8 · p1
Acción: SWITCH virizion (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=76%, rhydon activo HP=82% TOX
Pasó:
  |turn|9
  |switch|p1a: Virizion|Virizion, L81|280/280
  |move|p2a: Rhydon|Stone Edge|p1a: Virizion
  |-resisted|p1a: Virizion
  |-crit|p1a: Virizion
  |-damage|p1a: Virizion|55/280
  |-damage|p2a: Rhydon|64/100 tox|[from] psn
  |upkeep

Turno 10 · decisión 9 · p1
Acción: MOVE stoneedge (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=76%, rhydon activo HP=64% TOX
Pasó:
  |turn|10
  |move|p1a: Virizion|Stone Edge|p2a: Rhydon
  |-resisted|p2a: Rhydon
  |-damage|p2a: Rhydon|57/100 tox
  |-damage|p1a: Virizion|27/280|[from] item: Life Orb
  |move|p2a: Rhydon|Earthquake|p1a: Virizion
  |-resisted|p1a: Virizion
  |-damage|p1a: Virizion|0 fnt
  |faint|p1a: Virizion
  |-damage|p2a: Rhydon|33/100 tox|[from] psn
  |upkeep
  |switch|p1a: Ho-Oh|Ho-Oh, L70|264/264

Turno 10 · decisión 10 · p1
Acción: SWITCH hooh (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=76%, rhydon activo HP=33% TOX
Pasó:
  |turn|10
  |move|p1a: Virizion|Stone Edge|p2a: Rhydon
  |-resisted|p2a: Rhydon
  |-damage|p2a: Rhydon|57/100 tox
  |-damage|p1a: Virizion|27/280|[from] item: Life Orb
  |move|p2a: Rhydon|Earthquake|p1a: Virizion
  |-resisted|p1a: Virizion
  |-damage|p1a: Virizion|0 fnt
  |faint|p1a: Virizion
  |-damage|p2a: Rhydon|33/100 tox|[from] psn
  |upkeep
  |switch|p1a: Ho-Oh|Ho-Oh, L70|264/264

Turno 11 · decisión 11 · p1
Acción: MOVE sacredfire (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=76%, rhydon activo HP=33% TOX
Pasó:
  |turn|11
  |switch|p2a: Arbok|Arbok, L90, M|100/100
  |-ability|p2a: Arbok|Intimidate|boost
  |-unboost|p1a: Ho-Oh|atk|1
  |move|p1a: Ho-Oh|Sacred Fire|p2a: Arbok
  |-damage|p2a: Arbok|74/100
  |-status|p2a: Arbok|brn
  |-damage|p2a: Arbok|62/100 brn|[from] brn
  |upkeep

Turno 12 · decisión 12 · p1
Acción: SWITCH reuniclus (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=76%, rhydon HP=33% TOX, arbok activo HP=62% BRN
Pasó:
  |turn|12
  |switch|p2a: Wobbuffet|Wobbuffet, L90, F|76/100
  |switch|p1a: Reuniclus|Reuniclus, L85, F|326/326
  |upkeep

Turno 13 · decisión 13 · p1
Acción: MOVE signalbeam (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet activo HP=76%, rhydon HP=33% TOX, arbok HP=62% BRN
Pasó:
  |turn|13
  |move|p1a: Reuniclus|Signal Beam|p2a: Wobbuffet
  |-supereffective|p2a: Wobbuffet
  |-damage|p2a: Wobbuffet|27/100
  |move|p2a: Wobbuffet|Counter||[still]
  |-fail|p2a: Wobbuffet
  |upkeep

Turno 14 · decisión 14 · p1
Acción: MOVE signalbeam (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=27%, rhydon HP=33% TOX, arbok activo HP=33% BRN
Pasó:
  |turn|14
  |error|[Unavailable choice] Can't switch: The active Pokémon is trapped
  |switch|p2a: Arbok|Arbok, L90, M|62/100 brn
  |-ability|p2a: Arbok|Intimidate|boost
  |-unboost|p1a: Reuniclus|atk|1
  |move|p1a: Reuniclus|Signal Beam|p2a: Arbok
  |-resisted|p2a: Arbok
  |-damage|p2a: Arbok|45/100 brn
  |-damage|p2a: Arbok|33/100 brn|[from] brn
  |upkeep

Turno 15 · decisión 15 · p1
Acción: SWITCH porygon2 (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=62%, tauros HP=100%, wobbuffet HP=27%, rhydon HP=33% TOX, arbok activo HP=33% BRN
Pasó:
  |turn|15
  |switch|p2a: Wobbuffet|Wobbuffet, L90, F|27/100
  |switch|p1a: Porygon2|Porygon2, L81|270/270
  |-ability|p1a: Porygon2|Download|boost
  |-boost|p1a: Porygon2|spa|1
  |upkeep

Turno 16 · decisión 16 · p1
Acción: MOVE discharge (agent)
Rival conocido: mothim HP=68% TOX, galvantula activo HP=39%, tauros HP=100%, wobbuffet HP=27%, rhydon HP=33% TOX, arbok HP=33% BRN
Pasó:
  |turn|16
  |error|[Unavailable choice] Can't switch: The active Pokémon is trapped
  |switch|p2a: Galvantula|Galvantula, L79, F|62/100
  |move|p1a: Porygon2|Discharge|p2a: Galvantula
  |-resisted|p2a: Galvantula
  |-damage|p2a: Galvantula|39/100
  |upkeep

Turno 17 · decisión 17 · p1
Acción: MOVE icebeam (agent)
Rival conocido: mothim HP=68% TOX, galvantula activo HP=39%, tauros HP=100%, wobbuffet HP=27%, rhydon HP=33% TOX, arbok HP=33% BRN
Pasó:
  |turn|17
  |switch|p2a: Rhydon|Rhydon, L84, M|33/100 tox
  |move|p1a: Porygon2|Ice Beam|p2a: Rhydon
  |-supereffective|p2a: Rhydon
  |-damage|p2a: Rhydon|0 fnt
  |faint|p2a: Rhydon
  |upkeep
  |switch|p2a: Wobbuffet|Wobbuffet, L90, F|27/100

Turno 18 · decisión 18 · p1
Acción: MOVE triattack (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=39%, tauros HP=100%, wobbuffet activo HP=27%, rhydon HP=0% FNT, arbok HP=33% BRN
Pasó:
  |turn|18
  |move|p1a: Porygon2|Tri Attack|p2a: Wobbuffet
  |-damage|p2a: Wobbuffet|0 fnt
  |faint|p2a: Wobbuffet
  |upkeep
  |switch|p2a: Arbok|Arbok, L90, M|33/100 brn
  |-ability|p2a: Arbok|Intimidate|boost
  |-unboost|p1a: Porygon2|atk|1

Turno 19 · decisión 19 · p1
Acción: MOVE icebeam (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=39%, tauros HP=100%, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok activo HP=33% BRN
Pasó:
  |turn|19
  |move|p2a: Arbok|Coil|p2a: Arbok
  |-boost|p2a: Arbok|atk|1
  |-boost|p2a: Arbok|def|1
  |-boost|p2a: Arbok|accuracy|1
  |move|p1a: Porygon2|Ice Beam|p2a: Arbok
  |-damage|p2a: Arbok|0 fnt
  |faint|p2a: Arbok
  |upkeep
  |switch|p2a: Mothim|Mothim, L94, M|68/100 tox

Turno 20 · decisión 20 · p1
Acción: SWITCH electrode (agent)
Rival conocido: mothim activo HP=68% TOX, galvantula HP=39%, tauros HP=100%, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|20
  |switch|p2a: Tauros|Tauros, L83, M|100/100
  |switch|p1a: Electrode|Electrode, L88|104/249
  |-heal|p1a: Electrode|119/249|[from] item: Leftovers
  |upkeep

Turno 21 · decisión 21 · p1
Acción: MOVE toxic (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=39%, tauros activo HP=100%, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|21
  |move|p1a: Electrode|Toxic|p2a: Tauros
  |-status|p2a: Tauros|tox
  |move|p2a: Tauros|Body Slam|p1a: Electrode
  |-damage|p1a: Electrode|0 fnt
  |faint|p1a: Electrode
  |-damage|p2a: Tauros|94/100 tox|[from] psn
  |upkeep
  |switch|p1a: Porygon2|Porygon2, L81|270/270
  |-ability|p1a: Porygon2|Download|boost
  |-boost|p1a: Porygon2|spa|1

Turno 21 · decisión 22 · p1
Acción: SWITCH porygon2 (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=39%, tauros activo HP=94% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|21
  |move|p1a: Electrode|Toxic|p2a: Tauros
  |-status|p2a: Tauros|tox
  |move|p2a: Tauros|Body Slam|p1a: Electrode
  |-damage|p1a: Electrode|0 fnt
  |faint|p1a: Electrode
  |-damage|p2a: Tauros|94/100 tox|[from] psn
  |upkeep
  |switch|p1a: Porygon2|Porygon2, L81|270/270
  |-ability|p1a: Porygon2|Download|boost
  |-boost|p1a: Porygon2|spa|1

Turno 22 · decisión 23 · p1
Acción: MOVE recover (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=39%, tauros activo HP=94% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|22
  |switch|p2a: Galvantula|Galvantula, L79, F|39/100
  |move|p1a: Porygon2|Recover||[still]
  |-fail|p1a: Porygon2|heal
  |upkeep

Turno 23 · decisión 24 · p1
Acción: MOVE recover (agent)
Rival conocido: mothim HP=68% TOX, galvantula activo HP=39%, tauros HP=94% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|23
  |move|p2a: Galvantula|Thunder|p1a: Porygon2
  |-damage|p1a: Porygon2|184/270
  |-damage|p2a: Galvantula|29/100|[from] item: Life Orb
  |move|p1a: Porygon2|Recover|p1a: Porygon2
  |-heal|p1a: Porygon2|270/270
  |upkeep

Turno 24 · decisión 25 · p1
Acción: MOVE triattack (agent)
Rival conocido: mothim HP=68% TOX, galvantula activo HP=29%, tauros HP=94% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|24
  |move|p2a: Galvantula|Sticky Web|p1a: Porygon2
  |-sidestart|p1: LudexBot6548|move: Sticky Web
  |move|p1a: Porygon2|Tri Attack|p2a: Galvantula
  |-damage|p2a: Galvantula|0 fnt
  |faint|p2a: Galvantula
  |upkeep
  |switch|p2a: Tauros|Tauros, L83, M|94/100 tox

Turno 25 · decisión 26 · p1
Acción: MOVE triattack (agent)
Rival conocido: mothim HP=68% TOX, galvantula HP=0% FNT, tauros activo HP=94% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|25
  |switch|p2a: Mothim|Mothim, L94, M|68/100 tox
  |move|p1a: Porygon2|Tri Attack|p2a: Mothim
  |-damage|p2a: Mothim|5/100 tox
  |-heal|p2a: Mothim|11/100 tox|[from] item: Leftovers
  |-damage|p2a: Mothim|5/100 tox|[from] psn
  |upkeep

Turno 26 · decisión 27 · p1
Acción: MOVE triattack (agent)
Rival conocido: mothim activo HP=5% TOX, galvantula HP=0% FNT, tauros HP=94% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|26
  |switch|p2a: Tauros|Tauros, L83, M|94/100 tox
  |move|p1a: Porygon2|Tri Attack|p2a: Tauros
  |-damage|p2a: Tauros|36/100 tox
  |-damage|p2a: Tauros|30/100 tox|[from] psn
  |upkeep

Turno 27 · decisión 28 · p1
Acción: SWITCH reuniclus (agent)
Rival conocido: mothim HP=5% TOX, galvantula HP=0% FNT, tauros activo HP=30% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|27
  |switch|p1a: Reuniclus|Reuniclus, L85, F|326/326
  |-activate|p1a: Reuniclus|move: Sticky Web
  |-unboost|p1a: Reuniclus|spe|1
  |move|p2a: Tauros|Earthquake|p1a: Reuniclus
  |-damage|p1a: Reuniclus|226/326
  |-damage|p2a: Tauros|20/100 tox|[from] item: Life Orb
  |-damage|p2a: Tauros|8/100 tox|[from] psn
  |upkeep

Turno 28 · decisión 29 · p1
Acción: SWITCH porygon2 (agent)
Rival conocido: mothim HP=5% TOX, galvantula HP=0% FNT, tauros activo HP=8% TOX, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|28
  |switch|p1a: Porygon2|Porygon2, L81|270/270
  |-activate|p1a: Porygon2|move: Sticky Web
  |-unboost|p1a: Porygon2|spe|1
  |-ability|p1a: Porygon2|Download|boost
  |-boost|p1a: Porygon2|spa|1
  |move|p2a: Tauros|Zen Headbutt|p1a: Porygon2
  |-damage|p1a: Porygon2|206/270
  |-damage|p2a: Tauros|0 fnt|[from] psn
  |faint|p2a: Tauros
  |upkeep
  |switch|p2a: Mothim|Mothim, L94, M|5/100 tox

Turno 29 · decisión 30 · p1
Acción: MOVE icebeam (agent)
Rival conocido: mothim activo HP=5% TOX, galvantula HP=0% FNT, tauros HP=0% FNT, wobbuffet HP=0% FNT, rhydon HP=0% FNT, arbok HP=0% FNT
Pasó:
  |turn|29
  |move|p2a: Mothim|Energy Ball|p1a: Porygon2
  |-damage|p1a: Porygon2|152/270
  |move|p1a: Porygon2|Ice Beam|p2a: Mothim
  |-supereffective|p2a: Mothim
  |-crit|p2a: Mothim
  |-damage|p2a: Mothim|0 fnt
  |faint|p2a: Mothim
  |win|LudexBot6548
  |deinit
```

## Concerns

- La única familia de violaciones observada está concentrada en datos antiguos:
  batallas 266, 267, 269, 271, 272, 276 y 277. No se reparó el pipeline ni el
  dataset.
- La invariante de fuga usa cada línea individual del protocolo acumulado hasta
  el turno de la decisión y normaliza removiendo toda puntuación, símbolos,
  separadores y diacríticos. No concatena líneas.
- Showdown entrega la request inicial con `state.turn=0` y el resultado en
  `battle_turns.turn_number=1`. El auditor conserva la igualdad fila/estado;
  el renderer une explícitamente 0→1 para mostrar el resultado de la primera
  acción.
- El renderer omite líneas `|request|`, headers de room y timestamps para que
  una persona vea acciones y hechos del juego; el auditor sí conserva y examina
  el arreglo crudo completo.
