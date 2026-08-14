"""MON-20 DIAG-A ronda 2: monitor diagnostico productivo opt-in del benchmark.

La ronda 1 (c143e0b) introdujo `LudexPlayer.decision_snapshot()` pero el
unico caller era el test: `_benchmark_command()` espera completamente a
`run_benchmark()`, asi que durante un cuelgue no existe un monitor vivo que
capture la etapa. Esta ronda agrega el wiring productivo: un flag opt-in
`--diagnostic-snapshot-interval` que agenda desde el caller loop un monitor
periodico que emite `decision_snapshot()` sanitizado, con lifecycle seguro.

Canarios (cero infraestructura: POKE_LOOP real, agent LudexPlayer real,
grafo real con un context repository que se cuelga, sin Docker ni red):

1. `test_flag_diagnostico_se_propaga_del_cli_al_benchmark` — el flag del
   comando typer llega a `_benchmark_command`; sin flag, desactivado.
   Falla si se elimina el wiring CLI.
2. `test_monitor_productivo_emite_snapshot_util_de_decision_colgada` — el
   wiring productivo real de `_benchmark_command` (agent LudexPlayer real,
   grafo real con on_stage, run_benchmark real) emite al menos un snapshot
   util de una decision real colgada en un nodo del POKE_LOOP antes de
   liberar el bloqueo. Falla si se elimina el monitor o el `on_stage`.
"""

import asyncio
import random
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from poke_env.concurrency import POKE_LOOP
from typer.testing import CliRunner

from ludex_agent import cli as cli_module
from ludex_agent.benchmark import BenchmarkResult
from ludex_agent.cli import _benchmark_command, app
from ludex_agent.graph.provider import CompletionEnvelope, CompletionUsage
from ludex_agent.showdown import client as client_module
from ludex_agent.showdown.client import LudexPlayer


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
    """Snapshot minimo que atraviesa `parse_state` y `retrieve_context`."""
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
    """Agenda `coro` en `POKE_LOOP` (el loop real de poke-env, su propio
    thread) y devuelve el `concurrent.futures.Future`."""
    return asyncio.run_coroutine_threadsafe(coro, POKE_LOOP)


async def _await_on_pokeloop(coro) -> None:
    await asyncio.wrap_future(_run_on_pokeloop(coro))


