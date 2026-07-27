# Ludex Fase 2, rebanada 1 — Conexión, estado y persistencia: Plan de Implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el agente juegue batallas completas contra un bot baseline en el server local de Showdown y grabe cada turno en un formato que sirva para entrenar modelos dentro de un año.

**Architecture:** Un loop directo sobre `poke-env` (sin LangGraph todavía) con un agente que elige al azar entre acciones legales. Tres capas disjuntas: `state/` es pura y no conoce ni red ni base de datos; `showdown/` habla con el servidor y captura el protocolo crudo; `db/` persiste y no conoce poke-env. El protocolo crudo es la fuente de verdad y el estado derivado es una vista materializada, así que un error en el serializador se corrige re-derivando en vez de descartando datos.

**Tech Stack:** Python 3.12, uv, poke-env 0.15.0, SQLAlchemy 2.0 async + asyncpg, pytest + pytest-asyncio, Postgres 16 en `localhost:15432`, server local de Showdown en `localhost:8100`.

## Global Constraints

- **Ninguna generación se hardcodea** en `apps/agent/src/`. Al cerrar, `grep -ri "gen6" apps/agent/src/` no debe devolver nada fuera de configuración.
- **`poke-env` pineado en la versión exacta `0.15.0`.** Todos los hallazgos de este plan dependen de esa versión.
- **`state/` es puro**: no importa `db/`, no importa nada de red, no abre conexiones. `db/` no importa `poke_env`. Son las dos mitades que no deben tocarse.
- **El serializador es una lista blanca explícita.** Prohibido recorrer atributos del objeto `Battle` o serializarlo genéricamente. Cada campo que entra se nombra a mano.
- **`state_schema_version` arranca en 1** y se persiste en cada fila.
- **Un solo escritor por directorio.** Cada tarea declara sus archivos exactos; tocar algo fuera es un hallazgo de review.
- **Commits con `git commit -m "..." -- <rutas>`.** Nunca `git add` suelto: `git commit` sin `--` commitea todo el índice, incluido lo que otro agente dejó preparado.
- **Nunca `docker compose down`, `down -v`, `docker stop` ni `brew services stop`.** Hay contenedores de otro proyecto del usuario en la máquina.

---

## Hallazgos verificados contra poke-env 0.15.0

Salieron de correr batallas reales contra el server local durante la planificación. Son la base de los tests; no asumir otra cosa.

**El módulo es `poke_env.battle`, no `poke_env.environment`.** El segundo no existe en esta versión.

**La propiedad de no-fuga se cumple nativamente.** En una batalla real de `gen6randombattle`:

| turno | mi equipo | equipo rival revelado | ids del rival |
|---|---|---|---|
| 0 | 6 | **0** | `[]` |
| 1 | 6 | 1 | `["p2: Magnezone"]` |
| 2 | 6 | 1 | `["p2: Magnezone"]` |

`opponent_team` se puebla progresivamente. Igual hace falta la lista blanca: `_teampreview_opponent_team` y otros atributos privados sí pueden traer más.

**El protocolo crudo es POR JUGADOR, no por batalla.** Verificado: el `|request|` del jugador A trae el equipo de A (Hydro Pump, Waterfall) y el de B trae el de B (Ice Beam, Heal Bell). Los streams son distintos.

Consecuencia de esquema: `battle_turns` necesita `player_side` en la clave primaria. Guardar un solo stream por batalla haría imposible re-derivar el estado del otro jugador, y metería el equipo de un jugador en el contexto del otro — la misma fuga que el test busca atrapar.

**`Player._handle_battle_message(split_messages: List[List[str]])`** recibe las líneas crudas ya separadas por `|`, antes de parsearlas. Es el punto de captura.

**`AbstractBattle._replay_data`** acumula el log como `List[List[str]]` y crece durante la batalla (6 → 19 → 28 → 646 líneas en una batalla de 63 turnos). Es atributo privado: se usa como referencia cruzada en los tests, no como fuente primaria.

**Formas reales de los objetos** (batalla de `gen6randombattle`, turno 2):

```
clave de battle.team    "p1: Beautifly"          (player_role + nombre visible)
Pokemon.species         "beautifly"              (id normalizado, minúscula)
Pokemon.current_hp      279          .max_hp     279
Pokemon.current_hp_fraction  1.0
Pokemon.status          None                     (o un enum Status)
Pokemon.active          True         .fainted    False
Pokemon.item            "leftovers"  .ability    "swarm"     .level  99
Pokemon.boosts          {'accuracy': 0, 'atk': 0, 'def': 0, ...}
Pokemon.stats           {'hp': 279, 'atk': 143, 'def': 155, ...}
Pokemon.types           [<PokemonType.BUG: 1>, <PokemonType.FLYING: ...>]
Pokemon.moves           {'bugbuzz': Move, 'hiddenpower...': Move}
Move.id                 "bugbuzz"    .current_pp 15   .max_pp  16
Move.base_power         90           .accuracy   1.0  (float 0-1, no 0-100)
Move.category           MoveCategory enum         .type   PokemonType enum
battle.weather / .fields / .side_conditions / .opponent_side_conditions
                        dicts, vacíos cuando no hay nada activo
battle.player_role      "p1"         .battle_tag  "battle-gen6randombattle-3"
battle.gen              6
```

Los enums se serializan con `.name`. `accuracy` es float de 0 a 1: **no** es el mismo formato que `moves.accuracy` en la base de datos, que guarda enteros 0-100.

**Conexión al server local:**

```python
from poke_env import AccountConfiguration, ServerConfiguration
LOCAL = ServerConfiguration(
    "ws://localhost:8100/showdown/websocket",
    "https://play.pokemonshowdown.com/action.php?",
)
player = RandomPlayer(
    account_configuration=AccountConfiguration("LudexBot", None),  # None = sin password
    server_configuration=LOCAL,
    battle_format="gen6randombattle",
)
```

**`await a.battle_against(b, n_battles=N)`** coordina las dos partes. Baselines disponibles: `RandomPlayer`, `MaxBasePowerPlayer`, `SimpleHeuristicsPlayer`.

**`choose_move` se llama con `battle.turn == 0`** la primera vez. Los turnos empiezan en 0, no en 1.

---

## File Structure

```
db/migrations/20260727000005_battles_and_trajectories.sql

apps/agent/
  pyproject.toml
  .python-version
  src/ludex_agent/
    __init__.py
    config.py                 # env vars, sin lógica
    state/                    # PURO: sin red, sin DB
      __init__.py
      schema.py               # STATE_SCHEMA_VERSION + dataclasses
      actions.py              # acciones legales desde el Battle
      serializer.py           # Battle -> dict, lista blanca
    showdown/                 # red
      __init__.py
      protocol.py             # ProtocolRecorder: captura cruda por turno
      client.py               # LudexPlayer: subclase de RandomPlayer
    db/                       # persistencia, sin poke_env
      __init__.py
      models.py               # SQLAlchemy escritos a mano
      session.py              # engine + factory
      repository.py           # escrituras
    cli.py                    # agent play --n N
  tests/
    state/test_serializer.py
    state/test_actions.py
    showdown/test_protocol.py
    integration/test_play.py  # incluye fuga y re-derivación
    fixtures/battle_protocol.json
```

