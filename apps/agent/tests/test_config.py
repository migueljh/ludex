import os
import pytest
from ludex_agent.config import Settings, load_settings


def test_lee_las_variables_de_entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:15432/db")
    monkeypatch.setenv("SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket")
    s = load_settings()
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.showdown_ws_url == "ws://localhost:8100/showdown/websocket"


def test_convierte_el_esquema_para_asyncpg(monkeypatch):
    # dbmate y el seed usan postgres://, SQLAlchemy async necesita el driver.
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:15432/db?sslmode=disable")
    s = load_settings()
    assert s.database_url == "postgresql+asyncpg://u:p@h:15432/db"


def test_falla_ruidosamente_sin_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        load_settings()
