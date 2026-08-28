"""Observabilidad de decisiones en vuelo (MON-20 DIAG-A).

Canario de la ETAPA INVISIBLE: una decision atascada en un await del grafo
(por ejemplo, un await SQL sin deadline) no deja ningun registro de en que
etapa del pipeline esta. Las tasks de `choose_move` corren como fire-and-
forget en `ps_client.loop` (POKE_LOOP en produccion, otro thread), hermanas
de `battle_against`: `asyncio.all_tasks()` desde el loop del caller no las
ve, y el unico observable actual es la fase de correlacion de choices
(`PendingChoice.phase`), que nada dice del grafo.

Este modulo verifica la superficie diagnostica que el proximo diagnostico
en vivo va a usar: `LudexPlayer.decision_snapshot()` debe identificar la
decision en vuelo, su ETAPA actual y la linea exacta que esta esperando,
sin serializar prompts, credenciales, respuestas, filas ni payloads.

Cero infraestructura: POKE_LOOP real (el loop global de poke-env, en su
propio thread), un repositorio de contexto falso que se queda esperando una
senal que nadie envia (simula el await SQL sin deadline), y nada de Docker.
"""

import asyncio
import random
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from poke_env.concurrency import POKE_LOOP

from ludex_agent.graph.decision import DecisionMetrics
from ludex_agent.graph.provider import PinnedResolver
from ludex_agent.graph.workflow import build_decision_graph
from ludex_agent.hitl.policy import AlwaysGateApprovalPolicy
from ludex_agent.showdown import client as client_module
from ludex_agent.showdown.client import LudexPlayer, local_server_configuration


def _player(**kwargs) -> LudexPlayer:
    from poke_env import AccountConfiguration

    sufijo = random.randint(1000, 9999)
    kwargs.setdefault("start_listening", False)
    return LudexPlayer(
        account_configuration=AccountConfiguration(f"Obs{sufijo}", None),
        battle_format="gen6randombattle",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
        **kwargs,
    )