`state/` es la capa pura de esta fase, el equivalente a `extract/` en la fase anterior: todo el riesgo de corrección vive ahí y se testea sin levantar nada.

---

## Task 1: Migración de batallas y trayectorias

**Files:**
- Create: `db/migrations/20260727000005_battles_and_trajectories.sql`
- Modify: `db/schema.sql` (lo regenera dbmate)

**Interfaces:**
- Consumes: las tablas `generations` de la fase 1.
- Produces: `battles`, `battle_turns`, `trajectories`, `trajectory_steps`. Las tareas 6 y 7 escriben contra estos nombres de columna exactos.

- [ ] **Step 1: Escribir la migración**

`db/migrations/20260727000005_battles_and_trajectories.sql`:

```sql
-- migrate:up
CREATE TYPE played_by_kind AS ENUM ('bot', 'human');
CREATE TYPE battle_source  AS ENUM ('challenge', 'ladder', 'local', 'import');
CREATE TYPE battle_result  AS ENUM ('win', 'loss', 'tie');
CREATE TYPE action_source  AS ENUM ('agent', 'human', 'opponent');

CREATE TABLE battles (
  id             serial PRIMARY KEY,
  battle_tag     text NOT NULL UNIQUE,
  tournament_id  int,
  round_id       int,
  format         text NOT NULL,
  p1             text NOT NULL,
  p2             text NOT NULL,
  winner         text,
  played_by      played_by_kind NOT NULL,
  source         battle_source  NOT NULL,
  replay_url     text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- player_side esta en la PK porque el stream de protocolo es POR JUGADOR:
-- el |request| de p1 contiene el equipo de p1. Un solo stream por batalla
-- haria imposible re-derivar el estado del otro lado, y meteria el equipo
-- de un jugador en el contexto del otro.
CREATE TABLE battle_turns (
  battle_id       int  NOT NULL REFERENCES battles(id) ON DELETE CASCADE,
  player_side     text NOT NULL,
  turn_number     int  NOT NULL,
  protocol_lines  text[] NOT NULL,
  agent_reasoning jsonb,
  PRIMARY KEY (battle_id, player_side, turn_number)
);

CREATE TABLE trajectories (
  id           serial PRIMARY KEY,
  battle_id    int  NOT NULL REFERENCES battles(id) ON DELETE CASCADE,
  gen_id       int  NOT NULL REFERENCES generations(id),
  format       text NOT NULL,
  player_side  text NOT NULL,
  final_result battle_result,
  elo_bucket   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (battle_id, player_side)
);

CREATE TABLE trajectory_steps (
  trajectory_id        int  NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
  turn_number          int  NOT NULL,
  state                jsonb NOT NULL,
  state_schema_version int  NOT NULL,
  legal_actions        jsonb NOT NULL,
  action_taken         jsonb,
  action_source        action_source NOT NULL,
  reward               numeric,
  PRIMARY KEY (trajectory_id, turn_number)
);

CREATE INDEX trajectory_steps_version_idx ON trajectory_steps (state_schema_version);
CREATE INDEX battles_created_at_idx       ON battles (created_at DESC);

-- migrate:down
DROP TABLE trajectory_steps;
DROP TABLE trajectories;
DROP TABLE battle_turns;
DROP TABLE battles;
DROP TYPE action_source;
DROP TYPE battle_result;
DROP TYPE battle_source;
DROP TYPE played_by_kind;
```

- [ ] **Step 2: Aplicar y verificar**

```bash
docker compose up -d postgres
docker compose run --rm migrate up
docker compose exec -T postgres psql -U ludex -d ludex -c "\d battle_turns"
```

Esperado: la PK compuesta `(battle_id, player_side, turn_number)`.

- [ ] **Step 3: Verificar que el `down` funciona de verdad**

```bash
docker compose run --rm migrate rollback
docker compose run --rm migrate up
```

Esperado: baja las cuatro tablas y los cuatro tipos, y las vuelve a subir. Un `down` roto se descubre acá o no se descubre nunca. Ojo con el orden: los `DROP TYPE` van después de los `DROP TABLE`.

- [ ] **Step 4: Commit**

```bash
docker compose run --rm migrate dump
git commit -m "feat(db): tablas de batallas y trayectorias" -- db/
```

---

## Task 2: Paquete Python y configuración

**Files:**
- Create: `apps/agent/pyproject.toml`, `apps/agent/.python-version`
- Create: `apps/agent/src/ludex_agent/__init__.py`, `apps/agent/src/ludex_agent/config.py`
- Test: `apps/agent/tests/test_config.py`

**Interfaces:**
- Produces: `Settings` con `database_url: str`, `showdown_ws_url: str`, `showdown_battle_format: str`, `bot_username: str`; y `load_settings() -> Settings`.

- [ ] **Step 1: Crear el paquete**

`apps/agent/.python-version`:
```
3.12
```

`apps/agent/pyproject.toml`:
```toml
[project]
name = "ludex-agent"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = [
    "poke-env==0.15.0",
    "sqlalchemy[asyncio]==2.0.36",
    "asyncpg==0.30.0",
    "typer==0.15.1",
]

[dependency-groups]
dev = ["pytest==8.3.4", "pytest-asyncio==0.25.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ludex_agent"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
# Verificado empiricamente: sin esto, pytest-asyncio adopta "function" como
# default y la fixture de scope module de la Tarea 8 falla con ScopeMismatch,
# no con un warning. "module" alcanza para esa fixture y evita un loop de
# sesion sosteniendo engines de base entre modulos.
asyncio_default_fixture_loop_scope = "module"
testpaths = ["tests"]
```

Instalar y verificar la versión exacta:
```bash
cd apps/agent && uv sync
uv run python -c "import poke_env, importlib.metadata as m; print(m.version('poke-env'))"
```
Esperado: `0.15.0`. Si no coincide, parar: todos los hallazgos de este plan dependen de esa versión.

- [ ] **Step 2: Escribir el test que falla**

`apps/agent/tests/test_config.py`:
```python
import os
import pytest
from ludex_agent.config import Settings, load_settings


def test_lee_las_variables_de_entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:15432/db")
    monkeypatch.setenv("SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket")
    s = load_settings()
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.showdown_ws_url == "ws://localhost:8100/showdown/websocket"


def test_convierte_el_esquema_para_asyncpg(monkeypatch):
    # dbmate y el seed usan postgres://, SQLAlchemy async necesita el driver.
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:15432/db?sslmode=disable")
    s = load_settings()
    assert s.database_url == "postgresql+asyncpg://u:p@h:15432/db"


def test_falla_ruidosamente_sin_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        load_settings()
```

- [ ] **Step 3: Correr y verificar que falla**

```bash
cd apps/agent && uv run pytest tests/test_config.py -v
```
Esperado: FAIL, no existe `ludex_agent.config`.

- [ ] **Step 4: Implementar**

