"""`create_app`: ensamblado de la API de control de Fase 3 (spec S3.3, S7,
D65 MON-33 Task 4, D66 R2).

Bindea SOLO loopback (D65 S6.3): `LOOPBACK_HOST` es la unica constante que
`build_uvicorn_config` acepta como host, sin parametro para otro valor. El
estado vivo de una decision pendiente sale del `ApprovalRegistry` inyectado,
nunca de una consulta a `pending_decisions` (auditoria exclusiva de
`POKE_LOOP`, Task 3): esta API nunca importa `PendingDecisionRepository`.

Lecturas historicas (D65 S3.3, D66 T-03): `_LazyHistoricalRepoProvider`
memoiza UNA `ApiReadRepository` por app, creada perezosamente en el PRIMER
uso (asi el engine bindea al loop de FastAPI, no al loop de quien llamo
`create_app`) y el lifespan de la app la cierra con `aclose` al apagar.
Nunca se crea un repo/engine por request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..db.model_repository import ModelRepository
from ..hitl.events import EventHub
from ..hitl.registry import ApprovalRegistry
from ..showdown.challenge_gateway import ChallengeGateway, InMemoryChallengeGateway
from .routes import create_router
from .websockets import is_allowed_origin, register_websocket_routes

if TYPE_CHECKING:
    from ..db.api_read_repository import ApiReadRepository

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8420

_logger = logging.getLogger(__name__)


class _LazyHistoricalRepoProvider:
    """Dueno de la unica `ApiReadRepository` de la app (D66 T-03).

    La factory corre una sola vez, en el primer `get_repo()` -- dentro del
    loop de FastAPI, que es donde vive el engine (D65 S3.3). `aclose()`
    cierra la instancia si alguna vez se creo; lo invoca el lifespan."""

    def __init__(self, factory: "Callable[[], ApiReadRepository]") -> None:
        self._factory = factory
        self._repo: "ApiReadRepository | None" = None

    def get_repo(self) -> "ApiReadRepository":
        if self._repo is None:
            self._repo = self._factory()
        return self._repo

    async def aclose(self) -> None:
        repo, self._repo = self._repo, None
        if repo is not None:
            await repo.aclose()


def create_app(
    *,
    registry: ApprovalRegistry,
    event_hub: EventHub,
    settings_repo: ModelRepository,
    historical_repo_factory: "Callable[[], ApiReadRepository]",
    challenge_gateway: ChallengeGateway | None = None,
) -> FastAPI:
    historical_repo_provider = _LazyHistoricalRepoProvider(historical_repo_factory)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        await historical_repo_provider.aclose()

    app = FastAPI(lifespan=_lifespan)
    app.state.registry = registry
    app.state.event_hub = event_hub
    app.state.settings_repo = settings_repo
    app.state.historical_repo_factory = historical_repo_factory
    app.state.historical_repo_provider = historical_repo_provider
    app.state.challenge_gateway = (
        challenge_gateway if challenge_gateway is not None else InMemoryChallengeGateway()
    )

    @app.middleware("http")
    async def _enforce_loopback_origin(request: Request, call_next):
        if not is_allowed_origin(request.headers.get("origin")):
            return JSONResponse(
                {"error": "FORBIDDEN_ORIGIN"}, status_code=403,
            )
        return await call_next(request)

    @app.exception_handler(Exception)
    async def _sanitize_unhandled_errors(request: Request, exc: Exception) -> JSONResponse:
        # D65 S6.3: username/password nunca entran a settings, eventos NI
        # logs. `str(exc)` de un error de infraestructura (DB, DSN) puede
        # traer credenciales embebidas -- nunca se loguea ni se devuelve al
        # cliente, solo el tipo y la ruta.
        _logger.error(
            "unhandled %s in %s", type(exc).__name__, request.url.path,
        )
        return JSONResponse({"error": "INTERNAL_ERROR"}, status_code=500)

    app.include_router(create_router())
    register_websocket_routes(app)
    return app


def build_uvicorn_config(app: FastAPI, *, port: int = DEFAULT_PORT):
    """Config lista para `uvicorn.Server`; nunca acepta otro host que
    `LOOPBACK_HOST` (D65 S6.3: la API bindea solo loopback)."""
    import uvicorn

    return uvicorn.Config(app, host=LOOPBACK_HOST, port=port)
