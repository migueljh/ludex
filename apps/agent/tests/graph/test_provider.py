import asyncio
import time

import pytest

from ludex_agent.graph.provider import (
    DecisionDeadlineExceeded,
    DecisionMetrics,
    FatalProviderError,
    KeyRotatingProvider,
    ProviderChain,
    ProviderPoolExhausted,
    QuotaExceeded,
    TransientProviderError,
    provider_keys,
)


class ScriptedBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, prompt, *, api_key, deadline):
        self.calls.append((prompt, api_key))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_pool_prioriza_principal_descarta_vacias_y_deduplica():
    keys = provider_keys(
        {
            "GOOGLE_API_KEY": "principal",
            "GOOGLE_API_KEYS": " , secundaria,principal,secundaria, tercera ",
        },
        "GOOGLE_API_KEY",
        "GOOGLE_API_KEYS",
    )
    assert keys == ("principal", "secundaria", "tercera")


@pytest.mark.asyncio
async def test_429_rota_clave_y_repite_exactamente_el_mismo_prompt():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([QuotaExceeded("quota"), {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics
    )

    result = await provider.complete("mismo prompt", deadline=time.monotonic() + 1,
                                     turn_id="battle:1")

    assert result == {"ok": True}
    assert backend.calls == [("mismo prompt", "key-a"), ("mismo prompt", "key-b")]
    assert metrics.snapshot()["key_rotations"] == 1
    assert metrics.snapshot()["turns_quota_affected"] == 1


@pytest.mark.asyncio
async def test_transitorio_reintenta_misma_clave_y_prompt():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([TransientProviderError("5xx"), {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-a",), backend, metrics=metrics, transient_retries=1
    )

    await provider.complete("prompt", deadline=time.monotonic() + 1,
                            turn_id="battle:2")

    assert backend.calls == [("prompt", "key-a"), ("prompt", "key-a")]
    assert metrics.snapshot()["turns_transient_affected"] == 1
    assert metrics.snapshot()["key_rotations"] == 0


@pytest.mark.asyncio
async def test_pool_agotado_falla_ruidosamente_sin_exponer_claves():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([QuotaExceeded(), QuotaExceeded()])
    provider = KeyRotatingProvider(
        "google", ("super-secret-a", "super-secret-b"), backend, metrics=metrics
    )

    with pytest.raises(ProviderPoolExhausted) as exc:
        await provider.complete("prompt", deadline=time.monotonic() + 1,
                                turn_id="battle:3")

    rendered = repr(provider) + repr(exc.value)
    assert "super-secret" not in rendered


@pytest.mark.asyncio
async def test_cadena_cambia_proveedor_solo_en_juego():
    metrics = DecisionMetrics()
    exhausted = KeyRotatingProvider(
        "google", ("a",), ScriptedBackend([QuotaExceeded()]), metrics=metrics
    )
    alternate = KeyRotatingProvider(
        "kimi", ("b",), ScriptedBackend([{"provider": "kimi"}]), metrics=metrics
    )

    play = ProviderChain(
        [exhausted, alternate], allow_cross_provider=True, metrics=metrics
    )
    assert await play.complete("p", deadline=time.monotonic() + 1,
                               turn_id="battle:4") == {"provider": "kimi"}
    assert metrics.snapshot()["provider_switches"] == 1

    benchmark = ProviderChain(
        [KeyRotatingProvider(
            "google", ("a",), ScriptedBackend([QuotaExceeded()]), metrics=metrics
        ), alternate],
        allow_cross_provider=False,
        metrics=metrics,
    )
    with pytest.raises(ProviderPoolExhausted):
        await benchmark.complete("p", deadline=time.monotonic() + 1,
                                 turn_id="benchmark:1")


@pytest.mark.asyncio
async def test_deadline_expirado_no_invoca_proveedor():
    backend = ScriptedBackend([{"never": True}])
    provider = KeyRotatingProvider("google", ("a",), backend)

    with pytest.raises(DecisionDeadlineExceeded):
        await provider.complete("p", deadline=time.monotonic() - 1, turn_id="t")
    assert backend.calls == []


def test_errores_no_contienen_secretos_por_representacion():
    assert "secret" not in repr(QuotaExceeded())
    assert issubclass(FatalProviderError, Exception)


@pytest.mark.asyncio
async def test_timeout_asyncio_se_clasifica_como_transitorio():
    class TimeoutBackend:
        calls = 0

        async def complete(self, prompt, *, api_key, deadline):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError
            return {"ok": True}

    backend = TimeoutBackend()
    provider = KeyRotatingProvider(
        "google", ("a",), backend, transient_retries=1
    )
    assert await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    ) == {"ok": True}
