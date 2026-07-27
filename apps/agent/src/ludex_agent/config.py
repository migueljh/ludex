"""Configuracion por variables de entorno. Sin logica de dominio."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
    showdown_ws_url: str
    showdown_battle_format: str
    bot_username: str


def _to_asyncpg(url: str) -> str:
    """dbmate y el seed usan `postgres://...?sslmode=disable`.

    SQLAlchemy async necesita el driver explicito, y asyncpg no entiende
    `sslmode` como parametro de query: lo rechaza. Se normaliza el esquema y
    se descarta la query.
    """
    parts = urlsplit(url)
    scheme = "postgresql+asyncpg"
    return urlunsplit((scheme, parts.netloc, parts.path, "", ""))


def load_settings() -> Settings:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("Falta DATABASE_URL. Copiar .env.example a .env.")
    return Settings(
        database_url=_to_asyncpg(raw),
        showdown_ws_url=os.environ.get(
            "SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket"
        ),
        showdown_battle_format=os.environ.get(
            "SHOWDOWN_BATTLE_FORMAT", "gen6randombattle"
        ),
        bot_username=os.environ.get("SHOWDOWN_BOT_USERNAME", "LudexBot"),
    )
