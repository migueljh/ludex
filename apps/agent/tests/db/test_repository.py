import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ludex_agent.config import load_settings
from ludex_agent.db.repository import BattleRepository, BattleTagCollisionError
from ludex_agent.db.session import make_engine, session_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)

TAG = "battle-test-repo-1"

# I6 (review final): estas filas viven en las mismas tablas que el dataset de
# entrenamiento. `source="test"` (migracion 20260727000007) las marca como
# sinteticas, y el DELETE de la fixture se acota ADEMAS por `source = 'test'`
# para que ni siquiera una coincidencia de prefijo pueda tocar una fila real.
SOURCE = "test"


# loop_scope="function" explicito: el default del proyecto
# (asyncio_default_fixture_loop_scope = "module" en pyproject.toml) es para la
# fixture de scope module de la Tarea 8. Sin este override, esta fixture
# (scope function) corre en el loop de modulo mientras el test corre en el de
# funcion, y asyncpg revienta con "attached to a different loop".
@pytest_asyncio.fixture(loop_scope="function")
async def repo():
    engine = make_engine(load_settings().database_url)
    factory = session_factory(engine)
    async with factory() as s:
        await s.execute(text(
            "DELETE FROM battles WHERE battle_tag LIKE 'battle-test-%' AND source = 'test'"
        ))
        await s.commit()
    yield BattleRepository(factory)
    await engine.dispose()


async def test_guarda_batalla_turno_trayectoria_y_paso(repo):
    bid = await repo.save_battle(
        battle_tag=TAG, fmt="gen6randombattle", p1="A", p2="B",
        winner="A", source=SOURCE, played_by="bot",
    )
    await repo.save_turn(bid, "p1", 1, ["|turn|1", "|move|p1a: X|Y"])
    tid = await repo.save_trajectory(bid, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1,
        [{"kind": "move", "id": "y"}],
        {"kind": "move", "id": "y"}, "agent",
        action_path="llm_retry",
    )
    await repo.finalize(tid, result="win", reward=1)

    async with repo.factory() as s:
        row = (await s.execute(text(
            "SELECT reward, state_schema_version, decision_index, turn_number, "
            "action_source::text, action_path "
            "FROM trajectory_steps WHERE trajectory_id=:t"
        ), {"t": tid})).one()
        assert float(row[0]) == 1.0
        assert row[1] == 1
        assert row[2] == 0
        assert row[3] == 1
        assert row[4] == "agent"
        assert row[5] == "llm_retry"


async def test_action_path_nullable_y_restringido(repo):
    bid = await repo.save_battle(
        battle_tag="battle-test-action-path", fmt="gen6randombattle",
        p1="A", p2="B", winner=None, source=SOURCE, played_by="bot",
    )
    tid = await repo.save_trajectory(
        bid, gen_number=6, fmt="gen6randombattle", player_side="p1"
    )
    await repo.save_step(tid, 0, 1, {}, 1, [], None, "agent", action_path=None)

    async with repo.factory() as s:
        assert (await s.execute(text(
            "SELECT action_path FROM trajectory_steps "
            "WHERE trajectory_id=:t AND decision_index=0"
        ), {"t": tid})).scalar_one_or_none() is None
        with pytest.raises(DBAPIError, match="trajectory_steps_action_path_check") as exc:
            await s.execute(text(
                "UPDATE trajectory_steps SET action_path='random' "
                "WHERE trajectory_id=:t AND decision_index=0"
            ), {"t": tid})
        assert exc.value.orig.sqlstate == "23514"


async def test_es_idempotente_por_battle_tag(repo):
    a = await repo.save_battle(battle_tag=TAG, fmt="f", p1="A", p2="B",
                               winner=None, source=SOURCE, played_by="bot")
    b = await repo.save_battle(battle_tag=TAG, fmt="f", p1="A", p2="B",
                               winner="A", source=SOURCE, played_by="bot")
    assert a == b, "el mismo battle_tag no debe crear dos filas"
    async with repo.factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM battles WHERE battle_tag=:t"),
                             {"t": TAG})).scalar_one()
        assert n == 1


async def test_dos_decisiones_del_mismo_turno_no_se_pisan(repo):
    """C2/D21: un cambio forzado tras un debilitamiento no avanza el turno.
    Antes, la clave (trajectory_id, turn_number) hacia que la segunda
    decision pisara a la primera; ahora decision_index las distingue."""
    bid = await repo.save_battle(
        battle_tag=TAG, fmt="gen6randombattle", p1="A", p2="B",
        winner=None, source=SOURCE, played_by="bot",
    )
    # save_turn tambien, para no dejar un paso huerfano sin protocolo: el
    # invariante de dataset "cada paso tiene su protocolo" (test_play.py)
    # corre sobre TODA la tabla, sin filtrar por battle_tag ni source.
    await repo.save_turn(bid, "p1", 3, ["|turn|3"])
    tid = await repo.save_trajectory(bid, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(tid, 0, 3, {"paso": 0}, 1, [], {"kind": "move", "id": "tackle"}, "agent")
    await repo.save_step(tid, 1, 3, {"paso": 1}, 1, [], {"kind": "switch", "species": "y"}, "agent")

    async with repo.factory() as s:
        filas = (await s.execute(text(
            "SELECT decision_index, turn_number FROM trajectory_steps "
            "WHERE trajectory_id=:t ORDER BY decision_index"
        ), {"t": tid})).all()
        assert [tuple(f) for f in filas] == [(0, 3), (1, 3)]


async def test_colision_de_battle_tag_revienta(repo):
    """I2: si el mismo battle_tag ya existe con otro p1/p2/format, es una
    colision del contador de Showdown, no una re-persistencia legitima."""
    await repo.save_battle(battle_tag=TAG, fmt="gen6randombattle", p1="A", p2="B",
                           winner=None, source=SOURCE, played_by="bot")
    with pytest.raises(BattleTagCollisionError):
        await repo.save_battle(battle_tag=TAG, fmt="gen6randombattle", p1="OTRO", p2="B",
                               winner=None, source=SOURCE, played_by="bot")
