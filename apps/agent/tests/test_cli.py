"""Tests unitarios de `cli.py`: sin red, sin postgres real.

Complementan `tests/integration/test_play.py` (que juega batallas reales) con
los dos casos de la review de merge que no necesitan un server para
demostrarse: I6 (empate) e I3 (perdida silenciosa de pasos).
"""

from __future__ import annotations

import logging
import random
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ludex_agent.cli import (
    _battle_outcome,
    _benchmark_provider,
    _persist_one,
    app,
)
from ludex_agent.graph.provider import DecisionMetrics
from ludex_agent.showdown.client import LudexPlayer, local_server_configuration


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
        self.finalized: tuple | None = None
        self.saved_battle_kwargs: dict | None = None

    async def save_battle(self, **kwargs):
        self.saved_battle_kwargs = kwargs
        return 1

    async def save_turn(self, *args, **kwargs) -> None:
        pass

    async def save_trajectory(self, *args, **kwargs) -> int:
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


async def test_persist_one_loguea_y_cuenta_un_paso_none(caplog):
    """Un paso `None` en `agent.steps[tag]` (hoy inalcanzable, guarda
    defensiva) ya no se descarta en silencio: se loguea en WARNING y se
    cuenta en `agent.lost_step_count`."""
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
    with caplog.at_level(logging.WARNING):
        await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert repo.saved_steps == []
    assert any("se pierde del dataset" in r.message for r in caplog.records)


async def test_persist_one_loguea_y_cuenta_un_paso_sin_materializar(caplog):
    """Mismo camino para el otro caso posible: el paso no es `None` pero su
    `state` quedo en `None` (guarda de `wait_for_pending_steps`). Antes de
    este chequeo, esto revienta con un TypeError al leer
    `step["state"]["legal_actions"]` en vez de dejar rastro."""
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
    with caplog.at_level(logging.WARNING):
        await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert repo.saved_steps == []


async def test_persist_one_no_pierde_pasos_validos_junto_a_uno_perdido():
    """El contador no debe inflar por pasos que SI se guardan: solo el que
    se pierde incrementa `lost_step_count`, y el valido se persiste igual."""
    player = _player()
    tag = "battle-x-3"
    battle = SimpleNamespace(
        battle_tag=tag, player_role="p1",
        player_username="Bot", opponent_username="Rival",
        finished=False, won=None, gen=6,
    )
    player.battles[tag] = battle
    player.steps[tag] = [
        None,
        {
            "turn": 1, "decision_turn": 1,
            "state": {"legal_actions": [{"kind": "move", "id": "tackle"}]},
            "action_taken": {"kind": "move", "id": "tackle"},
        },
    ]

    repo = _FakeRepo()
    await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert player.lost_step_count == 1
    assert len(repo.saved_steps) == 1


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
