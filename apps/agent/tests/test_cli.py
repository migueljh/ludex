"""Tests unitarios de `cli.py`: sin red, sin postgres real.

Complementan `tests/integration/test_play.py` (que juega batallas reales) con
los dos casos de la review de merge que no necesitan un server para
demostrarse: I6 (empate) e I3 (perdida silenciosa de pasos).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ludex_agent import cli as cli_module
from ludex_agent.benchmark import BenchmarkDeadlineExceeded, BenchmarkResult, run_benchmark
from ludex_agent.graph.provider import FatalProviderError
from typer.testing import CliRunner

from ludex_agent.cli import (
    IncompleteTrajectoryError,
    _battle_against_or_failure,
    _battle_outcome,
    _benchmark_provider,
    _persist_one,
    _progress_summary,
    app,
)
from ludex_agent.graph.provider import DecisionMetrics
from ludex_agent.showdown.client import (
    LudexPlayer,
    PendingChoice,
    local_server_configuration,
)


def _player() -> LudexPlayer:
    from poke_env import AccountConfiguration

    # Sufijo aleatorio (mismo motivo que test_client.py::_player): un nombre
    # fijo choca con `|nametaken|` contra el server local si dos corridas de
    # la suite se solapan o se repiten en la misma sesion.
    sufijo = random.randint(1000, 9999)
    return LudexPlayer(
        account_configuration=AccountConfiguration(f"Foo{sufijo}", None),
        battle_format="gen6randombattle",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
    )


def test_cli_expone_benchmark_en_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "benchmark" in result.stdout
    assert "provider-smoke" in result.stdout


def test_benchmark_expone_registro_y_tabla_de_precios():
    result = CliRunner().invoke(app, ["benchmark", "--help"])
    assert result.exit_code == 0
    assert "--run-id" in result.stdout
    assert "--pricing" in result.stdout
    assert "--ledger" in result.stdout
    assert "--record" in result.stdout


def test_progreso_muestra_batallas_usage_y_costo_acumulado():
    record = SimpleNamespace(
        completed=3,
        requested=15,
        wins=1,
        losses=2,
        ties=0,
        metrics={
            "calls_total": 87,
            "input_tokens": 123_456,
            "output_tokens": 7_890,
        },
        total_cost=Decimal("0.1432"),
        pricing_currency="USD",
    )

    assert _progress_summary(record) == (
        "progress=3/15 w-l-t=1-2-0 calls=87 "
        "tokens=123456/7890 cost=USD 0.1432"
    )


def test_benchmark_imprime_progreso_antes_del_resultado_final(monkeypatch):
    async def fake_benchmark_command(*, on_progress, **kwargs):
        progress = BenchmarkResult(
            requested=2, completed=1, wins=1, losses=0, ties=0,
            provider="open_code_zen", model="mimo-v2.5-free",
        )
        metrics = {
            "turns_total": 3,
            "calls_total": 4,
            "input_tokens": 1000,
            "output_tokens": 200,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "turns_model_invalid": 0,
            "turns_fallback": 0,
            "turns_deadline_affected": 0,
            "key_rotations": 0,
        }
        await on_progress(progress, metrics)
        return (
            BenchmarkResult(
                requested=2, completed=2, wins=1, losses=1, ties=0,
                provider="open_code_zen", model="mimo-v2.5-free",
            ),
            metrics,
        )

    monkeypatch.setattr(
        "ludex_agent.cli._benchmark_command", fake_benchmark_command
    )
    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "2", "--opponent", "simple_heuristics",
            "--provider", "open_code_zen", "--model", "mimo-v2.5-free",
            "--no-record",
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "open_code_zen",
            "LUDEX_MODEL": "mimo-v2.5-free",
            "OPEN_CODE_ZEN_API_KEY": "fake-key",
            "OPEN_CODE_ZEN_BASE_URL": "https://opencode.ai/zen/v1",
        },
    )

    assert result.exit_code == 0
    assert "progress=1/2 w-l-t=1-0-0 calls=4" in result.stdout
    assert result.stdout.index("progress=1/2") < result.stdout.index(
        "completed=2/2"
    )


def test_provider_smoke_usa_flags_como_los_comandos_del_plan():
    result = CliRunner().invoke(app, ["provider-smoke", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.stdout
    assert "--model" in result.stdout


def test_provider_smoke_sanitiza_fallo_sin_traceback_ni_clave(monkeypatch):
    class FailingProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            raise FatalProviderError("provider rejected request")

    monkeypatch.setattr(
        "ludex_agent.cli._benchmark_provider",
        lambda *args, **kwargs: FailingProvider(),
    )
    result = CliRunner().invoke(
        app,
        ["provider-smoke", "--provider", "fake", "--model", "fake-model"],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "google",
            "LUDEX_MODEL": "fake",
            "GEMINI_API_KEY": "super-secret-key",
        },
    )

    assert result.exit_code == 1
    assert "ABORTED: FatalProviderError: provider rejected request" in result.stdout
    assert "Traceback" not in result.stdout
    assert "super-secret-key" not in result.stdout


def test_provider_smoke_sanitiza_respuesta_semanticamente_invalida(monkeypatch):
    class InvalidProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            return {"_invalid_response": "contenido privado del modelo"}

    monkeypatch.setattr(
        "ludex_agent.cli._benchmark_provider",
        lambda *args, **kwargs: InvalidProvider(),
    )
    result = CliRunner().invoke(
        app,
        ["provider-smoke", "--provider", "fake", "--model", "fake-model"],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "google",
            "LUDEX_MODEL": "fake",
            "GEMINI_API_KEY": "super-secret-key",
        },
    )

    assert result.exit_code == 1
    assert "ABORTED: invalid model response" in result.stdout
    assert "Traceback" not in result.stdout
    assert "contenido privado" not in result.stdout
    assert "super-secret-key" not in result.stdout


def test_benchmark_rechaza_modelo_sin_ruta_antes_de_llamarlo(monkeypatch):
    monkeypatch.setenv("OPEN_CODE_ZEN_API_KEY", "fake-key")
    with pytest.raises(ValueError, match="sin ruta"):
        _benchmark_provider(
            "open_code_zen", "modelo-inventado", 10, DecisionMetrics()
        )


class _FakeRepo:
    """Doble de `BattleRepository`: registra lo que se le pide grabar, sin
    tocar ninguna base."""

    def __init__(self) -> None:
        self.saved_steps: list[tuple] = []
        self.saved_step_kwargs: list[dict] = []
        self.saved_trajectories: list[tuple[tuple, dict]] = []
        self.finalized: tuple | None = None
        self.saved_battle_kwargs: dict | None = None

    async def save_battle(self, **kwargs):
        self.saved_battle_kwargs = kwargs
        return 1

    async def save_turn(self, *args, **kwargs) -> None:
        pass

    async def save_trajectory(self, *args, **kwargs) -> int:
        self.saved_trajectories.append((args, kwargs))
        return 1

    async def save_step(self, *args, **kwargs) -> None:
        self.saved_steps.append(args)
        self.saved_step_kwargs.append(kwargs)

    async def finalize(self, *args, **kwargs) -> None:
        self.finalized = (args, kwargs)


# --- I6: un empate no es una derrota ---
#
# `battle.won` de poke-env es `None` tanto para "no termino todavia" como
# para "empate" (`tied()` nunca setea `_won`). El codigo viejo
# (`battle.player_username if battle.won else battle.opponent_username`)
# colapsaba las dos situaciones: un empate quedaba grabado con el RIVAL como
# ganador, `final_result='loss'` y `reward=-1`. Estos tests fallan con esa
# expresion vieja (el rival apareceria como winner, "loss" en vez de "tie",
# -1 en vez de 0) y pasan con `_battle_outcome`.


def test_battle_outcome_victoria():
    battle = SimpleNamespace(
        won=True, player_username="Bot", opponent_username="Rival",
    )
    assert _battle_outcome(battle) == ("Bot", "win", 1.0)


def test_battle_outcome_derrota():
    battle = SimpleNamespace(
        won=False, player_username="Bot", opponent_username="Rival",
    )
    assert _battle_outcome(battle) == ("Rival", "loss", -1.0)


def test_battle_outcome_empate_no_es_una_derrota():
    """El caso que el bug original rompia: `won is None` con la batalla
    terminada solo puede significar empate, nunca "el rival gano"."""
    battle = SimpleNamespace(
        won=None, player_username="Bot", opponent_username="Rival",
    )
    winner, result, reward = _battle_outcome(battle)
    assert winner is None, "un empate no tiene ganador: no puede ser el rival"
    assert result == "tie"
    assert reward == 0.0


async def test_persist_one_graba_el_empate_sin_ganador_ni_reward_negativo():
    """Integra `_battle_outcome` con `_persist_one`: la fila de `battles`
    debe quedar con `winner=None` y `finalize` con `result='tie'`,
    `reward=0.0`, no con el rival como ganador."""
    player = _player()
    tag = "battle-empate-1"
    battle = SimpleNamespace(
        battle_tag=tag, player_role="p1",
        player_username="Bot", opponent_username="Rival",
        finished=True, won=None, gen=6,
    )
    player.battles[tag] = battle
    player.steps[tag] = []

    repo = _FakeRepo()
    await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert repo.saved_battle_kwargs["winner"] is None
    args, kwargs = repo.finalized
    assert kwargs.get("result") == "tie"
    assert kwargs.get("reward") == 0.0


# --- I3: un paso perdido tiene que dejar rastro, no perderse en silencio ---


async def test_persist_one_falla_antes_de_escribir_si_hay_un_slot_none():
    """Un slot perdido invalida la trayectoria completa antes de save_step."""
    player = _player()
    tag = "battle-x-1"
    battle = SimpleNamespace(
        battle_tag=tag, player_role="p1",
        player_username="Bot", opponent_username="Rival",
        finished=False, won=None, gen=6,
    )
    player.battles[tag] = battle
    player.steps[tag] = [None]

    repo = _FakeRepo()
    with pytest.raises(RuntimeError, match=rf"{tag}.*0"):
        await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert repo.saved_trajectories == []
    assert repo.saved_steps == []
    assert repo.finalized is None


async def test_persist_one_falla_antes_de_escribir_si_el_estado_es_none():
    player = _player()
    tag = "battle-x-2"
    battle = SimpleNamespace(
        battle_tag=tag, player_role="p1",
        player_username="Bot", opponent_username="Rival",
        finished=False, won=None, gen=6,
    )
    player.battles[tag] = battle
    player.steps[tag] = [{
        "turn": 1, "decision_turn": 1, "state": None,
        "action_taken": {"kind": "move", "id": "tackle"},
    }]

    repo = _FakeRepo()
    with pytest.raises(RuntimeError, match=rf"{tag}.*0"):
        await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert repo.saved_trajectories == []
    assert repo.saved_steps == []
    assert repo.finalized is None


async def test_persist_one_no_escribe_parcialmente_antes_de_un_slot_perdido():
    player = _player()
    tag = "battle-x-3"
    battle = SimpleNamespace(
        battle_tag=tag, player_role="p1",
        player_username="Bot", opponent_username="Rival",
        finished=True, won=True, gen=6,
    )
    player.battles[tag] = battle
    player.steps[tag] = [
        {
            "turn": 1, "decision_turn": 1,
            "state": {"legal_actions": [{"kind": "move", "id": "tackle"}]},
            "action_taken": {"kind": "move", "id": "tackle"},
        },
        None,
    ]

    repo = _FakeRepo()
    with pytest.raises(RuntimeError, match=rf"{tag}.*1"):
        await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert repo.saved_trajectories == []
    assert repo.saved_steps == []
    assert repo.finalized is None


async def test_persist_one_reporta_indice_y_fase_de_un_rechazo_pendiente():
    player = _player()
    tag = "battle-rejected-pending"
    player.battles[tag] = SimpleNamespace(
        battle_tag=tag,
        player_role="p1",
        player_username="Bot",
        opponent_username="Rival",
        finished=True,
        won=True,
        gen=6,
    )
    player.steps[tag] = [None]
    player._pending_choices[tag] = PendingChoice(
        decision_index=0,
        attempt_index=1,
        phase="rejected",
        request_rqid=6,
        request_frame_seq=20,
        step_index=0,
        step=None,
    )
    repo = _FakeRepo()

    with pytest.raises(
        IncompleteTrajectoryError,
        match=rf"{tag}.*decision_index=0.*phase=rejected",
    ):
        await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert repo.saved_trajectories == []
    assert repo.saved_steps == []
    assert repo.finalized is None


async def test_persist_one_separa_action_path_de_action_source():
    player = _player()
    tag = "battle-path-1"
    player.battles[tag] = SimpleNamespace(
        battle_tag=tag, player_role="p1", player_username="Bot",
        opponent_username="Rival", finished=False, won=None, gen=6,
    )
    player.steps[tag] = [{
        "turn": 1, "decision_turn": 1,
        "state": {"legal_actions": [{"kind": "move", "id": "tackle"}]},
        "action_taken": {"kind": "move", "id": "tackle"},
        "action_path": "llm_retry",
    }]
    repo = _FakeRepo()

    await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert repo.saved_steps[0][-1] == "agent"
    assert repo.saved_step_kwargs[0] == {"action_path": "llm_retry"}


def _patch_play_dependencies(monkeypatch, agent_type) -> None:
    """Aísla `play` de red y Postgres sin reemplazar su control de tareas."""

    class FakeRival:
        def __init__(self, **kwargs) -> None:
            pass

    class FakeEngine:
        async def dispose(self):
            pass

    async def reachable(url):
        pass

    monkeypatch.setattr(
        cli_module,
        "load_settings",
        lambda: SimpleNamespace(
            showdown_ws_url="ws://localhost:8100/showdown/websocket",
            bot_username="Bot",
            database_url="postgresql+asyncpg://x:x@localhost:15432/x",
        ),
    )
    monkeypatch.setattr(cli_module, "_check_showdown_reachable", reachable)
    monkeypatch.setattr(cli_module, "LudexPlayer", agent_type)
    monkeypatch.setattr(cli_module, "RandomPlayer", FakeRival)
    monkeypatch.setattr(cli_module, "make_engine", lambda url: FakeEngine())
    monkeypatch.setattr(cli_module, "BattleRepository", lambda factory: object())
    monkeypatch.setattr(cli_module, "session_factory", lambda engine: object())


async def test_play_propaga_el_fallo_background_sin_esperar_timeout(monkeypatch):
    failure = RuntimeError("choice protocol fatal")

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.battles = {}

        async def battle_against(self, rival, n_battles=1):
            await asyncio.Event().wait()

        async def wait_for_background_failure(self):
            return failure

    _patch_play_dependencies(monkeypatch, FakeAgent)

    with pytest.raises(RuntimeError, match="choice protocol fatal") as caught:
        await asyncio.wait_for(
            cli_module.play(1, "gen6randombattle", source="test"), timeout=0.2
        )

    assert caught.value is failure


async def test_battle_helper_cancela_hijas_ante_timeout_externo():
    """Rompe si el helper sale sin cancelar y esperar ambas tareas hijas."""
    child_tasks: list[asyncio.Task] = []
    battle_cancelled = asyncio.Event()
    failure_cancelled = asyncio.Event()

    class FakeAgent:
        async def battle_against(self, rival, n_battles=1):
            child_tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                battle_cancelled.set()
                raise

        async def wait_for_background_failure(self):
            child_tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                failure_cancelled.set()
                raise

    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await _battle_against_or_failure(FakeAgent(), object())
        await asyncio.sleep(0)

        assert battle_cancelled.is_set()
        assert failure_cancelled.is_set()
        assert len(child_tasks) == 2
        assert all(task.done() for task in child_tasks)
        assert not any(task in asyncio.all_tasks() for task in child_tasks)
    finally:
        for task in child_tasks:
            task.cancel()
        await asyncio.gather(*child_tasks, return_exceptions=True)


async def test_play_propaga_timeouterror_del_canal_background(monkeypatch):
    """Un timeout del websocket no es el deadline silencioso de la batalla."""
    failure = TimeoutError("websocket send timed out")
    child_tasks: list[asyncio.Task] = []

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.battles = {}

        async def battle_against(self, rival, n_battles=1):
            child_tasks.append(asyncio.current_task())
            await asyncio.Event().wait()

        async def wait_for_background_failure(self):
            return failure

    _patch_play_dependencies(monkeypatch, FakeAgent)

    with pytest.raises(TimeoutError, match="websocket send timed out") as caught:
        await cli_module.play(1, "gen6randombattle", source="test")

    assert caught.value is failure
    assert all(task.done() for task in child_tasks)


async def test_play_deadline_real_retorna_vacio_y_limpia_hijas(monkeypatch):
    """El deadline propio conserva el contrato [] sin dejar coroutines vivas."""
    child_tasks: list[asyncio.Task] = []
    battle_cancelled = asyncio.Event()
    failure_cancelled = asyncio.Event()

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.battles = {}

        async def battle_against(self, rival, n_battles=1):
            child_tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                battle_cancelled.set()
                raise

        async def wait_for_background_failure(self):
            child_tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                failure_cancelled.set()
                raise

    _patch_play_dependencies(monkeypatch, FakeAgent)
    monkeypatch.setattr(cli_module, "BATTLE_TIMEOUT_SECONDS", 0.01)

    try:
        tags = await asyncio.wait_for(
            cli_module.play(1, "gen6randombattle", source="test"), timeout=0.2
        )
        await asyncio.sleep(0)

        assert tags == []
        assert battle_cancelled.is_set()
        assert failure_cancelled.is_set()
        assert len(child_tasks) == 2
        assert all(task.done() for task in child_tasks)
        assert not any(task in asyncio.all_tasks() for task in child_tasks)
    finally:
        for task in child_tasks:
            task.cancel()
        await asyncio.gather(*child_tasks, return_exceptions=True)


def _patch_benchmark_command_dependencies(monkeypatch, agent_type) -> None:
    """Aísla `_benchmark_command` de red y Postgres manteniendo `run_benchmark`."""

    class FakeRival:
        def __init__(self, **kwargs) -> None:
            pass

    class FakeProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            return {"action": {"kind": "move", "id": "tackle"}}

    class FakeCalcClient:
        async def aclose(self):
            pass

    class FakeContextRepo:
        async def aclose(self):
            pass

    class FakeEngine:
        async def dispose(self):
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
            bot_username="Bot",
            showdown_battle_format="gen6randombattle",
        ),
    )
    monkeypatch.setattr(
        cli_module, "_check_showdown_reachable", reachable
    )
    monkeypatch.setattr(
        cli_module, "local_server_configuration", lambda url: object()
    )
    monkeypatch.setattr(
        cli_module, "_benchmark_provider", lambda *a, **k: FakeProvider()
    )
    monkeypatch.setattr(
        cli_module, "CalcClient", lambda *a, **k: FakeCalcClient()
    )
    monkeypatch.setattr(
        cli_module,
        "PostgresContextRepository",
        lambda *a, **k: FakeContextRepo(),
    )
    monkeypatch.setattr(
        cli_module, "build_decision_graph", lambda *a, **k: object()
    )
    monkeypatch.setattr(cli_module, "LudexPlayer", agent_type)
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


def test_benchmark_command_deadline_clasificado_y_escribe_final(
    monkeypatch, tmp_path
):
    """El CLI pasa `BATTLE_TIMEOUT_SECONDS`, clasifica el deadline y escribe
    snapshot/ledger final con progreso acumulado."""

    class SlowAgent:
        def __init__(self, **kwargs) -> None:
            self.n_won_battles = 0
            self.n_lost_battles = 0
            self.n_tied_battles = 0
            self.battles = {}

        async def battle_against(self, rival, n_battles=1):
            # Una batalla rapida, luego cuelga para que salte el deadline.
            if len(self.battles) >= 1:
                await asyncio.Event().wait()
            self.battles[f"battle-{len(self.battles)}"] = object()
            self.n_won_battles += 1

        async def wait_for_background_failure(self):
            await asyncio.Event().wait()

    _patch_benchmark_command_dependencies(monkeypatch, SlowAgent)
    monkeypatch.setattr(cli_module, "BATTLE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    real_run_benchmark = run_benchmark
    timeout_values: list[float | None] = []

    async def spy_run_benchmark(*args, timeout=None, **kwargs):
        expected = cli_module.BATTLE_TIMEOUT_SECONDS
        timeout_values.append(timeout)
        assert timeout == expected, (
            f"timeout={timeout!r} != BATTLE_TIMEOUT_SECONDS={expected!r}"
        )
        return await real_run_benchmark(*args, timeout=timeout, **kwargs)

    monkeypatch.setattr(cli_module, "run_benchmark", spy_run_benchmark)

    ledger_path = tmp_path / "ledger.md"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "--n", "3",
            "--opponent", "random",
            "--provider", "fake",
            "--model", "fake-model",
            "--run-id", "test-deadline",
            "--ledger", str(ledger_path),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "fake",
            "LUDEX_MODEL": "fake-model",
        },
    )

    assert result.exit_code == 1
    assert "ABORTED: BenchmarkDeadlineExceeded" in result.stdout
    assert "completed=1/3" in result.stdout
    assert timeout_values == [0.01], f"timeout_values={timeout_values}"

    artifact = tmp_path / "test-deadline.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text())
    assert data["status"] == "aborted"
    assert data["requested"] == 3
    assert data["completed"] == 1
    assert data["failure"].startswith("BenchmarkDeadlineExceeded")

    assert ledger_path.exists()
    ledger_text = ledger_path.read_text()
    assert "test-deadline" in ledger_text
    assert "1/3" in ledger_text
    assert "1-0-0" in ledger_text
