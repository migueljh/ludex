import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from ludex_agent.config import load_settings
from ludex_agent.db.repository import BattleRepository
from ludex_agent.db.session import make_engine, session_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)

TAG = "battle-test-repo-1"


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
