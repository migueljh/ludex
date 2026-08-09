import asyncio
import os
import time

import pytest
from poke_env import AccountConfiguration
from poke_env.player import RandomPlayer
from sqlalchemy import text

from _dex_template import disposable_dex_clone
from _disposable import verified_engine
from ludex_agent.cli import _battle_against_or_failure, _persist_one
from ludex_agent.config import load_settings
from ludex_agent.db.context_repository import PostgresContextRepository
from ludex_agent.db.repository import BattleRepository
from ludex_agent.db.session import make_engine, session_factory
from ludex_agent.graph.calc import CalcClient
from ludex_agent.graph.provider import (
    CompletionEnvelope,
    CompletionUsage,
    DecisionMetrics,
    PinnedResolver,
)
from ludex_agent.graph.workflow import build_decision_graph
from ludex_agent.showdown.client import LudexPlayer, local_server_configuration

# MON-11 (R3): igual que test_play.py -- TEST_DATABASE_URL (clon
# descartable) + DATABASE_URL (unica fuente admitida, READ-ONLY, para el
# dex). Este archivo ademas usa `PostgresContextRepository` (pokemon/moves/
# items/learnsets reales, no solo `generations`), así que necesita el clon
# completo de `_dex_template.py`, no una base vacía.
pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (clon descartable) + DATABASE_URL "
    "(fuente read-only del dex) + el server local de showdown",
)


@pytest.fixture(scope="module")
async def dex_clone():
    """Reapunta `DATABASE_URL` a un clon descartable del dex durante toda
    la vida del modulo -- las 4 funciones de test de este archivo abren su
    propia conexion via `load_settings().database_url`, así que ninguna
    llega a ver la base compartida. Mismo mecanismo que `test_play.py`."""
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]
    async with disposable_dex_clone(base, shared) as url:
        anterior = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        try:
            engine = await verified_engine(load_settings().database_url)
            await engine.dispose()
            yield url
        finally:
            if anterior is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = anterior


class FirstLegalGraph:
    async def ainvoke(self, graph_input):
        return {
            "action": graph_input["raw_state"]["legal_actions"][0],
            "action_path": "llm",
        }


class AlwaysIllegalProvider:
    """F2-08 (MON-13, integrado a esta rama en R1): `DecisionProvider.
    complete` devuelve un `CompletionEnvelope`, no un dict -- `decide()`
    lee `envelope.usage`/`.provider`/`.model`/`.latency_ms` para la
    metadata que persiste en `trajectory_steps` (D38)."""

    async def complete(self, prompt, *, deadline, turn_id):
        return CompletionEnvelope(
            payload={
                "action": {"kind": "move", "id": "definitelyillegal"},
                "rationale": "forced invalid response",
                "confidence": 0.5,
                "alternatives": [],
            },
            provider="fake",
            model="fake-model",
            usage=CompletionUsage(input_tokens=1, output_tokens=1),
            latency_ms=0.0,
        )


SHADOW_TAG_AGENT_TEAM = """
Pikachu
Ability: Static
Level: 100
- Thunderbolt

Bulbasaur
Ability: Overgrow
Level: 100
- Energy Ball
"""

SHADOW_TAG_OPPONENT_TEAM = """
Wobbuffet
Ability: Shadow Tag
Level: 1
- Splash

Magikarp
Ability: Swift Swim
Level: 1
- Splash
"""


class PreferSwitchGraph:
    """El primer request maybeTrapped ofrece un switch que Shadow Tag rechaza."""

    async def ainvoke(self, graph_input):
        legal = graph_input["raw_state"]["legal_actions"]
        active_opponent = next(
            (
                pokemon
                for pokemon in graph_input["raw_state"]["opponent"]["pokemon"]
                if pokemon["active"]
            ),
            None,
        )
        switch = next(
            (candidate for candidate in legal if candidate["kind"] == "switch"),
            None,
        )
        action = (
            switch
            if switch is not None
            and active_opponent is not None
            and active_opponent["species"] == "wobbuffet"
            else legal[0]
        )
        return {
            "action": action,
            "action_path": "llm",
            "reasoning": "prefer a switch when Showdown still offers one",
        }


class OrderedLudexPlayer(LudexPlayer):
    def teampreview(self, battle):
        for pokemon in battle.team.values():
            pokemon._selected_in_teampreview = True
        return "/team 12"


class OrderedRandomPlayer(RandomPlayer):
    def teampreview(self, battle):
        for pokemon in battle.team.values():
            pokemon._selected_in_teampreview = True
        return "/team 12"


async def _delete_test_battles(repo, tags):
    if not tags:
        return
    async with repo.factory() as session:
        await session.execute(text(
            "DELETE FROM battles "
            "WHERE battle_tag = ANY(:tags) AND source='test'"
        ), {"tags": tags})
        await session.commit()


