import asyncio
import time

import pytest

from ludex_agent.graph.provider import (
    CompletionUsage,
    DecisionDeadlineExceeded,
    DecisionMetrics,
    FatalProviderError,
    KeyRotatingProvider,
    ModelRoute,
    ProviderCompletion,
    ProviderChain,
    ProviderPoolExhausted,
    QuotaExceeded,
    TransientProviderError,
    load_model_routes,
    model_route,
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
        if isinstance(response, ProviderCompletion):
            return response
        return ProviderCompletion(
            payload=response,
            usage=CompletionUsage(input_tokens=0, output_tokens=0),
        )


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


def test_gemini_acepta_google_como_alias_secundario_sin_duplicar():
    keys = provider_keys(
        {
            "GEMINI_API_KEY": "gemini-primary",
            "GEMINI_API_KEYS": "shared,gemini-pool",
            "GOOGLE_API_KEY": "google-primary",
            "GOOGLE_API_KEYS": "shared,google-pool",
        },
        "GEMINI_API_KEY",
        "GEMINI_API_KEYS",
        aliases=(("GOOGLE_API_KEY", "GOOGLE_API_KEYS"),),
    )
    assert keys == (
        "gemini-primary", "shared", "gemini-pool",
        "google-primary", "google-pool",
    )


def test_rutas_de_modelo_eligen_el_protocolo_real():
    routes = load_model_routes()

    assert model_route(
        routes, "open_code_zen", "minimax-m2.7"
    ) == ModelRoute(protocol="chat_completions")
    assert model_route(
        routes, "open_code_zen", "deepseek-v4-pro"
    ) == ModelRoute(protocol="chat_completions")
    assert model_route(
        routes, "open_code_zen", "qwen3.5-plus"
    ) == ModelRoute(protocol="messages")
    assert model_route(
        routes, "open_code_zen", "qwen3.6-plus"
    ) == ModelRoute(protocol="messages")
    assert model_route(
        routes, "kimi", "kimi-k2.6"
    ) == ModelRoute(
        protocol="chat_completions",
        temperature=1.0,
        thinking="enabled",
        max_tokens=16_000,
    )


def test_modelo_sin_ruta_falla_antes_de_llamar_al_proveedor():
    with pytest.raises(ValueError, match="sin ruta"):
        model_route(load_model_routes(), "open_code_zen", "modelo-inventado")


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
async def test_usage_se_suma_desde_cada_respuesta_exitosa():
    metrics = DecisionMetrics()
    usage = CompletionUsage(
        input_tokens=120,
        output_tokens=30,
        cached_input_tokens=20,
        reasoning_tokens=10,
        model="minimax-m2.7",
    )
    backend = ScriptedBackend([
        ProviderCompletion(payload={"turn": 1}, usage=usage),
        ProviderCompletion(payload={"turn": 2}, usage=usage),
    ])
    provider = KeyRotatingProvider(
        "open_code_zen", ("key-a",), backend, metrics=metrics
    )

    assert await provider.complete(
        "turno 1", deadline=time.monotonic() + 1, turn_id="battle:1"
    ) == {"turn": 1}
    assert await provider.complete(
        "turno 2", deadline=time.monotonic() + 1, turn_id="battle:2"
    ) == {"turn": 2}

    snapshot = metrics.snapshot()
    assert snapshot["calls_total"] == 2
    assert snapshot["input_tokens"] == 240
    assert snapshot["output_tokens"] == 60
    assert snapshot["cached_input_tokens"] == 40
    assert snapshot["reasoning_tokens"] == 20


@pytest.mark.asyncio
async def test_429_envuelto_por_langchain_tambien_rota_la_clave():
    metrics = DecisionMetrics()
    wrapped = RuntimeError(
        "Error calling model 'gemini-2.5-flash' "
        "(RESOURCE_EXHAUSTED): 429 RESOURCE_EXHAUSTED"
    )
    backend = ScriptedBackend([wrapped, {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics
    )

    result = await provider.complete(
        "mismo prompt", deadline=time.monotonic() + 1, turn_id="battle:wrapped"
    )

    assert result == {"ok": True}
    assert backend.calls == [
        ("mismo prompt", "key-a"),
        ("mismo prompt", "key-b"),
    ]
    assert metrics.snapshot()["key_rotations"] == 1
    assert metrics.snapshot()["turns_quota_affected"] == 1


@pytest.mark.asyncio
async def test_clave_agotada_no_se_vuelve_a_probar_en_turnos_siguientes():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        QuotaExceeded("quota"),
        {"turn": 1},
        {"turn": 2},
    ])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics
    )

    first = await provider.complete(
        "turno 1", deadline=time.monotonic() + 1, turn_id="battle:1"
    )
    second = await provider.complete(
        "turno 2", deadline=time.monotonic() + 1, turn_id="battle:2"
    )

    assert first == {"turn": 1}
    assert second == {"turn": 2}
    assert backend.calls == [
        ("turno 1", "key-a"),
        ("turno 1", "key-b"),
        ("turno 2", "key-b"),
    ]
    assert metrics.snapshot()["key_rotations"] == 1


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
    metrics = DecisionMetrics()
    provider = KeyRotatingProvider("google", ("a",), backend, metrics=metrics)

    with pytest.raises(DecisionDeadlineExceeded):
        await provider.complete("p", deadline=time.monotonic() - 1, turn_id="t")
    assert backend.calls == []
    assert metrics.snapshot()["turns_deadline_affected"] == 1


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
            return ProviderCompletion(
                payload={"ok": True},
                usage=CompletionUsage(input_tokens=1, output_tokens=1),
            )

    backend = TimeoutBackend()
    provider = KeyRotatingProvider(
        "google", ("a",), backend, transient_retries=1
    )
    assert await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    ) == {"ok": True}
