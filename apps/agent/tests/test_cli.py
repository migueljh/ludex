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
from poke_env import AccountConfiguration
from ludex_agent import cli as cli_module
from ludex_agent.benchmark import (
    BenchmarkDeadlineExceeded,
    BenchmarkFailure,
    BenchmarkResult,
    ShowdownUnavailableError,
    run_benchmark,
)
from ludex_agent.graph.provider import FatalProviderError, TransientProviderError
from typer.testing import CliRunner

from ludex_agent.cli import (
    DEFAULT_RUNS_PATH,
    IncompleteTrajectoryError,
    _atomic_write_json,
    _battle_against_or_failure,
    _battle_outcome,
    _benchmark_provider,
    _check_showdown_reachable,
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


def test_provider_smoke_sin_credenciales_emite_not_run_y_no_traceback(monkeypatch):
    # El env de CliRunner se FUSIONA con el ambiente real: para ejercer el
    # path "sin credenciales" de forma determinista hay que remover las
    # variables de credenciales, no solo no pasarlas.
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEYS", raising=False)
    result = CliRunner().invoke(
        app,
        ["provider-smoke", "--provider", "kimi", "--model", "kimi-k2.6"],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "kimi",
            "LUDEX_MODEL": "kimi-k2.6",
        },
    )
    assert result.exit_code == 2
    assert "NOT RUN: credential unavailable" in result.stdout
    assert "Traceback" not in result.stdout


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
    from ludex_agent.graph.provider import (
        CompletionEnvelope,
        CompletionUsage,
    )

    class InvalidProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            # El contrato F2-08: complete devuelve un envelope; el payload
            # con shape invalido se rechaza en la validacion semantica.
            return CompletionEnvelope(
                payload={"_invalid_response": "contenido privado del modelo"},
                provider="fake", model="fake-model",
                usage=CompletionUsage(input_tokens=0, output_tokens=0),
                latency_ms=0.0,
            )

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


def test_benchmark_sin_credenciales_emite_not_run_y_no_publica_winrate(monkeypatch, tmp_path):
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", runs_dir)
    # CliRunner fusiona su env con el ambiente real: remover credenciales
    # para ejercer el path "sin credenciales" de forma determinista.
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEYS", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "5", "--opponent", "simple_heuristics",
            "--provider", "kimi", "--model", "kimi-k2.6",
            "--run-id", "test-kimi-not-run",
            "--ledger", str(tmp_path / "ledger.md"),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "kimi",
            "LUDEX_MODEL": "kimi-k2.6",
        },
    )
    assert result.exit_code == 2
    assert "NOT RUN: credential unavailable" in result.stdout
    artifact = runs_dir / "test-kimi-not-run.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text())
    assert data["status"] == "not-run"
    assert data["completed"] == 0
    assert data["win_rate"] is None
    assert data["wilson95"] is None
    assert "NOT RUN" in data["failure"]
    # L-01 (R2): sin muestras no hay latencia comparable: null, nunca 0.
    assert data["completion_latency_ms_total"] is None
    assert data["completion_latency_ms_p50"] is None
    assert data["completion_latency_ms_p95"] is None
    assert data["completion_latency_ms_max"] is None
    assert data["decision_latency_ms_total"] is None
    assert data["decision_latency_ms_p50"] is None
    assert data["decision_latency_ms_p95"] is None
    assert data["decision_latency_ms_max"] is None
    ledger_text = (tmp_path / "ledger.md").read_text()
    assert "0/0/0" not in ledger_text


def test_benchmark_rechaza_modelo_sin_ruta_antes_de_llamarlo(monkeypatch):
    monkeypatch.setenv("OPEN_CODE_ZEN_API_KEY", "fake-key")
    with pytest.raises(ValueError, match="sin ruta"):
        _benchmark_provider(
            "open_code_zen", "modelo-inventado", 10, DecisionMetrics()
        )


# --- L-01 (MON-14 R2): model-set usa la frontera unica de validacion -------


class _FakeModelRepo:
    """Doble del ModelRepository para model-set: replica la frontera
    fail-closed (provider/model habilitados) y registra lo que se pide
    fijar. `provider()`/`list_models()` existen solo para que una regresion
    a la validacion vieja (existencia sin enabled) quede al descubierto: el
    CLI llamaria `set_active` sobre un modelo disabled y el test se pone
    rojo."""

    def __init__(self, factory):
        self.active = ("google", "gemini-2.5-flash")
        self.enabled_models = {
            ("google", "gemini-2.5-flash"): True,
            ("google", "gemini-2.5-pro"): False,
        }
        self.enabled_providers = {"google": True, "off-provider": False}

    async def validate_selection(self, provider, model):
        from ludex_agent.db.model_repository import ModelSelectionError

        if not self.enabled_providers.get(provider):
            raise ModelSelectionError(
                f"provider {provider!r} no existe o esta deshabilitado en la DB"
            )
        if not self.enabled_models.get((provider, model)):
            raise ModelSelectionError(
                f"model {model!r} no existe o esta deshabilitado para "
                f"{provider!r} en la DB"
            )

    async def set_active(self, provider, model):
        self.active = (provider, model)

    async def provider(self, name):
        from types import SimpleNamespace

        return SimpleNamespace(
            name=name, enabled=self.enabled_providers.get(name, False)
        )

    async def list_models(self, provider_name=None):
        from types import SimpleNamespace

        return [
            SimpleNamespace(provider_name=p, model_id=m)
            for (p, m) in self.enabled_models
        ]


def _invoke_model_set(monkeypatch, repo, *args):
    from ludex_agent import cli as cli_module

    monkeypatch.setattr(cli_module, "ModelRepository", lambda factory: repo)
    runner = CliRunner()
    return runner.invoke(
        app, ["model-set", "--provider", args[0], "--model", args[1]],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
        },
    )


def test_model_set_rechaza_modelo_disabled_sin_tocar_settings(monkeypatch):
    """L-01: model-set no puede fijar un modelo deshabilitado; el valor
    anterior de la seleccion queda intacto (set_active nunca se llama)."""
    repo = _FakeModelRepo(None)
    result = _invoke_model_set(monkeypatch, repo, "google", "gemini-2.5-pro")

    assert result.exit_code != 0
    assert "deshabilitado" in result.output
    assert repo.active == ("google", "gemini-2.5-flash"), (
        "el rechazo no puede alterar la seleccion anterior"
    )


def test_model_set_rechaza_provider_disabled(monkeypatch):
    repo = _FakeModelRepo(None)
    result = _invoke_model_set(monkeypatch, repo, "off-provider", "cualquiera")

    assert result.exit_code != 0
    assert "deshabilitado" in result.output
    assert repo.active == ("google", "gemini-2.5-flash")


def test_model_set_rechaza_provider_y_modelo_inexistentes(monkeypatch):
    repo = _FakeModelRepo(None)

    r1 = _invoke_model_set(monkeypatch, repo, "no-existe", "modelo-x")
    assert r1.exit_code != 0
    assert "no existe" in r1.output
    assert repo.active == ("google", "gemini-2.5-flash")

    r2 = _invoke_model_set(monkeypatch, repo, "google", "modelo-no-seedeado")
    assert r2.exit_code != 0
    assert "no existe" in r2.output
    assert repo.active == ("google", "gemini-2.5-flash")


def test_model_set_valido_fija_la_seleccion(monkeypatch):
    repo = _FakeModelRepo(None)
    result = _invoke_model_set(monkeypatch, repo, "google", "gemini-2.5-flash")

    assert result.exit_code == 0
    assert "activo=google/gemini-2.5-flash" in result.output
    assert repo.active == ("google", "gemini-2.5-flash")


def _record_valid_opening(player: LudexPlayer, tag: str) -> None:
    """Alimenta al recorder REAL del player con una apertura publica valida.

    MON-10/F2-03: `_persist_one` ahora calcula `identity_key` con
    `compute_opening_identity` a partir de `recorders[tag].lines_for_turn(0)`
    ANTES de llegar a nada de lo que estos tests ejercen (empate, slot
    perdido, action_path). Sin una apertura valida, `_persist_one` fallaria
    con `OpeningIdentityError` antes de que el test pueda verificar lo que le
    importa. Ninguno de estos tests ejerce la identidad en si; solo necesitan
    que exista.
    """
    lines = [
        f">{tag}", "|init|battle", "|t:|1785186819", "|gametype|singles",
        "|player|p1|Bot|101|", "|player|p2|Rival|102|",
        "|teamsize|p1|6", "|teamsize|p2|6",
        "|gen|6", "|tier|[Gen 6] Random Battle",
        "|rule|HP Percentage Mod: HP is shown in percentages",
        "|start",
        "|switch|p1a: Furret|Furret, L93, F|309/309",
        "|switch|p2a: Rival|Rival, L88, M|100/100",
    ]
    player.recorders[tag].record([line.split("|") for line in lines])


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


# --- D49 (MON-25): `_check_showdown_reachable` debe hablar el protocolo
# de Showdown, no solo abrir un socket TCP. El defecto real (MON-25
# ROOT-CAUSE CHECKPOINT, seccion 7): un TCP connect desnudo da preflight
# VERDE contra cualquier listener que acepte la conexion, hable o no el
# protocolo. Estos dos tests fallan con la implementacion vieja (TCP
# connect + close) porque esa version nunca lee del socket.