`apps/agent/src/ludex_agent/config.py`:
```python
"""Configuracion por variables de entorno. Sin logica de dominio."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
    showdown_ws_url: str
    showdown_battle_format: str
    bot_username: str


def _to_asyncpg(url: str) -> str:
    """dbmate y el seed usan `postgres://...?sslmode=disable`.

    SQLAlchemy async necesita el driver explicito, y asyncpg no entiende
    `sslmode` como parametro de query: lo rechaza. Se normaliza el esquema y
    se descarta la query.
    """
    parts = urlsplit(url)
    scheme = "postgresql+asyncpg"
    return urlunsplit((scheme, parts.netloc, parts.path, "", ""))


def load_settings() -> Settings:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("Falta DATABASE_URL. Copiar .env.example a .env.")
    return Settings(
        database_url=_to_asyncpg(raw),
        showdown_ws_url=os.environ.get(
            "SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket"
        ),
        showdown_battle_format=os.environ.get(
            "SHOWDOWN_BATTLE_FORMAT", "gen6randombattle"
        ),
        bot_username=os.environ.get("SHOWDOWN_BOT_USERNAME", "LudexBot"),
    )
```

`apps/agent/src/ludex_agent/__init__.py`: vacío.

- [ ] **Step 5: Correr y verificar que pasa**

```bash
cd apps/agent && uv run pytest tests/test_config.py -v
```
Esperado: PASS, 3 tests.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(agent): paquete python y configuracion" -- apps/agent/
```

---

## Task 3: Acciones legales

**Files:**
- Create: `apps/agent/src/ludex_agent/state/__init__.py`, `apps/agent/src/ludex_agent/state/schema.py`, `apps/agent/src/ludex_agent/state/actions.py`
- Test: `apps/agent/tests/state/test_actions.py`

**Interfaces:**
- Produces: `STATE_SCHEMA_VERSION: int = 1`; `legal_actions(battle) -> list[dict]`; `action_from_order(order) -> dict`.

- [ ] **Step 1: Escribir el test que falla**

`apps/agent/tests/state/test_actions.py`:
```python
from types import SimpleNamespace

from ludex_agent.state.actions import action_from_order, legal_actions


def _move(mid):
    return SimpleNamespace(id=mid)


def _mon(species):
    return SimpleNamespace(species=species)


def test_lista_movimientos_y_cambios():
    battle = SimpleNamespace(
        available_moves=[_move("bugbuzz"), _move("roost")],
        available_switches=[_mon("magnezone")],
    )
    assert legal_actions(battle) == [
        {"kind": "move", "id": "bugbuzz"},
        {"kind": "move", "id": "roost"},
        {"kind": "switch", "species": "magnezone"},
    ]


def test_sin_acciones_devuelve_lista_vacia():
    battle = SimpleNamespace(available_moves=[], available_switches=[])
    assert legal_actions(battle) == []


def test_traduce_una_orden_de_movimiento():
    order = SimpleNamespace(order=_move("bugbuzz"))
    assert action_from_order(order) == {"kind": "move", "id": "bugbuzz"}


def test_traduce_una_orden_de_cambio():
    order = SimpleNamespace(order=_mon("magnezone"))
    assert action_from_order(order) == {"kind": "switch", "species": "magnezone"}


def test_orden_vacia_es_none():
    assert action_from_order(None) is None


def test_orden_sin_contenido_es_none():
    assert action_from_order(SimpleNamespace(order=None)) is None


def test_orden_con_contenido_inesperado_es_none():
    # Ni .id ni .species: no revienta, devuelve None.
    assert action_from_order(SimpleNamespace(order=SimpleNamespace(raro=1))) is None


def test_la_desambiguacion_vale_contra_los_objetos_reales():
    """Fija el supuesto del que depende `action_from_order`.

    La funcion distingue Move de Pokemon por hasattr, sin importar poke_env,
    porque state/ es puro. Este test SI importa la libreria — los tests pueden,
    src/ no — para que si una version futura le diera `.id` a Pokemon, falle
    ruidosamente en vez de clasificar todos los cambios como movimientos.
    """
    from poke_env.battle import Move, Pokemon

    mon = Pokemon(gen=6, species="charizard")
    mv = Move("flamethrower", gen=6)
    assert not hasattr(mon, "id") and hasattr(mon, "species")
    assert hasattr(mv, "id") and not hasattr(mv, "species")
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
cd apps/agent && uv run pytest tests/state/test_actions.py -v
```
Esperado: FAIL, no existe el módulo.

- [ ] **Step 3: Implementar**

`apps/agent/src/ludex_agent/state/schema.py`:
```python
"""Version del formato de estado.

Se persiste en cada fila de trajectory_steps. Si cambia la forma que produce
serializer.py, ESTE numero sube. El protocolo crudo persistido permite
re-derivar el historico a la version nueva.
"""

STATE_SCHEMA_VERSION = 1
```

`apps/agent/src/ludex_agent/state/actions.py`:
```python
"""Acciones legales del turno.

