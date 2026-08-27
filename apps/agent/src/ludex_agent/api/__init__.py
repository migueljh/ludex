"""API de control loopback de Fase 3 (spec S3.3, S7, D65 MON-33 Task 4)."""

from __future__ import annotations

from .app import create_app
from .routes import create_router
from .websockets import register_websocket_routes

__all__ = ["create_app", "create_router", "register_websocket_routes"]