async def test_check_showdown_reachable_falla_contra_listener_tcp_mudo():
    """Listener que acepta la conexion y jamas completa el handshake HTTP
    de websocket (nunca responde). El viejo TCP-connect-y-cerrar lo
    aceptaba como sano; el handshake real debe fallar cerrado."""

    async def _mute_handler(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(_mute_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ShowdownUnavailableError) as excinfo:
            await _check_showdown_reachable(
                f"ws://127.0.0.1:{port}/showdown/websocket"
            )
        assert isinstance(excinfo.value.__cause__, OSError)
    finally:
        # Sin `await wait_closed()`: `_mute_handler` nunca retorna (es el
        # punto del test), y desde Python 3.12 `wait_closed()` espera
        # tambien a que las conexiones activas terminen, no solo al socket
        # de escucha -- colgaria para siempre.
        server.close()


async def test_check_showdown_reachable_falla_contra_endpoint_http_invalido():
    """Un endpoint que responde HTTP valido pero rechaza el upgrade a
    websocket (no es un server de Showdown). Debe fallar, no solo por
    aceptar la conexion TCP."""

    async def _http_ok_handler(reader, writer):
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            b"Content-Type: text/plain\r\n\r\nhi"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_http_ok_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ShowdownUnavailableError) as excinfo:
            await _check_showdown_reachable(
                f"ws://127.0.0.1:{port}/showdown/websocket"
            )
        from websockets.exceptions import WebSocketException

        assert isinstance(excinfo.value.__cause__, WebSocketException)
    finally:
        server.close()
        await server.wait_closed()


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
    _record_valid_opening(player, tag)

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
    _record_valid_opening(player, tag)

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
    _record_valid_opening(player, tag)

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
    _record_valid_opening(player, tag)

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
    _record_valid_opening(player, tag)
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
    _record_valid_opening(player, tag)
    repo = _FakeRepo()

    await _persist_one(player, repo, tag, "gen6randombattle", "test")

    assert repo.saved_steps[0][-1] == "agent"
    # El cableado F2-08/F2-09: `_persist_one` pasa las 11 columnas de
    # metadata del step; un step sin metadata (ruta random/historica) las
    # deja en None -- jamas se inventan provider/model.
    kwargs = repo.saved_step_kwargs[0]
    assert kwargs["action_path"] == "llm_retry"
    assert kwargs["rationale"] is None
    assert kwargs["confidence"] is None
    assert kwargs["alternatives"] is None
    assert kwargs["target"] is None
    assert kwargs["provider"] is None
    assert kwargs["model"] is None
    assert kwargs["decision_latency_ms"] is None
    assert kwargs["input_tokens"] is None
    assert kwargs["output_tokens"] is None
    assert kwargs["cached_input_tokens"] is None
    assert kwargs["reasoning_tokens"] is None


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
            battle_timeout_seconds=5.0,
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
    monkeypatch.setattr(
        cli_module,
        "load_settings",
        lambda: SimpleNamespace(
            showdown_ws_url="ws://localhost:8100/showdown/websocket",
            bot_username="Bot",
            database_url="postgresql+asyncpg://x:x@localhost:15432/x",
            battle_timeout_seconds=0.01,
        ),
    )

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
            battle_timeout_seconds=0.01,
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
    """El CLI propaga `LUDEX_BATTLE_TIMEOUT_SECONDS` (via settings ->
    _benchmark_command -> run_benchmark), clasifica el deadline y escribe
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

        async def drain_inflight_decisions(self):
            pass

    _patch_benchmark_command_dependencies(monkeypatch, SlowAgent)
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    real_run_benchmark = run_benchmark
    timeout_values: list[float | None] = []

    async def spy_run_benchmark(*args, timeout=None, **kwargs):
        timeout_values.append(timeout)
        assert timeout == 0.01, (
            f"timeout={timeout!r} != valor configurado 0.01: si alguien "
            f"vuelve a una constante fija de 180s, la matriz abortaria "
            f"batallas largas sin respetar el config"
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
            "LUDEX_BATTLE_TIMEOUT_SECONDS": "0.01",
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
    # R3 (MON-15): evidencia durable y sanitizada — solo nombres de clase.
    assert data["failure_type"] == "BenchmarkDeadlineExceeded"
    assert data["failure_cause_type"] == "TimeoutError"

    assert ledger_path.exists()
    ledger_text = ledger_path.read_text()
    assert "test-deadline" in ledger_text
    assert "1/3" in ledger_text
    assert "1-0-0" in ledger_text


def test_benchmark_command_transient_con_causa_persiste_tipos_en_json(
    monkeypatch, tmp_path
):
    """R3: el camino de fallo transitorio de `_benchmark_command` persiste
    `failure_type` y `failure_cause_type` (solo nombres de clase) en el
    artefacto. La causa original (raw) viaja por `__cause__` del error
    clasificado, igual que en produccion con `raise error from raw`."""

    class FailingAgent:
        def __init__(self, **kwargs) -> None:
            self.n_won_battles = 0
            self.n_lost_battles = 0
            self.n_tied_battles = 0
            self.battles = {}

        async def battle_against(self, rival, n_battles=1):
            raw = TimeoutError(
                "Request timed out. (url: https://api.kimi.com/v1/chat/completions)"
            )
            try:
                raise TransientProviderError("provider transport failed") from raw
            except TransientProviderError as exc:
                raise exc

        async def wait_for_background_failure(self):
            await asyncio.Event().wait()

        async def drain_inflight_decisions(self):
            pass

    _patch_benchmark_command_dependencies(monkeypatch, FailingAgent)

    async def reachable(url):
        pass

    monkeypatch.setattr(cli_module, "_check_showdown_reachable", reachable)
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "--n", "1",
            "--opponent", "random",
            "--provider", "fake",
            "--model", "fake-model",
            "--run-id", "test-transient",
            "--ledger", str(tmp_path / "ledger.md"),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "fake",
            "LUDEX_MODEL": "fake-model",
            "LUDEX_BATTLE_TIMEOUT_SECONDS": "0.01",
        },
    )

    assert result.exit_code == 1
    assert "ABORTED: TransientProviderError" in result.stdout
    artifact = tmp_path / "test-transient.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text())
    assert data["status"] == "aborted"
    assert data["failure_type"] == "TransientProviderError"
    assert data["failure_cause_type"] == "TimeoutError"
    # Sanitizado: sin mensaje crudo, URL ni secreto en el artefacto.
    rendered = artifact.read_text()
    assert "Request timed out" not in rendered
    assert "api.kimi.com" not in rendered


def test_benchmark_pasa_battle_timeout_al_comando_interno(monkeypatch):
    """F2-10B (MON-20): `--battle-timeout` del CLI llega hasta
    `_benchmark_command`. Si la opcion se ignorara y se usara el default
    (180) en vez del valor configurado, este test falla."""
    captured: dict[str, object] = {}

    async def fake_benchmark_command(*, on_progress, battle_timeout_seconds,
                                     **kwargs):
        captured["battle_timeout_seconds"] = battle_timeout_seconds
        metrics = {
            "turns_total": 2, "calls_total": 2,
            "input_tokens": 100, "output_tokens": 20,
            "cached_input_tokens": 0, "reasoning_tokens": 0,
            "turns_model_invalid": 0, "turns_fallback": 0,
            "turns_deadline_affected": 0, "key_rotations": 0,
        }
        progress = BenchmarkResult(
            requested=2, completed=0, wins=0, losses=0, ties=0,
            provider="open_code_zen", model="mimo-v2.5-free",
        )
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
            "--battle-timeout", "1800", "--no-record",
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "LUDEX_PROVIDER": "open_code_zen",
            "LUDEX_MODEL": "mimo-v2.5-free",
            "OPEN_CODE_ZEN_API_KEY": "fake-key",
            "OPEN_CODE_ZEN_BASE_URL": "https://opencode.ai/zen/v1",
        },
    )
    assert result.exit_code == 0
    assert captured.get("battle_timeout_seconds") == 1800.0


def test_benchmark_rechaza_battle_timeout_no_positivo(monkeypatch):
    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "1", "--opponent", "random",
            "--provider", "google", "--model", "gemini-2.5-flash",
            "--battle-timeout", "0", "--no-record",
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "GEMINI_API_KEY": "fake-key",
        },
    )
    assert result.exit_code != 0
    assert "positivo" in result.stdout


# --- MON-28: fail-fast de `--run-id` antes de cualquier efecto live -------
#
# Evidencia real (MON-16): `--run-id 20260821t131800z-mon27-e2e-google-
# gemini-2.5-flash` (el punto de "gemini-2.5-flash") llego hasta DESPUES de
# una batalla real y su persistencia -- `build_benchmark_record` solo se
# alcanzaba por primera vez desde `report_progress`. `battle-id=3981`/
# `trajectory-id=2725`: 29 decisiones persistidas, cero artefacto de
# corrida, `ValueError` enmascarando el resultado.
# ---------------------------------------------------------------------------


def _fail_if_called(name: str):
    def _boom(*args, **kwargs):
        raise AssertionError(
            f"{name} no debe llamarse: el run_id invalido tiene que "
            f"fallar ANTES de cualquier efecto live"
        )
    return _boom


def test_benchmark_rechaza_run_id_con_punto_antes_de_efectos_live(
    monkeypatch, tmp_path
):
    """MON-28 R1: un `--run-id` con punto (reproduccion exacta del bug real)
    tiene que fallar con el error de validacion de `[a-z0-9-]+` ANTES de
    `_benchmark_command`, `write_run_snapshot` o `append_ledger_row`. Los
    tres espian con un `AssertionError` propio si se los llega a invocar --
    un mock que simplemente no se ejercita no demuestra nada por si solo,
    asi que ademas se confirma que ni el artefacto ni el ledger existen en
    disco."""
    monkeypatch.setattr(
        cli_module, "_benchmark_command", _fail_if_called("_benchmark_command")
    )
    monkeypatch.setattr(
        cli_module, "write_run_snapshot", _fail_if_called("write_run_snapshot")
    )
    monkeypatch.setattr(
        cli_module, "append_ledger_row", _fail_if_called("append_ledger_row")
    )
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)
    ledger_path = tmp_path / "ledger.md"

    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "1", "--opponent", "random",
            "--provider", "google", "--model", "gemini-2.5-flash",
            "--run-id", "20260821t131800z-mon27-e2e-google-gemini-2.5-flash",
            "--ledger", str(ledger_path), "--no-record",
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "GEMINI_API_KEY": "fake-key",
        },
    )
    assert result.exit_code != 0, result.stdout
    assert "[a-z0-9-]+" in str(result.exception), result.exception
    assert list(tmp_path.iterdir()) == [], (
        "ningun artefacto se escribe con un run_id invalido"
    )
    assert not ledger_path.exists()


def test_benchmark_rechaza_run_id_con_ruta_o_espacio_antes_de_efectos_live(
    monkeypatch, tmp_path
):
    """Contrapeso del canario anterior con un id de forma distinta (ruta +
    espacio) para no depender solo del caso puntual del punto -- mismo
    mecanismo, `RUN_ID_PATTERN` completo."""
    monkeypatch.setattr(
        cli_module, "_benchmark_command", _fail_if_called("_benchmark_command")
    )
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "1", "--opponent", "random",
            "--provider", "google", "--model", "gemini-2.5-flash",
            "--run-id", "../bad id", "--no-record",
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "GEMINI_API_KEY": "fake-key",
        },
    )
    assert result.exit_code != 0, result.stdout
    assert "[a-z0-9-]+" in str(result.exception), result.exception
    assert list(tmp_path.iterdir()) == []


def test_benchmark_acepta_run_id_valido_y_llega_a_efectos_live(monkeypatch, tmp_path):
    """Control positivo: un `--run-id` que YA cumple `[a-z0-9-]+` conserva
    el flujo normal -- `_benchmark_command` SI se invoca, con exactamente el
    run_id pedido."""
    captured: dict[str, object] = {}

    async def fake_benchmark_command(*, on_progress, **kwargs):
        captured["called"] = True
        metrics = {
            "turns_total": 1, "calls_total": 1,
            "input_tokens": 10, "output_tokens": 2,
            "cached_input_tokens": 0, "reasoning_tokens": 0,
            "turns_model_invalid": 0, "turns_fallback": 0,
            "turns_deadline_affected": 0, "key_rotations": 0,
        }
        return (
            BenchmarkResult(
                requested=1, completed=1, wins=1, losses=0, ties=0,
                provider="google", model="gemini-2.5-flash",
            ),
            metrics,
        )

    monkeypatch.setattr(cli_module, "_benchmark_command", fake_benchmark_command)
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "1", "--opponent", "random",
            "--provider", "google", "--model", "gemini-2.5-flash",
            "--run-id", "mon27-e2e-google-gemini-2-5-flash",
            "--ledger", str(tmp_path / "ledger.md"),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "GEMINI_API_KEY": "fake-key",
        },
    )
    assert result.exit_code == 0, result.stdout
    assert captured.get("called") is True
    artifact = tmp_path / "mon27-e2e-google-gemini-2-5-flash.json"
    assert artifact.exists()


def test_benchmark_sin_run_id_genera_uno_valido(monkeypatch, tmp_path):
    """Control por defecto: omitir `--run-id` sigue generando un id que
    pasa `validate_run_id` sin cambiar el contrato -- el default ya
    reemplaza `_`/`.` por `-` (`effective_run_id`, `cli.py`), asi que este
    control tiene que seguir en verde sin que el fix lo toque."""
    captured: dict[str, object] = {}

    async def fake_benchmark_command(*, on_progress, **kwargs):
        captured["called"] = True
        metrics = {
            "turns_total": 1, "calls_total": 1,
            "input_tokens": 10, "output_tokens": 2,
            "cached_input_tokens": 0, "reasoning_tokens": 0,
            "turns_model_invalid": 0, "turns_fallback": 0,
            "turns_deadline_affected": 0, "key_rotations": 0,
        }
        return (
            BenchmarkResult(
                requested=1, completed=1, wins=1, losses=0, ties=0,
                provider="google", model="gemini-2.5-flash",
            ),
            metrics,
        )

    monkeypatch.setattr(cli_module, "_benchmark_command", fake_benchmark_command)
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "1", "--opponent", "random",
            "--provider", "google", "--model", "gemini-2.5-flash",
            "--ledger", str(tmp_path / "ledger.md"),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "GEMINI_API_KEY": "fake-key",
        },
    )
    assert result.exit_code == 0, result.stdout
    assert captured.get("called") is True
    artifacts = [p for p in tmp_path.iterdir() if p.suffix == ".json"]
    assert len(artifacts) == 1, artifacts
    # el default reemplaza "." de "gemini-2.5-flash" por "-": sin punto.
    assert "." not in artifacts[0].stem


@pytest.mark.asyncio
async def test_battle_timeout_llega_a_run_benchmark(monkeypatch):
    """La propagacion NO puede volver a una constante fija: el timeout que
    recibe run_benchmark tiene que ser exactamente el configurado (1800),
    no el default productivo (180)."""
    # hermetico: load_settings() exige el STRING de la URL; todos los
    # repos/engine estan fakeados, nunca se conecta
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://x:x@localhost:15432/x"
    )
    captured: dict[str, object] = {}

    async def fake_run_benchmark(agent, rival, *, timeout, **kwargs):
        captured["timeout"] = timeout
        return BenchmarkResult(
            requested=2, completed=2, wins=1, losses=1, ties=0,
            provider="google", model="gemini-2.5-flash",
        )

    async def fake_reachable(ws_url):
        return None

    monkeypatch.setattr(cli_module, "run_benchmark", fake_run_benchmark)
    monkeypatch.setattr(cli_module, "_check_showdown_reachable", fake_reachable)
    monkeypatch.setattr(
        cli_module, "local_server_configuration", lambda ws_url: None
    )
    monkeypatch.setattr(
        cli_module, "_benchmark_provider",
        lambda *a, **k: type("Selected", (), {})() if False else object(),
    )
    monkeypatch.setattr(cli_module, "CalcClient", lambda *a, **k: type(
        "Calc", (), {"aclose": lambda self: asyncio.sleep(0)})())
    monkeypatch.setattr(cli_module, "PostgresContextRepository", lambda url: type(
        "Ctx", (), {"aclose": lambda self: asyncio.sleep(0)})())
    monkeypatch.setattr(cli_module, "build_decision_graph",
                        lambda *a, **k: None)
    def fake_player_class(**init_kwargs):
        class Player:
            n_won_battles = 1
            n_lost_battles = 1
            n_tied_battles = 0
            battles = []

            async def drain_inflight_decisions(self):
                return None

        return Player()

    monkeypatch.setattr(cli_module, "LudexPlayer", fake_player_class)
    monkeypatch.setattr(cli_module, "RandomPlayer", lambda **k: object())
    monkeypatch.setattr(cli_module, "MaxBasePowerPlayer", lambda **k: object())
    monkeypatch.setattr(cli_module, "SimpleHeuristicsPlayer", lambda **k: object())
    monkeypatch.setattr(cli_module, "make_engine", lambda url: type(
        "Engine", (), {"dispose": lambda self: asyncio.sleep(0)})())
    monkeypatch.setattr(cli_module, "session_factory", lambda engine: None)
    monkeypatch.setattr(cli_module, "BattleRepository", lambda factory: None)

    await cli_module._benchmark_command(
        n=2, opponent="random", concurrency=1, persist=False,
        provider_name="google", model="gemini-2.5-flash",
        fmt="gen6randombattle", battle_timeout_seconds=1800.0,
    )
    assert captured.get("timeout") == 1800.0


# --- F2-10B R3 (MON-20): ejecutor matrix-run fail-closed ----------------


def _manifest_document(tmp_path, rows=None):
    import json as _json

    if rows is None:
        rows = [
            {
                "provider": "open_code_zen", "model": "mimo-v2.5-free",
                "protocol": "chat_completions", "endpoint": None,
                "structured_output": "json_schema", "tier": "free",
                "status": "ready", "battles": 2, "concurrency": 1,
                "persist": False, "pin": ["open_code_zen", "mimo-v2.5-free"],
                "estimated_cost_usd": "0", "estimated_smoke_usd": "0",
                "classification_note": "free (zen-docs)",
            },
            {
                "provider": "open_code_zen", "model": "deepseek-v4-flash",
                "protocol": "chat_completions", "endpoint": None,
                "structured_output": "json_schema", "tier": "paid",
                "status": "ready", "battles": 2, "concurrency": 1,
                "persist": False, "pin": ["open_code_zen", "deepseek-v4-flash"],
                "estimated_cost_usd": "0.4536", "estimated_smoke_usd": "0.006",
                "classification_note": "paid (zen-docs)",
            },
        ]
    path = tmp_path / "manifest.json"
    path.write_text(_json.dumps({"rows": rows}))
    return path


def test_matrix_run_rechaza_opciones_de_persistencia_y_concurrencia(
    tmp_path, monkeypatch
):
    """Canario: el ejecutor de la matriz NO expone --persist ni
    --concurrency: la persistencia y la concurrencia estan prohibidas por
    diseno (persist=false, concurrency=1)."""
    manifest = _manifest_document(tmp_path)
    for extra in (["--persist"], ["--concurrency", "2"]):
        result = CliRunner().invoke(
            app,
            ["matrix-run", "--manifest", str(manifest), "--tier", "free"]
            + extra,
            env={
                "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
                "OPEN_CODE_ZEN_API_KEY": "fake-key",
            },
        )
        assert result.exit_code != 0, extra
        assert "No such option" in result.stdout, extra


def test_matrix_run_exige_tier_y_auto_reload_para_zen(tmp_path):
    manifest = _manifest_document(tmp_path)
    env = {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
        "OPEN_CODE_ZEN_API_KEY": "fake-key",
    }
    # --tier es obligatorio
    sin_tier = CliRunner().invoke(
        app, ["matrix-run", "--manifest", str(manifest)], env=env
    )
    assert sin_tier.exit_code != 0
    # fase que toca open_code_zen exige confirmacion de auto-reload OFF
    sin_confirm = CliRunner().invoke(
        app, ["matrix-run", "--manifest", str(manifest), "--tier", "free"],
        env=env,
    )
    assert sin_confirm.exit_code != 0
    assert "auto-reload" in sin_confirm.stdout


def test_matrix_run_refresca_catalogo_sin_referencia_indefinida(
    tmp_path, monkeypatch
):
    """ADDENDUM R1B (MON-20): `matrix-run` llama a `refresh_models` en su
    refresh de catalogo; el import faltante en `matrix_run_command` hacia
    crashear el CLI con NameError en el primer refresh (descubierto en la
    ejecucion R1B, latente desde R3 -- `matrix-run` nunca se habia
    ejecutado en vivo con el runner commiteado). El refresh del CLI tiene
    que poder ejecutarse: sin esta cobertura, el defecto vuelve a ser
    silencioso hasta la proxima corrida real."""
    manifest = _manifest_document(tmp_path)
    calls: list[str] = []
    invoked: dict[str, object] = {}

    async def fake_refresh(provider, *, base_url, api_key, environ,
                           client=None):
        calls.append(provider)
        return ["mimo-v2.5-free"]

    async def fake_run_matrix_round(**kwargs):
        # el refresh_catalog REAL del CLI corre dentro de la invocacion,
        # con el env del CliRunner activo
        fresh = await kwargs["refresh_catalog"]()
        invoked["fresh"] = fresh
        invoked["tier"] = kwargs["tier"]
        return []

    monkeypatch.setattr("ludex_agent.matrix.refresh_models", fake_refresh)
    monkeypatch.setattr(
        "ludex_agent.matrix.run_matrix_round", fake_run_matrix_round
    )
    # Hermetico: conftest carga el .env real; esta ronda no expone
    # Gemini/Kimi al proceso, asi que el refresh solo puede tocar zen.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:15432/x")
    monkeypatch.setenv("OPEN_CODE_ZEN_API_KEY", "fake-key")
    monkeypatch.setenv("OPEN_CODE_ZEN_BASE_URL", "https://opencode.ai/zen/v1")
    for secret_env in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY",
                       "GOOGLE_API_KEYS", "KIMI_API_KEY", "KIMI_BASE_URL"):
        monkeypatch.delenv(secret_env, raising=False)

    result = CliRunner().invoke(
        app,
        ["matrix-run", "--manifest", str(manifest), "--tier", "free",
         "--round", "test-refresh", "--zen-auto-reload-confirmed"],
    )
    assert result.exit_code == 0, result.stdout
    # el refresh_catalog REAL del CLI se invoco sin NameError y refresco
    # solo open_code_zen (google/kimi no tienen clave en este env)
    assert invoked["fresh"] == {"open_code_zen": ["mimo-v2.5-free"]}
    assert calls == ["open_code_zen"]


def test_matrix_run_rechaza_battle_timeout_no_positivo(tmp_path):
    manifest = _manifest_document(tmp_path)
    result = CliRunner().invoke(
        app,
        ["matrix-run", "--manifest", str(manifest), "--tier", "free",
         "--battle-timeout", "0"],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
        },
    )
    assert result.exit_code != 0
    assert "positivo" in result.stdout


def test_matrix_run_rechaza_interval_diagnostico_no_positivo(
    tmp_path, monkeypatch
):
    """D51 exige un intervalo positivo; cero no puede desactivar en silencio
    un monitor que el operador pidio explicitamente."""
    manifest = _manifest_document(tmp_path, rows=[])

    async def fake_run_matrix_round(**kwargs):
        return []

    monkeypatch.setattr(
        "ludex_agent.matrix.run_matrix_round", fake_run_matrix_round
    )
    result = CliRunner().invoke(
        app,
        ["matrix-run", "--manifest", str(manifest), "--tier", "free",
         "--diagnostic-snapshot-interval", "0"],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
        },
    )
    assert result.exit_code != 0
    assert "numero positivo" in result.stdout


def test_matrix_run_propaga_monitor_d51_al_benchmark(tmp_path, monkeypatch):
    """Canario D51: una matriz debe poder observar la decision colgada.

    Romper la propagacion del intervalo desde el CLI hasta
    `_benchmark_command` deja nuevamente una corrida paga sin snapshots ni
    artefacto clasificable, que es el defecto observado en R4/R5.
    """
    manifest = _manifest_document(tmp_path, rows=[{
        "provider": "fake", "model": "fake-model",
        "protocol": "chat_completions", "endpoint": None,
        "structured_output": "json_schema", "tier": "free",
        "status": "ready", "battles": 2, "concurrency": 1,
        "persist": False, "pin": ["fake", "fake-model"],
        "estimated_cost_usd": "0", "estimated_smoke_usd": "0",
        "classification_note": "fixture offline",
    }])
    captured: dict[str, object] = {}

    async def fake_benchmark_command(**kwargs):
        captured.update(kwargs)
        return BenchmarkResult(
            requested=2, completed=2, wins=1, losses=1, ties=0,
            provider="fake", model="fake-model",
        ), {}

    async def fake_run_matrix_round(**kwargs):
        await kwargs["run_battles"](
            "fake", "fake-model", n=2,
            battle_timeout_seconds=1800.0,
            fmt="gen6randombattle", opponent="simple_heuristics",
        )
        return []

    monkeypatch.setattr(
        "ludex_agent.matrix.run_matrix_round", fake_run_matrix_round
    )
    monkeypatch.setattr(
        cli_module, "_benchmark_command", fake_benchmark_command
    )

    result = CliRunner().invoke(app, [
        "matrix-run", "--manifest", str(manifest), "--tier", "free",
        "--round", "diag-matrix",
        "--diagnostic-snapshot-interval", "0.25",
    ], env={
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
    })

    assert result.exit_code == 0, result.stdout
    assert captured.get("diagnostic_snapshot_interval") == 0.25


def test_escritura_parcial_no_reemplaza_el_ultimo_artefacto_valido(tmp_path):
    """Canario: una escritura fallida a mitad de camino (JSON no
    serializable) no debe tocar el artefacto valido previo."""
    artifact = tmp_path / "r1-matrix.json"
    _atomic_write_json(artifact, {"status": "compatible", "battles": 2})
    assert artifact.read_text() == (
        '{\n  "status": "compatible",\n  "battles": 2\n}\n'
    )
    with pytest.raises(TypeError):
        _atomic_write_json(
            artifact,
            {"status": "running", "bad": object()},
        )
    # el artefacto valido sigue intacto
    assert artifact.read_text() == (
        '{\n  "status": "compatible",\n  "battles": 2\n}\n'
    )
    assert not artifact.with_suffix(".json.tmp").exists()


def test_matrix_plan_no_refresh_funciona_offline_sin_claves(tmp_path):
    """SECURITY HOLD: matrix-plan --no-refresh construye el manifiesto
    desde el inventario commiteado, SIN red y SIN claves de proveedor en el
    entorno (solo DATABASE_URL). Cero requests, cero lectura de .env."""
    import json as _json

    inventory = tmp_path / "inventory.json"
    inventory.write_text(_json.dumps({
        "models": {
            "google": [{"id": "gemma-4-26b-a4b-it", "in_scope": True}],
            "kimi": [{"id": "kimi-k2.6", "in_scope": True}],
            "open_code_zen": [
                {"id": "mimo-v2.5-free", "in_scope": True},
            ],
        },
    }))
    out = tmp_path / "manifest-out.json"
    env = {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
        "LUDEX_PRICING_TABLE": "evals/pricing-2026-08-08.json",
    }
    for key in ("GEMINI_API_KEY", "KIMI_API_KEY", "OPEN_CODE_ZEN_API_KEY"):
        env.pop(key, None)
    result = CliRunner().invoke(
        app,
        ["matrix-plan", "--inventory", str(inventory),
         "--manifest", str(out), "--no-refresh",
         "--budget", "evals/budget-2026-08-08.json"],
        env=env,
    )
    assert result.exit_code == 0, result.stdout
    document = _json.loads(out.read_text())
    rows = {f"{r['provider']}/{r['model']}": r for r in document["rows"]}
    assert "google/gemma-4-26b-a4b-it" in rows
    assert rows["open_code_zen/mimo-v2.5-free"]["tier"] == "free"
    assert "kimi/kimi-k2.6" in rows
    assert rows["kimi/kimi-k2.6"]["tier"] == "paid"
    assert rows["kimi/kimi-k2.6"]["status"] == "ready"


def test_benchmark_command_drena_decisiones_antes_de_cerrar_calc_y_contexto(
    monkeypatch, tmp_path
):
    """D46/MON-23: `_benchmark_command` debe drenar las decisiones en vuelo
    del agente ANTES de cerrar `CalcClient` y el context repository.

    L-01 (correccion LATWAN): la frontera estructurada termina SIEMPRE en
    `engine.dispose()`, que queda observado en el orden (los canarios
    anteriores no lo observaban). Si alguien omitiera el tramo engine, este
    canario se pone rojo.

    `run_benchmark`/`battle_against` corren como task HERMANA de la que
    procesa `choose_move -> calc_damage` en `ps_client.loop`: cancelar o
    terminar esa task no cancela una decision huerfana. Sin drenar primero,
    una decision asi puede seguir usando `CalcClient` despues de cerrado
    (`RuntimeError`/`httpx.ReadError`, el sintoma verificado del issue).
    Este test no reproduce la carrera de red (eso lo cubre
    `test_client.py`); prueba el CONTRATO DE ORDEN que el caller productivo
    tiene que respetar."""
    order: list[str] = []
    stop_counts: dict[str, int] = {}

    class SpyPSClient:
        def __init__(self, name: str, fail: bool = False) -> None:
            self._name = name
            self._fail = fail
            self.stop_calls = 0

        async def stop_listening(self):
            self.stop_calls += 1
            stop_counts[self._name] = self.stop_calls
            order.append(f"{self._name}_stop")
            if self._fail:
                raise RuntimeError(f"{self._name} close failed")

    class SpyAgent:
        def __init__(self, **kwargs) -> None:
            self.n_won_battles = 0
            self.n_lost_battles = 0
            self.n_tied_battles = 0
            self.battles: dict[str, object] = {}
            self.ps_client = SpyPSClient("agent")

        async def battle_against(self, rival, n_battles=1):
            self.battles[f"battle-{len(self.battles)}"] = object()
            self.n_won_battles += 1

        async def wait_for_background_failure(self):
            await asyncio.Event().wait()

        async def drain_inflight_decisions(self):
            order.append("drain")

    class SpyRival:
        def __init__(self, **kwargs) -> None:
            self.ps_client = SpyPSClient("rival")

    class SpyCalcClient:
        async def aclose(self):
            order.append("calc_aclose")

    class SpyContextRepo:
        async def aclose(self):
            order.append("context_aclose")

    class SpyEngine:
        async def dispose(self):
            order.append("engine_dispose")

    _patch_benchmark_command_dependencies(monkeypatch, SpyAgent)
    monkeypatch.setattr(cli_module, "RandomPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "MaxBasePowerPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "SimpleHeuristicsPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "CalcClient", lambda *a, **k: SpyCalcClient())
    monkeypatch.setattr(
        cli_module, "PostgresContextRepository", lambda *a, **k: SpyContextRepo()
    )
    monkeypatch.setattr(cli_module, "make_engine", lambda url: SpyEngine())
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "--n", "1",
            "--opponent", "random",
            "--provider", "fake",
            "--model", "fake-model",
            "--run-id", "test-drain-order",
            "--ledger", str(tmp_path / "ledger.md"),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "SHOWDOWN_WS_URL": "ws://localhost:8100/showdown/websocket",
            "LUDEX_PROVIDER": "fake",
            "LUDEX_MODEL": "fake-model",
        },
    )

    assert result.exit_code == 0, result.stdout
    # L-01 (post-R1B): lifecycle completo — drain D46, cierre de AMBOS
    # players (stop_listening) y recién despues calc/contexto/engine.
    assert order == [
        "drain", "agent_stop", "rival_stop", "calc_aclose", "context_aclose",
        "engine_dispose",
    ], order
    # cada PSClient se cierra exactamente UNA vez
    assert stop_counts == {"agent": 1, "rival": 1}, stop_counts


def test_fallo_al_cerrar_un_player_no_impide_cerrar_el_resto_ni_oculta_la_primaria(
    monkeypatch,
):
    """L-01 (post-R1B): si el cierre del primer player falla, el segundo y
    calc/contexto/engine se cierran igual; y el error del cleanup jamas
    oculta la excepcion primaria del benchmark.

    L-01 (correccion LATWAN): la primaria viaja como `BenchmarkFailure` con
    resultado parcial tipado (el fallo del cleanup tampoco la reemplaza), y
    el tramo `engine.dispose()` queda observado en el orden."""
    order: list[str] = []

    class SpyPSClient:
        def __init__(self, name: str, fail: bool = False) -> None:
            self._name = name
            self._fail = fail

        async def stop_listening(self):
            order.append(f"{self._name}_stop")
            if self._fail:
                raise RuntimeError(f"{self._name} close failed")

    class SpyAgent:
        def __init__(self, **kwargs) -> None:
            self.n_won_battles = 0
            self.n_lost_battles = 0
            self.n_tied_battles = 0
            self.battles: dict[str, object] = {}
            self.ps_client = SpyPSClient("agent", fail=True)

        async def drain_inflight_decisions(self):
            order.append("drain")

    class SpyRival:
        def __init__(self, **kwargs) -> None:
            self.ps_client = SpyPSClient("rival")

    class SpyCalcClient:
        async def aclose(self):
            order.append("calc_aclose")

    class SpyContextRepo:
        async def aclose(self):
            order.append("context_aclose")

    class SpyEngine:
        async def dispose(self):
            order.append("engine_dispose")

    async def boom(*args, **kwargs):
        raise ValueError("primary boom")

    _patch_benchmark_command_dependencies(monkeypatch, SpyAgent)
    monkeypatch.setattr(cli_module, "run_benchmark", boom)
    monkeypatch.setattr(cli_module, "RandomPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "MaxBasePowerPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "SimpleHeuristicsPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "CalcClient", lambda *a, **k: SpyCalcClient())
    monkeypatch.setattr(
        cli_module, "PostgresContextRepository", lambda *a, **k: SpyContextRepo()
    )
    monkeypatch.setattr(cli_module, "make_engine", lambda url: SpyEngine())

    with pytest.raises(BenchmarkFailure, match="primary boom") as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=1, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    # la primaria se preserva como causa del BenchmarkFailure
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "primary boom" in str(excinfo.value.__cause__)
    # el fallo del cierre del agent NO impide cerrar rival + recursos
    assert order == [
        "drain", "agent_stop", "rival_stop", "calc_aclose", "context_aclose",
        "engine_dispose",
    ], order


def _benchmark_spy_resources(
    monkeypatch,
    build_agent,
    *,
    agent_stop_error: BaseException | None = None,
    calc_error: BaseException | None = None,
    context_error: BaseException | None = None,
    engine_error: BaseException | None = None,
) -> list[str]:
    """Monta la cadena de recursos de `_benchmark_command` con spies que
    registran el ORDEN de cierre y pueden fallar por tramo (fallo del primer
    player, de Calc, del context repository o de engine). `build_agent`
    recibe el rig `{"order": [...]}` para que el agente registre su drain.
    Devuelve el orden observado."""
    rig: dict[str, list[str]] = {"order": []}

    class SpyPSClient:
        def __init__(self, name: str) -> None:
            self._name = name

        async def stop_listening(self):
            rig["order"].append(f"{self._name}_stop")
            if self._name == "agent" and agent_stop_error is not None:
                raise agent_stop_error

    class SpyRival:
        def __init__(self, **kwargs) -> None:
            self.ps_client = SpyPSClient("rival")

    class SpyCalcClient:
        async def aclose(self):
            rig["order"].append("calc_aclose")
            if calc_error is not None:
                raise calc_error

    class SpyContextRepo:
        async def aclose(self):
            rig["order"].append("context_aclose")
            if context_error is not None:
                raise context_error

    class SpyEngine:
        async def dispose(self):
            rig["order"].append("engine_dispose")
            if engine_error is not None:
                raise engine_error

    def agent_factory(**kwargs):
        agent = build_agent(rig)
        agent.ps_client = SpyPSClient("agent")
        return agent

    _patch_benchmark_command_dependencies(monkeypatch, agent_factory)
    monkeypatch.setattr(cli_module, "RandomPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "MaxBasePowerPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "SimpleHeuristicsPlayer", SpyRival)
    monkeypatch.setattr(cli_module, "CalcClient", lambda *a, **k: SpyCalcClient())
    monkeypatch.setattr(
        cli_module, "PostgresContextRepository", lambda *a, **k: SpyContextRepo()
    )
    monkeypatch.setattr(cli_module, "make_engine", lambda url: SpyEngine())
    return rig["order"]


_FULL_CLEANUP_ORDER = [
    "drain", "agent_stop", "rival_stop", "calc_aclose", "context_aclose",
    "engine_dispose",
]


def _boom_agent_factory(drain_error: BaseException | None = None):
    def build_agent(rig: dict[str, list[str]]):
        class BoomAgent:
            n_won_battles = 0
            n_lost_battles = 0
            n_tied_battles = 0
            battles: dict[str, object] = {}

            async def drain_inflight_decisions(self):
                rig["order"].append("drain")
                if drain_error is not None:
                    raise drain_error

        return BoomAgent()

    return build_agent


def test_cleanup_drain_fallido_no_oculta_la_primaria_y_cierra_el_resto(
    monkeypatch,
):
    """L-01 (correccion LATWAN): si el drain falla, los players, calc,
    contexto y engine se intentan igual, y la primaria del benchmark se
    preserva como `__cause__` del `BenchmarkFailure`."""
    order = _benchmark_spy_resources(
        monkeypatch, _boom_agent_factory(drain_error=RuntimeError("drain cleanup"))
    )

    async def boom(*args, **kwargs):
        raise ValueError("primary boom")

    monkeypatch.setattr(cli_module, "run_benchmark", boom)

    with pytest.raises(BenchmarkFailure, match="primary boom") as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=1, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    # todos los tramos posteriores al drain fallido se intentaron
    assert order == _FULL_CLEANUP_ORDER, order
    # la primaria viaja intacta como causa
    assert isinstance(excinfo.value.__cause__, ValueError)
    # el resultado parcial tipado conserva progreso real (0 batallas) e
    # identidad efectiva (el pin)
    partial = excinfo.value.result
    assert partial.requested == 1
    assert partial.completed == 0
    assert partial.provider == "fake"
    assert partial.model == "fake-model"
    assert partial.failure_type == "ValueError"


def test_cleanup_primer_player_fallido_no_oculta_la_primaria_y_cierra_el_resto(
    monkeypatch,
):
    """L-01 (correccion LATWAN): si el cierre del primer player falla, el
    segundo y calc/contexto/engine se intentan igual; la primaria no se
    reemplaza por el error del cierre."""
    order = _benchmark_spy_resources(
        monkeypatch, _boom_agent_factory(),
        agent_stop_error=RuntimeError("agent close failed"),
    )

    async def boom(*args, **kwargs):
        raise ValueError("primary boom")

    monkeypatch.setattr(cli_module, "run_benchmark", boom)

    with pytest.raises(BenchmarkFailure, match="primary boom") as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=1, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    assert order == _FULL_CLEANUP_ORDER, order
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_cleanup_calc_fallido_no_oculta_la_primaria_y_cierra_el_resto(
    monkeypatch,
):
    """L-01 (correccion LATWAN): si `CalcClient.aclose()` falla, context
    repository y engine se intentan igual (el caso 2 de la reproduccion de
    Latwan) y la primaria se preserva."""
    order = _benchmark_spy_resources(
        monkeypatch, _boom_agent_factory(),
        calc_error=RuntimeError("calc cleanup"),
    )

    async def boom(*args, **kwargs):
        raise ValueError("primary boom")

    monkeypatch.setattr(cli_module, "run_benchmark", boom)

    with pytest.raises(BenchmarkFailure, match="primary boom") as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=1, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    assert order == _FULL_CLEANUP_ORDER, order
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_cleanup_context_fallido_no_oculta_la_primaria_y_cierra_engine(
    monkeypatch,
):
    order = _benchmark_spy_resources(
        monkeypatch, _boom_agent_factory(),
        context_error=RuntimeError("context cleanup"),
    )

    async def boom(*args, **kwargs):
        raise ValueError("primary boom")

    monkeypatch.setattr(cli_module, "run_benchmark", boom)

    with pytest.raises(BenchmarkFailure, match="primary boom") as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=1, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    # context fallido no impide el tramo engine (el ultimo)
    assert order == _FULL_CLEANUP_ORDER, order
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_cleanup_engine_fallido_no_oculta_la_primaria(monkeypatch):
    order = _benchmark_spy_resources(
        monkeypatch, _boom_agent_factory(),
        engine_error=RuntimeError("engine cleanup"),
    )

    async def boom(*args, **kwargs):
        raise ValueError("primary boom")

    monkeypatch.setattr(cli_module, "run_benchmark", boom)

    with pytest.raises(BenchmarkFailure, match="primary boom") as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=1, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    assert order == _FULL_CLEANUP_ORDER, order
    assert isinstance(excinfo.value.__cause__, ValueError)


def _two_battles_agent_factory(drain_error: BaseException | None = None):
    def build_agent(rig: dict[str, list[str]]):
        class TwoBattlesAgent:
            n_won_battles = 0
            n_lost_battles = 0
            n_tied_battles = 0
            battles: dict[str, object] = {}

            async def battle_against(self, rival, n_battles=1):
                self.battles[f"battle-{len(self.battles)}"] = object()
                self.n_won_battles += 1

            async def wait_for_background_failure(self):
                await asyncio.Event().wait()

            async def drain_inflight_decisions(self):
                rig["order"].append("drain")
                if drain_error is not None:
                    raise drain_error

        return TwoBattlesAgent()

    return build_agent


def test_benchmark_exitoso_con_cleanup_fallido_termina_internal_defect_sanitizado(
    monkeypatch,
):
    """L-01 (correccion LATWAN): el benchmark termina bien (2/2) pero el
    cleanup falla (drain). Sin excepcion primaria, la corrida NO puede
    quedar `compatible`: `_benchmark_command` devuelve un resultado
    `InternalCleanupError` sanitizado con el progreso real preservado."""
    order = _benchmark_spy_resources(
        monkeypatch,
        _two_battles_agent_factory(drain_error=RuntimeError("drain cleanup")),
    )

    result, _ = asyncio.run(cli_module._benchmark_command(
        n=2, opponent="random", concurrency=1, persist=False,
        provider_name="fake", model="fake-model",
        fmt="gen6randombattle", battle_timeout_seconds=0.01,
    ))
    # los tramos posteriores al drain fallido se intentaron
    assert order == _FULL_CLEANUP_ORDER, order
    # nunca compatible: internal-defect sanitizado con progreso real
    assert result.failure_type == "InternalCleanupError"
    assert result.failure_cause_type == "RuntimeError"
    assert result.failure is not None
    assert "drain cleanup" not in result.failure, result.failure
    assert not result.comparable
    assert result.completed == 2
    assert result.requested == 2
    assert result.provider == "fake"
    assert result.model == "fake-model"


def test_benchmark_exitoso_con_cierre_de_player_fallido_no_es_compatible(
    monkeypatch,
):
    """L-01 (correccion LATWAN, punto 4): antes, `_close_player_sockets`
    tragaba el fallo del cierre y la corrida podia reportar `compatible` con
    websockets vivos. Ahora un cierre fallido sin primaria tambien termina
    `InternalCleanupError`."""
    order = _benchmark_spy_resources(
        monkeypatch, _two_battles_agent_factory(),
        agent_stop_error=RuntimeError("agent close failed"),
    )

    result, _ = asyncio.run(cli_module._benchmark_command(
        n=2, opponent="random", concurrency=1, persist=False,
        provider_name="fake", model="fake-model",
        fmt="gen6randombattle", battle_timeout_seconds=0.01,
    ))
    assert order == _FULL_CLEANUP_ORDER, order
    assert result.failure_type == "InternalCleanupError"
    assert result.failure_cause_type == "RuntimeError"
    assert not result.comparable
    assert result.completed == 2


@pytest.mark.asyncio
async def test_cancelacion_externa_preserva_cancelled_error_y_ejecuta_cleanup(
    monkeypatch,
):
    """L-01 (correccion LATWAN, punto 5): la cancelacion externa NO se
    traga como fallo ordinario ni se convierte en `InternalCleanupError`: el
    `CancelledError` se preserva y el cleanup estructurado se ejecuta igual
    (todos los tramos)."""
    order: list[str] = []

    def build_hanging(rig: dict[str, list[str]]):
        class HangingAgent:
            n_won_battles = 0
            n_lost_battles = 0
            n_tied_battles = 0
            battles: dict[str, object] = {}

            async def battle_against(self, rival, n_battles=1):
                await asyncio.Event().wait()

            async def wait_for_background_failure(self):
                await asyncio.Event().wait()

            async def drain_inflight_decisions(self):
                rig["order"].append("drain")

        return HangingAgent()

    order = _benchmark_spy_resources(monkeypatch, build_hanging)

    task = asyncio.create_task(cli_module._benchmark_command(
        n=2, opponent="random", concurrency=1, persist=False,
        provider_name="fake", model="fake-model",
        fmt="gen6randombattle", battle_timeout_seconds=60.0,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    # el cleanup estructurado corrio completo, incluso bajo cancelacion
    assert order == _FULL_CLEANUP_ORDER, order


def test_benchmark_command_batalla_2_connection_closed_preserva_progreso_e_identidad(
    monkeypatch,
):
    """L-02 (correccion LATWAN, canario vinculante): smoke verde (ya paso,
    esto es `_benchmark_command`), batalla 1 termina, batalla 2 lanza
    `ConnectionClosedError`. La frontera real conserva un resultado parcial
    TIPADO (`BenchmarkFailure.result`) con requested=2, completed=1, W/L/T
    reales, provider/model efectivos iguales al pin, stage battle y
    evidencia sanitizada — jamas ceros inventados."""
    from websockets.exceptions import ConnectionClosedError

    def build_battle_failing(rig: dict[str, list[str]]):
        class BattleFailingAgent:
            n_won_battles = 0
            n_lost_battles = 0
            n_tied_battles = 0
            battles: dict[str, object] = {}

            async def battle_against(self, rival, n_battles=1):
                if len(self.battles) >= 1:
                    raise ConnectionClosedError(None, None)
                self.battles["battle-0"] = object()
                self.n_won_battles += 1

            async def wait_for_background_failure(self):
                await asyncio.Event().wait()

            async def drain_inflight_decisions(self):
                rig["order"].append("drain")

        return BattleFailingAgent()

    order = _benchmark_spy_resources(monkeypatch, build_battle_failing)

    with pytest.raises(BenchmarkFailure) as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=2, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    # la primaria real se preserva como causa del wrapper tipado
    assert isinstance(excinfo.value.__cause__, ConnectionClosedError)
    partial = excinfo.value.result
    # progreso real: no se inventa ni se borra
    assert partial.requested == 2
    assert partial.completed == 1
    assert partial.wins == 1
    assert partial.losses == 0
    assert partial.ties == 0
    # identidad efectiva igual al pin
    assert partial.provider == "fake"
    assert partial.model == "fake-model"
    # evidencia sanitizada
    assert partial.failure_type == "ConnectionClosedError"
    assert partial.failure_cause_type is None
    assert order == _FULL_CLEANUP_ORDER, order


def test_benchmark_command_preflight_showdown_fallido_preserva_identidad_sin_progreso(
    monkeypatch,
):
    """L-02 (correccion LATWAN, counterweight): el preflight de Showdown
    falla ANTES de crear players. La frontera conserva completed=0 (no hubo
    batallas), stage battle y la identidad efectiva ya demostrada por el
    smoke (provider/model del pin); jamas vuelve a None."""
    from ludex_agent.benchmark import ShowdownUnavailableError

    async def unreachable(url):
        try:
            raise OSError("connection refused")
        except OSError as exc:
            raise ShowdownUnavailableError(
                "No se pudo conectar a Showdown en localhost:8100"
            ) from exc

    # player/resource spies: nada deberia crearse ni cerrarse. El preflight
    # se reemplaza DESPUES (el helper de parches lo pisa).
    _benchmark_spy_resources(monkeypatch, _boom_agent_factory())
    monkeypatch.setattr(cli_module, "_check_showdown_reachable", unreachable)

    with pytest.raises(BenchmarkFailure) as excinfo:
        asyncio.run(cli_module._benchmark_command(
            n=2, opponent="random", concurrency=1, persist=False,
            provider_name="fake", model="fake-model",
            fmt="gen6randombattle", battle_timeout_seconds=0.01,
        ))
    assert isinstance(excinfo.value.__cause__, ShowdownUnavailableError)
    partial = excinfo.value.result
    assert partial.requested == 2
    assert partial.completed == 0
    # identidad efectiva del pin preservada (demostrada por el smoke)
    assert partial.provider == "fake"
    assert partial.model == "fake-model"
    assert partial.failure_type == "ShowdownUnavailableError"


@pytest.mark.asyncio
async def test_cierre_de_players_termina_websockets_y_listeners_en_pokeloop_real():
    """L-01 (post-R1B): con el POKE_LOOP real de poke-env, cerrar via
    `ps_client.stop_listening()` deja los websockets cerrados y los futures
    de `listen()` terminados: no quedan listeners acumulados entre
    benchmarks (la fuga de la ronda R1B)."""
    import websockets
    from websockets.asyncio.server import serve as ws_serve

    from ludex_agent.showdown.client import (
        LudexPlayer,
        local_server_configuration,
    )
    accepted = 0
    accepted_event = asyncio.Event()

    async def handler(websocket):
        nonlocal accepted
        accepted += 1
        if accepted == 2:
            accepted_event.set()
        try:
            await websocket.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    server = await ws_serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/showdown/websocket"
    common = {
        "server_configuration": local_server_configuration(url),
        "battle_format": "gen6randombattle",
        "log_level": 40,
    }
    agent = LudexPlayer(
        account_configuration=AccountConfiguration("LC1", None), **common
    )
    rival = cli_module.SimpleHeuristicsPlayer(
        account_configuration=AccountConfiguration("LC2", None), **common
    )
    try:
        await asyncio.wait_for(accepted_event.wait(), timeout=10)
        # `self.websocket` se asigna dentro de `listen()` tras el handshake;
        # esperar a que AMBOS esten seteados (poll breve).
        for _ in range(200):
            if (
                getattr(agent.ps_client, "websocket", None) is not None
                and getattr(rival.ps_client, "websocket", None) is not None
            ):
                break
            await asyncio.sleep(0.01)
        assert agent.ps_client.websocket is not None
        assert rival.ps_client.websocket is not None
        assert agent.ps_client.websocket.state.name == "OPEN"
        assert rival.ps_client.websocket.state.name == "OPEN"
        assert not agent.ps_client._listening_coroutine.done()
        assert not rival.ps_client._listening_coroutine.done()

        await cli_module._close_player_sockets(agent, rival)

        assert agent.ps_client.websocket.state.name == "CLOSED"
        assert rival.ps_client.websocket.state.name == "CLOSED"
        # los futures de listen() terminan: no quedan listeners colgados
        await asyncio.wait_for(
            asyncio.gather(
                asyncio.wrap_future(agent.ps_client._listening_coroutine),
                asyncio.wrap_future(rival.ps_client._listening_coroutine),
            ),
            timeout=10,
        )
        assert agent.ps_client._listening_coroutine.done()
        assert rival.ps_client._listening_coroutine.done()
    finally:
        await cli_module._close_player_sockets(agent, rival)
        server.close()
        await server.wait_closed()


def test_benchmark_sin_credenciales_valida_antes_de_tocar_showdown_y_emite_not_run(
    monkeypatch, tmp_path
):
    """L-05 (post-R1B): sin KIMI_API_KEY, la seleccion/validacion local de
    credenciales ocurre ANTES del chequeo de Showdown: el comando produce
    artefacto not-run con `failure_type=ProviderSelectionError` y exit 2
    SIN intentar abrir conexion a Showdown (aunque estuviera apagado)."""
    showdown_calls: list[str] = []

    def reachable(url):
        showdown_calls.append(url)
        raise AssertionError("no deberia comprobarse Showdown sin credenciales")

    monkeypatch.setattr(cli_module, "_check_showdown_reachable", reachable)
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "benchmark", "--n", "1", "--opponent", "random",
            "--provider", "kimi", "--model", "kimi-k2.6",
            "--run-id", "test-not-run-hermetic",
            "--ledger", str(tmp_path / "ledger.md"),
        ],
        env={
            "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
            "LUDEX_PROVIDER": "kimi",
        },
    )
    assert result.exit_code == 2, result.stdout
    assert "credential unavailable" in result.stdout
    assert showdown_calls == [], (
        "la validacion de credenciales debe preceder al chequeo de Showdown"
    )
    # artefacto not-run emitido con la clasificacion correcta
    artifact = tmp_path / "test-not-run-hermetic.json"
    assert artifact.exists()
    record = json.loads(artifact.read_text())
    assert record["status"] == "not-run"
    assert record["failure_type"] == "ProviderSelectionError"


def test_matrix_plan_registra_procedencia_de_la_tabla_efectiva(tmp_path, monkeypatch):
    """DIAG-B: matrix-plan --no-refresh registra en el artefacto la tabla
    efectiva que produjo el manifiesto: `table_id`, `currency` y la ruta
    efectiva seleccionada (default o `LUDEX_PRICING_TABLE`). La ruta
    explicita se conserva verbatim; el default se registra tal cual lo
    resuelve el CLI, sin secretos y sin depender de otras fuentes.

    R2 (T-01): `CliRunner env=` fusiona el entorno real, asi que un
    `LUDEX_PRICING_TABLE` heredado invalidaba el caso default; se elimina
    explicitamente antes de esa invocacion (monkeypatch) y el caso
    explicito sigue fijando su propio override."""
    import json as _json

    from ludex_agent.eval_cost import DEFAULT_PRICING_PATH, PricingTable

    inventory = tmp_path / "inventory.json"
    inventory.write_text(_json.dumps({
        "models": {
            "google": [{"id": "gemini-2.5-flash", "in_scope": True}],
            "open_code_zen": [{"id": "grok-4.6", "in_scope": True}],
        },
    }))
    base_env = {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost:15432/x",
    }
    monkeypatch.delenv("LUDEX_PRICING_TABLE", raising=False)

    out_default = tmp_path / "manifest-default.json"
    result = CliRunner().invoke(
        app,
        ["matrix-plan", "--inventory", str(inventory),
         "--manifest", str(out_default), "--no-refresh"],
        env=base_env,
    )
    assert result.exit_code == 0, result.stdout
    document = _json.loads(out_default.read_text())
    provenance = document["pricing"]
    default_table = PricingTable.load()
    assert provenance["table_id"] == default_table.table_id
    assert provenance["currency"] == default_table.currency == "USD"
    assert provenance["path"] == str(DEFAULT_PRICING_PATH)

    out_explicit = tmp_path / "manifest-explicit.json"
    env_explicit = dict(base_env)
    env_explicit["LUDEX_PRICING_TABLE"] = "evals/pricing-2026-08-08.json"
    result = CliRunner().invoke(
        app,
        ["matrix-plan", "--inventory", str(inventory),
         "--manifest", str(out_explicit), "--no-refresh"],
        env=env_explicit,
    )
    assert result.exit_code == 0, result.stdout
    document = _json.loads(out_explicit.read_text())
    assert document["pricing"] == {
        "table_id": "2026-08-08-zen-moonshot-modelsdev",
        "currency": "USD",
        "path": "evals/pricing-2026-08-08.json",
    }


# --- MON-20 R2 (Changes Requested): I2 CLI, I4 wiring, I3 fail-closed ----


def test_matrix_run_cancela_en_vuelo_y_el_artefacto_es_durable(
    tmp_path, monkeypatch
):
    """I2 a nivel matrix-run: si una fila se interrumpe en vuelo, el CLI
    escribe SIEMPRE el artefacto/state de stop (on_result sincronico) y la
    excepcion se relanza (exit != 0), en vez de perder la fila entera.

    El runner real corre con el on_result REAL del CLI; la cancelacion
    llega desde `_benchmark_command` (monkeypatcheado) a mitad de batalla.
    """
    import asyncio as _asyncio
    import json as _json

    manifest = _manifest_document(tmp_path)
    monkeypatch.setattr(
        cli_module, "DEFAULT_RUNS_PATH", tmp_path / "runs"
    )
    run_dir = tmp_path / "runs"
    run_dir.mkdir(parents=True)

    class _FakeProvider:
        def metrics_snapshot(self):
            return None

        async def complete(self, prompt, *, deadline, turn_id):
            from ludex_agent.graph.decision import DecisionResponse
            from ludex_agent.graph.provider import (
                CompletionEnvelope, CompletionUsage,
            )
            payload = {
                "action": {"kind": "move", "id": "tackle"},
                "target": None,
                "rationale": "smoke",
                "confidence": 0.9,
                "alternatives": [],
            }
            DecisionResponse.model_validate(payload)
            return CompletionEnvelope(
                payload=payload, provider="open_code_zen",
                model="mimo-v2.5-free",
                usage=CompletionUsage(input_tokens=1, output_tokens=1),
                latency_ms=1.0,
            )

    def fake_build_provider(provider_name, model_name, timeout, metrics):
        return _FakeProvider()

    async def fake_benchmark_command(**kwargs):
        raise _asyncio.CancelledError()

    monkeypatch.setattr(
        cli_module, "_benchmark_provider", fake_build_provider
    )
    monkeypatch.setattr(
        cli_module, "_benchmark_command", fake_benchmark_command
    )

    async def fake_refresh_models(provider, *, base_url, api_key, environ,
                                  client=None):
        # el catalogo fresco conserva la fila del manifiesto: el smoke y la
        # batalla pueden correr (y la batalla ser cancelada en vuelo)
        return ["mimo-v2.5-free"]

    monkeypatch.setattr("ludex_agent.matrix.refresh_models", fake_refresh_models)
    # Hermetico: el refresh usa la clave fake de zen y no toca otros proveedores
    monkeypatch.setenv("OPEN_CODE_ZEN_API_KEY", "fake-key")
    for secret_env in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY",
                       "GOOGLE_API_KEYS", "KIMI_API_KEY", "KIMI_BASE_URL"):
        monkeypatch.delenv(secret_env, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:15432/x")

    # la excepcion NO se traga: CancelledError (BaseException) escapa del
    # CLI tal cual, como una interrupcion real de Ctrl+C
    with pytest.raises(_asyncio.CancelledError):
        CliRunner().invoke(
            app,
            ["matrix-run", "--manifest", str(manifest), "--tier", "free",
             "--round", "r2-cancel-cli", "--zen-auto-reload-confirmed"],
        )
    # el artefacto de stop es durable
    state_path = run_dir / "r2-cancel-cli-matrix-run-state.json"
    assert state_path.exists(), "falta el state file de la fila interrumpida"
    state = _json.loads(state_path.read_text())
    stop = state["open_code_zen/mimo-v2.5-free"]
    assert stop["status"] == "internal-defect"
    assert stop["failure_type"] == "CancelledError"
    assert stop["failure_stage"] == "battle"
    assert stop["compatibility_result"] == "indeterminate-current-run"
    # artefacto atomico por modelo escrito por on_result antes del re-lanzamiento
    artifact = run_dir / (
        "r2-cancel-cli-open_code_zen-mimo-v2.5-free-matrix.json"
    )
    assert artifact.exists(), "falta el artefacto atomico del stop"
    assert _json.loads(artifact.read_text())["status"] == "internal-defect"
    # el contexto de corrida llega al artefacto
    assert _json.loads(artifact.read_text())["round"] == "r2-cancel-cli"


def test_matrix_run_pasa_contexto_y_hash_del_manifiesto_al_runner(
    tmp_path, monkeypatch
):
    """I4: el CLI computa la referencia y el sha256 del manifiesto y los
    propaga al runner junto con la identidad de ronda, para que cada
    artefacto sea auditable sin contexto externo."""
    import hashlib as _hashlib
    import json as _json

    manifest = _manifest_document(tmp_path)
    captured: dict[str, object] = {}

    async def fake_run_matrix_round(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "ludex_agent.matrix.run_matrix_round", fake_run_matrix_round
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:15432/x")
    for secret_env in ("GEMINI_API_KEY", "KIMI_API_KEY", "OPEN_CODE_ZEN_API_KEY"):
        monkeypatch.delenv(secret_env, raising=False)

    result = CliRunner().invoke(
        app,
        ["matrix-run", "--manifest", str(manifest), "--tier", "free",
         "--round", "r2-ctx-cli", "--zen-auto-reload-confirmed"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["round_name"] == "r2-ctx-cli"
    assert captured["manifest_ref"] == manifest.name
    expected_hash = _hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    assert captured["manifest_sha256"] == expected_hash


def test_matrix_run_rechaza_manifiesto_con_fila_operador_prohibida(
    tmp_path, monkeypatch
):
    """I3: un manifiesto que trae gpt-5.6-luna como ready (p.ej. el
    versionado) es rechazado por matrix-run antes de tocar cualquier
    request: exit != 0 y el mensaje nombra la politica."""
    manifest = _manifest_document(tmp_path, rows=[
        {
            "provider": "open_code_zen", "model": "gpt-5.6-luna",
            "protocol": "responses", "endpoint": None,
            "structured_output": "text_json", "tier": "paid",
            "status": "ready", "battles": 2, "concurrency": 1,
            "persist": False, "pin": ["open_code_zen", "gpt-5.6-luna"],
            "estimated_cost_usd": "0.744", "estimated_smoke_usd": "0.0104",
            "classification_note": "paid (zen-docs)",
        },
    ])
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:15432/x")
    for secret_env in ("GEMINI_API_KEY", "KIMI_API_KEY", "OPEN_CODE_ZEN_API_KEY"):
        monkeypatch.delenv(secret_env, raising=False)

    # hermetico: los artefactos de la corrida van a tmp, nunca al repo
    monkeypatch.setattr(cli_module, "DEFAULT_RUNS_PATH", tmp_path / "runs")
    (tmp_path / "runs").mkdir(parents=True)

    result = CliRunner().invoke(
        app,
        ["matrix-run", "--manifest", str(manifest), "--tier", "paid"],
    )
    assert result.exit_code != 0, result.stdout
    assert "operator-prohibited" in str(result.exception)
