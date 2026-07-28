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


def test_configura_proveedor_pool_base_url_y_cadena_sin_leer_claves():
    s = load_settings({
        "DATABASE_URL": "postgres://u:p@localhost:15432/db",
        "LUDEX_PROVIDER": "google",
        "LUDEX_MODEL": "gemini-test",
        "LUDEX_PROVIDER_CHAIN": "kimi,open_code_zen",
        "GEMINI_API_KEY": "secreto-principal",
        "GEMINI_API_KEYS": "secreto-2,secreto-3",
        "KIMI_BASE_URL": "https://api.moonshot.ai/v1",
        "OPEN_CODE_ZEN_BASE_URL": "https://opencode.ai/zen/v1",
        "OPEN_CODE_ZEN_MODEL": "claude-haiku-4-5",
    })

    assert s.llm_provider == "google"
    assert s.llm_model == "gemini-test"
    assert s.llm_api_key_env == "GEMINI_API_KEY"
    assert s.llm_api_keys_env == "GEMINI_API_KEYS"
    assert s.llm_base_url is None
    assert s.llm_provider_chain == ("kimi", "open_code_zen")
    assert "secreto" not in repr(s)


def test_kimi_usa_los_nombres_reales_y_base_url():
    s = load_settings({
        "DATABASE_URL": "postgres://u:p@localhost:15432/db",
        "LUDEX_PROVIDER": "kimi",
        "LUDEX_MODEL": "kimi-k2",
        "KIMI_BASE_URL": "https://api.moonshot.ai/v1",
    })

    assert s.llm_api_key_env == "KIMI_API_KEY"
    assert s.llm_api_keys_env is None
    assert s.llm_base_url == "https://api.moonshot.ai/v1"


def test_presupuesto_total_debe_quedar_debajo_del_reloj():
    with pytest.raises(RuntimeError, match="menor.*reloj"):
        load_settings({
            "DATABASE_URL": "postgres://u:p@localhost:15432/db",
            "LUDEX_DECISION_BUDGET_SECONDS": "300",
            "LUDEX_SHOWDOWN_TURN_LIMIT_SECONDS": "300",
        })


@pytest.mark.parametrize("value", ["0", "-1", "basura"])
def test_timeout_de_request_debe_ser_positivo(value):
    with pytest.raises(RuntimeError, match="LUDEX_LLM_REQUEST_TIMEOUT_SECONDS"):
        load_settings({
            "DATABASE_URL": "postgres://u:p@localhost:15432/db",
            "LUDEX_LLM_REQUEST_TIMEOUT_SECONDS": value,
        })


def test_rechaza_base_url_no_http():
    with pytest.raises(RuntimeError, match="KIMI_BASE_URL"):
        load_settings({
            "DATABASE_URL": "postgres://u:p@localhost:15432/db",
            "LUDEX_PROVIDER": "kimi",
            "KIMI_BASE_URL": "file:///tmp/socket",
        })
