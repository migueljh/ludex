"""`create_app` wiring y `ApiReadRepository` (spec Fase 3 S3.3/S7, D65
MON-33 Task 4).

Cubre: el ensamblado exacto de `create_app` (registry/event_hub/settings_repo/
historical_repo_factory en `app.state`, router y rutas WS registradas),
`GET /health`, que las lecturas historicas pasan por el factory inyectado
(nunca por `PendingDecisionRepository`, que ni siquiera se importa en este
paquete), el bind loopback-only de `build_uvicorn_config`, y el canario de
uso cross-loop tipado de `ApiReadRepository`.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from ludex_agent.api import routes as routes_module
from ludex_agent.api.app import LOOPBACK_HOST, build_uvicorn_config, create_app
from ludex_agent.config import Settings
from ludex_agent.db.api_read_repository import (
    ApiReadRepository,
    BattleSummary,
    CrossLoopRepositoryError,
)
from ludex_agent.db.model_repository import ProviderModel
from ludex_agent.hitl.events import EventHub
from ludex_agent.hitl.registry import ApprovalRegistry


class _FakeSettingsRepo:
    def __init__(self, flags: dict[str, bool] | None = None) -> None:
        self.flags = flags or {}

    async def active_selection(self):
        return ProviderModel("google", "gemini-test")

    def factory(self):
        return _FakeSettingsSession(self.flags)


class _FakeSettingsResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class _FakeSettingsSession:
    def __init__(self, flags: dict[str, bool]) -> None:
        self._flags = flags

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def execute(self, stmt, params):
        key = params["key"]
        row = (self._flags[key],) if key in self._flags else None
        return _FakeSettingsResult(row)

    async def commit(self) -> None:
        pass


class _RecordingLadderPlayer:
    """Cuenta la actividad de red de `ladder` (llamada, socket, `/search`).
    Con `hold=True` queda retenida en la batalla en curso hasta
    `finish_current_battle`; con `hold=False` retorna de inmediato, para que
    una mutacion de un interlock falle en rojo sin colgarse."""

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


_ACCEPTANCE_DSN = "postgresql+asyncpg://ludex:ludex@127.0.0.1:9999/acceptance"
_CANONICAL_DSN = "postgresql+asyncpg://ludex:ludex@127.0.0.1:15432/ludex"
_LADDER_ACCEPTANCE_FORMAT_ENV = "LADDER_ACCEPTANCE_FORMAT"
_OPEN_FLAGS = {"ladder_enabled": True, "testing_account_confirmed": True}


def _open_settings(**overrides) -> Settings:
    base = dict(
        database_url=_ACCEPTANCE_DSN,
        connection_mode="official",
        database_role="acceptance",
        showdown_ws_url="ws://localhost:8100/showdown/websocket",
        showdown_battle_format="gen6randombattle",
        bot_username="LudexBot",
        llm_provider="google",
        llm_model="gemini-test",
        llm_api_key_env="GEMINI_API_KEY",
        llm_api_keys_env=None,
        llm_base_url=None,
        llm_provider_chain=(),
        llm_request_timeout_seconds=30,
        decision_budget_seconds=240,
        approval_timeout_seconds=10,
        send_margin_seconds=5,
        showdown_turn_limit_seconds=300,
        battle_timeout_seconds=300,
    )
    base.update(overrides)
    return Settings(**base)


class _SlowFlagSettingsResult(_FakeSettingsResult):
    pass


class _SlowFlagSettingsSession:
    """Igual que `_FakeSettingsSession` pero `execute` hace un `await`
    deliberado (ventana de interleaving) para hacer determinista la carrera
    TOCTOU de T-01 entre dos `POST /sessions` concurrentes."""

    def __init__(self, flags: dict[str, bool], delay: float) -> None:
        self._flags = flags
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def execute(self, stmt, params):
        await asyncio.sleep(self._delay)
        key = params["key"]
        row = (self._flags[key],) if key in self._flags else None
        return _SlowFlagSettingsResult(row)

    async def commit(self) -> None:
        pass


class _SlowFlagSettingsRepo(_FakeSettingsRepo):
    def __init__(self, flags: dict[str, bool], delay: float = 0.05) -> None:
        super().__init__(flags)
        self._delay = delay

    def factory(self):
        return _SlowFlagSettingsSession(self.flags, self._delay)


class _FakeReadRepository:
    def __init__(self, battle: BattleSummary | None) -> None:
        self._battle = battle
        self.calls = 0

    async def get_battle_by_tag(self, battle_tag: str) -> BattleSummary | None:
        self.calls += 1
        return self._battle


def _app(*, read_repo=None, settings_repo=None, settings_flags=None):
    registry = ApprovalRegistry()
    event_hub = EventHub()
    settings_repo = settings_repo or _FakeSettingsRepo(settings_flags)
    factory = (lambda: read_repo) if read_repo is not None else (lambda: _FakeReadRepository(None))
    app = create_app(
        registry=registry, event_hub=event_hub,
        settings_repo=settings_repo, historical_repo_factory=factory,
    )
    return app, registry, event_hub


def test_create_app_wires_state_from_injected_dependencies():
    registry = ApprovalRegistry()
    event_hub = EventHub()
    settings_repo = _FakeSettingsRepo()
    read_repo = _FakeReadRepository(None)

    app = create_app(
        registry=registry, event_hub=event_hub, settings_repo=settings_repo,
        historical_repo_factory=lambda: read_repo,
    )

    assert app.state.registry is registry
    assert app.state.event_hub is event_hub
    assert app.state.settings_repo is settings_repo
    assert app.state.historical_repo_factory() is read_repo
    # D65 S3.3 (T-03): UNA instancia por app/loop, memoizada y cerrada por
    # el lifespan de la app, no una factory corrida por request.
    assert app.state.historical_repo_provider.get_repo() is read_repo
    assert app.state.historical_repo_provider.get_repo() is read_repo


def test_create_app_includes_the_rest_router():
    # OpenAPI es el contrato publico y estable de rutas montadas; los
    # internals de como FastAPI 0.141 representa un router incluido en
    # `app.routes` no son API publica y no hay que acoplarse a ellos.
    app, _, _ = _app()
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert "/battles/{battle_tag}/decisions/{decision_index}/approve" in paths
    assert "/battles/{battle_tag}/decisions/{decision_index}/override" in paths


def test_create_app_registers_websocket_routes():
    app, _, _ = _app()
    ws_paths = {
        route.path for route in app.routes
        if getattr(route, "path", None) and "/ws/" in route.path
    }
    assert "/ws/battle/{battle_tag}" in ws_paths
    assert "/ws/lobby" in ws_paths


def test_health_endpoint_ok():
    app, _, _ = _app()
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_battle_read_goes_through_the_injected_historical_factory():
    battle = BattleSummary(
        battle_tag="battle-42", format="gen6randombattle", p1="A", p2="B",
        winner=None, played_by="bot", source="test",
    )
    read_repo = _FakeReadRepository(battle)
    app, _, _ = _app(read_repo=read_repo)
    client = TestClient(app)

    response = client.get("/battles/battle-42")

    assert response.status_code == 200
    assert response.json()["battle_tag"] == "battle-42"
    assert read_repo.calls == 1


def test_historical_repository_is_memoized_per_app_and_closed_on_shutdown():
    """T-03: la factory se invoca UNA vez por app (en el primer GET, dentro
    del loop de FastAPI) y el lifespan de la app cierra esa instancia con
    `aclose`. Dos GET usan la misma instancia; shutdown cierra una sola."""
    state = {"factory_calls": 0, "closed": 0}

    class _ClosingReadRepository(_FakeReadRepository):
        async def aclose(self) -> None:
            state["closed"] += 1

    def factory():
        state["factory_calls"] += 1
        return _ClosingReadRepository(None)

    app = create_app(
        registry=ApprovalRegistry(), event_hub=EventHub(),
        settings_repo=_FakeSettingsRepo(), historical_repo_factory=factory,
    )

    with TestClient(app) as client:
        first = client.get("/battles/battle-1")
        second = client.get("/battles/battle-2")
        assert first.status_code == 404
        assert second.status_code == 404
        assert state["factory_calls"] == 1
        assert state["closed"] == 0

    assert state["closed"] == 1


def test_get_battles_lists_recent_battles_from_the_injected_repository():
    battle_2 = BattleSummary(
        battle_tag="battle-2", format="gen6randombattle", p1="C", p2="D",
        winner=None, played_by="bot", source="test",
    )
    battle_1 = BattleSummary(
        battle_tag="battle-1", format="gen6randombattle", p1="A", p2="B",
        winner="A", played_by="bot", source="test",
    )

    class _ListReadRepository(_FakeReadRepository):
        async def list_recent_battles(self, limit: int = 50):
            return [battle_2, battle_1]

    read_repo = _ListReadRepository(None)
    app, _, _ = _app(read_repo=read_repo)
    client = TestClient(app)

    response = client.get("/battles")

    assert response.status_code == 200
    body = response.json()
    assert [b["battle_tag"] for b in body] == ["battle-2", "battle-1"]
    assert body[1]["winner"] == "A"


def test_routes_module_never_imports_pending_decision_repository():
    """Task 4 nunca usa `PendingDecisionRepository` (Task 3): el estado
    vivo sale del registry, las lecturas historicas de `ApiReadRepository`.
    Chequeo por simbolo importado (no substring), para no reaccionar a la
    mencion en la documentacion del propio modulo."""
    assert not hasattr(routes_module, "PendingDecisionRepository")
    assert "ludex_agent.db.pending_repository" not in {
        getattr(value, "__module__", None) for value in vars(routes_module).values()
    }


def test_the_whole_api_surface_never_imports_pending_decision_repository():
    """El contrato S3.3 aplica a TODA la superficie de la API, no solo al
    router: ni `app.py`, ni `schemas.py`, ni `websockets.py`, ni
    `api_read_repository.py` pueden importar el unico escritor de
    `pending_decisions` (vive exclusivamente dentro de `POKE_LOOP`)."""
    import ludex_agent.api.app as app_module
    import ludex_agent.api.schemas as schemas_module
    import ludex_agent.api.websockets as websockets_module
    from ludex_agent.db import api_read_repository as read_module

    for module in (
        app_module, routes_module, schemas_module, websockets_module, read_module,
    ):
        assert not hasattr(module, "PendingDecisionRepository"), module.__name__
        assert "ludex_agent.db.pending_repository" not in {
            getattr(value, "__module__", None)
            for value in vars(module).values()
        }, module.__name__


def test_build_uvicorn_config_binds_only_loopback():
    app, _, _ = _app()
    config = build_uvicorn_config(app, port=8888)
    assert config.host == LOOPBACK_HOST == "127.0.0.1"
    assert config.port == 8888


# ---------------------------------------------------------------------------
# ApiReadRepository: cross-loop typed failure (D65 S3.3)
# ---------------------------------------------------------------------------


def test_cross_loop_repository_use_raises_typed_error():
    """El engine se bindea al loop del PRIMER uso; un segundo uso desde otro
    loop de asyncio tiene que fallar con un tipo propio, no con el error
    generico de asyncio/asyncpg ("Future attached to a different loop")."""
    repo = ApiReadRepository(
        "postgresql+asyncpg://ludex:ludex@127.0.0.1:15432/ludex_test_fake"
    )

    async def bind_to_current_loop() -> None:
        # create_async_engine es perezoso: no abre conexion real, asi que
        # este test no necesita una base viva para ejercer el guardia.
        repo._ensure_factory()

    asyncio.run(bind_to_current_loop())

    with pytest.raises(CrossLoopRepositoryError) as exc_info:
        asyncio.run(bind_to_current_loop())

    assert exc_info.value.created_loop_id != exc_info.value.used_loop_id


def test_repository_pins_the_binding_loop_by_strong_reference(monkeypatch):
    """D66 R2: la guardia cross-loop es inmune al reciclado de direcciones
    de CPython. `asyncio.run` reusa el mismo address para loops sucesivos
    (verificado en este worktree: ids identicos en varias corridas), asi
    que una guardia que compare SOLO por `id()` trata como "el mismo loop"
    a un loop nuevo creado donde murio el anterior y deja pasar el uso
    cross-loop en silencio.

    Este canario es DETERMINISTA y pinea el mecanismo: el repo tiene que
    guardar una REFERENCIA FUERTE al loop de creacion (`repo._loop is
    loop_a`, que impide que su address se recicle mientras el repo vive) y
    el segundo uso desde otro loop tiene que fallar tipado aunque ambos
    objetos reportaran el mismo `id()`."""
    repo = ApiReadRepository(
        "postgresql+asyncpg://ludex:ludex@127.0.0.1:15432/ludex_test_fake"
    )

    class _FakeLoop:
        pass

    loop_a = _FakeLoop()
    loop_b = _FakeLoop()
    loops = iter([loop_a, loop_b])
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: next(loops))

    # create_async_engine es perezoso: no abre conexion real.
    repo._ensure_factory()  # bindea loop_a
    assert repo._loop is loop_a

    with pytest.raises(CrossLoopRepositoryError) as exc_info:
        repo._ensure_factory()  # loop_b: OTRO objeto, no el loop de creacion

    assert exc_info.value.created_loop_id != exc_info.value.used_loop_id


async def test_api_read_repository_reads_battles_from_a_real_disposable_db():
    """Sin fixture compartida: `tests/api/` no tiene conftest propio (Task 4
    solo puede tocar los 13 paths listados), asi que este test arma su
    propia base descartable con el mismo helper que usa `tests/db/`
    (`_disposable`, expuesto globalmente por `tests/conftest.py`)."""
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip(
            "necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)"
        )
    import asyncpg
    from _disposable import disposable_database

    async with disposable_database(base) as url:
        asyncpg_url = url
        engine_url = url.replace("postgres://", "postgresql+asyncpg://", 1).replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )

        conn = await asyncpg.connect(asyncpg_url)
        try:
            await conn.execute(
                """
                INSERT INTO battles (battle_tag, identity_key, format, p1, p2,
                                      winner, played_by, source)
                VALUES ('battle-read-1', 'ps-open-v1:sha256:read1',
                        'gen6randombattle', 'A', 'B', 'A', 'bot', 'test')
                """
            )
        finally:
            await conn.close()

        repo = ApiReadRepository(engine_url)
        try:
            battle = await repo.get_battle_by_tag("battle-read-1")
            assert battle is not None
            assert battle.winner == "A"
            assert battle.source == "test"

            missing = await repo.get_battle_by_tag("battle-does-not-exist")
            assert missing is None
        finally:
            await repo.aclose()


async def test_settings_model_validation_runs_against_the_real_db():
    """`PATCH /settings/model` valida contra la DB REAL (F2-09): un model
    que no existe para el provider es `422 INVALID_MODEL_SELECTION` y no
    toca `settings`; uno valido se persiste y queda visible en el GET.

    Sin fixture compartida (mismo motivo que el test de arriba): base
    descartable propia sobre `TEST_DATABASE_URL`."""
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip(
            "necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)"
        )
    import asyncpg
    from _disposable import disposable_database
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from ludex_agent.db.model_repository import ModelRepository

    class _LazySessionFactory:
        """Crea el engine en el PRIMER uso, dentro del loop que este
        corriendo en ese momento -- el patron de produccion (D65 S3.3):
        `TestClient` corre la app en un hilo con su propio loop de asyncio,
        asi que crear el engine aca (loop del test) cruzaria loops."""

        def __init__(self, database_url: str) -> None:
            self._url = database_url
            self._factory = None

        def __call__(self):
            if self._factory is None:
                engine = create_async_engine(self._url, poolclass=NullPool)
                self._factory = async_sessionmaker(
                    engine, expire_on_commit=False,
                )
            return self._factory()

    async with disposable_database(base) as url:
        asyncpg_url = url
        engine_url = url.replace("postgres://", "postgresql+asyncpg://", 1).replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )

        conn = await asyncpg.connect(asyncpg_url)
        try:
            provider_id = await conn.fetchval(
                "INSERT INTO providers (name, base_url, api_key_env, enabled) "
                "VALUES ('google', 'https://example.invalid', 'GOOGLE_API_KEY', true) "
                "RETURNING id"
            )
            await conn.execute(
                "INSERT INTO models (provider_id, model_id, label, is_default, enabled) "
                "VALUES ($1, 'gemini-test', 'Gemini Test', true, true)",
                provider_id,
            )
        finally:
            await conn.close()

        settings_repo = ModelRepository(_LazySessionFactory(engine_url))
        app = create_app(
            registry=ApprovalRegistry(),
            event_hub=EventHub(),
            settings_repo=settings_repo,
            historical_repo_factory=lambda: _FakeReadRepository(None),
        )
        client = TestClient(app)

        invalid = client.patch(
            "/settings/model",
            json={"provider": "google", "model": "does-not-exist"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["error"] == "INVALID_MODEL_SELECTION"

        valid = client.patch(
            "/settings/model",
            json={"provider": "google", "model": "gemini-test"},
        )
        assert valid.status_code == 200
        assert valid.json() == {"provider": "google", "model": "gemini-test"}

        current = client.get("/settings/model")
        assert current.status_code == 200
        assert current.json() == {"provider": "google", "model": "gemini-test"}


async def test_settings_hitl_providers_models_and_battles_routes_against_the_real_db():
    """T-02 sobre la DB real: `GET /providers` (solo name+enabled),
    `GET /models`, `PATCH /settings/hitl` persistiendo en la tabla
    `settings` (mismo store que `active_model`, F2-09), `GET /settings` y
    `GET /battles` via el `ApiReadRepository` real, todo dentro del
    lifespan de la app (que hace `aclose` al salir)."""
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip(
            "necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)"
        )
    import asyncpg
    from _disposable import disposable_database
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from ludex_agent.db.model_repository import ModelRepository

    class _LazySessionFactory:
        def __init__(self, database_url: str) -> None:
            self._url = database_url
            self._factory = None

        def __call__(self):
            if self._factory is None:
                engine = create_async_engine(self._url, poolclass=NullPool)
                self._factory = async_sessionmaker(
                    engine, expire_on_commit=False,
                )
            return self._factory()

    async with disposable_database(base) as url:
        asyncpg_url = url
        engine_url = url.replace("postgres://", "postgresql+asyncpg://", 1).replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )

        conn = await asyncpg.connect(asyncpg_url)
        try:
            provider_id = await conn.fetchval(
                "INSERT INTO providers (name, base_url, api_key_env, enabled) "
                "VALUES ('google', 'https://example.invalid', 'GOOGLE_API_KEY', true) "
                "RETURNING id"
            )
            await conn.execute(
                "INSERT INTO models (provider_id, model_id, label, is_default, enabled) "
                "VALUES ($1, 'gemini-test', 'Gemini Test', true, true)",
                provider_id,
            )
            await conn.execute(
                """
                INSERT INTO battles (battle_tag, identity_key, format, p1, p2,
                                      winner, played_by, source)
                VALUES ('battle-list-1', 'ps-open-v1:sha256:list1',
                        'gen6randombattle', 'A', 'B', 'A', 'bot', 'test')
                """
            )
        finally:
            await conn.close()

        settings_repo = ModelRepository(_LazySessionFactory(engine_url))
        read_repo = ApiReadRepository(engine_url)
        app = create_app(
            registry=ApprovalRegistry(),
            event_hub=EventHub(),
            settings_repo=settings_repo,
            historical_repo_factory=lambda: read_repo,
        )
        with TestClient(app) as client:
            providers = client.get("/providers")
            assert providers.status_code == 200
            assert providers.json() == [{"name": "google", "enabled": True}]

            models = client.get("/models")
            assert models.status_code == 200
            assert models.json() == [{"provider": "google", "model": "gemini-test"}]

            patch = client.patch(
                "/settings/hitl", json={"approval_mode": "autonomous"},
            )
            assert patch.status_code == 200
            assert patch.json()["approval_mode"] == "autonomous"

            settings = client.get("/settings")
            assert settings.status_code == 200
            assert settings.json()["approval_mode"] == "autonomous"
            assert settings.json()["active_model"] == {
                "provider": "google", "model": "gemini-test",
            }

            battles = client.get("/battles")
            assert battles.status_code == 200
            tags = [b["battle_tag"] for b in battles.json()]
            assert "battle-list-1" in tags


# --- Correccion puntual MON-36 R2 (T-03/T-04) ------------------------------
#
# Alcance NARROW: estos dos canarios HTTP son la unica adicion a este
# archivo en el ciclo de correccion de MON-36. Task 6 (connection/sessions,
# D66 T-02) no incluia `tests/api/test_app.py` en su lista de archivos
# autorizada y el brief original de la tarea prohibia agregar tests de API;
# el ciclo de correccion R2 pidio explicitamente estos dos canarios HTTP
# como la unica forma de probar T-03 (`POST /connection/connect` debe
# responder 422 `UNSAFE_OFFICIAL_DATABASE`, no 500) y T-04 (`DELETE
# /sessions/{id}` debe liberar `active` para que una sesion nueva pueda
# arrancar). No se toco ninguna otra parte de este archivo.


def test_connect_with_unsafe_official_database_returns_422_not_500(monkeypatch):
    """T-03: antes de este fix, `load_settings()` levantaba un
    `RuntimeError` sin capturar (official sin `DATABASE_ROLE=acceptance`) y
    FastAPI lo devolvia como 500 `INTERNAL_ERROR`, dejando el 422
    documentado (D68) inalcanzable."""
    app, _, _ = _app()

    def _unsafe_settings():
        raise RuntimeError(
            "CONNECTION_MODE=official exige DATABASE_ROLE=acceptance"
        )

    monkeypatch.setattr(routes_module, "load_settings", _unsafe_settings)

    with TestClient(app) as client:
        response = client.post("/connection/connect")
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "UNSAFE_OFFICIAL_DATABASE"


async def test_session_delete_frees_active_for_a_new_session(monkeypatch):
    """T-04 (adaptado a Task 8): `DELETE /sessions/{id}` hace stop-after-current
    real (nunca cancela la batalla en curso); el slot de matchmaking se libera
    cuando TERMINA la batalla en curso, no en el momento del DELETE. Antes del
    fix de T-04, `active` quedaba bloqueado para siempre y el POST posterior
    devolvia 409 `ACTIVE_MATCHMAKING` sin limite."""
    player = _RecordingLadderPlayer()
    app, _, _ = _app(settings_flags=dict(_OPEN_FLAGS))
    app.state.ladder_player = player
    monkeypatch.setattr(routes_module, "load_settings", lambda: _open_settings())
    monkeypatch.setenv(_LADDER_ACCEPTANCE_FORMAT_ENV, "gen6randombattle")

    with TestClient(app) as client:
        created = client.post("/sessions", json={"n_battles": 1, "confirm": True})
        assert created.status_code == 200
        session_id = created.json()["id"]

        second_attempt = client.post("/sessions", json={"n_battles": 1, "confirm": True})
        assert second_attempt.status_code == 409

        deleted = client.delete(f"/sessions/{session_id}")
        assert deleted.status_code == 200
        # stop-after-current: la batalla en curso sigue viva -> active True.
        assert deleted.json()["stop_requested"] is True
        assert deleted.json()["active"] is True

        player.finish_current_battle()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            reopened = client.post("/sessions", json={"n_battles": 1, "confirm": True})
            if reopened.status_code == 200:
                break
            await asyncio.sleep(0.01)
        assert reopened.status_code == 200


# --- Task 7 (MON-37/F3-07, D65 S5/S7.1): challenges -------------------------


def test_list_challenges_reflects_the_injected_gateway():
    from ludex_agent.showdown.challenge_gateway import InMemoryChallengeGateway

    gateway = InMemoryChallengeGateway()
    gateway.seed_incoming("rival1", "gen6ou")
    app, _, _ = _app()
    app.state.challenge_gateway = gateway

    with TestClient(app) as client:
        response = client.get("/challenges")
        assert response.status_code == 200
        assert response.json() == [{"user": "rival1", "format": "gen6ou"}]


def test_accept_unknown_challenge_returns_404():
    app, _, _ = _app()

    with TestClient(app) as client:
        response = client.post("/challenges/ghost/accept")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "UNKNOWN_CHALLENGE"


def test_accept_known_challenge_removes_it_from_the_list():
    from ludex_agent.showdown.challenge_gateway import InMemoryChallengeGateway

    gateway = InMemoryChallengeGateway()
    gateway.seed_incoming("rival1", "gen6ou")
    app, _, _ = _app()
    app.state.challenge_gateway = gateway

    with TestClient(app) as client:
        accepted = client.post("/challenges/rival1/accept")
        assert accepted.status_code == 200
        assert accepted.json() == {"user": "rival1", "action": "accept"}

        remaining = client.get("/challenges")
        assert remaining.json() == []


def test_reject_unknown_challenge_returns_404():
    app, _, _ = _app()

    with TestClient(app) as client:
        response = client.post("/challenges/ghost/reject")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "UNKNOWN_CHALLENGE"


def test_reject_known_challenge_removes_it_from_the_list():
    from ludex_agent.showdown.challenge_gateway import InMemoryChallengeGateway

    gateway = InMemoryChallengeGateway()
    gateway.seed_incoming("rival1", "gen6ou")
    app, _, _ = _app()
    app.state.challenge_gateway = gateway

    with TestClient(app) as client:
        rejected = client.post("/challenges/rival1/reject")
        assert rejected.status_code == 200
        assert rejected.json() == {"user": "rival1", "action": "reject"}
        assert client.get("/challenges").json() == []


def test_send_outgoing_challenge_echoes_the_request():
    app, _, _ = _app()

    with TestClient(app) as client:
        response = client.post(
            "/challenges/outgoing", json={"user": "rival1", "format": "gen6ou"},
        )
        assert response.status_code == 200
        assert response.json() == {"user": "rival1", "action": "outgoing"}


def test_create_app_defaults_to_an_in_memory_challenge_gateway():
    from ludex_agent.showdown.challenge_gateway import InMemoryChallengeGateway

    app, _, _ = _app()
    assert isinstance(app.state.challenge_gateway, InMemoryChallengeGateway)


# --- Task 8 (MON-38/F3-08, D65 S6.2): ladder sessions fail-closed -----------


def _ladder_app(monkeypatch, *, flags, settings_overrides=None, player=True, hold=True):
    player_ = _RecordingLadderPlayer(hold=hold) if player else None
    app, _, _ = _app(settings_flags=flags)
    if player_ is not None:
        app.state.ladder_player = player_
    monkeypatch.setattr(
        routes_module, "load_settings",
        lambda: _open_settings(**(settings_overrides or {})),
    )
    monkeypatch.setenv(_LADDER_ACCEPTANCE_FORMAT_ENV, "gen6randombattle")
    return app, player_


@pytest.mark.parametrize(
    "flags,settings_overrides,payload",
    [
        (dict(_OPEN_FLAGS), {"connection_mode": "local"}, {"n_battles": 1, "confirm": True}),
        ({"ladder_enabled": False, "testing_account_confirmed": True}, None, {"n_battles": 1, "confirm": True}),
        (dict(_OPEN_FLAGS), None, {"n_battles": 1}),
        ({"ladder_enabled": True, "testing_account_confirmed": False}, None, {"n_battles": 1, "confirm": True}),
        (dict(_OPEN_FLAGS), {"database_url": _CANONICAL_DSN}, {"n_battles": 1, "confirm": True}),
        (dict(_OPEN_FLAGS), {"showdown_battle_format": "gen6ou"}, {"n_battles": 1, "confirm": True}),
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
def test_ladder_session_interlock_fails_closed_with_zero_network_calls(
    monkeypatch, flags, settings_overrides, payload,
):
    """D65 canario 30 a nivel de ruta: al faltar un interlock, `POST
    /sessions` responde 422 `LADDER_INTERLOCK` y el player nunca abre socket
    ni envia `/search`."""
    app, player = _ladder_app(
        monkeypatch, flags=flags, settings_overrides=settings_overrides,
        hold=False,
    )

    with TestClient(app) as client:
        response = client.post("/sessions", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "LADDER_INTERLOCK"
    assert player.ladder_calls == []
    assert player.socket_opens == 0
    assert player.search_sends == []


def test_ladder_session_without_a_wired_player_fails_closed(monkeypatch):
    """Sin `app.state.ladder_player` no existe camino de red: fail-closed."""
    app, player = _ladder_app(
        monkeypatch, flags=dict(_OPEN_FLAGS), player=False,
    )

    with TestClient(app) as client:
        response = client.post("/sessions", json={"n_battles": 1, "confirm": True})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "LADDER_INTERLOCK"


def test_ladder_session_with_unsafe_official_database_returns_422_not_500(monkeypatch):
    """`load_settings()` rechaza official+canonical como `RuntimeError`; la ruta
    lo mapea a 422 (nunca 500) igual que `/connection/connect` (T-03)."""
    app, player = _ladder_app(
        monkeypatch, flags=dict(_OPEN_FLAGS), hold=False,
    )

    def _unsafe_settings():
        raise RuntimeError(
            "CONNECTION_MODE=official no puede persistir en la base canónica de Ludex"
        )

    monkeypatch.setattr(routes_module, "load_settings", _unsafe_settings)

    with TestClient(app) as client:
        response = client.post("/sessions", json={"n_battles": 1, "confirm": True})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "UNSAFE_OFFICIAL_DATABASE"
    assert player.ladder_calls == []


async def test_open_ladder_interlocks_start_a_ladder_session(monkeypatch):
    """Step 2: con los seis interlocks abiertos, la sesion dispara
    `Player.ladder(1)` y queda marcada `source="ladder"`."""
    player = _RecordingLadderPlayer()
    app, _, _ = _app(settings_flags=dict(_OPEN_FLAGS))
    app.state.ladder_player = player
    monkeypatch.setattr(routes_module, "load_settings", lambda: _open_settings())
    monkeypatch.setenv(_LADDER_ACCEPTANCE_FORMAT_ENV, "gen6randombattle")

    with TestClient(app) as client:
        response = client.post("/sessions", json={"n_battles": 1, "confirm": True})
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "ladder"
        assert body["active"] is True

        deadline = time.monotonic() + 2
        while not player.ladder_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert player.ladder_calls == [1]

        player.finish_current_battle()
        deadline = time.monotonic() + 2
        while _session_slot_busy(client) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    assert player.ladder_calls == [1]
    assert player.socket_opens == 1
    assert player.search_sends == ["/search"]


async def test_ladder_disabled_after_a_session_blocks_subsequent_requests(monkeypatch):
    """Step 3 (off-after-session): al deshabilitar ladder, una solicitud nueva
    sin re-habilitar falla antes de red y no produce una segunda llamada."""
    flags = dict(_OPEN_FLAGS)
    player = _RecordingLadderPlayer()
    app, _, _ = _app(settings_flags=flags)
    app.state.ladder_player = player
    monkeypatch.setattr(routes_module, "load_settings", lambda: _open_settings())
    monkeypatch.setenv(_LADDER_ACCEPTANCE_FORMAT_ENV, "gen6randombattle")

    with TestClient(app) as client:
        first = client.post("/sessions", json={"n_battles": 1, "confirm": True})
        assert first.status_code == 200

        deadline = time.monotonic() + 2
        while not player.ladder_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert player.ladder_calls == [1]

        player.finish_current_battle()
        deadline = time.monotonic() + 2
        while _session_slot_busy(client) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        flags["ladder_enabled"] = False
        second = client.post("/sessions", json={"n_battles": 1, "confirm": True})

    assert second.status_code == 422
    assert second.json()["detail"]["error"] == "LADDER_INTERLOCK"
    assert player.ladder_calls == [1]
    assert player.socket_opens == 1
    assert player.search_sends == ["/search"]


def _session_slot_busy(client) -> bool:
    """Sondea el slot de matchmaking SIN efectos: un POST con `confirm`
    ausente falla por interlock (422) si el slot esta libre y devuelve 409 si
    sigue ocupado -- nunca dispara una sesion nueva."""
    probe = client.post("/sessions", json={"n_battles": 1})
    return probe.status_code == 409


# --- MON-38 R2 (T-01): reserva atomica del slot bajo concurrencia ------------


async def test_two_concurrent_session_requests_reserve_the_slot_atomically(monkeypatch):
    """T-01: dos `POST /sessions` concurrentes con gates abiertos y una
    ventana de `await` en la lectura de flags -> exactamente un 200, un 409,
    un `ladder(1)`, un socket y un `/search`.

    `httpx.ASGITransport` corre la app en el MISMO loop del test, y
    `_SlowFlagSettingsSession.execute` abre una ventana de interleaving
    deterministico entre el check de `active` y la reserva: sin reserva
    atomica, ambos requests observan `active=False` y disparan dos
    `Player.ladder(1)` (el bug que `TestClient` serial no ve)."""
    import httpx

    player = _RecordingLadderPlayer()  # hold=True: la primera batalla queda viva
    settings_repo = _SlowFlagSettingsRepo(dict(_OPEN_FLAGS), delay=0.05)
    app, _, _ = _app(settings_repo=settings_repo)
    app.state.ladder_player = player
    monkeypatch.setattr(routes_module, "load_settings", lambda: _open_settings())
    monkeypatch.setenv(_LADDER_ACCEPTANCE_FORMAT_ENV, "gen6randombattle")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            client.post("/sessions", json={"n_battles": 1, "confirm": True}),
            client.post("/sessions", json={"n_battles": 1, "confirm": True}),
        )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409]

    deadline = time.monotonic() + 2
    while not player.ladder_calls and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert player.ladder_calls == [1]
    assert player.socket_opens == 1
    assert player.search_sends == ["/search"]

    player.finish_current_battle()
    await asyncio.sleep(0.05)
