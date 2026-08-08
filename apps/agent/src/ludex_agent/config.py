"""Configuracion por variables de entorno. Sin logica de dominio."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
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
    showdown_turn_limit_seconds: float


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
    turn_limit = _positive_number(
        env, "LUDEX_SHOWDOWN_TURN_LIMIT_SECONDS", 300,
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
        showdown_turn_limit_seconds=turn_limit,
    )
