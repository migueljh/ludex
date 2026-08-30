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


@pytest.mark.asyncio
async def test_sequential_n_runs_one_battle_at_a_time() -> None:
    player = _FakePlayer()
    runner = SessionRunner(player=player)

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
    runner = SessionRunner(player=player)
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
    runner = SessionRunner(player=player)
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
    runner = SessionRunner(player=player)
    task = asyncio.ensure_future(runner.start(n_battles=5))
    await asyncio.sleep(0)
    await runner.stop()
    player.finish_current_battle()
    await asyncio.wait_for(task, timeout=1.0)
    assert player.ladder_calls == [1]
