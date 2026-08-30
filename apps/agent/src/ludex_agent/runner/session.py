"""Sesiones secuenciales con `N` configurable y stop-after-current.

Spec 7.1: "La sesion es secuencial, tiene `N` configurable y al cancelarse
termina la batalla actual antes de parar." Solo una solicitud de
matchmaking puede estar activa a la vez (`POST /sessions` -> 409 si ya hay
una corriendo); `DELETE /sessions/{id}` setea stop-after-current y JAMAS
cancela la task de la batalla en curso.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ActiveMatchmakingError(RuntimeError):
    """Ya hay una sesion de matchmaking activa; no se admite una segunda."""


class _LadderPlayer(Protocol):
    async def ladder(self, n_battles: int) -> None:
        ...


@dataclass
class SessionRunner:
    player: _LadderPlayer
    _active: bool = field(default=False, init=False)
    _stop_requested: bool = field(default=False, init=False)
    _battles_played: int = field(default=0, init=False)

    async def start(self, n_battles: int) -> None:
        if self._active:
            raise ActiveMatchmakingError(
                "Ya hay una sesion de matchmaking activa"
            )
        self._active = True
        self._stop_requested = False
        try:
            for _ in range(n_battles):
                if self._stop_requested:
                    break
                await self.player.ladder(1)
                self._battles_played += 1
        finally:
            self._active = False

    async def stop(self) -> None:
        """Marca stop-after-current. No cancela la batalla en curso."""
        self._stop_requested = True

    @property
    def battles_played(self) -> int:
        return self._battles_played