def _fake_battle(**overrides) -> SimpleNamespace:
    base = dict(
        turn=3, battle_tag="battle-x-1", player_role="p1",
        format="gen6randombattle", gen=6,
        weather={}, fields={}, side_conditions={}, opponent_side_conditions={},
        team={}, opponent_team={},
        available_moves=[], available_switches=[], can_mega_evolve=False,
        active_pokemon=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_serialize(battle) -> dict:
    """Snapshot minimo que atraviesa `parse_state` y `retrieve_context`:
    `gen` entero y lados con `pokemon` vacios (sin especies que consultar)."""
    return {
        "turn": battle.turn,
        "player_role": "p1",
        "format": "gen6randombattle",
        "gen": 6,
        "me": {"pokemon": []},
        "opponent": {"pokemon": []},
        "field": {
            "weather": {}, "field_effects": {},
            "my_side": {}, "opponent_side": {},
        },
        "legal_actions": [{"kind": "move", "id": "tackle"}],
    }


def _run_on_pokeloop(coro):
    """Agenda `coro` en `POKE_LOOP` (el loop real de poke-env, en su propio
    thread) y devuelve el `concurrent.futures.Future`. Mismo mecanismo que
    `poke_env.concurrency.handle_threaded_coroutines`, reteniendo el future
    para inspeccionar una decision huerfana."""
    return asyncio.run_coroutine_threadsafe(coro, POKE_LOOP)


async def _await_on_pokeloop(coro) -> None:
    await asyncio.wrap_future(_run_on_pokeloop(coro))


class _StuckContextRepository:
    """Simula el await SQL sin deadline de `PostgresContextRepository`
    (context_repository.py: no hay timeout en ningun `session.execute`):
    `load_battle_context` se queda esperando una senal que nadie envia.

    El `asyncio.Event` se crea DENTRO del metodo, que corre en POKE_LOOP:
    un Event creado en el loop del test no se puede esperar desde otro loop.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self._gate: asyncio.Event | None = None

    async def load_battle_context(self, **kwargs) -> dict:
        self.started.set()
        if self._gate is None:
            self._gate = asyncio.Event()
        await self._gate.wait()
        raise AssertionError("load_battle_context no debe retornar: el test la cuelga")

    async def load_moves(self, **kwargs) -> dict:
        return {}

    async def load_mega_forms(self, **kwargs) -> dict:
        return {}


class _SilentCalculator:
    """Nunca se llama: `retrieve_context` bloquea antes de `calc_damage`."""

    async def calculate(self, request) -> dict:
        raise AssertionError("calc_damage no debe alcanzarse: el contexto bloquea antes")


class _SilentProvider:
    """Nunca se llama: `retrieve_context` bloquea antes de `decide`."""

    async def complete(self, prompt, *, deadline, turn_id):
        raise AssertionError("decide no debe alcanzarse: el contexto bloquea antes")


@pytest.mark.asyncio
async def test_snapshot_identifica_etapa_y_linea_de_una_decision_atascada():
    """La etapa del grafo de una decision en vuelo debe ser observable.

    Sin la superficie diagnostica, una decision atascada en `retrieve_context`
    es INVISIBLE: la task vive en POKE_LOOP (otro thread), el caller-loop no
    la ve con `asyncio.all_tasks()`, y no hay ningun registro de la etapa.
    Este canario exige que `decision_snapshot()` identifique la decision en
    vuelo, su etapa exacta y la linea que esta esperando.
    """
    tag = "battle-stage-snapshot-1"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(battle_tag=tag, available_moves=[move])
    repository = _StuckContextRepository()
    player = _player(decision_graph=None, decision_budget_seconds=60)
    player.decision_graph = build_decision_graph(
        _SilentCalculator(),
        PinnedResolver(_SilentProvider(), "fake", "fake-model"),
        DecisionMetrics(),
        repository,
        on_stage=player.record_decision_stage,
    )

    try:
        with patch.object(client_module, "serialize_battle", _stub_serialize):
            pending = player.choose_move(battle)
            # Topologia real: la coroutine corre como task en POKE_LOOP,
            # hermana de un wrapper tipo `battle_against`.
            decision_future = _run_on_pokeloop(pending)
            await _await_on_pokeloop(player.frame_inbox.publish(tag, ("|upkeep",)))
            assert repository.started.wait(timeout=2), (
                "la decision nunca llego a retrieve_context"
            )

            snapshot = await player.decision_snapshot()
            assert snapshot, "decision_snapshot() debe listar la decision en vuelo"
            stage_entries = [e for e in snapshot if e["stage"] == "retrieve_context"]
            assert stage_entries, (
                "el snapshot debe incluir una entrada con etapa retrieve_context; "
                "vistas: "
                + ", ".join(f"{e['stage']!r}" for e in snapshot)
            )
            awaited = [
                frame
                for entry in stage_entries
                for frame in entry["frames"]
                if frame["function"] == "load_battle_context"
            ]
            assert awaited, (
                "el snapshot debe incluir el frame de load_battle_context "
                "(la linea exacta que la decision esta esperando); frames: "
                + ", ".join(
                    f"{frame['function']}:{frame['line']}"
                    for entry in snapshot
                    for frame in entry["frames"]
                )
            )
    finally:
        await player.drain_inflight_decisions()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wrap_future(decision_future)


class _OkGraph:
    """Grafo que decide al instante: la decision se queda esperando el GATE."""

    async def ainvoke(self, graph_input):
        return {
            "action": {"kind": "move", "id": "tackle"},
            "action_path": "llm",
            "rationale": "r", "confidence": 0.5, "alternatives": [],
            "target": None, "provider": "fake", "model": "fake-model",
            "decision_latency_ms": 1.0, "input_tokens": 1,
            "output_tokens": 1, "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        }


class _FakePendingRepo:
    """El gate persiste antes de publicar: el repo debe existir y ser rapido."""

    async def insert_awaiting(self, record) -> None:
        pass

    async def resolve(self, key, resolution, approval_wait_ms) -> None:
        pass

    async def abort_stale(self, reason: str = "process_restart") -> int:
        return 0


async def test_snapshot_identifica_la_etapa_approval_gate_mientras_espera():
    """MON-35: una decision detenida en el gate de aprobacion debe ser
    observable con `stage="approval_gate"` y la linea exacta que espera
    (`await_resolution`): igual que `retrieve_context`, un await sin
    deadline en el gate seria invisible sin esta superficie."""
    tag = "battle-stage-gate-1"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(battle_tag=tag, available_moves=[move])
    player = _player(
        decision_graph=None,
        decision_budget_seconds=60,
        approval_policy=AlwaysGateApprovalPolicy(),
        approval_timeout_seconds=30.0,
        pending_repository=_FakePendingRepo(),
    )
    player.decision_graph = _OkGraph()

    try:
        with patch.object(client_module, "serialize_battle", _stub_serialize):
            pending = player.choose_move(battle)
            decision_future = _run_on_pokeloop(pending)
            await _await_on_pokeloop(player.frame_inbox.publish(tag, ("|upkeep",)))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if player._approval_registry.get(tag, 0) is not None:
                    break
                await asyncio.sleep(0.01)
            assert player._approval_registry.get(tag, 0) is not None, (
                "el gate nunca se abrio: la decision no llego a esperarlo"
            )

            snapshot = await player.decision_snapshot()
            assert snapshot, "decision_snapshot() debe listar la decision en vuelo"
            stage_entries = [
                e for e in snapshot if e["stage"] == "approval_gate"
            ]
            assert stage_entries, (
                "el snapshot debe incluir una entrada con etapa "
                "approval_gate; vistas: "
                + ", ".join(f"{e['stage']!r}" for e in snapshot)
            )
            awaited = [
                frame
                for entry in stage_entries
                for frame in entry["frames"]
                if frame["function"] == "await_resolution"
            ]
            assert awaited, (
                "el snapshot debe incluir el frame de await_resolution "
                "(la linea exacta que el gate espera); frames: "
                + ", ".join(
                    f"{frame['function']}:{frame['line']}"
                    for entry in snapshot
                    for frame in entry["frames"]
                )
            )
    finally:
        await player.drain_inflight_decisions()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wrap_future(decision_future)