Sin la mascara de acciones legales no se puede entrenar una politica, asi que
esto se persiste en cada paso junto al estado.
"""

from __future__ import annotations

from typing import Any


def legal_actions(battle: Any) -> list[dict]:
    """Movimientos y cambios disponibles, en ese orden."""
    actions: list[dict] = [
        {"kind": "move", "id": move.id} for move in battle.available_moves
    ]
    actions += [
        {"kind": "switch", "species": mon.species} for mon in battle.available_switches
    ]
    return actions


def action_from_order(order: Any) -> dict | None:
    """Traduce una BattleOrder de poke-env a la forma que se persiste.

    Una orden envuelve un Move (que tiene `.id`) o un Pokemon (que tiene
    `.species`); se distinguen por que atributo esta presente.
    """
    if order is None:
        return None
    inner = getattr(order, "order", None)
    if inner is None:
        return None
    if hasattr(inner, "id"):
        return {"kind": "move", "id": inner.id}
    if hasattr(inner, "species"):
        return {"kind": "switch", "species": inner.species}
    return None
```

`apps/agent/src/ludex_agent/state/__init__.py`: vacío.

- [ ] **Step 4: Correr y verificar que pasa**

```bash
cd apps/agent && uv run pytest tests/state/test_actions.py -v
```
Esperado: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(agent): acciones legales y version de esquema de estado" -- apps/agent/
```

---

## Task 4: El serializador de estado

Es la tarea de mayor riesgo del plan. Todo lo demás es reversible; un defecto acá contamina los datos de entrenamiento.

**Files:**
- Create: `apps/agent/src/ludex_agent/state/serializer.py`
- Test: `apps/agent/tests/state/test_serializer.py`

**Interfaces:**
- Consumes: `STATE_SCHEMA_VERSION` de `state/schema.py`, `legal_actions` de `state/actions.py`.
- Produces: `serialize_battle(battle) -> dict`.

- [ ] **Step 1: Escribir el test que falla**

`apps/agent/tests/state/test_serializer.py`:
```python
from enum import Enum
from types import SimpleNamespace

from ludex_agent.state.serializer import serialize_battle


class FakeType(Enum):
    BUG = 1
    FLYING = 2


class FakeStatus(Enum):
    BRN = 1


def _move(mid, pp=15, maxpp=16):
    return SimpleNamespace(id=mid, current_pp=pp, max_pp=maxpp)


def _mon(species, *, hp_frac=1.0, active=False, fainted=False, item="leftovers",
         ability="swarm", level=99, status=None, moves=None, secreto="NO DEBE APARECER"):
    return SimpleNamespace(
        species=species, current_hp_fraction=hp_frac, active=active, fainted=fainted,
        item=item, ability=ability, level=level, status=status,
        boosts={"atk": 1, "def": 0}, stats={"hp": 279, "atk": 143},
        types=[FakeType.BUG, FakeType.FLYING],
        moves=moves if moves is not None else {"bugbuzz": _move("bugbuzz")},
        atributo_privado=secreto,
    )


def _battle(**kw):
    base = dict(
        turn=3, player_role="p1", format="gen6randombattle", gen=6,
        team={"p1: Beautifly": _mon("beautifly", active=True)},
        opponent_team={"p2: Magnezone": _mon("magnezone", active=True)},
        weather={}, fields={}, side_conditions={}, opponent_side_conditions={},
        available_moves=[_move("bugbuzz")], available_switches=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_incluye_version_turno_y_metadatos():
    s = serialize_battle(_battle())
    assert s["schema_version"] == 1
    assert s["turn"] == 3
    assert s["player_role"] == "p1"
    assert s["format"] == "gen6randombattle"
    assert s["gen"] == 6


def test_serializa_mi_lado_completo():
    s = serialize_battle(_battle())
    mine = s["me"]["pokemon"][0]
    assert mine["species"] == "beautifly"
    assert mine["hp_fraction"] == 1.0
    assert mine["item"] == "leftovers"
    assert mine["ability"] == "swarm"
    assert mine["stats"] == {"hp": 279, "atk": 143}
    assert mine["moves"] == [{"id": "bugbuzz", "pp": 15, "max_pp": 16}]


def test_el_lado_rival_no_expone_stats():
    # Los stats del rival no son observables: poke-env los estima o los deja
    # en None. Incluirlos seria inventar informacion que un jugador no tiene.
    s = serialize_battle(_battle())
    opp = s["opponent"]["pokemon"][0]
    assert opp["species"] == "magnezone"
    assert "stats" not in opp


def test_es_lista_blanca_no_copia():
    # El fake trae un atributo que el serializador nunca nombra. Si aparece,
    # alguien esta recorriendo atributos en vez de nombrarlos uno por uno.
    s = serialize_battle(_battle())
    assert "NO DEBE APARECER" not in str(s)
    assert "atributo_privado" not in str(s)


def test_el_rival_vacio_en_el_turno_cero():
    s = serialize_battle(_battle(turn=0, opponent_team={}))
    assert s["opponent"]["pokemon"] == []


def test_normaliza_enums_a_nombre():
    s = serialize_battle(_battle())
    assert s["me"]["pokemon"][0]["types"] == ["BUG", "FLYING"]


def test_status_none_y_status_presente():
    assert serialize_battle(_battle())["me"]["pokemon"][0]["status"] is None
    b = _battle(team={"p1: X": _mon("beautifly", status=FakeStatus.BRN, active=True)})
    assert serialize_battle(b)["me"]["pokemon"][0]["status"] == "BRN"


def test_incluye_las_acciones_legales():
    s = serialize_battle(_battle())
    assert s["legal_actions"] == [{"kind": "move", "id": "bugbuzz"}]


def test_es_serializable_a_json():
    import json
    json.dumps(serialize_battle(_battle()))
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
cd apps/agent && uv run pytest tests/state/test_serializer.py -v
```
Esperado: FAIL, no existe `state/serializer.py`.

- [ ] **Step 3: Implementar**

`apps/agent/src/ludex_agent/state/serializer.py`:
```python
"""Battle de poke-env -> dict serializable.

REGLA DURA: esto es una LISTA BLANCA. Cada campo que entra se nombra a mano.
Esta prohibido recorrer los atributos del objeto Battle o serializarlo
genericamente, porque poke-env expone mi equipo completo y el del rival con la
misma forma, y del rival solo debe verse lo revelado. Con lista blanca, un
campo nuevo en una version futura de la libreria no se cuela solo.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .actions import legal_actions
from .schema import STATE_SCHEMA_VERSION


def _name(value: Any) -> Any:
    """Los enums de poke-env se persisten por nombre, no por valor numerico."""
    return value.name if isinstance(value, Enum) else value


def _moves(mon: Any) -> list[dict]:
    return [
        {"id": mid, "pp": mv.current_pp, "max_pp": mv.max_pp}
        for mid, mv in (mon.moves or {}).items()
    ]


def _pokemon(mon: Any, *, mine: bool) -> dict:
    """Campos observables de un pokemon.

    `mine=False` omite `stats`: los stats del rival no son observables, poke-env
    los estima o los deja en None, y persistirlos seria inventar informacion que
    el jugador no tiene.
    """
    out = {
        "species": mon.species,
        "hp_fraction": mon.current_hp_fraction,
        "active": mon.active,
        "fainted": mon.fainted,
        "status": _name(mon.status),
        "level": mon.level,
        "item": mon.item,
        "ability": mon.ability,
        "types": [_name(t) for t in (mon.types or []) if t is not None],
        "boosts": dict(mon.boosts or {}),
        "moves": _moves(mon),
    }
    if mine:
        out["stats"] = dict(mon.stats or {})
    return out


def _side(team: dict, *, mine: bool) -> dict:
    return {"pokemon": [_pokemon(m, mine=mine) for m in (team or {}).values()]}


def _conditions(raw: dict) -> dict:
    return {_name(k): _name(v) for k, v in (raw or {}).items()}


