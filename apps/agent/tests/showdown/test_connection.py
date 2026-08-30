"""RED tests for Task 6: ConnectionManager + LoginWatchdog (MON-36 S6).

Cubre: configuracion mode-aware, rechazo de DB insegura ANTES de construir
el socket, login invalido convertido en fallo tipado dentro de la ventana
del watchdog (reloj falso), y login prohibido durante `choose_move`.
"""

from __future__ import annotations

import asyncio

import pytest

from ludex_agent.config import Settings
from ludex_agent.showdown.connection import (
    ConnectionManager,
    LoginFailedError,
    LoginWatchdog,
    UnsafeOfficialDatabaseError,
)


def _settings(**overrides: object) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:15432/ludex",
        connection_mode="local",
        database_role="canonical",
        showdown_ws_url="ws://localhost:8100/showdown/websocket",
        showdown_battle_format="gen6randombattle",
        bot_username="LudexBot",
        llm_provider="google",
        llm_model="",
        llm_api_key_env="GEMINI_API_KEY",
        llm_api_keys_env="GEMINI_API_KEYS",
        llm_base_url=None,
        llm_provider_chain=(),
        llm_request_timeout_seconds=30.0,
        decision_budget_seconds=240.0,
        approval_timeout_seconds=10.0,
        send_margin_seconds=5.0,
        showdown_turn_limit_seconds=300.0,
        battle_timeout_seconds=300.0,
    )
    base.update(overrides)
    return Settings(**base)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_local_mode_builds_local_server_configuration() -> None:
    manager = ConnectionManager(settings=_settings(connection_mode="local"))
    config = manager.build_server_configuration()
    assert config.websocket_url == "ws://localhost:8100/showdown/websocket"


def test_official_mode_rejects_canonical_database_before_socket() -> None:
    """La Fase 3 exige DATABASE_ROLE=acceptance para official (D65 S5.4).

    El rechazo debe ocurrir ANTES de intentar construir cualquier socket:
    `build_server_configuration` nunca debe llamarse.
    """
    settings = _settings(connection_mode="official", database_role="canonical")
    manager = ConnectionManager(settings=settings)
    with pytest.raises(UnsafeOfficialDatabaseError):
        manager.build_server_configuration()


def test_official_mode_accepts_acceptance_database() -> None:
    """T-02 (MON-36 R2): asertar la URL oficial EXACTA, no solo `is not None`.

    Una mutacion que devuelva `local_server_configuration(...)` en la rama
    official pasaba en silencio con la asercion anterior (2/2 GREEN con el
    bug inyectado). La URL exacta es el unico canario que la detecta.
    """
    settings = _settings(
        connection_mode="official",
        database_role="acceptance",
        database_url="postgresql+asyncpg://u:p@127.0.0.1:5555/acceptance",
    )
    manager = ConnectionManager(settings=settings)
    config = manager.build_server_configuration()
    assert config.websocket_url == "wss://sim3.psim.us/showdown/websocket"


def test_official_mode_rejects_canonical_dsn_even_with_acceptance_role() -> None:
    """T-05 (MON-36 R2): el guardarraiel de socket reusa el MISMO chequeo
    de DSN canonico que `load_settings` (D65 S5.4), no solo `database_role`.

    Un `database_role=acceptance` mal configurado sobre el DSN canonico real
    de Ludex (127.0.0.1:15432/ludex) no puede colar un socket oficial.
    """
    settings = _settings(
        connection_mode="official",
        database_role="acceptance",
        database_url="postgresql+asyncpg://u:p@127.0.0.1:15432/ludex",
    )
    manager = ConnectionManager(settings=settings)
    with pytest.raises(UnsafeOfficialDatabaseError):
        manager.build_server_configuration()


