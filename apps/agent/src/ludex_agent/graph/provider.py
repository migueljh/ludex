"""Proveedores LLM: fallos de infraestructura separados del contrato semántico."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from anthropic import APIConnectionError as AnthropicAPIConnectionError
from openai import APIConnectionError as OpenAIAPIConnectionError
from pydantic import ValidationError

from .. import config as _config
from ..db.model_repository import ModelSelectionError

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class QuotaExceeded(ProviderError):
    """`retry_after`, cuando el proveedor lo informa, es el tiempo en
    segundos que `KeyRotatingProvider` debería esperar antes de reintentar
    ESTA clave (ver `_quota_retry_after_seconds`). `None` dice "el
    proveedor no lo dijo", y el llamador cae a su propio default."""

    def __init__(
        self,
        message: str = "provider quota exhausted",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientProviderError(ProviderError):
    pass


class FatalProviderError(ProviderError):
    pass


class ProviderPoolExhausted(ProviderError):
    pass


class CredentialRejected(ProviderError):
    """401 o 403 CON señal estructurada de rechazo de credencial.

    F2-10B (MON-20): un 401/403 especifico de UNA clave (vencida, revocada,
    sin permiso) no detiene la corrida: la clave se pone en CUARENTENA
    permanente para el proceso y se intenta la siguiente del pool. Un 403
    SIN señal estructurada de credencial (permiso de modelo/proyecto/region
    o error model-wide) es `FatalProviderError` y se detiene en la primera
    clave. El mensaje es fijo y no expone la clave; la clasificacion final
    si el pool se agota de credenciales es `ProviderPoolExhausted` (nunca
    un falso "incompatible").
    """


class DecisionDeadlineExceeded(ProviderError):
    pass


class ProviderSelectionError(ProviderError):
    """F2-09 (MON-14): no se pudo resolver la seleccion activa de
    provider/model (DB sin seleccion ni bootstrap, provider inexistente o
    deshabilitado, sin claves en el entorno)."""


class ProviderMixError(ProviderError):
    """F2-09 (MON-14): el benchmark fijo provider/model al inicio y una
    respuesta efectiva difirio del pin: la corrida debe abortar, jamas
    mezclar mediciones."""


@dataclass(frozen=True)
class CompletionUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    model: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.reasoning_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("token usage cannot be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")


@dataclass(frozen=True)
class ProviderCompletion:
    payload: dict[str, Any]
    usage: CompletionUsage


@dataclass(frozen=True)
class CompletionEnvelope:
    """Respuesta de una llamada logica al proveedor, inmutable y autocontenida.

    F2-08 (MON-13): `DecisionProvider.complete` devuelve un envelope por
    llamada con el payload, el provider/model EFECTIVOS (quien respondio de
    verdad, no quien se configuro) y la latencia de ESA llamada. Es la unica
    via de que la metadata de la decision llegue a `decide`: el patron
    rechazado `last_completion_info` (estado mutable compartido leido despues
    del await) cruza metadata entre batallas concurrentes -- el runner juega
    varias en paralelo en el mismo proceso, y la llamada lenta terminaria
    reportando la metadata de la rapida (ver test_provider.py,
    `test_envelopes_concurrentes_no_se_cruzan_metadata`).
    """

    payload: dict[str, Any]
    provider: str
    model: str
    usage: CompletionUsage
    latency_ms: float


@dataclass(frozen=True)
class ModelRoute:
    protocol: str
    temperature: float = 0.0
    thinking: str | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    endpoint: str | None = None
    structured_output: str | None = None


DEFAULT_MODEL_ROUTES = (
    Path(__file__).resolve().parents[3] / "evals" / "model-routes.json"
)


def load_model_routes(
    path: str | Path = DEFAULT_MODEL_ROUTES,
) -> dict[tuple[str, str], ModelRoute]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    routes: dict[tuple[str, str], ModelRoute] = {}
    for raw in document["routes"]:
        key = (raw["provider"], raw["model"])
        if key in routes:
            raise ValueError(f"ruta de modelo duplicada: {key}")
        protocol = raw["protocol"]
        if protocol not in {
            "chat_completions", "messages", "responses", "google",
        }:
            raise ValueError(f"protocolo de modelo desconocido: {protocol}")
        structured_output = raw.get("structured_output")
        if structured_output is not None and structured_output not in {
            "json_schema", "text_json",
        }:
            raise ValueError(
                f"método de structured output desconocido: {structured_output}"
            )
        endpoint = raw.get("endpoint")
        if endpoint is not None and not endpoint.startswith("http"):
            raise ValueError(f"endpoint inválido: {endpoint}")
        routes[key] = ModelRoute(
            protocol=protocol,
            temperature=float(raw.get("temperature", 0)),
            thinking=raw.get("thinking"),
            max_tokens=raw.get("max_tokens"),
            timeout_seconds=raw.get("timeout_seconds"),
            endpoint=endpoint,
            structured_output=structured_output,
        )
    return routes


def model_route(
    routes: Mapping[tuple[str, str], ModelRoute],
    provider: str,
    model: str,
) -> ModelRoute:
    try:
        return routes[(provider, model)]
    except KeyError:
        raise ValueError(
            f"modelo sin ruta explícita: {provider}/{model}"
        ) from None


def anthropic_sdk_base_url(base_url: str | None) -> str | None:
    """Adapta una base API completa al SDK, que agrega `/v1/messages`."""
    if base_url is None:
        return None
    return base_url.removesuffix("/").removesuffix("/v1")


def structured_output_method(kind: str, base_url: str | None) -> str:
    """Los gateways `messages` no necesariamente implementan tools."""
    if kind == "anthropic" and base_url is not None:
        return "text_json"
    return "json_schema"


def route_structured_output(
    route: ModelRoute | None, kind: str, base_url: str | None
) -> str:
    """F2-10B (MON-20): el metodo de structured output es metadata
    declarativa de la ruta (`structured_output`); si la ruta no lo declara,
    se deriva del protocolo: `responses` y los gateways `messages` usan JSON
    textual estricto (misma validacion semantica de D26), todo lo demas usa
    `json_schema` nativo."""
    if route is not None and route.structured_output is not None:
        return route.structured_output
    if route is not None and route.protocol == "responses":
        return "text_json"
    if kind == "anthropic" and base_url is not None:
        return "text_json"
    return "json_schema"


def text_json_payload(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"_invalid_response": content}
    if not isinstance(parsed, dict):
        return {"_invalid_response": content}
    return parsed


def message_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block["text"]
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "".join(texts)
    return ""


def provider_response_schema(response_schema: type) -> dict[str, Any]:
    """El backend estructura sintaxis; `decide` valida el contrato semántico."""
    return response_schema.model_json_schema()


class DecisionProvider(Protocol):
    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> CompletionEnvelope: ...


class ProviderBackend(Protocol):
    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> ProviderCompletion: ...


def _percentile(sorted_values: list[float], percentile: int) -> float:
    """Percentil lineal sobre una lista ya ordenada (0 < percentile < 100)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Fórmula de interpolación lineal: Excel/Numpy-compatible.
    rank = (percentile / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


class DecisionMetrics:
    def __init__(self) -> None:
        self._counts: dict[str, int | None] = {
            "turns_total": 0,
            "calls_total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "key_rotations": 0,
            "keys_quarantined": 0,
            "provider_switches": 0,
            "turns_quota_affected": 0,
            "turns_transient_affected": 0,
            "turns_deadline_affected": 0,
            "turns_model_invalid": 0,
            "turns_fallback": 0,
            # L-01 (R2): DOS poblaciones de latencia, jamas mezcladas. Una
            # completion (llamada a `provider.complete()`) y una decision
            # (end-to-end, retries incluidos) son muestras de poblaciones
            # distintas: una decision con una completion aporta
            # exactamente una muestra a cada una, nunca dos al mismo
            # contador. Los percentiles sin muestras son None (null en
            # artefactos y blanco en el ledger), nunca 0/0/0 comparable.
            "completion_latency_ms_count": 0,
            "completion_latency_ms_total": None,
            "completion_latency_ms_p50": None,
            "completion_latency_ms_p95": None,
            "completion_latency_ms_max": None,
            "decision_latency_ms_count": 0,
            "decision_latency_ms_total": None,
            "decision_latency_ms_p50": None,
            "decision_latency_ms_p95": None,
            "decision_latency_ms_max": None,
        }
        self._quota_turns: set[str] = set()
        self._transient_turns: set[str] = set()
        self._deadline_turns: set[str] = set()
        self._invalid_turns: set[str] = set()
        self._fallback_turns: set[str] = set()
        self._turns: set[str] = set()
        self._completion_latencies: list[float] = []
        self._decision_latencies: list[float] = []

    def turn(self, turn_id: str) -> None:
        if turn_id not in self._turns:
            self._turns.add(turn_id)
            self._counts["turns_total"] += 1

    def key_rotation(self) -> None:
        self._counts["key_rotations"] += 1

    def key_quarantine(self) -> None:
        """F2-10B (MON-20): una clave rechazada por 401/403 entra en
        cuarentena permanente para el proceso. Contador separado de
        `key_rotations` (429) para que el artefacto distinga rotacion por
        cuota de descarte de credencial."""
        self._counts["keys_quarantined"] += 1

    def usage(self, usage: CompletionUsage) -> None:
        self._counts["calls_total"] += 1
        self._counts["input_tokens"] += usage.input_tokens
        self._counts["output_tokens"] += usage.output_tokens
        self._counts["cached_input_tokens"] += usage.cached_input_tokens
        self._counts["reasoning_tokens"] += usage.reasoning_tokens

    def _register_latency(
        self, prefix: str, samples: list[float], latency_ms: float
    ) -> None:
        """Registra una muestra en la poblacion `prefix` (completion o
        decision). Nunca truncamos con `int()`: la politica de redondeo es
        entero mas cercano via `round()` (un `99.999...` no puede quedar
        en 99). Sin muestras, total/p50/p95/max son None, no 0."""
        if latency_ms < 0:
            raise ValueError("latency cannot be negative")
        samples.append(latency_ms)
        count = len(samples)
        self._counts[f"{prefix}_count"] = count
        self._counts[f"{prefix}_total"] = round(sum(samples))
        self._counts[f"{prefix}_max"] = round(max(samples))
        sorted_latencies = sorted(samples)
        self._counts[f"{prefix}_p50"] = round(_percentile(sorted_latencies, 50))
        self._counts[f"{prefix}_p95"] = round(_percentile(sorted_latencies, 95))

    def completion_latency(self, latency_ms: float) -> None:
        """L-01 (R2): una muestra POR LLAMADA a `provider.complete()`.

        Se registra aca, en el provider, por cada envelope devuelto; es la
        poblacion de completions, no la de decisiones. `decide` jamas debe
        llamar a este metodo (ver el test cruzado en test_decision.py).
        """
        self._register_latency(
            "completion_latency_ms", self._completion_latencies, latency_ms
        )

    def decision_latency(self, latency_ms: float) -> None:
        """L-01 (R2): una muestra POR DECISION, end-to-end, retries incluidos.

        Se registra en `decide`, desde el primer intento LLM hasta la
        respuesta aceptada o el fallback. `KeyRotatingProvider.complete`
        jamas debe llamar a este metodo (ver el test cruzado).
        """
        self._register_latency(
            "decision_latency_ms", self._decision_latencies, latency_ms
        )

    def provider_switch(self) -> None:
        self._counts["provider_switches"] += 1

    def quota(self, turn_id: str) -> None:
        if turn_id not in self._quota_turns:
            self._quota_turns.add(turn_id)
            self._counts["turns_quota_affected"] += 1

    def transient(self, turn_id: str) -> None:
        if turn_id not in self._transient_turns:
            self._transient_turns.add(turn_id)
            self._counts["turns_transient_affected"] += 1

    def deadline(self, turn_id: str) -> None:
        if turn_id not in self._deadline_turns:
            self._deadline_turns.add(turn_id)
            self._counts["turns_deadline_affected"] += 1

    def model_invalid(self, turn_id: str) -> None:
        if turn_id not in self._invalid_turns:
            self._invalid_turns.add(turn_id)
            self._counts["turns_model_invalid"] += 1

    def fallback(self, turn_id: str) -> None:
        if turn_id not in self._fallback_turns:
            self._fallback_turns.add(turn_id)
            self._counts["turns_fallback"] += 1

    def snapshot(self) -> dict[str, int | None]:
        return dict(self._counts)


def provider_keys(
    environ: Mapping[str, str],
    primary_env: str,
    pool_env: str | None = None,
    *,
    aliases: Sequence[tuple[str, str | None]] = (),
) -> tuple[str, ...]:
    candidates: list[str] = []
    for current_primary, current_pool in (
        (primary_env, pool_env), *aliases
    ):
        candidates.append(environ.get(current_primary, ""))
        if current_pool:
            candidates.extend(environ.get(current_pool, "").split(","))
    result: list[str] = []
    for candidate in candidates:
        key = candidate.strip()
        if key and key not in result:
            result.append(key)
    return tuple(result)


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


# F2-10B R3 (MON-20): senales ESTRUCTURADAS de rechazo de credencial.
# `google.genai.APIError` expone `details` (el cuerpo) con
# `error.details[].reason`; los SDK de openai/anthropic exponen `body` con
# `error.code` / `error.type`. langchain_google_genai envuelve el APIError
# de Google en `ChatGoogleGenerativeAIError`, asi que la cadena de causas
# (`__cause__`) es la que conserva la senal estructurada.
_GOOGLE_CREDENTIAL_REASONS = frozenset({
    "API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_NOT_FOUND",
    "API_KEY_SERVICE_DISABLED", "API_KEY_REQUIRED", "API_KEY_DISABLED",
    "API_KEY_ANDROID_APPLICATION_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED",
    "API_KEY_IP_ADDRESS_BLOCKED", "API_KEY_IN_ANDROID_APPLICATION",
    "API_KEY_IN_HTTP_REFERRER", "API_KEY_IN_IP_ADDRESS",
    "KEY_DELETED", "KEY_EXPIRED", "KEY_INVALID", "KEY_REVOKED",
    "CONSUMER_INVALID", "CONSUMER_DEACTIVATED",
    "CONSUMER_QUOTA_REQUIRES_SIGN_UP",
    "MISSING_API_KEY", "USER_DELETED", "USER_INVALID", "DOMAIN_BLOCKED",
})

_OPENAI_CREDENTIAL_CODES = frozenset({
    "invalid_api_key", "api_key_expired", "api_key_revoked",
    "api_key_not_found", "authentication_error",
    "account_deactivated", "account_disabled",
})

# Razones de Google y tipos de openai/anthropic que son NOTORIAMENTE
# model/account-wide: se citan en el docstring para que la exclusion sea
# explicita, no implícita (ACCESS_DENIED, PERMISSION_DENIED, SERVICE_DISABLED,
# insufficient_quota, model_not_accessible, permission_error, ...).


def _error_chain(exc: BaseException) -> list[BaseException]:
    """Cadena de causas completa (`raise ... from`), incluyendo el wrapper
    de langchain_google_genai que envuelve el APIError real de Google."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    return chain


def _http_status_chain(exc: Exception) -> int | None:
    """Status HTTP mirando la cadena completa: `.response.status_code`,
    `.status_code`, o `.code` numerico de `google.genai.APIError`."""
    for candidate in _error_chain(exc):
        response = getattr(candidate, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(candidate, "status_code", None)
        if status is None:
            code = getattr(candidate, "code", None)
            if isinstance(code, int) and 100 <= code <= 599:
                status = code
        if isinstance(status, int) and 100 <= status <= 599:
            return status
    return None


def _structured_body(exc: Exception) -> dict[str, Any] | None:
    """Cuerpo estructurado de la respuesta de error: `details` (google),
    `body` (openai/anthropic) o `response.json()` (httpx). Solo dicts."""
    for candidate in _error_chain(exc):
        details = getattr(candidate, "details", None)
        if isinstance(details, dict):
            return details
        body = getattr(candidate, "body", None)
        if isinstance(body, dict):
            return body
        response = getattr(candidate, "response", None)
        if response is not None and hasattr(response, "json"):
            try:
                parsed = response.json()
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _google_reasons(body: dict[str, Any]) -> list[str]:
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    reasons: list[str] = []
    details = error.get("details", []) if isinstance(error, dict) else []
    if not isinstance(details, list):
        return reasons
    for entry in details:
        if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
            reasons.append(entry["reason"])
    return reasons


def _openai_code(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    return None


def _credential_specific_rejection(exc: Exception) -> bool:
    """403: solo rota si una senal ESTRUCTURADA demuestra que el rechazo es
    de la credencial. Sin senal -> model-wide (falla cerrada). Nunca se usa
    texto libre para decidir esto: el mensaje puede llegar censurado o
    variar entre proveedores, y un 403 de permiso de modelo/proyecto/region
    debe detener la corrida en la primera clave."""
    body = _structured_body(exc)
    if body is None:
        return False
    if any(reason in _GOOGLE_CREDENTIAL_REASONS for reason in _google_reasons(body)):
        return True
    code = _openai_code(body)
    if code is not None and code in _OPENAI_CREDENTIAL_CODES:
        return True
    return False


# Gemini (google.rpc.RetryInfo) manda un `retry_delay` estructurado en el
# 429, pero `langchain_google_genai` lo aplana a texto antes de que nos
# llegue -- lo confirma su propio docstring
# (`.venv/.../langchain_google_genai/_common.py`, campo `max_retries`, que
# documenta esta MISMA regex como la forma soportada de recuperar el valor
# porque "el SDK ignora retry_delay y usa backoff fijo"). No es una
# suposición: es el patrón que la librería aguas arriba dice que hay que
# usar. Ningún otro proveedor de esta rueda expone un equivalente parseable
# hoy, así que `retry_after` queda en `None` para todos los demás 429 y el
# llamador cae a su propio default.
_QUOTA_RETRY_DELAY_RE = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)")


def _quota_retry_after_seconds(rendered: str) -> float | None:
    match = _QUOTA_RETRY_DELAY_RE.search(rendered)
    return float(match.group(1)) if match else None


# Los mensajes de error de los proveedores NO son texto de confianza: varios
# arrastran la URL de la request, y Gemini manda la clave en el query string
# (`...?key=AIza...`), así que el mensaje crudo de un 429 o de un error de
# transporte puede contener una clave entera. El log de `_classified` es el
# único lugar donde ese texto crudo sale del proceso; se censura acá, antes
# de escribirlo, y no en el llamador, para que no dependa de que cada sitio
# de log se acuerde.
_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)\b(key|api[_-]?key|access[_-]?token|token)=[^&\s\"']+"),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*"), "Bearer <redacted>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{10,}"), "<redacted>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{12,}"), "<redacted>"),
)