def serialize_battle(battle: Any) -> dict:
    """Estado observable del turno, desde el punto de vista de ESTE jugador."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "turn": battle.turn,
        "player_role": battle.player_role,
        "format": battle.format,
        "gen": battle.gen,
        "field": {
            "weather": _conditions(battle.weather),
            # `battle.fields` no son solo terrenos: incluye Trick Room, Gravity
            # y Wonder Room. La clave se llama field_effects para no mentirle a
            # quien consuma el dataset en la fase de entrenamiento.
            "field_effects": _conditions(battle.fields),
            "my_side": _conditions(battle.side_conditions),
            "opponent_side": _conditions(battle.opponent_side_conditions),
        },
        "me": _side(battle.team, mine=True),
        "opponent": _side(battle.opponent_team, mine=False),
        "legal_actions": legal_actions(battle),
    }
```

- [ ] **Step 4: Correr y verificar que pasa**

```bash
cd apps/agent && uv run pytest tests/state/test_serializer.py -v
```
Esperado: PASS, 9 tests.

- [ ] **Step 5: Verificar el corte de capas**

```bash
cd apps/agent && grep -rn "import" src/ludex_agent/state/ | grep -E "poke_env|sqlalchemy|asyncpg" && echo "FALLA: state/ no es puro" || echo "OK: state/ no importa poke_env ni la DB"
```
Esperado: la línea `OK`. `state/` recibe el objeto `Battle` por parámetro pero no importa la librería: eso es lo que lo hace testeable con fakes.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(agent): serializador de estado por lista blanca" -- apps/agent/
```

---

## Task 5: Captura del protocolo crudo

**Files:**
- Create: `apps/agent/src/ludex_agent/showdown/__init__.py`, `apps/agent/src/ludex_agent/showdown/protocol.py`
- Test: `apps/agent/tests/showdown/test_protocol.py`

**Interfaces:**
- Produces: `ProtocolRecorder` con `record(split_messages: list[list[str]]) -> None`, `lines_for_turn(turn: int) -> list[str]`, `turns() -> list[int]`, y la propiedad `all_lines: list[str]`.

- [ ] **Step 1: Escribir el test que falla**

`apps/agent/tests/showdown/test_protocol.py`:
```python
from ludex_agent.showdown.protocol import ProtocolRecorder


def _split(raw: str) -> list[str]:
    return raw.split("|")


def test_agrupa_las_lineas_por_turno():
    r = ProtocolRecorder()
    r.record([_split("|init|battle"), _split("|turn|1")])
    r.record([_split("|move|p1a: Beautifly|Bug Buzz"), _split("|turn|2")])
    r.record([_split("|switch|p2a: Magnezone")])

    assert r.turns() == [0, 1, 2]
    assert "|init|battle" in r.lines_for_turn(0)
    assert "|move|p1a: Beautifly|Bug Buzz" in r.lines_for_turn(1)
    assert "|switch|p2a: Magnezone" in r.lines_for_turn(2)


def test_la_linea_de_turno_abre_el_turno_nuevo():
    # |turn|N marca el comienzo del turno N: la linea pertenece a N, no a N-1.
    r = ProtocolRecorder()
    r.record([_split("|turn|1")])
    assert "|turn|1" in r.lines_for_turn(1)
    assert "|turn|1" not in r.lines_for_turn(0)


def test_conserva_el_orden_dentro_del_turno():
    r = ProtocolRecorder()
    r.record([_split("|turn|1"), _split("|a"), _split("|b"), _split("|c")])
    assert r.lines_for_turn(1) == ["|turn|1", "|a", "|b", "|c"]


def test_preserva_el_request_con_mi_equipo():
    # El |request| trae MI equipo y es lo que permite re-derivar el estado.
    # Perderlo romperia la re-derivacion.
    r = ProtocolRecorder()
    req = '|request|{"active":[{"moves":[{"id":"bugbuzz"}]}]}'
    r.record([_split(req)])
    assert req in r.lines_for_turn(0)


def test_un_turno_sin_lineas_devuelve_vacio():
    assert ProtocolRecorder().lines_for_turn(7) == []


def test_all_lines_devuelve_todo_en_orden():
    r = ProtocolRecorder()
    r.record([_split("|a"), _split("|turn|1"), _split("|b")])
    assert r.all_lines == ["|a", "|turn|1", "|b"]
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
cd apps/agent && uv run pytest tests/showdown/test_protocol.py -v
```
Esperado: FAIL, no existe el módulo.

- [ ] **Step 3: Implementar**

`apps/agent/src/ludex_agent/showdown/protocol.py`:
```python
"""Captura del stream crudo de protocolo, agrupado por turno.

El protocolo es la FUENTE DE VERDAD del estado (ver D17): el estado derivado es
una vista materializada que se puede volver a calcular desde aca. Por eso se
guarda tal como llega, incluido el |request|, que trae el equipo propio.

IMPORTANTE: el stream es POR JUGADOR. El |request| de p1 contiene el equipo de
p1 y el de p2 el de p2. Un recorder por jugador, nunca compartido.
"""

from __future__ import annotations

from collections import defaultdict


class ProtocolRecorder:
    def __init__(self) -> None:
        self._by_turn: dict[int, list[str]] = defaultdict(list)
        self._order: list[str] = []
        self._current_turn = 0

    def record(self, split_messages: list[list[str]]) -> None:
        """Recibe las lineas ya separadas por `|`, tal como las da poke-env."""
        for parts in split_messages:
            line = "|".join(parts)
            # `|turn|N` ABRE el turno N: la linea pertenece al turno nuevo.
            if len(parts) > 2 and parts[1] == "turn":
                try:
                    self._current_turn = int(parts[2])
                except ValueError:
                    pass
            self._by_turn[self._current_turn].append(line)
            self._order.append(line)

    def lines_for_turn(self, turn: int) -> list[str]:
        return list(self._by_turn.get(turn, []))

    def turns(self) -> list[int]:
        return sorted(self._by_turn)

    @property
    def all_lines(self) -> list[str]:
        return list(self._order)
```

`apps/agent/src/ludex_agent/showdown/__init__.py`: vacío.

- [ ] **Step 4: Correr y verificar que pasa**

```bash
cd apps/agent && uv run pytest tests/showdown/test_protocol.py -v
```
Esperado: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(agent): captura del protocolo crudo por turno" -- apps/agent/
```

---

## Task 6: Cliente de Showdown

**Files:**
- Create: `apps/agent/src/ludex_agent/showdown/client.py`

**Interfaces:**
- Consumes: `ProtocolRecorder`, `serialize_battle`, `legal_actions`, `action_from_order`, `Settings`.
- Produces: `LudexPlayer(Player)` con `recorders: dict[str, ProtocolRecorder]`, `steps: dict[str, list[dict]]`, y `local_server_configuration(ws_url) -> ServerConfiguration`.

- [ ] **Step 1: Implementar**

No lleva test unitario propio: su comportamiento se verifica en la Tarea 8 contra un servidor real, que es la única prueba honesta de un cliente de red.

`apps/agent/src/ludex_agent/showdown/client.py`:
```python
"""Jugador que graba mientras juega.

Elige al azar entre las acciones legales: el entregable de esta rebanada no es
que juegue bien, es que grabe bien.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from poke_env import ServerConfiguration
from poke_env.player import RandomPlayer

from ..state.actions import action_from_order
from ..state.serializer import serialize_battle
from .protocol import ProtocolRecorder


def local_server_configuration(ws_url: str) -> ServerConfiguration:
    """El server local corre con --no-security: la URL de auth no se usa."""
    return ServerConfiguration(
        ws_url, "https://play.pokemonshowdown.com/action.php?"
    )


