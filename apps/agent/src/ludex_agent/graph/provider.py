"""Proveedores LLM: fallos de infraestructura separados del contrato semántico."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
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


class DecisionProvider(Protocol):
    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> dict[str, Any]: ...


class ProviderBackend(Protocol):
    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> dict[str, Any]: ...


class DecisionMetrics:
    def __init__(self) -> None:
        self._counts = {
            "key_rotations": 0,
            "provider_switches": 0,
            "turns_quota_affected": 0,
            "turns_transient_affected": 0,
            "turns_model_invalid": 0,
            "turns_fallback": 0,
        }
        self._quota_turns: set[str] = set()
        self._transient_turns: set[str] = set()
        self._invalid_turns: set[str] = set()
        self._fallback_turns: set[str] = set()

    def key_rotation(self) -> None:
        self._counts["key_rotations"] += 1

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
    environ: Mapping[str, str], primary_env: str, pool_env: str | None = None
) -> tuple[str, ...]:
    candidates = [environ.get(primary_env, "")]
    if pool_env:
        candidates.extend(environ.get(pool_env, "").split(","))
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
    if status == 429:
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

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, "
            f"key_count={len(self._keys)})"
        )

    async def complete(
        self, prompt: str, *, deadline: float, turn_id: str
    ) -> dict[str, Any]:
        for key_index, key in enumerate(self._keys):
            transient_attempts = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DecisionDeadlineExceeded("decision deadline exhausted")
                try:
                    async with asyncio.timeout(remaining):
                        return await self._backend.complete(
                            prompt, api_key=key, deadline=deadline
                        )
                except Exception as raw:
                    error = _classified(raw)
                    if isinstance(error, QuotaExceeded):
                        self._metrics.quota(turn_id)
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
    ) -> None:
        self.kind = kind
        self.model = model
        self.response_schema = response_schema
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url

    async def complete(
        self, prompt: str, *, api_key: str, deadline: float
    ) -> dict[str, Any]:
        if self.kind == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            model = ChatGoogleGenerativeAI(
                model=self.model, api_key=api_key, temperature=0,
                retries=0, request_timeout=self.timeout_seconds,
            )
        elif self.kind == "anthropic":
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(
                model_name=self.model, api_key=api_key, temperature=0,
                max_retries=0, timeout=self.timeout_seconds,
            )
        else:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(
                model=self.model, api_key=api_key, base_url=self.base_url,
                temperature=0, max_retries=0, timeout=self.timeout_seconds,
            )
        structured = model.with_structured_output(
            self.response_schema, method="json_schema"
        )
        result = await structured.ainvoke(prompt)
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)


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
        metrics: DecisionMetrics | None = None,
    ) -> None:
        super().__init__(
            name, keys,
            _LangChainBackend(
                kind="openai", model=model, response_schema=response_schema,
                timeout_seconds=timeout_seconds, base_url=base_url,
            ),
            metrics=metrics,
        )


class AnthropicDecisionProvider(KeyRotatingProvider):
    def __init__(
        self, keys: Sequence[str], *, model: str, response_schema: type,
        timeout_seconds: float, metrics: DecisionMetrics | None = None,
    ) -> None:
        super().__init__(
            "anthropic", keys,
            _LangChainBackend(
                kind="anthropic", model=model, response_schema=response_schema,
                timeout_seconds=timeout_seconds,
            ),
            metrics=metrics,
        )