def _redacted(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _classified(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    status = _http_status_chain(exc)
    rendered = str(exc)
    if status == 429 or (
        "RESOURCE_EXHAUSTED" in rendered and "429" in rendered
    ):
        classified: ProviderError = QuotaExceeded(
            "provider quota exhausted",
            retry_after=_quota_retry_after_seconds(rendered),
        )
    elif status is not None and status >= 500:
        classified = TransientProviderError("provider server error")
    elif status == 401:
        # 401 es, por definicion, rechazo de la credencial: rota.
        classified = CredentialRejected("provider authentication failed")
    elif status == 403 and _credential_specific_rejection(exc):
        # 403 SOLO rota si una senal estructurada demuestra rechazo de
        # credencial (p.ej. reason=API_KEY_INVALID de Google). Un 403 de
        # permiso de modelo/proyecto/region o sin senal es model-wide:
        # FatalProviderError -> se detiene en la primera clave y no quema
        # el pool de 11.
        classified = CredentialRejected("provider authentication failed")
    elif status in (403, 404):
        classified = FatalProviderError(
            "provider permission or model unavailable"
        )
    elif isinstance(exc, (
        asyncio.TimeoutError,
        TimeoutError,
        httpx.TransportError,
        OpenAIAPIConnectionError,
        AnthropicAPIConnectionError,
    )):
        classified = TransientProviderError("provider transport failed")
    else:
        detail = type(exc).__name__
        if isinstance(exc, ValidationError):
            shapes = [
                f"{item['type']}@{'.'.join(map(str, item['loc']))}"
                for item in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
            detail += f": {','.join(shapes)}"
        classified = FatalProviderError(f"unexpected provider failure ({detail})")
    # No se puede diagnosticar lo que no se ve: la clasificación colapsa
    # cualquier excepción de transporte a un puñado de mensajes fijos (a
    # propósito, para no filtrar detalle de proveedor en `str(classified)`,
    # que termina en `evals/runs/*.json` y de ahí en el repo). El tipo y
    # mensaje ORIGINALES —ConnectError vs. ReadTimeout vs. PoolTimeout, la
    # causa real detrás de "provider transport failed"— solo quedan acá, en
    # el log y en `__cause__` (ver los `raise ... from` en
    # `KeyRotatingProvider.complete`, que ya no usan `from None`).
    #
    # Censurado (`_redacted`) y SIN `exc_info`: el traceback vuelve a
    # imprimir el mensaje crudo del proveedor, sin pasar por la censura, y
    # con él la clave que Gemini manda en el query string. El tipo original
    # ya va en el mensaje, que es lo que hacía falta para distinguir un
    # ConnectError de un ReadTimeout; la cadena completa sigue disponible en
    # vivo por `__cause__`, que nunca se escribe a disco.
    logger.warning(
        "provider error classified as %s (original=%s: %s)",
        type(classified).__name__, type(exc).__name__, _redacted(rendered),
    )
    return classified


class KeyRotatingProvider:
    # Default conservador cuando el proveedor no informa `retry_after` (ver
    # `_quota_retry_after_seconds`): 60s es lo que la propia documentación de
    # `langchain_google_genai` usa como fallback para 429 de Gemini. No
    # distingue cuota diaria de límite por minuto -- no hay señal parseable
    # para eso hoy (ver docstring de `_quota_retry_after_seconds`) -- así que
    # una clave con cuota DIARIA agotada también vuelve a intentarse cada
    # 60s. Es un desperdicio acotado (una llamada perdida por minuto, no por
    # turno), muy distinto del bug que esto reemplaza (exclusión permanente
    # de por vida del proceso) y del bug anterior a ese (reintentar TODAS las
    # claves agotadas en CADA turno). Documentado en vez de adivinado: si el
    # proveedor deja distinguir cuota diaria de límite por minuto en el
    # futuro, este es el lugar para hacerlo.
    DEFAULT_QUOTA_COOLDOWN_SECONDS = 60.0

    def __init__(
        self,
        name: str,
        keys: Sequence[str],
        backend: ProviderBackend,
        *,
        model: str | None = None,
        metrics: DecisionMetrics | None = None,
        transient_retries: int = 2,
        quota_cooldown_seconds: float = DEFAULT_QUOTA_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not keys:
            raise ValueError(f"{name}: no API keys configured")
        self.name = name
        self._keys = tuple(keys)
        self._backend = backend
        # F2-08: el model configurado. El "model efectivo" del envelope es
        # `usage.model` cuando el proveedor lo reporta (response_metadata),
        # y cae a este configurado cuando no. Es el unico model conocido de
        # la llamada; nunca se inventa otro.
        self._model = model
        self._metrics = metrics or DecisionMetrics()
        self._transient_retries = transient_retries
        self._quota_cooldown_seconds = quota_cooldown_seconds
        self._clock = clock
        # Enfriamiento por clave (D25/fix-transporte), no exclusión
        # permanente: una clave con 429 vuelve a estar disponible pasado
        # `_cooldown_until[key_index]`, nunca antes. Ausente == disponible
        # desde el arranque. Reemplaza a `_first_available_key`, que solo
        # avanzaba y nunca volvía: con 11 claves de Gemini (límite por
        # MINUTO, no diario) eso quemaba el pool entero en ~3-4 llamadas por
        # clave y nunca se recuperaba, ni siquiera entre batallas del mismo
        # run (ver docs/DECISIONS.md).
        self._cooldown_until: dict[int, float] = {}
        # F2-10B (MON-20): cuarentena por 401 (rechazo de credencial) y
        # por 403 CON senal estructurada de credencial, PERMANENTE para el
        # proceso (a diferencia del cooldown, que es acotado). Una clave
        # vencida/revocada no vuelve; un 403 model-wide o un error fatal
        # no queman el pool porque `FatalProviderError` se propaga de
        # inmediato en la primera clave.
        self._quarantined: set[int] = set()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, "
            f"key_count={len(self._keys)})"
        )

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> CompletionEnvelope:
        # F2-10 (MON-15): el reloj inyectable gobierna TODOS los puntos de
        # tiempo de este proveedor: cooldown, deadline y latencia. Ningún
        # `time.monotonic()` debe filtrarse acá; en producción el default es
        # `time.monotonic`, pero en tests usamos un reloj falso/scripted.
        started_at = self._clock()
        while True:
            now = self._clock()
            for key_index in range(len(self._keys)):
                if key_index in self._quarantined:
                    continue
                cooldown_until = self._cooldown_until.get(key_index)
                if cooldown_until is not None and cooldown_until > now:
                    continue
                key = self._keys[key_index]
                transient_attempts = 0
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        self._metrics.deadline(turn_id)
                        raise DecisionDeadlineExceeded("decision deadline exhausted")
                    try:
                        async with asyncio.timeout(remaining):
                            completion = await self._backend.complete(
                                prompt, api_key=key, deadline=deadline
                            )
                        self._metrics.usage(completion.usage)
                        # F2-08: el envelope se construye AQUI, con datos
                        # locales de esta llamada, antes de cualquier punto de
                        # suspension posterior. Ninguna lectura de estado
                        # compartido entre llamadas: ver docstring de
                        # CompletionEnvelope.
                        latency_ms = (self._clock() - started_at) * 1000
                        self._metrics.completion_latency(latency_ms)
                        return CompletionEnvelope(
                            payload=completion.payload,
                            provider=self.name,
                            model=completion.usage.model or self._model,
                            usage=completion.usage,
                            latency_ms=latency_ms,
                        )
                    except Exception as raw:
                        error = _classified(raw)
                        if isinstance(error, DecisionDeadlineExceeded):
                            self._metrics.deadline(turn_id)
                            raise error from raw
                        if isinstance(error, CredentialRejected):
                            # F2-10B: cuarentena solo de ESTA clave (401 o
                            # 403 credential-specific); la siguiente del
                            # pool se intenta en el proximo pase. Un pool
                            # completo de credenciales rechazadas termina
                            # en ProviderPoolExhausted, nunca en falso
                            # incompatible/unsupported.
                            self._quarantined.add(key_index)
                            self._metrics.key_quarantine()
                            break
                        if isinstance(error, QuotaExceeded):
                            self._metrics.quota(turn_id)
                            retry_after = (
                                error.retry_after
                                if error.retry_after is not None
                                else self._quota_cooldown_seconds
                            )
                            self._cooldown_until[key_index] = (
                                self._clock() + retry_after
                            )
                            self._metrics.key_rotation()
                            break
                        if isinstance(error, TransientProviderError):
                            self._metrics.transient(turn_id)
                            if transient_attempts < self._transient_retries:
                                transient_attempts += 1
                                continue
                        raise error from raw

            # Se recorrieron todas las claves sin devolver ni lanzar: cada
            # una está enfriándose (recién puesta a enfriar en este mismo
            # pase, o ya lo estaba de un turno anterior). No es
            # `ProviderPoolExhausted` todavía -- eso es solo para cuando
            # ESPERAR no alcanza el deadline del turno (ver comentario del
            # canario en tests/graph/test_provider.py).
            now = self._clock()
            cooldowns = [
                until for until in self._cooldown_until.values() if until > now
            ]
            if not cooldowns:
                # No debería poder pasar (guardia defensiva): si ninguna
                # clave está enfriando, el for de arriba tendría que haber
                # devuelto o lanzado. Falla ruidoso en vez de loopear en
                # silencio. F2-10B: con todo el pool en cuarentena de
                # credencial, el mensaje lo dice (no es cuota ni
                # incompatibilidad).
                if len(self._quarantined) == len(self._keys):
                    raise ProviderPoolExhausted(
                        f"{self.name}: all configured keys quarantined "
                        "(401/403)"
                    )
                raise ProviderPoolExhausted(
                    f"{self.name}: all configured keys exhausted"
                )
            soonest = min(cooldowns)
            wait_seconds = soonest - now
            remaining_deadline = deadline - self._clock()
            if wait_seconds <= 0 or wait_seconds >= remaining_deadline:
                raise ProviderPoolExhausted(
                    f"{self.name}: all configured keys exhausted"
                )
            await asyncio.sleep(wait_seconds)


class ProviderChain:
    def __init__(
        self,
        providers: Sequence[DecisionProvider],
        *,
        allow_cross_provider: bool,
        metrics: DecisionMetrics | None = None,
    ) -> None:
        if not providers:
            raise ValueError("provider chain cannot be empty")
        self._providers = tuple(providers)
        self._allow_cross_provider = allow_cross_provider
        self._metrics = metrics or DecisionMetrics()

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> CompletionEnvelope:
        for index, provider in enumerate(self._providers):
            try:
                # F2-08: el envelope del provider que respondio ya trae su
                # provider/model efectivos; el chain solo lo propaga.
                return await provider.complete(
                    prompt, deadline=deadline, turn_id=turn_id
                )
            except (ProviderPoolExhausted, TransientProviderError):
                if not self._allow_cross_provider or index + 1 == len(self._providers):
                    raise
                self._metrics.provider_switch()
        raise ProviderPoolExhausted("provider chain exhausted")


class FakeDecisionProvider:
    """Provider de tests que devuelve un envelope por respuesta.

    Acepta dicts (payload crudo), `ProviderCompletion` o `CompletionEnvelope`
    ya armados; las respuestas llanas se envuelven con provider="fake" y
    model="fake-model" para que los tests de `decide` verifiquen metadata sin
    levantar un backend real.
    """

    def __init__(
        self,
        responses: Sequence[dict[str, Any] | Exception],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        self._clock = clock

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> CompletionEnvelope:
        if self._clock() >= deadline:
            raise DecisionDeadlineExceeded("decision deadline exhausted")
        self.prompts.append(prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, CompletionEnvelope):
            return response
        if isinstance(response, ProviderCompletion):
            return CompletionEnvelope(
                payload=response.payload,
                provider="fake",
                model=response.usage.model or "fake-model",
                usage=response.usage,
                latency_ms=0.0,
            )
        return CompletionEnvelope(
            payload=response,
            provider="fake",
            model="fake-model",
            usage=CompletionUsage(input_tokens=1, output_tokens=1),
            latency_ms=0.0,
        )


class _LangChainBackend:
    def __init__(
        self,
        *,
        kind: str,
        model: str,
        response_schema: type,
        timeout_seconds: float,
        base_url: str | None = None,
        route: ModelRoute | None = None,
    ) -> None:
        self.kind = kind
        self.model = model
        self.response_schema = response_schema
        self.base_url = base_url
        self.route = route or ModelRoute(protocol=kind)
        self.timeout_seconds = (
            self.route.timeout_seconds
            if self.route.timeout_seconds is not None
            else timeout_seconds
        )

    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> ProviderCompletion:
        if self.kind == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            # F2-10B (MON-20): los nombres REALES de los campos de
            # ChatGoogleGenerativeAI son `google_api_key`, `max_retries` y
            # `timeout`. Los nombres `api_key`/`retries`/`request_timeout`
            # NO existen: pydantic los ignora en silencio (extra=ignore) y
            # la llamada caia a la clave del entorno en vez de la del pool,
            # por lo que la rotacion de 11 claves jamas rotaba credenciales
            # reales. `base_url` habilita el endpoint nativo de Gemini
            # detras del gateway de OpenCode Zen (protocolo "google").
            google_options: dict[str, Any] = dict(
                model=self.model, google_api_key=api_key,
                max_retries=0, timeout=self.timeout_seconds,
                temperature=self.route.temperature,
            )
            if self.base_url is not None:
                google_options["base_url"] = self.base_url
            model = ChatGoogleGenerativeAI(**google_options)
        elif self.kind == "anthropic":
            from langchain_anthropic import ChatAnthropic

            anthropic_options: dict[str, Any] = dict(
                model_name=self.model, api_key=api_key,
                base_url=anthropic_sdk_base_url(self.base_url),
                temperature=self.route.temperature,
                max_retries=0, timeout=self.timeout_seconds,
            )
            if self.route.max_tokens is not None:
                anthropic_options["max_tokens"] = self.route.max_tokens
            model = ChatAnthropic(**anthropic_options)
        else:
            from langchain_openai import ChatOpenAI

            extra_body: dict[str, Any] = {}
            if self.route.thinking is not None:
                extra_body["thinking"] = {"type": self.route.thinking}
            if self.route.max_tokens is not None:
                extra_body["max_tokens"] = self.route.max_tokens
            model = ChatOpenAI(
                model=self.model, api_key=api_key, base_url=self.base_url,
                temperature=self.route.temperature, max_retries=0,
                timeout=self.timeout_seconds,
                extra_body=extra_body or None,
            )
        output_method = route_structured_output(
            self.route, self.kind, self.base_url
        )
        if output_method == "text_json":
            raw = await model.ainvoke(
                prompt
                + "\nRespondé únicamente con un objeto JSON válido, sin "
                "Markdown ni texto fuera del objeto."
            )
            return ProviderCompletion(
                payload=text_json_payload(message_text_content(raw.content)),
                usage=self._usage_from_message(raw),
            )
        structured = model.with_structured_output(
            provider_response_schema(self.response_schema),
            method=output_method,
            include_raw=True,
        )
        result = await structured.ainvoke(prompt)
        raw = result["raw"]
        parsed = result["parsed"]
        usage = self._usage_from_message(raw)
        if parsed is None:
            payload = {"_invalid_response": raw.content}
        else:
            payload = (
                parsed.model_dump()
                if hasattr(parsed, "model_dump")
                else dict(parsed)
            )
        return ProviderCompletion(payload=payload, usage=usage)

    @staticmethod
    def _usage_from_message(message: Any) -> CompletionUsage:
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, Mapping):
            raise FatalProviderError("provider response did not include token usage")
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        response_metadata = getattr(message, "response_metadata", None) or {}
        return CompletionUsage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cached_input_tokens=int(input_details.get("cache_read", 0)),
            reasoning_tokens=int(output_details.get("reasoning", 0)),
            model=response_metadata.get("model_name")
            or response_metadata.get("model"),
        )


class GeminiDecisionProvider(KeyRotatingProvider):
    def __init__(
        self, keys: Sequence[str], *, model: str, response_schema: type,
        timeout_seconds: float, metrics: DecisionMetrics | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            "google", keys,
            _LangChainBackend(
                kind="google", model=model, response_schema=response_schema,
                timeout_seconds=timeout_seconds, base_url=base_url,
            ),
            model=model,
            metrics=metrics,
        )


class OpenAICompatibleDecisionProvider(KeyRotatingProvider):
    def __init__(
        self, name: str, keys: Sequence[str], *, model: str, base_url: str | None,
        response_schema: type, timeout_seconds: float,
        metrics: DecisionMetrics | None = None, route: ModelRoute | None = None,
    ) -> None:
        super().__init__(
            name, keys,
            _LangChainBackend(
                kind="openai", model=model, response_schema=response_schema,
                timeout_seconds=timeout_seconds, base_url=base_url, route=route,
            ),
            model=model,
            metrics=metrics,
        )


def _responses_output_text(document: dict[str, Any]) -> str:
    """Extrae el texto de una respuesta OpenAI Responses API: los items de
    `output` con type `message` contienen bloques de contenido `output_text`
    (o `text`)."""
    fragments: list[str] = []
    output = document.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text") if block.get("type") == "output_text" else None
            if isinstance(text, str):
                fragments.append(text)
    return "".join(fragments)


class _ResponsesBackend(ProviderBackend):
    """Backend del protocolo OpenAI Responses (F2-10B/MON-20).

    CONTRATO DE ENDPOINT (D43): `endpoint` es la URL COMPLETA del endpoint
    (p.ej. `https://opencode.ai/zen/v1/responses`), exactamente como la
    publica la documentacion oficial de Zen. El backend la postea tal cual
    con httpx; NO usa el SDK de openai (que volveria a agregar `/responses`
    al base_url y produciria `/responses/responses`). Si la ruta no declara
    `endpoint`, se deriva `{base_url}/responses`.

    El payload se obtiene del texto de la respuesta (JSON textual estricto,
    misma validacion semantica de D26) y el usage del objeto `usage` del
    documento. Un modelo sin soporte nativo queda como clasificacion
    explicita de la respuesta, nunca como discovery por ensayo pagado.
    """

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        endpoint: str,
        route: ModelRoute,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.route = route
        self.timeout_seconds = (
            route.timeout_seconds
            if route.timeout_seconds is not None
            else timeout_seconds
        )

    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> ProviderCompletion:
        body: dict[str, Any] = {
            "model": self.model,
            "input": prompt,
            "temperature": self.route.temperature,
        }
        if self.route.max_tokens is not None:
            body["max_output_tokens"] = self.route.max_tokens
        # L-01 (MON-20): httpx.AsyncClient NO acepta `max_retries` (en
        # 0.28.1 lanza TypeError: kwarg inesperado). El retry de
        # infraestructura se controla con el transporte:
        # `AsyncHTTPTransport(retries=0)` — firma verificada contra la
        # librería instalada. La clasificacion de errores corre aguas
        # arriba en `_classified`, igual que con cualquier otro backend.
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=httpx.AsyncHTTPTransport(retries=0),
        ) as client:
            response = await client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            # raise_for_status deja un httpx.HTTPStatusError con
            # `response.json()` disponible: la clasificacion de 429/401/403
            # (con senal estructurada) corre aguas arriba en `_classified`.
            response.raise_for_status()
            try:
                document = response.json()
            except ValueError as exc:
                raise FatalProviderError(
                    "provider response was not valid JSON"
                ) from exc
        if not isinstance(document, dict):
            raise FatalProviderError(
                "provider response was not a JSON object"
            )
        usage = document.get("usage")
        if not isinstance(usage, dict):
            raise FatalProviderError(
                "provider response did not include token usage"
            )
        details = usage.get("output_tokens_details") or {}
        return ProviderCompletion(
            payload=text_json_payload(_responses_output_text(document)),
            usage=CompletionUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                reasoning_tokens=int(
                    details.get("reasoning_tokens", 0) or 0
                ),
                model=document.get("model") or self.model,
            ),
        )