class LudexPlayer(RandomPlayer):
    """RandomPlayer que captura protocolo crudo y estado por turno.

    Un ProtocolRecorder por battle_tag, nunca compartido entre jugadores: el
    |request| de cada uno trae su propio equipo.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorders: dict[str, ProtocolRecorder] = defaultdict(ProtocolRecorder)
        self.steps: dict[str, list[dict]] = defaultdict(list)

    def _handle_battle_message(self, split_messages: list[list[str]]) -> Any:
        # El battle_tag llega como primera linea con formato `>battle-...`.
        tag = None
        for parts in split_messages:
            if parts and parts[0].startswith(">"):
                tag = parts[0][1:].strip()
                break
        if tag is None and len(self.recorders) == 1:
            tag = next(iter(self.recorders))
        if tag:
            self.recorders[tag].record(split_messages)
        return super()._handle_battle_message(split_messages)

    def choose_move(self, battle: Any) -> Any:
        order = super().choose_move(battle)
        self.steps[battle.battle_tag].append(
            {
                "turn": battle.turn,
                "state": serialize_battle(battle),
                "action_taken": action_from_order(order),
            }
        )
        return order
```

- [ ] **Step 2: Verificar que importa y que el corte se sostiene**

```bash
cd apps/agent && uv run python -c "from ludex_agent.showdown.client import LudexPlayer, local_server_configuration; print('import OK')"
grep -rn "sqlalchemy\|asyncpg" src/ludex_agent/showdown/ && echo "FALLA: showdown/ toca la DB" || echo "OK: showdown/ no conoce la DB"
```
Esperado: `import OK` y la línea `OK`.

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(agent): cliente de showdown que graba mientras juega" -- apps/agent/
```

---

## Task 7: Persistencia

**Files:**
- Create: `apps/agent/src/ludex_agent/db/__init__.py`, `models.py`, `session.py`, `repository.py`
- Test: `apps/agent/tests/db/test_repository.py`

**Interfaces:**
- Consumes: `Settings.database_url`.
- Produces: `make_engine(url)`, `session_factory(engine)`; y en `repository.py`, la clase `BattleRepository` con `save_battle(...) -> int`, `save_turn(...)`, `save_trajectory(...) -> int`, `save_step(...)`, `finalize(trajectory_id, result, reward)`.

- [ ] **Step 1: Escribir el test que falla**

`apps/agent/tests/db/test_repository.py`:
```python
import os

import pytest
from sqlalchemy import text

from ludex_agent.config import load_settings
from ludex_agent.db.repository import BattleRepository
from ludex_agent.db.session import make_engine, session_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)

TAG = "battle-test-repo-1"


@pytest.fixture
async def repo():
    engine = make_engine(load_settings().database_url)
    factory = session_factory(engine)
    async with factory() as s:
        await s.execute(text("DELETE FROM battles WHERE battle_tag LIKE 'battle-test-%'"))
        await s.commit()
    yield BattleRepository(factory)
    await engine.dispose()


async def test_guarda_batalla_turno_trayectoria_y_paso(repo):
    bid = await repo.save_battle(
        battle_tag=TAG, fmt="gen6randombattle", p1="A", p2="B",
        winner="A", source="local", played_by="bot",
    )
    await repo.save_turn(bid, "p1", 1, ["|turn|1", "|move|p1a: X|Y"])
    tid = await repo.save_trajectory(bid, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(tid, 1, {"schema_version": 1}, 1, [{"kind": "move", "id": "y"}],
                         {"kind": "move", "id": "y"}, "agent")
    await repo.finalize(tid, result="win", reward=1)

    async with repo.factory() as s:
        row = (await s.execute(text(
            "SELECT reward, state_schema_version FROM trajectory_steps WHERE trajectory_id=:t"
        ), {"t": tid})).one()
        assert float(row[0]) == 1.0
        assert row[1] == 1


async def test_es_idempotente_por_battle_tag(repo):
    a = await repo.save_battle(battle_tag=TAG, fmt="f", p1="A", p2="B",
                               winner=None, source="local", played_by="bot")
    b = await repo.save_battle(battle_tag=TAG, fmt="f", p1="A", p2="B",
                               winner="A", source="local", played_by="bot")
    assert a == b, "el mismo battle_tag no debe crear dos filas"
    async with repo.factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM battles WHERE battle_tag=:t"),
                             {"t": TAG})).scalar_one()
        assert n == 1
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
cd apps/agent && DATABASE_URL="postgres://ludex:ludex@localhost:15432/ludex" uv run pytest tests/db/ -v
```
Esperado: FAIL, no existen los módulos.

- [ ] **Step 3: Implementar la sesión**

`apps/agent/src/ludex_agent/db/session.py`:
```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 4: Implementar los modelos**

`apps/agent/src/ludex_agent/db/models.py`:
```python
"""Modelos SQLAlchemy escritos a mano.

Por D1 el esquema vive en las migraciones SQL: estos modelos son un espejo, no
la fuente de verdad. Si divergen, manda la migracion.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Battle(Base):
    __tablename__ = "battles"
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_tag: Mapped[str] = mapped_column(Text, unique=True)
    format: Mapped[str] = mapped_column(Text)
    p1: Mapped[str] = mapped_column(Text)
    p2: Mapped[str] = mapped_column(Text)
    winner: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_by: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column()


class BattleTurn(Base):
    __tablename__ = "battle_turns"
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"), primary_key=True)
    player_side: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_number: Mapped[int] = mapped_column(primary_key=True)
    protocol_lines: Mapped[list[str]] = mapped_column(ARRAY(String))


class Trajectory(Base):
    __tablename__ = "trajectories"
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"))
    gen_id: Mapped[int] = mapped_column()
    format: Mapped[str] = mapped_column(Text)
    player_side: Mapped[str] = mapped_column(Text)
    final_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column()


class TrajectoryStep(Base):
    __tablename__ = "trajectory_steps"
    trajectory_id: Mapped[int] = mapped_column(
        ForeignKey("trajectories.id"), primary_key=True
    )
    turn_number: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[dict] = mapped_column(JSONB)
    state_schema_version: Mapped[int] = mapped_column()
    legal_actions: Mapped[list] = mapped_column(JSONB)
    action_taken: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action_source: Mapped[str] = mapped_column(Text)
    reward: Mapped[float | None] = mapped_column(Numeric, nullable=True)
