"""Conexion oficial mode-aware y watchdog de login (Fase 3 Task 6, D65/D66).

`ConnectionManager` decide `local` vs `official` (spec 1.1) y rechaza
configuraciones de DB inseguras ANTES de construir cualquier socket (spec
5.4). El username/password oficiales salen del entorno solo al construir el
`AccountConfiguration`: nunca se retienen en el manager ni entran a
`Settings`/logs/eventos (spec 6.3).

`LoginWatchdog` observa `logged_in`/`_background_failure` de poke-env
(nunca `asyncio.wait_for` sobre el Future fuente, D65 S4.1) y falla acotado
dentro del presupuesto configurado en vez de esperar indefinidamente.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol

from poke_env import ServerConfiguration
from poke_env.ps_client.account_configuration import AccountConfiguration

from ludex_agent.config import Settings

_OFFICIAL_AUTH_URL = "https://play.pokemonshowdown.com/action.php?"
_OFFICIAL_WS_URL = "wss://sim3.psim.us/showdown/websocket"


class UnsafeOfficialDatabaseError(RuntimeError):
    """`CONNECTION_MODE=official` sobre una DB que no es `acceptance`."""


class LoginFailedError(RuntimeError):
    """Login invalido o vencido dentro de la ventana del watchdog."""


def local_server_configuration(ws_url: str) -> ServerConfiguration:
    return ServerConfiguration(ws_url, _OFFICIAL_AUTH_URL)


def official_server_configuration() -> ServerConfiguration:
    return ServerConfiguration(_OFFICIAL_WS_URL, _OFFICIAL_AUTH_URL)


@dataclass
class ConnectionManager:
    """Arma la configuracion de servidor/cuenta mode-aware para `LudexPlayer`."""

    settings: Settings
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def build_server_configuration(self) -> ServerConfiguration:
        if self.settings.connection_mode == "official":
            if self.settings.database_role != "acceptance":
                raise UnsafeOfficialDatabaseError(
                    "CONNECTION_MODE=official exige DATABASE_ROLE=acceptance "
                    f"(recibido {self.settings.database_role!r})"
                )
            return official_server_configuration()
        return local_server_configuration(self.settings.showdown_ws_url)

    def build_account_configuration(self) -> AccountConfiguration:
        """Lee username/password SOLO aca; el manager no los retiene."""
        if self.settings.connection_mode == "official":
            username = self.environ.get("SHOWDOWN_OFFICIAL_USERNAME", "").strip()
            password = self.environ.get("SHOWDOWN_OFFICIAL_PASSWORD") or None
            if not username:
                raise RuntimeError(
                    "SHOWDOWN_OFFICIAL_USERNAME es obligatorio en modo official"
                )
            return AccountConfiguration(username, password)
        return AccountConfiguration(self.settings.bot_username, None)


class _PokeLoginClient(Protocol):
    logged_in: object

    async def wait_for_background_failure(self) -> Exception:
        ...


@dataclass
class LoginWatchdog:
    """Observa el login de poke-env y falla tipado dentro de `timeout_seconds`.

    `clock`/`sleep` son inyectables (D42): produccion usa `time.monotonic`
    y `asyncio.sleep`; los tests usan un reloj falso avanzado a mano, para
    que el watchdog nunca dependa de tiempo de pared real.
    """

    timeout_seconds: float
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    _login_complete: bool = field(default=False, init=False)

    async def wait_for_login(self, poke_client: _PokeLoginClient) -> None:
        import asyncio

        deadline = self.clock() + self.timeout_seconds
        logged_in_task = asyncio.ensure_future(poke_client.logged_in.wait())
        failure_task = asyncio.ensure_future(
            poke_client.wait_for_background_failure()
        )
        try:
            while True:
                if logged_in_task.done():
                    self._login_complete = True
                    return
                if failure_task.done():
                    raise LoginFailedError(str(failure_task.exception()))
                if self.clock() >= deadline:
                    raise LoginFailedError(
                        f"Login no completo dentro de {self.timeout_seconds}s"
                    )
                await self.sleep(0)
        finally:
            for task in (logged_in_task, failure_task):
                if not task.done():
                    task.cancel()

    def assert_login_complete_before_battle(self) -> None:
        """Prohibe `choose_move` antes de que el login haya resuelto (spec 6.3)."""
        if not self._login_complete:
            raise LoginFailedError(
                "Login todavia no completo: choose_move esta prohibido"
            )
