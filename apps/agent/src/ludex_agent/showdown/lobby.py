"""Canal pre-lock de eventos de lobby (conexion/challenges/sesion).

`/ws/lobby` (spec 7.2) publica conexion, challenges, batallas y estado de
sesion. `LobbyInbox` es el mismo patron de ring buffer con `seq` monotono y
`resume`/`REPLAY_GAP` que ya usa el inbox de batalla (D31): nunca lee
`Battle` ni el `ProtocolRecorder`.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque


class LobbyInbox:
    class ReplayGapError(RuntimeError):
        """El cursor pedido ya rotó fuera del ring buffer."""

    def __init__(self, max_size: int = 500) -> None:
        self._max_size = max_size
        self._events: Deque[dict[str, Any]] = deque(maxlen=max_size)
        self._next_seq = 1
        self._oldest_dropped_seq = 0
        self._waiters: list[asyncio.Future[dict[str, Any]]] = []

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        if len(self._events) == self._max_size:
            oldest = self._events[0]
            self._oldest_dropped_seq = oldest["seq"]
        stamped = {**event, "seq": self._next_seq}
        self._next_seq += 1
        self._events.append(stamped)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(stamped)
        return stamped

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._events)

    def resume(self, last_seq: int) -> list[dict[str, Any]]:
        if last_seq < self._oldest_dropped_seq:
            raise LobbyInbox.ReplayGapError(
                f"last_seq={last_seq} ya rotó fuera del ring buffer "
                f"(oldest={self._oldest_dropped_seq})"
            )
        return [event for event in self._events if event["seq"] > last_seq]

    async def wait_for_next(self, after_seq: int) -> dict[str, Any]:
        pending = self.resume(after_seq)
        if pending:
            return pending[0]
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._waiters.append(waiter)
        return await waiter