class OpenAIResponsesDecisionProvider(KeyRotatingProvider):
    def __init__(
        self, name: str, keys: Sequence[str], *, model: str,
        base_url: str | None, timeout_seconds: float,
        metrics: DecisionMetrics | None = None, route: ModelRoute | None = None,
        response_schema: type | None = None,
    ) -> None:
        # `response_schema` se acepta por consistencia de firma con los
        # demas providers; el protocolo responses valida el JSON textual
        # (text_json) aguas abajo en `decide`, no en el backend.
        # CONTRATO DE ENDPOINT (D43): `route.endpoint` es la URL COMPLETA
        # del endpoint; si la ruta no la declara, se deriva de la base.
        effective_route = route or ModelRoute(protocol="responses")
        endpoint = effective_route.endpoint or (
            f"{base_url.rstrip('/')}/responses"
        )
        super().__init__(
            name, keys,
            _ResponsesBackend(
                model=model, timeout_seconds=timeout_seconds,
                endpoint=endpoint, route=effective_route,
            ),
            model=model,
            metrics=metrics,
        )


class AnthropicDecisionProvider(KeyRotatingProvider):
    def __init__(
        self, keys: Sequence[str], *, model: str, response_schema: type,
        timeout_seconds: float, metrics: DecisionMetrics | None = None,
        name: str = "anthropic", base_url: str | None = None,
        route: ModelRoute | None = None,
    ) -> None:
        super().__init__(
            name, keys,
            _LangChainBackend(
                kind="anthropic", model=model, response_schema=response_schema,
                timeout_seconds=timeout_seconds, base_url=base_url, route=route,
            ),
            model=model,
            metrics=metrics,
        )