```

- [ ] **Step 5: Implementar el repositorio**

`apps/agent/src/ludex_agent/db/repository.py`:
```python
"""Escrituras. No conoce poke_env: recibe dicts ya serializados."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text


class BattleRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def save_battle(self, *, battle_tag: str, fmt: str, p1: str, p2: str,
                          winner: str | None, source: str, played_by: str) -> int:
        """Idempotente por battle_tag: reejecutar el runner no duplica."""
        async with self.factory() as s:
            row = await s.execute(text("""
                INSERT INTO battles (battle_tag, format, p1, p2, winner, played_by, source)
                VALUES (:tag, :fmt, :p1, :p2, :w, CAST(:pb AS played_by_kind),
                        CAST(:src AS battle_source))
                ON CONFLICT (battle_tag) DO UPDATE SET winner = EXCLUDED.winner
                RETURNING id
            """), {"tag": battle_tag, "fmt": fmt, "p1": p1, "p2": p2,
                   "w": winner, "pb": played_by, "src": source})
            await s.commit()
            return row.scalar_one()

    async def save_turn(self, battle_id: int, player_side: str, turn: int,
                        lines: list[str]) -> None:
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO battle_turns (battle_id, player_side, turn_number, protocol_lines)
                VALUES (:b, :ps, :t, :lines)
                ON CONFLICT (battle_id, player_side, turn_number)
                DO UPDATE SET protocol_lines = EXCLUDED.protocol_lines
            """), {"b": battle_id, "ps": player_side, "t": turn, "lines": lines})
            await s.commit()

    async def save_trajectory(self, battle_id: int, *, gen_number: int, fmt: str,
                              player_side: str) -> int:
        async with self.factory() as s:
            row = await s.execute(text("""
                INSERT INTO trajectories (battle_id, gen_id, format, player_side)
                SELECT :b, g.id, :fmt, :ps FROM generations g WHERE g.gen_number = :gen
                ON CONFLICT (battle_id, player_side) DO UPDATE SET format = EXCLUDED.format
                RETURNING id
            """), {"b": battle_id, "gen": gen_number, "fmt": fmt, "ps": player_side})
            await s.commit()
            return row.scalar_one()

    async def save_step(self, trajectory_id: int, turn: int, state: dict,
                        version: int, legal: list, action: dict | None,
                        source: str) -> None:
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, turn_number, state, state_schema_version,
                   legal_actions, action_taken, action_source)
                VALUES (:tj, :t, CAST(:st AS jsonb), :v, CAST(:la AS jsonb),
                        CAST(:at AS jsonb), CAST(:src AS action_source))
                ON CONFLICT (trajectory_id, turn_number) DO UPDATE
                  SET state = EXCLUDED.state, legal_actions = EXCLUDED.legal_actions,
                      action_taken = EXCLUDED.action_taken
            """), {"tj": trajectory_id, "t": turn, "st": json.dumps(state), "v": version,
                   "la": json.dumps(legal),
                   "at": json.dumps(action) if action is not None else None,
                   "src": source})
            await s.commit()

    async def finalize(self, trajectory_id: int, *, result: str,
                       reward: float) -> None:
        """Propaga el reward a TODOS los pasos: sin esto no se puede entrenar."""
        async with self.factory() as s:
            await s.execute(text(
                "UPDATE trajectories SET final_result = CAST(:r AS battle_result) WHERE id = :t"
            ), {"r": result, "t": trajectory_id})
            await s.execute(text(
                "UPDATE trajectory_steps SET reward = :rw WHERE trajectory_id = :t"
            ), {"rw": reward, "t": trajectory_id})
            await s.commit()
```

`apps/agent/src/ludex_agent/db/__init__.py`: vacío.

- [ ] **Step 6: Correr y verificar que pasa**

```bash
cd apps/agent && DATABASE_URL="postgres://ludex:ludex@localhost:15432/ludex" uv run pytest tests/db/ -v
```
Esperado: PASS, 2 tests.

- [ ] **Step 7: Verificar el corte de capas**

```bash
cd apps/agent && grep -rn "poke_env" src/ludex_agent/db/ && echo "FALLA: db/ conoce poke_env" || echo "OK: db/ no conoce poke_env"
```
Esperado: la línea `OK`.

- [ ] **Step 8: Commit**

```bash
git commit -m "feat(agent): modelos y repositorio de persistencia" -- apps/agent/
```

---

## Task 8: Runner, integración y los dos tests que importan

**Files:**
- Create: `apps/agent/src/ludex_agent/cli.py`
- Test: `apps/agent/tests/integration/test_play.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: `play(n: int, fmt: str) -> list[str]` (devuelve los battle_tags jugados) y el comando `agent play --n N`.

- [ ] **Step 1: Implementar el runner**

`apps/agent/src/ludex_agent/cli.py`:
```python
"""Runner: juega N batallas en el server local y las persiste."""

from __future__ import annotations

import asyncio

import typer
from poke_env import AccountConfiguration
from poke_env.player import RandomPlayer

from .config import load_settings
from .db.repository import BattleRepository
from .db.session import make_engine, session_factory
from .showdown.client import LudexPlayer, local_server_configuration
from .state.schema import STATE_SCHEMA_VERSION

app = typer.Typer()


async def play(n: int, fmt: str) -> list[str]:
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(abs(hash((n, fmt))) % 10_000)

    agent = LudexPlayer(
        account_configuration=AccountConfiguration(f"{settings.bot_username}{suffix}", None),
        server_configuration=server, battle_format=fmt, log_level=40,
    )
    rival = RandomPlayer(
        account_configuration=AccountConfiguration(f"Rival{suffix}", None),
        server_configuration=server, battle_format=fmt, log_level=40,
    )
    await agent.battle_against(rival, n_battles=n)

    engine = make_engine(settings.database_url)
    repo = BattleRepository(session_factory(engine))
    tags: list[str] = []
    try:
        for tag, battle in agent.battles.items():
            tags.append(tag)
            side = battle.player_role
            battle_id = await repo.save_battle(
                battle_tag=tag, fmt=fmt,
                p1=battle.player_username, p2=battle.opponent_username,
                winner=(battle.player_username if battle.won else battle.opponent_username)
                if battle.finished else None,
                source="local", played_by="bot",
            )
            recorder = agent.recorders[tag]
            for turn in recorder.turns():
                await repo.save_turn(battle_id, side, turn, recorder.lines_for_turn(turn))

            traj = await repo.save_trajectory(
                battle_id, gen_number=battle.gen, fmt=fmt, player_side=side
            )
            for step in agent.steps[tag]:
                await repo.save_step(
                    traj, step["turn"], step["state"], STATE_SCHEMA_VERSION,
                    step["state"]["legal_actions"], step["action_taken"], "agent",
                )
            if battle.finished:
                await repo.finalize(
                    traj, result="win" if battle.won else "loss",
                    reward=1 if battle.won else -1,
                )
    finally:
        await engine.dispose()
    return tags


@app.command()
def run(n: int = 5, fmt: str = "gen6randombattle") -> None:
    tags = asyncio.run(play(n, fmt))
    typer.echo(f"{len(tags)} batallas persistidas")


if __name__ == "__main__":
    app()
```

- [ ] **Step 2: Escribir los dos tests que importan**

`apps/agent/tests/integration/test_play.py`:
```python
import os

import pytest
from sqlalchemy import text

from ludex_agent.config import load_settings
from ludex_agent.cli import play
from ludex_agent.db.session import make_engine, session_factory
from ludex_agent.state.schema import STATE_SCHEMA_VERSION

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="necesita postgres y el server local de showdown",
)


@pytest.fixture(scope="module")
async def jugadas():
    return await play(2, "gen6randombattle")


async def test_persiste_batallas_turnos_y_pasos(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                bid = (await s.execute(text(
                    "SELECT id FROM battles WHERE battle_tag=:t"), {"t": tag})).scalar_one()
                turnos = (await s.execute(text(
                    "SELECT count(*) FROM battle_turns WHERE battle_id=:b"),
                    {"b": bid})).scalar_one()
                pasos = (await s.execute(text("""
                    SELECT count(*) FROM trajectory_steps ts
                    JOIN trajectories t ON t.id = ts.trajectory_id WHERE t.battle_id = :b
                """), {"b": bid})).scalar_one()
                assert turnos > 0 and pasos > 0
    finally:
        await engine.dispose()


async def test_el_reward_esta_propagado(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            sin_reward = (await s.execute(text("""
                SELECT count(*) FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags) AND ts.reward IS NULL
            """), {"tags": list(jugadas)})).scalar_one()
            assert sin_reward == 0
    finally:
        await engine.dispose()


async def test_no_hay_fuga_de_informacion_del_rival(jugadas):
    """LA propiedad de correccion de esta rebanada.

    Para cada turno N, ningun pokemon del rival puede estar en el estado si el
    protocolo no lo revelo hasta ese turno. El protocolo persistido es el juez.
    Si un modelo se entrena con informacion que un jugador no tiene, es inutil
    en batalla real.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT ts.turn_number, ts.state, t.player_side, t.battle_id
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags)
                ORDER BY t.battle_id, ts.turn_number
            """), {"tags": list(jugadas)})).all()
            assert filas, "no hay pasos que verificar"

            for turno, estado, side, battle_id in filas:
                acumulado = (await s.execute(text("""
                    SELECT string_agg(array_to_string(protocol_lines, ' '), ' ')
                    FROM battle_turns
                    WHERE battle_id = :b AND player_side = :ps AND turn_number <= :t
                """), {"b": battle_id, "ps": side, "t": turno})).scalar_one() or ""
                for mon in estado["opponent"]["pokemon"]:
                    especie = mon["species"].replace("-", "").lower()
                    visto = especie in acumulado.replace("-", "").replace(" ", "").lower()
                    assert visto, (
                        f"FUGA: {mon['species']} aparece en el estado del turno "
                        f"{turno} pero el protocolo no lo revelo hasta ahi"
                    )
    finally:
        await engine.dispose()


async def test_la_version_de_esquema_esta_en_todas_las_filas(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            distintas = (await s.execute(text("""
                SELECT DISTINCT ts.state_schema_version FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags)
            """), {"tags": list(jugadas)})).scalars().all()
            assert distintas == [STATE_SCHEMA_VERSION]
    finally:
        await engine.dispose()


async def test_reejecutar_no_duplica(jugadas):
    """El runner es idempotente por battle_tag."""
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                n = (await s.execute(text(
                    "SELECT count(*) FROM battles WHERE battle_tag=:t"),
                    {"t": tag})).scalar_one()
                assert n == 1
    finally:
        await engine.dispose()
```

- [ ] **Step 3: Levantar el server local y correr**

```bash
docker compose --profile local up -d showdown
sleep 5 && curl -sf http://localhost:8100/ -o /dev/null && echo "showdown OK"
cd apps/agent && DATABASE_URL="postgres://ludex:ludex@localhost:15432/ludex" uv run pytest tests/integration/ -v
```
Esperado: PASS, 8 tests. Tarda unos minutos: son dos batallas reales.

- [ ] **Step 4: Correr el runner a mano**

```bash
cd apps/agent && DATABASE_URL="postgres://ludex:ludex@localhost:15432/ludex" uv run python -m ludex_agent.cli --n 5
```
Esperado: `5 batallas persistidas`.

- [ ] **Step 5: Verificar la re-derivación**

Es la propiedad que hace reversible el serializador. Con el protocolo guardado, un consumidor futuro tiene que poder reconstruir el estado.

```bash
cd apps/agent && DATABASE_URL="postgres://ludex:ludex@localhost:15432/ludex" uv run python - <<'PY'
import asyncio
from sqlalchemy import text
from ludex_agent.config import load_settings
from ludex_agent.db.session import make_engine, session_factory

async def main():
    e = make_engine(load_settings().database_url)
    async with session_factory(e)() as s:
        rows = (await s.execute(text("""
            SELECT b.battle_tag, t.player_side, count(DISTINCT bt.turn_number) AS turnos,
                   count(DISTINCT ts.turn_number) AS pasos
            FROM battles b
            JOIN trajectories t ON t.battle_id = b.id
            JOIN battle_turns bt ON bt.battle_id = b.id AND bt.player_side = t.player_side
            JOIN trajectory_steps ts ON ts.trajectory_id = t.id
            GROUP BY 1,2 ORDER BY 1 LIMIT 5
        """))).all()
        for r in rows:
            print(f"{r[0]:38} side={r[1]}  turnos_protocolo={r[2]:3}  pasos_estado={r[3]}")
        assert rows, "no hay batallas persistidas"
        assert all(r[2] >= r[3] for r in rows), \
            "hay mas pasos de estado que turnos de protocolo: falta protocolo para re-derivar"
        print("OK: cada paso de estado tiene su protocolo crudo")
    await e.dispose()
asyncio.run(main())
PY
```
Esperado: una línea por batalla y `OK` al final. Pegá la salida en el reporte.

- [ ] **Step 6: Verificar que no hay generación hardcodeada**

```bash
cd /Users/miguelhernandez/Documents/ludex && grep -rin "gen6" apps/agent/src/
```
Esperado: cero resultados. Las apariciones válidas están en `tests/` y en los valores por defecto de `config.py`, que son configuración.

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(agent): runner y tests de fuga y re-derivacion" -- apps/agent/
```

---

## Self-Review

**Cobertura de la spec:**

| requisito de la spec | tarea |
|---|---|
| §4 D16 calc con Node | fuera de alcance de este plan, es de Kimi |
| §4 D17 protocolo crudo como fuente de verdad | 1 (esquema), 5 (captura), 8 step 5 |
| §4 D18 lista blanca explícita | 4, con test que lo verifica |
| §4 D19 `gen6randombattle` | 8 |
| §4 D20 un escritor por directorio | Global Constraints |
| §5 reparto paralelo | Global Constraints, y `packages/calc` no aparece acá |
| §6 estructura de archivos | File Structure |
| §7 esquema, con `player_side` en la PK | 1 |
| §8 contenido del serializador | 4 |
| §8 `reward` propagado | 7 (`finalize`), 8 (test) |
| §9 test de fuga | 8 |
| §9 test de re-derivación | 8 step 5 |
| §9 tests puros de `state/` | 3, 4, 5 |
| §9 test de integración | 8 |
| §10 criterios de aceptación 1-8 | 8 |

Sin huecos. Dos desviaciones respecto de la spec, ambas justificadas por los hallazgos empíricos:

1. **`battle_turns` lleva `player_side` en la PK.** La spec lo tenía por batalla. El protocolo es por jugador; guardarlo compartido rompe la re-derivación y filtra el equipo de un jugador al contexto del otro.
2. **La spec mencionaba `elo_bucket` y `agent_reasoning`.** Las columnas se crean pero no se llenan: no hay elo en el server local ni razonamiento sin LLM. Se llenan en las rebanadas siguientes.

**Consistencia de tipos:** `STATE_SCHEMA_VERSION` se define en la Tarea 3 y lo usan 4, 7 y 8. `legal_actions` y `action_from_order` se definen en la 3 y los usa la 4 y la 6. `ProtocolRecorder` se define en la 5 y la usa la 6. `BattleRepository` se define en la 7 y la usa la 8 con los mismos nombres de parámetro.

**Nota de entorno:** el server local de Showdown quedó levantado durante la planificación (`docker compose --profile local up -d showdown`). La Tarea 8 lo necesita. Para bajarlo al terminar: `docker compose stop showdown`, nunca `docker compose down`.
