"""`create_app`: ensamblado de la API de control de Fase 3 (spec S3.3, S7,
D65 MON-33 Task 4).

Bindea SOLO loopback (D65 S6.3): `LOOPBACK_HOST` es la unica constante que
`build_uvicorn_config` acepta como host, sin parametro para otro valor. El
estado vivo de una decision pendiente sale del `ApprovalRegistry` inyectado,
nunca de una consulta a `pending_decisions` (auditoria exclusiva de
`POKE_LOOP`, Task 3): esta API nunca importa `PendingDecisionRepository`.
Las lecturas historicas usan `historical_repo_factory`, que construye un
`ApiReadRepository` con un engine propio del loop de FastAPI (D65 S3.3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..db.model_repository import ModelRepository
from ..hitl.events import EventHub
from ..hitl.registry import ApprovalRegistry
from .routes import create_router
from .websockets import is_allowed_origin, register_websocket_routes

if TYPE_CHECKING:
    from ..db.api_read_repository import ApiReadRepository

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8420

_logger = logging.getLogger(__name__)


def create_app(
    *,
    registry: ApprovalRegistry,
    event_hub: EventHub,
    settings_repo: ModelRepository,
    historical_repo_factory: "Callable[[], ApiReadRepository]",
) -> FastAPI:
    app = FastAPI()
    app.state.registry = registry
    app.state.event_hub = event_hub
    app.state.settings_repo = settings_repo
    app.state.historical_repo_factory = historical_repo_factory

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