# --- F2-09 (MON-14): resolucion de provider/model por decision -------------

# kind de cada proveedor del catalogo: el backend concreto que lo construye.
_PROVIDER_KINDS = {
    "google": "google",
    "anthropic": "anthropic",
    "openai": "openai",
    "kimi": "openai",
    "open_code_zen": "openai",
}


@dataclass(frozen=True)
class ResolvedProvider:
    """La seleccion activa resuelta para UNA decision: nombres + instancia."""

    provider_name: str
    model_id: str
    provider: DecisionProvider


class ProviderResolver:
    """Resuelve provider/model activos POR DECISION desde la DB (F2-09).

    La seleccion activa se consulta en CADA `resolve()` — nunca se cachea:
    cambiar el modelo activo entre dos invocaciones del mismo grafo surte
    efecto sin recompilar ni reiniciar la batalla. Lo unico cacheado es la
    INSTANCIA del provider por `(provider_name, model_id)`: el cooldown de
    claves (D30) y los reintentos de infraestructura viven en la instancia y
    deben sobrevivir entre turnos del mismo modelo.

    El env es bootstrap: si la DB no tiene seleccion activa, se usa la
    seleccion de configuracion; la DB gobierna cuando existe.
    """

    def __init__(
        self,
        repository: Any,
        *,
        provider_factory: Callable[..., DecisionProvider] | None = None,
        metrics: DecisionMetrics | None = None,
        request_timeout_seconds: float = 30.0,
        routes: Mapping[tuple[str, str], ModelRoute] | None = None,
        environ: Mapping[str, str] | None = None,
        bootstrap: Any | None = None,
    ) -> None:
        self._repository = repository
        self._factory = provider_factory or default_provider_factory
        self._metrics = metrics or DecisionMetrics()
        self._request_timeout_seconds = request_timeout_seconds
        self._routes = routes if routes is not None else load_model_routes()
        self._environ = os.environ if environ is None else environ
        self._bootstrap = bootstrap
        self._instances: dict[tuple[str, str], DecisionProvider] = {}

    async def resolve(self) -> ResolvedProvider:
        selection = await self._repository.active_selection()
        from_db = selection is not None
        if selection is None:
            selection = self._bootstrap
        if selection is None:
            raise ProviderSelectionError(
                "no hay seleccion activa de modelo en la DB ni bootstrap de "
                "configuracion"
            )
        if from_db:
            # L-01 (R2): fail-closed. La seleccion de la DB (settings o
            # default) se valida contra la frontera unica antes de construir
            # nada: un modelo inexistente o deshabilitado es un error, NUNCA
            # un motivo para caer silenciosamente al bootstrap o al default.
            # El bootstrap de env no se valida contra la DB: es el ultimo
            # recurso y no depende de ella (la factory ya valida claves).
            try:
                row = await self._repository.validate_selection(
                    selection.provider_name, selection.model_id
                )
            except ModelSelectionError as exc:
                raise ProviderSelectionError(str(exc)) from exc
        else:
            row = await self._repository.provider(selection.provider_name)
            if row is None or not row.enabled:
                raise ProviderSelectionError(
                    f"provider {selection.provider_name!r} no existe o esta "
                    "deshabilitado en la DB"
                )
        key = (selection.provider_name, selection.model_id)
        provider = self._instances.get(key)
        if provider is None:
            provider = self._factory(
                selection.provider_name,
                selection.model_id,
                base_url=row.base_url,
                api_key_env=row.api_key_env,
                metrics=self._metrics,
                timeout_seconds=self._request_timeout_seconds,
                routes=self._routes,
                environ=self._environ,
            )
            self._instances[key] = provider
        return ResolvedProvider(selection.provider_name, selection.model_id, provider)


