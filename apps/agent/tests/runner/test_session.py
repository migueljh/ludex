"""RED tests for Task 6: SessionRunner (MON-36 S6).

Sesiones secuenciales con `N` configurable (spec 7.1): solo una solicitud
de matchmaking activa a la vez; borrar la sesion activa la marca
stop-after-current y JAMAS cancela una batalla en curso.
"""

from __future__ import annotations

import asyncio

import pytest

from ludex_agent.runner.session import (
    ActiveMatchmakingError,
    LadderGates,
    LadderInterlockError,
    SessionKind,
    SessionRunner,
)


class _FakePlayer:
    def __init__(self) -> None:
        self.ladder_calls: list[int] = []
        self._battle_done = asyncio.Event()

    async def ladder(self, n_battles: int) -> None:
        self.ladder_calls.append(n_battles)
        await self._battle_done.wait()

    def finish_current_battle(self) -> None:
        self._battle_done.set()


_ACCEPTANCE_DSN = "postgresql+asyncpg://ludex:ludex@127.0.0.1:9999/acceptance"
_CANONICAL_DSN = "postgresql+asyncpg://ludex:ludex@127.0.0.1:15432/ludex"


def _open_gates(**overrides) -> LadderGates:
    """Todos los interlocks abiertos; `overrides` cierra uno por mutacion."""
    gates = dict(
        connection_mode="official",
        battle_format="gen6randombattle",
        required_format="gen6randombattle",
        database_role="acceptance",
        database_url=_ACCEPTANCE_DSN,
        ladder_enabled=True,
        confirm=True,
        testing_account_confirmed=True,
    )
    gates.update(overrides)
    return LadderGates(**gates)


class _RecordingLadderPlayer:
    """Cuenta la actividad de red de `ladder`: la llamada, la apertura del
    socket y el envio de `/search`. Con `hold=True` (default) queda retenida
    en la batalla en curso (socket vivo); con `hold=False` retorna de
    inmediato, para que una mutacion de un interlock falle en rojo sin
    colgarse."""

    def __init__(self, hold: bool = True) -> None:
        self.hold = hold
        self.ladder_calls: list[int] = []
        self.socket_opens = 0
        self.search_sends: list[str] = []
        self._battle_done = asyncio.Event()

    async def ladder(self, n_battles: int) -> None:
        self.ladder_calls.append(n_battles)
        self.socket_opens += 1
        self.search_sends.append("/search")
        if self.hold:
            await self._battle_done.wait()

    def finish_current_battle(self) -> None:
        self._battle_done.set()


@pytest.mark.asyncio
async def test_sequential_n_runs_one_battle_at_a_time() -> None:
    player = _FakePlayer()
    runner = SessionRunner(player=player, gates=_open_gates())

    task = asyncio.ensure_future(runner.start(n_battles=3))
    await asyncio.sleep(0)
    assert player.ladder_calls == [1]

    player.finish_current_battle()
    await asyncio.sleep(0)
    player.finish_current_battle()
    await asyncio.wait_for(task, timeout=1.0)
    assert player.ladder_calls == [1, 1, 1]


@pytest.mark.asyncio
async def test_only_one_active_matchmaking_request_at_a_time() -> None:
    player = _FakePlayer()
    runner = SessionRunner(player=player, gates=_open_gates())
    task = asyncio.ensure_future(runner.start(n_battles=2))
    await asyncio.sleep(0)
    with pytest.raises(ActiveMatchmakingError):
        await runner.start(n_battles=1)
    player.finish_current_battle()
    player.finish_current_battle()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_stop_never_cancels_a_live_battle() -> None:
    """Borrar la sesion setea stop-after-current: la batalla en curso debe
    terminar normalmente, nunca cancelarse a mitad de camino."""
    player = _FakePlayer()
    runner = SessionRunner(player=player, gates=_open_gates())
    task = asyncio.ensure_future(runner.start(n_battles=5))
    await asyncio.sleep(0)
    await runner.stop()
    assert not task.done(), "stop no debe cancelar la batalla activa"
    player.finish_current_battle()
    await asyncio.wait_for(task, timeout=1.0)
    assert player.ladder_calls == [1]


@pytest.mark.asyncio
async def test_stop_after_current_prevents_next_battle_from_starting() -> None:
    player = _FakePlayer()
    runner = SessionRunner(player=player, gates=_open_gates())
    task = asyncio.ensure_future(runner.start(n_battles=5))
    await asyncio.sleep(0)
    await runner.stop()
    player.finish_current_battle()
    await asyncio.wait_for(task, timeout=1.0)
    assert player.ladder_calls == [1]


# --- Task 8 (MON-38/F3-08, D65 S6.2): ladder fail-closed --------------------


def test_session_kind_ladder_value() -> None:
    assert SessionKind.LADDER.value == "ladder"


@pytest.mark.parametrize(
    "overrides",
    [
        {"connection_mode": "local"},
        {"ladder_enabled": False},
        {"confirm": False},
        {"testing_account_confirmed": False},
        {"database_url": _CANONICAL_DSN},
        {"battle_format": "gen6ou"},
    ],
    ids=[
        "local-mode",
        "disabled-ladder",
        "missing-call-confirmation",
        "unconfirmed-testing-account",
        "canonical-db",
        "wrong-format",
    ],
)
@pytest.mark.asyncio
async def test_ladder_interlock_fails_closed_with_zero_network_calls(
    overrides: dict[str, object],
) -> None:
    """Canario de cero llamadas (D65 canario 30): al faltar un interlock, el
    runner rechaza ANTES de abrir socket o enviar `/search`."""
    player = _RecordingLadderPlayer(hold=False)
    runner = SessionRunner(player=player, gates=_open_gates(**overrides))

    with pytest.raises(LadderInterlockError):
        await runner.start(1)

    assert player.ladder_calls == []
    assert player.socket_opens == 0
    assert player.search_sends == []


@pytest.mark.asyncio
async def test_ladder_without_gates_fails_closed() -> None:
    """Sin `LadderGates` configurado no existe camino de red: fail-closed."""
    player = _RecordingLadderPlayer(hold=False)
    runner = SessionRunner(player=player)

    with pytest.raises(LadderInterlockError):
        await runner.start(1)

    assert player.ladder_calls == []


@pytest.mark.asyncio
async def test_open_interlocks_call_ladder_once() -> None:
    player = _RecordingLadderPlayer()
    runner = SessionRunner(player=player, gates=_open_gates())
    player.finish_current_battle()

    await runner.start(1)

    assert player.ladder_calls == [1]
    assert player.socket_opens == 1
    assert player.search_sends == ["/search"]


@pytest.mark.asyncio
async def test_ladder_disabled_after_a_session_blocks_the_next_start() -> None:
    """Step 3 (off-after-session): el interlock se evalua por `start()`, no se
    cachea al construir el runner; al deshabilitar ladder, el proximo arranque
    falla antes de red."""
    player = _RecordingLadderPlayer()
    runner = SessionRunner(player=player, gates=_open_gates())
    player.finish_current_battle()
    await runner.start(1)
    assert player.ladder_calls == [1]

    runner.gates = _open_gates(ladder_enabled=False)
    with pytest.raises(LadderInterlockError):
        await runner.start(1)

    assert player.ladder_calls == [1]
