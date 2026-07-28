"""Proveedores LLM: fallos de infraestructura separados del contrato semántico."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


class ProviderError(RuntimeError):
    pass


class QuotaExceeded(ProviderError):
    pass


class TransientProviderError(ProviderError):
    pass


class FatalProviderError(ProviderError):
    pass


class ProviderPoolExhausted(ProviderError):
    pass


class DecisionDeadlineExceeded(ProviderError):
    pass


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
class ModelRoute:
    protocol: str
    temperature: float = 0.0
    thinking: str | None = None
    max_tokens: int | None = None


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
        if protocol not in {"chat_completions", "messages", "responses"}:
            raise ValueError(f"protocolo de modelo desconocido: {protocol}")
        routes[key] = ModelRoute(
            protocol=protocol,
            temperature=float(raw.get("temperature", 0)),
            thinking=raw.get("thinking"),
            max_tokens=raw.get("max_tokens"),
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


class DecisionProvider(Protocol):
    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> dict[str, Any]: ...


class ProviderBackend(Protocol):
    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> ProviderCompletion: ...


class DecisionMetrics:
    def __init__(self) -> None:
        self._counts = {
            "turns_total": 0,
            "calls_total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "key_rotations": 0,
            "provider_switches": 0,
            "turns_quota_affected": 0,
            "turns_transient_affected": 0,
            "turns_deadline_affected": 0,
            "turns_model_invalid": 0,
            "turns_fallback": 0,
        }
        self._quota_turns: set[str] = set()
        self._transient_turns: set[str] = set()
        self._deadline_turns: set[str] = set()
        self._invalid_turns: set[str] = set()
        self._fallback_turns: set[str] = set()
        self._turns: set[str] = set()

    def turn(self, turn_id: str) -> None:
        if turn_id not in self._turns:
            self._turns.add(turn_id)
            self._counts["turns_total"] += 1

    def key_rotation(self) -> None:
        self._counts["key_rotations"] += 1

    def usage(self, usage: CompletionUsage) -> None:
        self._counts["calls_total"] += 1
        self._counts["input_tokens"] += usage.input_tokens
        self._counts["output_tokens"] += usage.output_tokens
        self._counts["cached_input_tokens"] += usage.cached_input_tokens
        self._counts["reasoning_tokens"] += usage.reasoning_tokens

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

    def snapshot(self) -> dict[str, int]:
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


def _classified(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    status = _status_code(exc)
    rendered = str(exc)
    if status == 429 or (
        "RESOURCE_EXHAUSTED" in rendered and "429" in rendered
    ):
        return QuotaExceeded("provider quota exhausted")
    if status is not None and status >= 500:
        return TransientProviderError("provider server error")
    if status in (401, 403):
        return FatalProviderError("provider authentication failed")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TransportError)):
        return TransientProviderError("provider transport failed")
    return FatalProviderError("unexpected provider failure")


class KeyRotatingProvider:
    def __init__(
        self,
        name: str,
        keys: Sequence[str],
        backend: ProviderBackend,
        *,
        metrics: DecisionMetrics | None = None,
        transient_retries: int = 2,
    ) -> None:
        if not keys:
            raise ValueError(f"{name}: no API keys configured")
        self.name = name
        self._keys = tuple(keys)
        self._backend = backend
        self._metrics = metrics or DecisionMetrics()
        self._transient_retries = transient_retries
        self._first_available_key = 0

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, "
            f"key_count={len(self._keys)})"
        )

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> dict[str, Any]:
        for key_index in range(self._first_available_key, len(self._keys)):
            key = self._keys[key_index]
            transient_attempts = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._metrics.deadline(turn_id)
                    raise DecisionDeadlineExceeded("decision deadline exhausted")
                try:
                    async with asyncio.timeout(remaining):
                        completion = await self._backend.complete(
                            prompt, api_key=key, deadline=deadline
                        )
                    self._metrics.usage(completion.usage)
                    return completion.payload
                except Exception as raw:
                    error = _classified(raw)
                    if isinstance(error, DecisionDeadlineExceeded):
                        self._metrics.deadline(turn_id)
                        raise error from None
                    if isinstance(error, QuotaExceeded):
                        self._metrics.quota(turn_id)
                        self._first_available_key = max(
                            self._first_available_key, key_index + 1
                        )
                        if key_index + 1 < len(self._keys):
                            self._metrics.key_rotation()
                            break
                        raise ProviderPoolExhausted(
                            f"{self.name}: all configured keys exhausted"
                        ) from None
                    if isinstance(error, TransientProviderError):
                        self._metrics.transient(turn_id)
                        if transient_attempts < self._transient_retries:
                            transient_attempts += 1
                            continue
                    raise error from None
        raise ProviderPoolExhausted(f"{self.name}: all configured keys exhausted")


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
    ) -> dict[str, Any]:
        for index, provider in enumerate(self._providers):
            try:
                return await provider.complete(
                    prompt, deadline=deadline, turn_id=turn_id
                )
            except (ProviderPoolExhausted, TransientProviderError):
                if not self._allow_cross_provider or index + 1 == len(self._providers):
                    raise
                self._metrics.provider_switch()
        raise ProviderPoolExhausted("provider chain exhausted")


class FakeDecisionProvider:
    def __init__(self, responses: Sequence[dict[str, Any] | Exception]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> dict[str, Any]:
        if time.monotonic() >= deadline:
            raise DecisionDeadlineExceeded("decision deadline exhausted")
        self.prompts.append(prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url
        self.route = route or ModelRoute(protocol=kind)

    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> ProviderCompletion:
        if self.kind == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            model = ChatGoogleGenerativeAI(
                model=self.model, api_key=api_key,
                temperature=self.route.temperature,
                retries=0, request_timeout=self.timeout_seconds,
            )
        elif self.kind == "anthropic":
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(
                model_name=self.model, api_key=api_key,
                base_url=self.base_url,
                temperature=self.route.temperature,
                max_retries=0, timeout=self.timeout_seconds,
            )
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
        structured = model.with_structured_output(
            self.response_schema, method="json_schema", include_raw=True
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
    ) -> None:
        super().__init__(
            "google", keys,
            _LangChainBackend(
                kind="google", model=model, response_schema=response_schema,
                timeout_seconds=timeout_seconds,
            ),
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
            metrics=metrics,
        )