@pytest.mark.asyncio
async def test_watchdog_reports_typed_failure_on_invalid_login() -> None:
    """Login invalido debe convertirse en fallo tipado dentro de 15s falsos.

    El watchdog observa `logged_in` (nunca se setea) y `_background_failure`
    de poke-env; si nunca resuelve, tiene que fallar acotado con un tipo
    reconocible, nunca colgarse.
    """
    clock = _FakeClock()

    class _FakePokeClient:
        logged_in = asyncio.Event()

        def __init__(self) -> None:
            self._background_failure: asyncio.Future[Exception] = asyncio.get_event_loop().create_future()

        async def wait_for_background_failure(self) -> Exception:
            return await asyncio.shield(self._background_failure)

    fake_client = _FakePokeClient()
    watchdog = LoginWatchdog(
        timeout_seconds=15.0,
        clock=clock,
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    async def _tick_clock() -> None:
        for _ in range(20):
            await asyncio.sleep(0)
            clock.advance(1.0)

    ticker = asyncio.ensure_future(_tick_clock())
    with pytest.raises(LoginFailedError):
        await watchdog.wait_for_login(fake_client)
    await ticker


@pytest.mark.asyncio
async def test_watchdog_propagates_background_login_failure() -> None:
    """Si poke-env pierde el error de login en su task fire-and-forget,
    el watchdog tiene que republicarlo como fallo tipado, no perderlo.

    T-01 (MON-36 R2): `LudexPlayer._publish_background_failure`
    (client.py:1111-1113) PUBLICA el error como VALOR
    (`self._background_failure.set_result(exc)`), nunca lo levanta con
    `set_exception`. El fake tiene que reflejar EXACTAMENTE ese contrato
    real, o el canario no prueba nada: con `set_exception`, la task del
    watchdog terminaria en excepcion en vez de resultado, y un
    `failure_task.exception()` roto (que siempre da `None` contra la forma
    real de poke-env) pasaba igual porque el fake no coincidia con la
    produccion. Se asertea ademas el mensaje EXACTO preservado.
    """
    clock = _FakeClock()
    published = RuntimeError("bad password")

    class _FakePokeClient:
        logged_in = asyncio.Event()

        def __init__(self) -> None:
            self._background_failure: asyncio.Future[Exception] = asyncio.get_event_loop().create_future()
            self._background_failure.set_result(published)

        async def wait_for_background_failure(self) -> Exception:
            return await asyncio.shield(self._background_failure)

    watchdog = LoginWatchdog(
        timeout_seconds=15.0,
        clock=clock,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    with pytest.raises(LoginFailedError, match="bad password"):
        await watchdog.wait_for_login(_FakePokeClient())


@pytest.mark.asyncio
async def test_watchdog_forbids_choose_move_before_login_completes() -> None:
    """Login siempre debe completarse antes de cualquier `choose_move`
    (spec 6.3): pedir permiso de jugar sin login resuelto debe fallar."""
    clock = _FakeClock()
    watchdog = LoginWatchdog(
        timeout_seconds=15.0,
        clock=clock,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    with pytest.raises(LoginFailedError):
        watchdog.assert_login_complete_before_battle()


def test_connection_manager_never_stores_password() -> None:
    """La contrasena nunca entra al dataclass Settings, tabla settings,
    logs, eventos o artefactos (spec 6.3): ConnectionManager la lee del
    entorno solo al construir AccountConfiguration y no la retiene."""
    manager = ConnectionManager(
        settings=_settings(
            connection_mode="official",
            database_role="acceptance",
            database_url="postgresql+asyncpg://u:p@127.0.0.1:5555/acceptance",
        ),
        environ={
            "SHOWDOWN_OFFICIAL_USERNAME": "ludex-testing",
            "SHOWDOWN_OFFICIAL_PASSWORD": "s3cr3t",
        },
    )
    account = manager.build_account_configuration()
    assert account.username == "ludex-testing"
    assert not hasattr(manager, "password")
    other_fields = {k: v for k, v in manager.__dict__.items() if k != "environ"}
    assert "s3cr3t" not in repr(other_fields)