def default_provider_factory(
    name: str,
    model_id: str,
    *,
    base_url: str | None,
    api_key_env: str,
    metrics: DecisionMetrics,
    timeout_seconds: float,
    routes: Mapping[tuple[str, str], ModelRoute],
    environ: Mapping[str, str],
) -> DecisionProvider:
    """Construye el DecisionProvider de la seleccion activa desde la fila de
    la DB.

    El backend (google/anthropic/openai-compatible) se deriva del nombre del
    proveedor contra el catalogo de config. La API key se lee del ENTORNO por
    el NOMBRE guardado en la DB — nunca hay valores de claves en la DB, en
    logs ni en snapshots. Los pools y aliases de google derivan del catalogo
    (igual que el bootstrap de env). Excepcion documentada (D39): no se usa
    `init_chat_model` de LangChain porque el contrato real necesita opciones
    por proveedor (timeout, thinking/max_tokens, structured output) que esa
    API no expone sin ramas.
    """
    from .decision import DecisionResponse  # evita el ciclo decision<->provider

    kind = _PROVIDER_KINDS.get(name)
    if kind is None:
        raise ProviderSelectionError(
            f"proveedor sin backend registrado en el catalogo: {name!r}"
        )
    primary_env, pool_env, _ = _config._PROVIDERS.get(name, (api_key_env, None, None))
    keys_env = api_key_env or primary_env
    aliases = (
        (("GOOGLE_API_KEY", "GOOGLE_API_KEYS"),)
        if name == "google" else ()
    )
    keys = provider_keys(environ, keys_env, pool_env, aliases=aliases)
    if not keys:
        raise ProviderSelectionError(
            f"sin claves configuradas para {name!r}: falta la variable "
            f"de entorno {keys_env!r}"
        )
    route = (
        model_route(routes, name, model_id)
        if name in {"kimi", "open_code_zen"} else None
    )
    if kind == "google":
        return GeminiDecisionProvider(
            keys, model=model_id, response_schema=DecisionResponse,
            timeout_seconds=timeout_seconds, metrics=metrics,
        )
    if kind == "anthropic" and route is None:
        return AnthropicDecisionProvider(
            keys, name=name, model=model_id, base_url=base_url,
            response_schema=DecisionResponse, timeout_seconds=timeout_seconds,
            metrics=metrics, route=route,
        )
    return build_route_provider(
        name, model_id, base_url=base_url, keys=keys, metrics=metrics,
        timeout_seconds=timeout_seconds, route=route,
    )


