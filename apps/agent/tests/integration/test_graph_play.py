import asyncio
import os
import time

import pytest
from poke_env import AccountConfiguration
from poke_env.player import RandomPlayer
from sqlalchemy import text

from ludex_agent.cli import _persist_one
from ludex_agent.config import load_settings
from ludex_agent.db.context_repository import PostgresContextRepository
from ludex_agent.db.repository import BattleRepository
from ludex_agent.db.session import make_engine, session_factory
from ludex_agent.graph.calc import CalcClient
from ludex_agent.graph.provider import DecisionMetrics
from ludex_agent.graph.workflow import build_decision_graph
from ludex_agent.showdown.client import LudexPlayer, local_server_configuration

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)


class FirstLegalGraph:
    async def ainvoke(self, graph_input):
        return {
            "action": graph_input["raw_state"]["legal_actions"][0],
            "action_path": "llm",
        }


class AlwaysIllegalProvider:
    async def complete(self, prompt, *, deadline, turn_id):
        return {
            "action": {"kind": "move", "id": "definitelyillegal"},
            "reasoning": "forced invalid response",
        }


@pytest.mark.asyncio
async def test_diez_batallas_fake_persisten_camino_y_acciones_de_su_mascara():
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(time.time_ns())[-8:]
    common = {
        "server_configuration": server,
        "battle_format": settings.showdown_battle_format,
        "log_level": 40,
        "max_concurrent_battles": 5,
    }
    agent = LudexPlayer(
        account_configuration=AccountConfiguration(f"Graph{suffix}", None),
        decision_graph=FirstLegalGraph(),
        **common,
    )
    rival = RandomPlayer(
        account_configuration=AccountConfiguration(f"GraphOpp{suffix}", None),
        **common,
    )
    engine = make_engine(settings.database_url)
    repo = BattleRepository(session_factory(engine))
    tags = []
    try:
        await agent.battle_against(rival, n_battles=10)
        tags = list(agent.battles)
        for tag in tags:
            await _persist_one(
                agent, repo, tag, settings.showdown_battle_format, "test"
            )

        async with repo.factory() as session:
            rows = (await session.execute(text("""
                SELECT ts.action_source::text, ts.action_path,
                       ts.action_taken, ts.legal_actions
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id=ts.trajectory_id
                JOIN battles b ON b.id=t.battle_id
                WHERE b.battle_tag = ANY(:tags)
            """), {"tags": tags})).all()
        assert rows
        assert all(source == "agent" and path == "llm" for source, path, _, _ in rows)
        assert all(action in legal for _, _, action, legal in rows)
    finally:
        if tags:
            async with repo.factory() as session:
                await session.execute(text(
                    "DELETE FROM battles "
                    "WHERE battle_tag = ANY(:tags) AND source='test'"
                ), {"tags": tags})
                await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_respuesta_ilegal_dos_veces_juega_y_persiste_fallback():
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(time.time_ns())[-8:]
    engine = make_engine(settings.database_url)
    factory = session_factory(engine)
    calculator = CalcClient("http://127.0.0.1:8200", timeout_seconds=3)
    graph = build_decision_graph(
        calculator,
        AlwaysIllegalProvider(),
        DecisionMetrics(),
        PostgresContextRepository(factory),
    )
    common = {
        "server_configuration": server,
        "battle_format": settings.showdown_battle_format,
        "log_level": 40,
    }
    agent = LudexPlayer(
        account_configuration=AccountConfiguration(f"Fallback{suffix}", None),
        decision_graph=graph,
        **common,
    )
    rival = RandomPlayer(
        account_configuration=AccountConfiguration(f"FbOpp{suffix}", None),
        **common,
    )
    repo = BattleRepository(factory)
    tags = []
    try:
        try:
            async with asyncio.timeout(45):
                await agent.battle_against(rival, n_battles=1)
        except TimeoutError:
            observed = {
                tag: {
                    "steps": len(agent.steps[tag]),
                    "turn": battle.turn,
                    "finished": battle.finished,
                    "last_actions": [
                        step.get("action_taken")
                        for step in agent.steps[tag][-5:]
                        if step is not None
                    ],
                }
                for tag, battle in agent.battles.items()
            }
            pytest.fail(f"fallback battle excedió 45s: {observed}")
        tags = list(agent.battles)
        for tag in tags:
            await _persist_one(
                agent, repo, tag, settings.showdown_battle_format, "test"
            )
        async with repo.factory() as session:
            paths = (await session.execute(text("""
                SELECT DISTINCT ts.action_path
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id=ts.trajectory_id
                JOIN battles b ON b.id=t.battle_id
                WHERE b.battle_tag = ANY(:tags)
            """), {"tags": tags})).scalars().all()
        assert paths == ["fallback"]
    finally:
        tags = tags or list(agent.battles)
        if tags:
            async with repo.factory() as session:
                await session.execute(text(
                    "DELETE FROM battles "
                    "WHERE battle_tag = ANY(:tags) AND source='test'"
                ), {"tags": tags})
                await session.commit()
        await calculator.aclose()
        await engine.dispose()