class _StuckContextRepository:
    """Simula el await SQL sin deadline de `PostgresContextRepository`:
    `load_battle_context` se queda esperando una senal que el test libera.

    El `asyncio.Event` se crea DENTRO del metodo, que corre en POKE_LOOP;
    `release()` agenda el `set()` en POKE_LOOP (thread-safe).
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self._gate: asyncio.Event | None = None

    async def load_battle_context(self, **kwargs) -> dict:
        self.started.set()
        if self._gate is None:
            self._gate = asyncio.Event()
        await self._gate.wait()
        # El test libera el bloqueo: la decision completa por el camino
        # real (context -> calc -> decide -> execute) con sides vacios.
        return {
            "generation": {"gen_number": 6, "label": "gen6"},
            "own": [],
            "opponent": [],
        }

    async def load_moves(self, **kwargs) -> dict:
        return {}

    async def load_mega_forms(self, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass

    def release(self) -> None:
        if self._gate is not None:
            asyncio.run_coroutine_threadsafe(self._set_gate(), POKE_LOOP)

    async def _set_gate(self) -> None:
        self._gate.set()


class _FakeEnvelopeProvider:
    """Reemplaza a `_benchmark_provider`: una completion estructurada valida
    para que `decide` complete el camino cuando el test libera el bloqueo."""

    async def complete(self, prompt, *, deadline, turn_id):
        return CompletionEnvelope(
            payload={
                "action": {"kind": "move", "id": "tackle"},
                "rationale": "forced for the canary",
                "confidence": 0.5,
                "alternatives": [],
            },
            provider="fake",
            model="fake-model",
            usage=CompletionUsage(input_tokens=1, output_tokens=1),
            latency_ms=0.0,
        )


def _patch_benchmark_infra(monkeypatch, agent_factory, stuck_repo) -> None:
    """Aisla `_benchmark_command` de red y Postgres manteniendo el wiring
    REAL: `build_decision_graph` (con on_stage) y `run_benchmark` NO se
    parchean; el agente es un LudexPlayer real."""

    class FakeRival:
        def __init__(self, **kwargs) -> None:
            pass

    class FakeCalcClient:
        async def aclose(self) -> None:
            pass

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    async def reachable(url):
        pass

    monkeypatch.setattr(
        cli_module,
        "load_settings",
        lambda: SimpleNamespace(
            showdown_ws_url="ws://localhost:8100/showdown/websocket",
            database_url="postgresql+asyncpg://x:x@localhost:15432/x",
            llm_provider="fake",
            llm_model="fake-model",
            llm_request_timeout_seconds=10,
            decision_budget_seconds=10,
            battle_timeout_seconds=1.5,
            bot_username="Bot",
            showdown_battle_format="gen6randombattle",
        ),
    )
    monkeypatch.setattr(cli_module, "_check_showdown_reachable", reachable)
    monkeypatch.setattr(
        cli_module, "local_server_configuration", lambda url: object()
    )
    monkeypatch.setattr(
        cli_module, "_benchmark_provider", lambda *a, **k: _FakeEnvelopeProvider()
    )
    monkeypatch.setattr(
        cli_module, "CalcClient", lambda *a, **k: FakeCalcClient()
    )
    monkeypatch.setattr(
        cli_module, "PostgresContextRepository", lambda *a, **k: stuck_repo
    )
    monkeypatch.setattr(cli_module, "LudexPlayer", agent_factory)
    monkeypatch.setattr(cli_module, "RandomPlayer", FakeRival)
    monkeypatch.setattr(cli_module, "MaxBasePowerPlayer", FakeRival)
    monkeypatch.setattr(cli_module, "SimpleHeuristicsPlayer", FakeRival)
    monkeypatch.setattr(cli_module, "make_engine", lambda url: FakeEngine())
    monkeypatch.setattr(
        cli_module, "session_factory", lambda engine: object()
    )
    monkeypatch.setattr(
        cli_module, "BattleRepository", lambda factory: object()
    )


def test_flag_diagnostico_se_propaga_del_cli_al_benchmark(monkeypatch, tmp_path):
    """El flag `--diagnostic-snapshot-interval` del comando `benchmark`
    llega a `_benchmark_command`; sin flag, el default es None (desactivado:
    un benchmark normal no crea monitor ni cambia su salida)."""
    captured: dict = {}

    async def spy_benchmark_command(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return (
            BenchmarkResult(
                requested=1, completed=1, wins=0, losses=1, ties=0,
            ),
            {},
        )

    monkeypatch.setattr(
        cli_module,
        "load_settings",
        lambda: SimpleNamespace(
            showdown_ws_url="ws://localhost:8100/showdown/websocket",
            database_url="postgresql+asyncpg://x:x@localhost:15432/x",
            llm_provider="fake",
            llm_model="fake-model",
            llm_request_timeout_seconds=10,
            decision_budget_seconds=10,
            battle_timeout_seconds=0.01,
            bot_username="Bot",
            showdown_battle_format="gen6randombattle",
        ),
    )
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)
    # `ledger`/`pricing` son defaults de typer resueltos en import-time: el
    # monkeypatch de DEFAULT_LEDGER_PATH no alcanza; se pasan explicitos
    # para que el canario nunca escriba en docs/BENCHMARKS.md real.
    ledger_path = tmp_path / "ledger.md"
    monkeypatch.setattr(cli_module, "_benchmark_command", spy_benchmark_command)

    result = CliRunner().invoke(app, [
        "benchmark",
        "--provider", "fake",
        "--model", "fake-model",
        "--run-id", "diag-flag-on",
        "--ledger", str(ledger_path),
        "--diagnostic-snapshot-interval", "0.25",
    ])
    assert result.exit_code == 0, result.stdout
    assert captured.get("diagnostic_snapshot_interval") == 0.25, (
        "el flag --diagnostic-snapshot-interval debe propagarse a "
        f"_benchmark_command; recibido {captured.get('diagnostic_snapshot_interval')!r}"
    )

    captured.clear()
    result = CliRunner().invoke(app, [
        "benchmark",
        "--provider", "fake",
        "--model", "fake-model",
        "--run-id", "diag-flag-off",
        "--ledger", str(ledger_path),
    ])
    assert result.exit_code == 0, result.stdout
    assert captured.get("diagnostic_snapshot_interval") is None, (
        "sin flag, el monitor debe quedar desactivado (default None)"
    )


@pytest.mark.asyncio
async def test_monitor_productivo_emite_snapshot_util_de_decision_colgada(
    monkeypatch, tmp_path
):
    """El wiring productivo real emite al menos un snapshot util.

    `_benchmark_command` real (monitor + on_stage + agent LudexPlayer real
    + grafo real + run_benchmark real) corre en vuelo; una decision REAL se
    cuelga en el nodo `retrieve_context` del POKE_LOOP; el monitor debe
    emitir un snapshot con stage `retrieve_context` y el frame de
    `load_battle_context` ANTES de que el test libere el bloqueo. El
    monitor se cancela y espera al terminar, y un fallo de observabilidad
    (emit que lanza en la primera llamada) no altera la semantica del
    benchmark (mismo `failure_type`).
    """
    stuck = _StuckContextRepository()
    captured: list[LudexPlayer] = []
    created = threading.Event()
    emitted: list[list[dict]] = []
    emitted_event = threading.Event()
    emit_failures: list[str] = []

    def flaky_emit(snapshot):
        # Un fallo de observabilidad (primera emision) no debe alterar la
        # semantica del benchmark: el monitor lo captura y sigue.
        if not emit_failures:
            emit_failures.append("boom")
            raise RuntimeError("emit observability failure")
        emitted.append(snapshot)
        if any(
            entry.get("stage") == "retrieve_context"
            and any(
                frame.get("function") == "load_battle_context"
                for frame in entry.get("frames", [])
            )
            for entry in snapshot
        ):
            emitted_event.set()

    def agent_factory(**kwargs):
        agent = LudexPlayer(**kwargs)

        async def hanging_battle(rival, n_battles=1):
            # Sin server de Showdown (offline): la batalla queda en vuelo
            # hasta el battle timeout, como el escenario real de un cuelgue.
            await asyncio.Event().wait()

        agent.battle_against = hanging_battle
        captured.append(agent)
        created.set()
        return agent

    _patch_benchmark_infra(monkeypatch, agent_factory, stuck)

    bench_task = asyncio.create_task(_benchmark_command(
        n=1, opponent="random", concurrency=1, persist=False,
        provider_name="fake", model="fake-model",
        fmt="gen6randombattle", battle_timeout_seconds=1.5,
        diagnostic_snapshot_interval=0.02,
        snapshot_emit=flaky_emit,
    ))
    try:
        # `to_thread`: un `Event.wait` bloqueante congelaria el loop del
        # test y con el la task de `_benchmark_command` que esperamos.
        assert await asyncio.to_thread(created.wait, 5), (
            "el benchmark nunca creo el agente"
        )
        agent = captured[0]
        tag = "battle-diag-monitor-1"
        move = SimpleNamespace(id="tackle")
        battle = _fake_battle(battle_tag=tag, available_moves=[move])
        with patch.object(client_module, "serialize_battle", _stub_serialize):
            pending = agent.choose_move(battle)
            # Topologia real: la decision corre como task en POKE_LOOP.
            decision_future = _run_on_pokeloop(pending)
            await _await_on_pokeloop(
                agent.frame_inbox.publish(tag, ("|upkeep",))
            )
            assert await asyncio.to_thread(stuck.started.wait, 2), (
                "la decision nunca llego a retrieve_context"
            )
            assert await asyncio.to_thread(emitted_event.wait, 5), (
                "el monitor productivo nunca emitio un snapshot util "
                "(stage=retrieve_context + frame load_battle_context) "
                "antes de liberar el bloqueo"
            )
            # Liberar el bloqueo: la decision completa limpiamente.
            stuck.release()
            await asyncio.wait_for(
                asyncio.wrap_future(decision_future), timeout=5
            )
        result, metrics = await asyncio.wait_for(bench_task, timeout=15)
    finally:
        if not bench_task.done():
            bench_task.cancel()
            await asyncio.gather(bench_task, return_exceptions=True)

    assert result.failure_type == "BenchmarkDeadlineExceeded", (
        "el monitor no debe alterar la semantica del benchmark; "
        f"failure_type={result.failure_type!r}"
    )
    assert emit_failures == ["boom"], (
        "el fallo de observabilidad de la primera emision debe haberse "
        "producido una vez y haberse tolerado"
    )
    assert not any(
        task.get_name() == "ludex-snapshot-monitor"
        for task in asyncio.all_tasks()
    ), "el monitor debe estar cancelado y esperado al terminar el benchmark"