def build_route_provider(
    name: str,
    model_id: str,
    *,
    base_url: str | None,
    keys: Sequence[str],
    metrics: DecisionMetrics,
    timeout_seconds: float,
    route: ModelRoute,
) -> DecisionProvider:
    """F2-10B (MON-20): unico punto de despacho por PROTOCOLO declarativo
    de la ruta (nunca condicionales por model_id). Cada protocolo documentado
    tiene su backend: `responses`, `messages`, `chat_completions` y el
    nativo de Gemini (`google`) cuando el gateway lo sirve por su endpoint
    propio. Un protocolo que el repo todavia no implementa se declara en la
    ruta como tal y se clasifica de forma explicita, jamas como "default
    chat_completions por ensayo pagado"."""
    from .decision import DecisionResponse  # evita el ciclo decision<->provider

    if route.protocol == "responses":
        # CONTRATO (D43): `route.endpoint` es la URL COMPLETA del endpoint
        # y la resuelve el propio provider; aca solo pasa la base.
        return OpenAIResponsesDecisionProvider(
            name, keys, model=model_id, base_url=base_url,
            response_schema=DecisionResponse, timeout_seconds=timeout_seconds,
            metrics=metrics, route=route,
        )
    if route.protocol == "google":
        return GeminiDecisionProvider(
            keys, model=model_id, response_schema=DecisionResponse,
            timeout_seconds=timeout_seconds, metrics=metrics,
            base_url=base_url,
        )
    if route.protocol == "messages":
        return AnthropicDecisionProvider(
            keys, name=name, model=model_id,
            base_url=route.endpoint or base_url,
            response_schema=DecisionResponse, timeout_seconds=timeout_seconds,
            metrics=metrics, route=route,
        )
    if route.protocol == "chat_completions":
        return OpenAICompatibleDecisionProvider(
            name, keys, model=model_id,
            base_url=route.endpoint or base_url,
            response_schema=DecisionResponse, timeout_seconds=timeout_seconds,
            metrics=metrics, route=route,
        )
    raise ProviderSelectionError(
        f"{name}/{model_id}: protocolo {route.protocol!r} sin backend "
        "registrado en Ludex"
    )