@pytest.mark.asyncio
async def test_diez_batallas_fake_persisten_camino_y_acciones_de_su_mascara(dex_clone):
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
async def test_respuesta_ilegal_dos_veces_juega_y_persiste_fallback(dex_clone):
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(time.time_ns())[-8:]
    engine = make_engine(settings.database_url)
    factory = session_factory(engine)
    calculator = CalcClient("http://127.0.0.1:8200", timeout_seconds=3)
    context_repository = PostgresContextRepository(settings.database_url)
    graph = build_decision_graph(
        calculator,
        PinnedResolver(
            AlwaysIllegalProvider(), "fake", "fake-model",
        ),
        DecisionMetrics(),
        context_repository,
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
        await context_repository.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_tag_reconcilia_unavailable_sin_persistir_ni_premiar_switch(dex_clone):
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(time.time_ns())[-8:]
    fmt = "gen6customgame"
    common = {
        "server_configuration": server,
        "battle_format": fmt,
        "log_level": 40,
    }
    agent = OrderedLudexPlayer(
        account_configuration=AccountConfiguration(f"Trap{suffix}", None),
        decision_graph=PreferSwitchGraph(),
        team=SHADOW_TAG_AGENT_TEAM,
        **common,
    )
    rival = OrderedRandomPlayer(
        account_configuration=AccountConfiguration(f"TrapOpp{suffix}", None),
        team=SHADOW_TAG_OPPONENT_TEAM,
        **common,
    )
    engine = make_engine(settings.database_url)
    repo = BattleRepository(session_factory(engine))
    tags: list[str] = []
    try:
        async with asyncio.timeout(45):
            await _battle_against_or_failure(agent, rival)
        tags = list(agent.battles)
        assert len(tags) == 1
        tag = tags[0]
        assert agent.rejected_choice_count >= 1, (
            "CANARIO: Shadow Tag no produjo ningun Unavailable choice"
        )
        await _persist_one(agent, repo, tag, fmt, "test")

        async with repo.factory() as session:
            raw_errors = (await session.execute(text("""
                SELECT count(*)
                FROM battles b
                JOIN battle_turns bt ON bt.battle_id=b.id,
                     LATERAL unnest(bt.protocol_lines) line
                WHERE b.battle_tag=:tag
                  AND line LIKE '|error|[Unavailable choice]%'
            """), {"tag": tag})).scalar_one()
            rows = (await session.execute(text("""
                SELECT ts.decision_index, ts.action_taken, ts.reward
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id=ts.trajectory_id
                JOIN battles b ON b.id=t.battle_id
                WHERE b.battle_tag=:tag
                ORDER BY ts.decision_index
            """), {"tag": tag})).all()

        assert raw_errors >= 1
        assert rows
        assert [row[0] for row in rows] == list(range(len(rows)))
        assert len(rows) == len(agent.steps[tag])
        assert all(row[2] is not None for row in rows)
        assert rows[0][1] != {"kind": "switch", "species": "bulbasaur"}, (
            "el switch rechazado del primer slot no puede entrar al dataset "
            "ni recibir reward; un switch posterior aceptado si es una "
            "decision distinta y valida"
        )
    finally:
        tags = tags or list(agent.battles)
        await _delete_test_battles(repo, tags)
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_move_99_reintenta_inline_sin_crear_un_step_fantasma(dex_clone):
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(time.time_ns())[-8:]
    fmt = "gen6customgame"
    common = {
        "server_configuration": server,
        "battle_format": fmt,
        "log_level": 40,
    }
    agent = OrderedLudexPlayer(
        account_configuration=AccountConfiguration(f"Invalid{suffix}", None),
        decision_graph=FirstLegalGraph(),
        team=SHADOW_TAG_AGENT_TEAM,
        **common,
    )
    rival = OrderedRandomPlayer(
        account_configuration=AccountConfiguration(f"InvalidOpp{suffix}", None),
        team=SHADOW_TAG_OPPONENT_TEAM.replace("Shadow Tag", "Telepathy"),
        **common,
    )
    real_send = agent._send_message_original
    corrupted = 0

    async def corrupt_one_choice(message, room="", message_2=None):
        nonlocal corrupted
        if corrupted == 0 and message.startswith("/choose"):
            corrupted += 1
            message = "/choose move 99"
        return await real_send(message, room, message_2)

    agent._send_message_original = corrupt_one_choice
    engine = make_engine(settings.database_url)
    repo = BattleRepository(session_factory(engine))
    tags: list[str] = []
    try:
        async with asyncio.timeout(45):
            await _battle_against_or_failure(agent, rival)
        tags = list(agent.battles)
        assert corrupted == 1, "CANARIO: el seam nunca envio move 99"
        assert agent.rejected_choice_count == 1
        tag = tags[0]
        await _persist_one(agent, repo, tag, fmt, "test")

        async with repo.factory() as session:
            raw_errors = (await session.execute(text("""
                SELECT count(*)
                FROM battles b
                JOIN battle_turns bt ON bt.battle_id=b.id,
                     LATERAL unnest(bt.protocol_lines) line
                WHERE b.battle_tag=:tag
                  AND line LIKE '|error|[Invalid choice]%'
            """), {"tag": tag})).scalar_one()
            rows = (await session.execute(text("""
                SELECT ts.decision_index, ts.action_taken, ts.reward
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id=ts.trajectory_id
                JOIN battles b ON b.id=t.battle_id
                WHERE b.battle_tag=:tag
                ORDER BY ts.decision_index
            """), {"tag": tag})).all()

        assert raw_errors == 1
        assert rows
        assert [row[0] for row in rows] == list(range(len(rows)))
        assert len(rows) == len(agent.steps[tag])
        assert all(row[2] is not None for row in rows)
        assert not any(
            action == {"kind": "move", "id": "99"}
            for _, action, _ in rows
        )
    finally:
        tags = tags or list(agent.battles)
        await _delete_test_battles(repo, tags)
        await engine.dispose()
