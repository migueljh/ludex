"""Configuracion por variables de entorno. Sin logica de dominio."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Mapping
from urllib.parse import urlsplit, urlunsplit

# Fase 3 D65 (MON-31): dos ejes ortogonales de runtime, ninguno inferido del
# otro. `local` siempre saltea el gate humano en produccion; `official` exige
# `acceptance` (ver guardarraiel de _reject_unsafe_official_database).
ConnectionMode = Literal["local", "official"]
DatabaseRole = Literal["canonical", "acceptance"]

_CONNECTION_MODES: tuple[ConnectionMode, ...] = ("local", "official")
_DATABASE_ROLES: tuple[DatabaseRole, ...] = ("canonical", "acceptance")

# DSN de la base canonica que levanta docker-compose (.env.example,
# POSTGRES_DB=ludex sobre 127.0.0.1:15432). Ambas grafias de loopback
# cuentan: AGENTS.md documenta un Postgres nativo que alguna vez ensombrecio
# este puerto sin fallar.
_CANONICAL_DB_HOSTS = {"127.0.0.1", "localhost"}
_CANONICAL_DB_PORT = 15432
_CANONICAL_DB_NAME = "ludex"


@dataclass(frozen=True)
class Settings:
    database_url: str
    connection_mode: ConnectionMode
    database_role: DatabaseRole
    showdown_ws_url: str
    showdown_battle_format: str
    bot_username: str
    llm_provider: str
    llm_model: str
    llm_api_key_env: str
    llm_api_keys_env: str | None
    llm_base_url: str | None
    llm_provider_chain: tuple[str, ...]
    llm_request_timeout_seconds: float
    decision_budget_seconds: float
    approval_timeout_seconds: float
    send_margin_seconds: float
    showdown_turn_limit_seconds: float
    battle_timeout_seconds: float


_PROVIDERS: dict[str, tuple[str, str | None, str | None]] = {
    "google": ("GEMINI_API_KEY", "GEMINI_API_KEYS", None),
    "kimi": ("KIMI_API_KEY", None, "KIMI_BASE_URL"),
    "open_code_zen": (
        "OPEN_CODE_ZEN_API_KEY", None, "OPEN_CODE_ZEN_BASE_URL",
    ),
    "anthropic": ("ANTHROPIC_API_KEY", None, None),
    "openai": ("OPENAI_API_KEY", None, None),
}

# F2-09 (MON-14): catalogo publico (nombre -> (env var principal, pool de
# env vars, env var de base URL)). Es el bootstrap de la DB: `provider init`
# puebla `providers` desde aca con el NOMBRE de la env var, nunca el valor.
PROVIDER_CATALOG = dict(_PROVIDERS)


def _to_asyncpg(url: str) -> str:
    """dbmate y el seed usan `postgres://...?sslmode=disable`.

    SQLAlchemy async necesita el driver explicito, y asyncpg no entiende
    `sslmode` como parametro de query: lo rechaza. Se normaliza el esquema y
    se descarta la query.
    """
    parts = urlsplit(url)
    scheme = "postgresql+asyncpg"
    return urlunsplit((scheme, parts.netloc, parts.path, "", ""))


def _positive_number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} debe ser un numero positivo") from exc
    if value <= 0:
        raise RuntimeError(f"{name} debe ser un numero positivo")
    return value


def _is_canonical_dsn(raw_dsn: str) -> bool:
    parts = urlsplit(raw_dsn)
    return (
        parts.hostname in _CANONICAL_DB_HOSTS
        and parts.port == _CANONICAL_DB_PORT
        and parts.path.lstrip("/") == _CANONICAL_DB_NAME
    )


def _reject_unsafe_official_database(
    connection_mode: ConnectionMode, database_role: DatabaseRole, raw_dsn: str,
) -> None:
    """Guardarraiel D65 (spec S0 5.4): falla antes de abrir red o autenticar.

    `official` exige DATABASE_ROLE=acceptance y prohibe el DSN canonico
    de Ludex, sin flag de override en Fase 3. Se parsea el DSN ORIGINAL
    (antes de `_to_asyncpg`), porque asyncpg descarta netloc/path distinto.
    """
    if connection_mode != "official":
        return
    if database_role != "acceptance":
        raise RuntimeError(
            "CONNECTION_MODE=official exige DATABASE_ROLE=acceptance "
            f"(recibido DATABASE_ROLE={database_role!r})"
        )
    if _is_canonical_dsn(raw_dsn):
        raise RuntimeError(
            "CONNECTION_MODE=official no puede persistir en la base canónica "
            f"de Ludex ({_CANONICAL_DB_HOSTS!r}:{_CANONICAL_DB_PORT}/"
            f"{_CANONICAL_DB_NAME}). Fase 3 no tiene flag de override."
        )


def _optional_http_url(env: Mapping[str, str], name: str | None) -> str | None:
    if name is None:
        return None
    value = env.get(name, "").strip()
    if not value:
        return None
    if urlsplit(value).scheme not in {"http", "https"}:
        raise RuntimeError(f"{name} debe ser una URL http(s)")
    return value.rstrip("/")


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    raw = env.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("Falta DATABASE_URL. Copiar .env.example a .env.")
    connection_mode = env.get("CONNECTION_MODE", "local").strip().lower()
    if connection_mode not in _CONNECTION_MODES:
        raise RuntimeError(
            f"CONNECTION_MODE desconocido: {connection_mode!r}; "
            f"conocidos: {', '.join(_CONNECTION_MODES)}"
        )
    database_role = env.get("DATABASE_ROLE", "canonical").strip().lower()
    if database_role not in _DATABASE_ROLES:
        raise RuntimeError(
            f"DATABASE_ROLE desconocido: {database_role!r}; "
            f"conocidos: {', '.join(_DATABASE_ROLES)}"
        )
    _reject_unsafe_official_database(connection_mode, database_role, raw)
    provider = env.get("LUDEX_PROVIDER", "google").strip().lower()
    if provider not in _PROVIDERS:
        raise RuntimeError(
            f"LUDEX_PROVIDER desconocido: {provider!r}; "
            f"conocidos: {', '.join(_PROVIDERS)}"
        )
    key_env, keys_env, base_url_env = _PROVIDERS[provider]
    request_timeout = _positive_number(
        env, "LUDEX_LLM_REQUEST_TIMEOUT_SECONDS", 30,
    )
    decision_budget = _positive_number(
        env, "LUDEX_DECISION_BUDGET_SECONDS", 240,
    )
    approval_timeout = _positive_number(
        env, "LUDEX_APPROVAL_TIMEOUT_SECONDS", 10,
    )
    send_margin = _positive_number(
        env, "LUDEX_SEND_MARGIN_SECONDS", 5,
    )
    turn_limit = _positive_number(
        env, "LUDEX_SHOWDOWN_TURN_LIMIT_SECONDS", 300,
    )
    battle_timeout = _positive_number(
        env, "LUDEX_BATTLE_TIMEOUT_SECONDS", 300,
    )
    if decision_budget >= turn_limit:
        raise RuntimeError(
            "LUDEX_DECISION_BUDGET_SECONDS debe ser menor que el reloj "
            "LUDEX_SHOWDOWN_TURN_LIMIT_SECONDS"
        )
    chain = tuple(
        item.strip().lower()
        for item in env.get("LUDEX_PROVIDER_CHAIN", "").split(",")
        if item.strip()
    )
    unknown = [item for item in chain if item not in _PROVIDERS]
    if unknown:
        raise RuntimeError(
            f"LUDEX_PROVIDER_CHAIN contiene proveedores desconocidos: {unknown}"
        )
    model = env.get("LUDEX_MODEL", "").strip()
    if provider == "open_code_zen" and not model:
        model = env.get("OPEN_CODE_ZEN_MODEL", "").strip()
    return Settings(
        database_url=_to_asyncpg(raw),
        connection_mode=connection_mode,
        database_role=database_role,
        showdown_ws_url=env.get(
            "SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket"
        ),
        showdown_battle_format=env.get(
            "SHOWDOWN_BATTLE_FORMAT", "gen6randombattle"
        ),
        bot_username=env.get("SHOWDOWN_BOT_USERNAME", "LudexBot"),
        llm_provider=provider,
        llm_model=model,
        llm_api_key_env=key_env,
        llm_api_keys_env=keys_env,
        llm_base_url=_optional_http_url(env, base_url_env),
        llm_provider_chain=chain,
        llm_request_timeout_seconds=request_timeout,
        decision_budget_seconds=decision_budget,
        approval_timeout_seconds=approval_timeout,
        send_margin_seconds=send_margin,
        showdown_turn_limit_seconds=turn_limit,
        battle_timeout_seconds=battle_timeout,
    )
