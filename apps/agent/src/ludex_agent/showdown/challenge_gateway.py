"""Gateway de challenges para la superficie REST (Fase 3 Task 7, D65 S5/S7.1).

Desacopla `GET /challenges` y `POST /challenges/{user}/accept|reject` del
productor real (`LudexPlayer.incoming_challenges`, ver `showdown/client.py`).
`InMemoryChallengeGateway` es el default de `create_app` -- mismo patron que
`_connection_state`/`_session_state` de Task 6 (D66 T-02): ninguna conexion
ni challenge real todavia en esta rebanada, coherencia deliberada con el
comentario de `api/routes.py` ("challenges a S5/Task 7"). Wirear un gateway
respaldado por un `LudexPlayer` vivo queda para la rebanada que abra socket
(S9a, "aceptacion live de challenge en DB descartable").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class UnknownChallengeError(KeyError):
    """`accept`/`reject` sobre un usuario sin challenge entrante conocido."""


class ChallengeGateway(Protocol):
    async def list_incoming(self) -> dict[str, str]: ...

    async def accept(self, username: str) -> None: ...

    async def reject(self, username: str) -> None: ...


@dataclass
class InMemoryChallengeGateway:
    _incoming: dict[str, str] = field(default_factory=dict)

    def seed_incoming(self, username: str, format_: str) -> None:
        """Solo para tests: no hay ruta REST que popule challenges entrantes
        en esta rebanada (ver docstring del modulo)."""
        self._incoming[username] = format_

    async def list_incoming(self) -> dict[str, str]:
        return dict(self._incoming)

    async def accept(self, username: str) -> None:
        if username not in self._incoming:
            raise UnknownChallengeError(username)
        del self._incoming[username]

    async def reject(self, username: str) -> None:
        if username not in self._incoming:
            raise UnknownChallengeError(username)
        del self._incoming[username]