class _PinnedAuditor:
    """Envuelve un provider y audita cada envelope contra el pin: una
    respuesta efectiva con provider/model distintos aborta (mezcla)."""

    def __init__(self, inner: DecisionProvider, pin: tuple[str, str]) -> None:
        self._inner = inner
        self._pin = pin

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> CompletionEnvelope:
        envelope = await self._inner.complete(
            prompt, deadline=deadline, turn_id=turn_id
        )
        effective = (envelope.provider, envelope.model)
        if effective != self._pin:
            raise ProviderMixError(
                f"provider/model mezclado en la corrida: pin {self._pin!r} "
                f"pero la respuesta efectiva fue {effective!r}"
            )
        return envelope


class PinnedResolver:
    """Fija provider/model para un contexto que lo exige (benchmark, tests).

    Con `enforce_pin=True` audita cada envelope: si la metadata efectiva
    difiere del pin, `ProviderMixError` aborta la corrida (D28/F2-09: el
    benchmark fija provider y modelo al inicio y jamas los mezcla).
    """

    def __init__(
        self,
        provider: DecisionProvider,
        provider_name: str,
        model_id: str,
        *,
        enforce_pin: bool = False,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name
        self._model_id = model_id
        self._enforce_pin = enforce_pin

    async def resolve(self) -> ResolvedProvider:
        inner: DecisionProvider = self._provider
        if self._enforce_pin:
            inner = _PinnedAuditor(inner, (self._provider_name, self._model_id))
        return ResolvedProvider(self._provider_name, self._model_id, inner)
