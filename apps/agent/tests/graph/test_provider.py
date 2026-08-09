import asyncio
import logging
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APITimeoutError as AnthropicAPITimeoutError
from openai import APITimeoutError as OpenAIAPITimeoutError
from pydantic import BaseModel, Field, ValidationError

from ludex_agent.graph.decision import DecisionResponse

from ludex_agent.graph.provider import (
    CompletionEnvelope,
    CompletionUsage,
    DecisionDeadlineExceeded,
    DecisionMetrics,
    FatalProviderError,
    KeyRotatingProvider,
    ModelRoute,
    PinnedResolver,
    ProviderChain,
    ProviderCompletion,
    ProviderMixError,
    ProviderPoolExhausted,
    QuotaExceeded,
    TransientProviderError,
    _LangChainBackend,
    _classified,
    _redacted,
    anthropic_sdk_base_url,
    load_model_routes,
    message_text_content,
    model_route,
    provider_keys,
    provider_response_schema,
    structured_output_method,
    text_json_payload,
)


def test_fallo_inesperado_conserva_tipo_pero_no_mensaje_sensible():
    error = RuntimeError("request failed with api_key=secret-value")

    classified = _classified(error)

    assert str(classified) == "unexpected provider failure (RuntimeError)"
    assert "secret-value" not in str(classified)


def test_validation_error_informa_solo_tipo_y_ubicacion():
    class Positive(BaseModel):
        value: int = Field(gt=0)

    try:
        Positive(value=-3)
    except ValidationError as raw:
        classified = _classified(raw)

    assert str(classified) == (
        "unexpected provider failure "
        "(ValidationError: greater_than@value)"
    )
    assert "-3" not in str(classified)


def test_base_anthropic_quita_v1_que_el_sdk_agrega_al_endpoint_messages():
    assert anthropic_sdk_base_url(
        "https://opencode.ai/zen/v1"
    ) == "https://opencode.ai/zen"
    assert anthropic_sdk_base_url(
        "https://api.anthropic.com"
    ) == "https://api.anthropic.com"
    assert anthropic_sdk_base_url(None) is None


def test_compatibilidad_anthropic_usa_tools_y_nativo_conserva_json_schema():
    assert structured_output_method("anthropic", None) == "json_schema"
    assert structured_output_method(
        "anthropic", "https://opencode.ai/zen/v1"
    ) == "text_json"
    assert structured_output_method(
        "openai", "https://opencode.ai/zen/v1"
    ) == "json_schema"


def test_json_textual_se_parsea_y_markdown_se_trata_como_respuesta_invalida():
    assert text_json_payload(
        '{"action":{"kind":"move","id":"tackle"},"reasoning":"safe"}'
    ) == {
        "action": {"kind": "move", "id": "tackle"},
        "reasoning": "safe",
    }
    assert text_json_payload("```json\n{\"action\": {}}\n```") == {
        "_invalid_response": "```json\n{\"action\": {}}\n```"
    }


def test_bloques_anthropic_extraen_el_texto_sin_serializar_el_envoltorio():
    assert message_text_content([
        {"type": "text", "text": '{"action":{"kind":"move","id":"tackle"}}'},
    ]) == '{"action":{"kind":"move","id":"tackle"}}'
    assert message_text_content("plain") == "plain"


def test_backend_envia_schema_dict_y_deja_validacion_semantica_a_decide():
    class Response(BaseModel):
        action: dict

    schema = provider_response_schema(Response)

    assert isinstance(schema, dict)
    assert schema["properties"]["action"]["type"] == "object"


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
    ) == ModelRoute(
        protocol="chat_completions", structured_output="json_schema",
        timeout_seconds=60,
    )
    assert model_route(
        routes, "open_code_zen", "deepseek-v4-pro"
    ) == ModelRoute(
        protocol="chat_completions", structured_output="json_schema",
        timeout_seconds=60,
    )
    assert model_route(
        routes, "open_code_zen", "deepseek-v4-flash"
    ) == ModelRoute(
        protocol="chat_completions", structured_output="json_schema",
        timeout_seconds=60,
    )
    assert model_route(
        routes, "open_code_zen", "gpt-5.5"
    ) == ModelRoute(
        protocol="responses", structured_output="text_json",
        timeout_seconds=120,
    )
    assert model_route(
        routes, "open_code_zen", "grok-4.5"
    ) == ModelRoute(
        protocol="responses", structured_output="text_json",
        timeout_seconds=120,
    )
    assert model_route(
        routes, "open_code_zen", "claude-haiku-4-5"
    ) == ModelRoute(
        protocol="messages", structured_output="text_json",
        timeout_seconds=60,
    )
    assert model_route(
        routes, "open_code_zen", "gemini-3.6-flash"
    ) == ModelRoute(
        protocol="google", structured_output="json_schema",
        timeout_seconds=60,
    )
    assert model_route(
        routes, "open_code_zen", "qwen3.5-plus"
    ) == ModelRoute(
        protocol="messages", structured_output="text_json",
        max_tokens=1024,
        timeout_seconds=60,
    )
    assert model_route(
        routes, "open_code_zen", "qwen3.6-plus"
    ) == ModelRoute(
        protocol="messages", structured_output="text_json",
        timeout_seconds=60,
    )
    assert model_route(
        routes, "kimi", "kimi-k2.6"
    ) == ModelRoute(
        protocol="chat_completions", structured_output="json_schema",
        temperature=1.0,
        thinking="enabled",
        max_tokens=16_000,
        timeout_seconds=120,
    )


def test_modelo_sin_ruta_falla_antes_de_llamar_al_proveedor():
    with pytest.raises(ValueError, match="sin ruta"):
        model_route(load_model_routes(), "open_code_zen", "modelo-inventado")


@pytest.mark.asyncio
async def test_backend_messages_aplica_timeout_y_limite_de_salida(monkeypatch):
    captured = {}

    class FakeMessage:
        content = '{"action":{"kind":"move","id":"tackle"},"reasoning":"ok"}'
        usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 5,
        }
        response_metadata = {}

    class FakeChatAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def ainvoke(self, prompt):
            return FakeMessage()

    monkeypatch.setattr(
        "langchain_anthropic.ChatAnthropic", FakeChatAnthropic
    )
    backend = _LangChainBackend(
        kind="anthropic",
        model="qwen3.5-plus",
        response_schema=BaseModel,
        timeout_seconds=30,
        base_url="https://opencode.ai/zen/v1",
        route=ModelRoute(
            protocol="messages",
            max_tokens=1024,
            timeout_seconds=60,
        ),
    )

    result = await backend.complete(
        "prompt", api_key="secret", deadline=time.monotonic() + 240
    )

    assert result.payload["action"]["id"] == "tackle"
    assert captured["timeout"] == 60
    assert captured["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_429_rota_clave_y_repite_exactamente_el_mismo_prompt():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([QuotaExceeded("quota"), {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics
    )

    result = await provider.complete("mismo prompt", deadline=time.monotonic() + 1,
                                     turn_id="battle:1")

    assert result.payload == {"ok": True}
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

    assert (await provider.complete(
        "turno 1", deadline=time.monotonic() + 1, turn_id="battle:1"
    )).payload == {"turn": 1}
    assert (await provider.complete(
        "turno 2", deadline=time.monotonic() + 1, turn_id="battle:2"
    )).payload == {"turn": 2}

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

    assert result.payload == {"ok": True}
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

    assert first.payload == {"turn": 1}
    assert second.payload == {"turn": 2}
    assert backend.calls == [
        ("turno 1", "key-a"),
        ("turno 1", "key-b"),
        ("turno 2", "key-b"),
    ]
    assert metrics.snapshot()["key_rotations"] == 1


class FakeClock:
    """Reloj inyectable para probar enfriamiento sin depender de que un
    `time.sleep` real termine antes de que expire la ventana de la prueba."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_deadline_se_evalua_con_el_reloj_inyectado_no_con_time_monotonic():
    """F2-10: `KeyRotatingProvider` debe usar `self._clock()` para decidir si
    el deadline venció. Si se filtra `time.monotonic()`, este test falla:
    construimos un deadline en el futuro del reloj real pero en el pasado
    del reloj falso; avanzamos el falso más allá del deadline y exigimos que
    complete() lo detecte SIN invocar el backend."""
    # time.monotonic() real es del orden de segundos desde el boot
    # (típicamente < 1e9); 1e12 está lejos en el futuro real.
    clock = FakeClock(start=1e12)
    backend = ScriptedBackend([{"never": True}])
    provider = KeyRotatingProvider(
        "google", ("k",), backend, clock=clock
    )

    deadline = clock.now + 5.0
    clock.advance(10.0)  # el reloj falso superó el deadline; el real no.

    with pytest.raises(DecisionDeadlineExceeded):
        await provider.complete("p", deadline=deadline, turn_id="t")

    assert backend.calls == []
    assert provider._metrics.snapshot()["turns_deadline_affected"] == 1


@pytest.mark.asyncio
async def test_cooldown_y_deadline_usan_el_mismo_reloj_inyectado():
    """F2-10: el cooldown de una clave y el deadline del turno deben vivir en
    la misma escala de tiempo; si usaran relojes distintos, una clave podría
    parecer disponible cuando el deadline ya venció o viceversa."""
    clock = FakeClock(start=0.0)
    backend = ScriptedBackend([
        QuotaExceeded("quota"),
        QuotaExceeded("quota"),
        {"ok": True},
    ])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, clock=clock,
        quota_cooldown_seconds=30.0,
    )

    # Turno 1: ambas claves dan 429. El deadline es demasiado corto para
    # esperar el cooldown, así que la decisión aborta.
    with pytest.raises(ProviderPoolExhausted):
        await provider.complete(
            "turno 1", deadline=clock.now + 10, turn_id="battle:1"
        )

    # Avanzamos 20s: ambas claves todavía enfriándose (quedan 10s).
    clock.advance(20.0)
    # El deadline del turno 2 vence en 5s, antes de que se libere ninguna.
    with pytest.raises(ProviderPoolExhausted):
        await provider.complete(
            "turno 2", deadline=clock.now + 5, turn_id="battle:2"
        )

    # Avanzamos 15s más: key-a ya está disponible (30s totales).
    clock.advance(15.0)
    second = await provider.complete(
        "turno 3", deadline=clock.now + 10, turn_id="battle:3"
    )
    assert second.payload == {"ok": True}
    assert backend.calls == [
        ("turno 1", "key-a"),
        ("turno 1", "key-b"),
        ("turno 3", "key-a"),
    ]


@pytest.mark.asyncio
async def test_clave_enfriada_no_se_reintenta_antes_de_que_expire_el_enfriamiento():
    """Canario (mitad 1 de 2, D25): revivir el bug de las 98 rotaciones sería
    volver a probar key-a en el turno 2 aunque su enfriamiento de 30s recién
    lleva 10s. Tiene que seguir descartada -- igual que con la exclusión
    permanente que esto reemplaza -- hasta que el enfriamiento expire de
    verdad."""
    clock = FakeClock()
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        QuotaExceeded("quota"),
        {"turn": 1},
        {"turn": 2},
    ])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics,
        clock=clock, quota_cooldown_seconds=30.0,
    )

    first = await provider.complete(
        "turno 1", deadline=time.monotonic() + 1, turn_id="battle:1"
    )
    clock.advance(10.0)  # menos que los 30s de enfriamiento
    second = await provider.complete(
        "turno 2", deadline=time.monotonic() + 1, turn_id="battle:2"
    )

    assert first.payload == {"turn": 1}
    assert second.payload == {"turn": 2}
    assert backend.calls == [
        ("turno 1", "key-a"),
        ("turno 1", "key-b"),
        ("turno 2", "key-b"),
    ]


@pytest.mark.asyncio
async def test_clave_enfriada_vuelve_a_estar_disponible_tras_el_enfriamiento():
    """Canario (mitad 2 de 2, D25): la razón de todo este cambio es que la
    exclusión permanente anterior NUNCA dejaba volver a una clave, ni
    siquiera cuando el límite era por minuto (el caso real de Gemini,
    verificado: 39 llamadas y 10 rotaciones con 11 claves configuradas,
    evals/runs/20260728-gemini25flash-5.json). Pasado el enfriamiento,
    key-a -- la preferida, primera en el pool -- tiene que volver a
    intentarse antes que key-b."""
    clock = FakeClock()
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        QuotaExceeded("quota"),
        {"turn": 1},
        {"turn": 2},
    ])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics,
        clock=clock, quota_cooldown_seconds=30.0,
    )

    await provider.complete(
        "turno 1", deadline=time.monotonic() + 1, turn_id="battle:1"
    )
    clock.advance(31.0)  # ya expiró el enfriamiento de 30s
    second = await provider.complete(
        "turno 2", deadline=time.monotonic() + 1, turn_id="battle:2"
    )

    assert second.payload == {"turn": 2}
    assert backend.calls == [
        ("turno 1", "key-a"),
        ("turno 1", "key-b"),
        ("turno 2", "key-a"),
    ]


@pytest.mark.asyncio
async def test_todas_las_claves_enfriando_espera_si_el_deadline_alcanza():
    """Si TODAS las claves están enfriándose pero el deadline del turno deja
    tiempo de sobra para que la primera se libere, hay que esperar en vez de
    rendirse: el turno todavía puede resolverse con la misma llamada."""
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        QuotaExceeded("quota"),
        QuotaExceeded("quota"),
        {"ok": True},
    ])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics,
        quota_cooldown_seconds=0.05,
    )

    result = await provider.complete(
        "p", deadline=time.monotonic() + 2, turn_id="t"
    )

    assert result.payload == {"ok": True}
    assert backend.calls == [("p", "key-a"), ("p", "key-b"), ("p", "key-a")]


@pytest.mark.asyncio
async def test_todas_las_claves_enfriando_no_espera_si_el_deadline_no_alcanza():
    """Si el enfriamiento no va a expirar antes del deadline del turno,
    `ProviderPoolExhausted` tiene que salir YA -- no esperar 5s con un
    deadline de 50ms."""
    metrics = DecisionMetrics()
    backend = ScriptedBackend([QuotaExceeded("quota"), QuotaExceeded("quota")])
    provider = KeyRotatingProvider(
        "google", ("key-a", "key-b"), backend, metrics=metrics,
        quota_cooldown_seconds=5.0,
    )

    with pytest.raises(ProviderPoolExhausted):
        await provider.complete(
            "p", deadline=time.monotonic() + 0.05, turn_id="t"
        )


def test_quota_extrae_retry_delay_del_formato_documentado_por_langchain_google_genai():
    """`langchain_google_genai` (ver `_common.py`, campo `max_retries` de
    `ChatGoogleGenerativeAI`) documenta este MISMO formato como la forma
    soportada de recuperar `retry_delay` de un 429 de Gemini: el SDK lo
    aplana a texto y descarta la estructura. No es una regex inventada."""
    wrapped = RuntimeError(
        "Error calling model 'gemini-2.5-flash' (RESOURCE_EXHAUSTED): "
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429}} "
        "[retry_delay {\n  seconds: 42\n}\n]"
    )

    classified = _classified(wrapped)

    assert isinstance(classified, QuotaExceeded)
    assert classified.retry_after == 42.0


def test_quota_sin_retry_delay_deja_retry_after_en_none():
    classified = _classified(RuntimeError(
        "Error calling model 'gemini-2.5-flash' (RESOURCE_EXHAUSTED): "
        "429 RESOURCE_EXHAUSTED"
    ))

    assert isinstance(classified, QuotaExceeded)
    assert classified.retry_after is None


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
    assert (await play.complete("p", deadline=time.monotonic() + 1,
                                turn_id="battle:4")).payload == {"provider": "kimi"}
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
async def test_error_transitorio_preserva_tipo_mensaje_y_causa_original(caplog):
    """Diagnóstico bloqueado (D25): reclasificar sin conservar la excepción
    original hacía indistinguibles un ConnectError, un ReadTimeout y un
    PoolTimeout detrás del mismo "provider transport failed" — exactamente
    lo que impedía diagnosticar los abortos de Kimi/DeepSeek/Gemini. La
    clasificación pública (`str(classified)`) tiene que seguir fija (termina
    en `evals/runs/*.json`, que se commitea), pero `__cause__` y el log
    tienen que exponer el original completo."""
    original = httpx.ConnectError(
        "Connection reset by peer while decoding turn 42"
    )

    class FlakyBackend:
        async def complete(self, prompt, *, api_key, deadline):
            raise original

    metrics = DecisionMetrics()
    provider = KeyRotatingProvider(
        "kimi", ("k",), FlakyBackend(), metrics=metrics, transient_retries=0
    )

    with caplog.at_level(
        logging.WARNING, logger="ludex_agent.graph.provider"
    ):
        with pytest.raises(TransientProviderError) as exc_info:
            await provider.complete(
                "p", deadline=time.monotonic() + 1, turn_id="t"
            )

    raised = exc_info.value
    # La clasificación pública no cambia: sigue siendo el mensaje fijo, sin
    # el detalle del proveedor (ese detalle podría contener información que
    # no queremos en un JSON commiteado).
    assert str(raised) == "provider transport failed"
    # Pero la causa original queda enganchada para quien inspeccione la
    # excepción (debugger, `traceback.print_exc()`, etc.).
    assert raised.__cause__ is original
    assert isinstance(raised.__cause__, httpx.ConnectError)
    # Y el log —que no se commitea a ningún lado— sí trae tipo y mensaje.
    assert any(
        "ConnectError" in record.getMessage()
        and "Connection reset by peer while decoding turn 42" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_el_log_de_clasificacion_censura_la_clave_del_mensaje(caplog):
    """El mensaje de error de un proveedor NO es texto de confianza: Gemini
    manda la clave en el query string, así que un 429 o un error de
    transporte puede arrastrar la clave entera dentro de `str(exc)`. Ese
    texto crudo se loguea (es la única forma de diagnosticar el transporte),
    y la regla del proyecto es que una clave no se imprime nunca. El detalle
    diagnóstico —el tipo real de la excepción y la URL— tiene que sobrevivir
    a la censura, o el log deja de servir para lo que se agregó."""
    original = httpx.ConnectError(
        "Connection reset for url "
        "'https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=AIzaSyD-clave-de-prueba-000'"
    )

    class LeakyBackend:
        async def complete(self, prompt, *, api_key, deadline):
            raise original

    provider = KeyRotatingProvider(
        "google", ("k",), LeakyBackend(), transient_retries=0
    )

    with caplog.at_level(
        logging.WARNING, logger="ludex_agent.graph.provider"
    ):
        with pytest.raises(TransientProviderError):
            await provider.complete(
                "p", deadline=time.monotonic() + 1, turn_id="t"
            )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "AIzaSyD-clave-de-prueba-000" not in logged
    assert "key=<redacted>" in logged
    # La censura no puede comerse el diagnóstico: sin el tipo y la URL, el
    # log no distingue un ConnectError de un ReadTimeout, que es lo único
    # para lo que existe.
    assert "ConnectError" in logged
    assert "generativelanguage.googleapis.com" in logged
    # `exc_info` reimprimiría el mensaje crudo sin pasar por la censura.
    assert all(record.exc_info is None for record in caplog.records)


def test_la_censura_no_deja_pasar_las_formas_de_clave_conocidas():
    censurado = _redacted(
        "AIzaSyD-000000000000000000 y sk-proj-abcdefghijklmnop y "
        "Authorization: Bearer sk-ant-0123456789abcdef y ?api_key=zzzzzzzzzzzz"
    )
    for secreto in (
        "AIzaSyD-000000000000000000",
        "sk-proj-abcdefghijklmnop",
        "sk-ant-0123456789abcdef",
        "zzzzzzzzzzzz",
    ):
        assert secreto not in censurado


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
    assert (await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    )).payload == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout_error",
    [
        OpenAIAPITimeoutError(httpx.Request("POST", "https://provider.test")),
        AnthropicAPITimeoutError(
            httpx.Request("POST", "https://provider.test")
        ),
    ],
)
async def test_timeout_de_sdk_se_reintenta_como_infraestructura(timeout_error):
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        timeout_error,
        ProviderCompletion(
            payload={"ok": True},
            usage=CompletionUsage(input_tokens=3, output_tokens=1),
        ),
    ])
    provider = KeyRotatingProvider(
        "provider", ("a",), backend, metrics=metrics, transient_retries=1
    )

    result = await provider.complete(
        "mismo prompt", deadline=time.monotonic() + 1, turn_id="battle:9"
    )

    assert result.payload == {"ok": True}
    assert backend.calls == [
        ("mismo prompt", "a"),
        ("mismo prompt", "a"),
    ]
    assert metrics.snapshot()["turns_transient_affected"] == 1
    assert metrics.snapshot()["calls_total"] == 1


# --- F2-08 (MON-13): envelope inmutable por llamada ------------------------


async def test_complete_devuelve_envelope_inmutable_y_completo():
    usage = CompletionUsage(
        input_tokens=10, output_tokens=5, cached_input_tokens=2,
        reasoning_tokens=1, model="minimax-m2.7",
    )
    backend = ScriptedBackend([
        ProviderCompletion(payload={"action": "x"}, usage=usage),
    ])
    provider = KeyRotatingProvider(
        "open_code_zen", ("k",), backend, model="minimax-m2.7"
    )

    envelope = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="battle:1"
    )

    assert envelope.payload == {"action": "x"}
    assert envelope.provider == "open_code_zen"
    # El model efectivo reportado por el proveedor gana sobre el configurado.
    assert envelope.model == "minimax-m2.7"
    assert envelope.usage is usage
    assert envelope.latency_ms >= 0
    with pytest.raises(FrozenInstanceError):
        envelope.payload = {}  # type: ignore[misc]


async def test_envelope_model_efectivo_cae_al_configurado_sin_reporte():
    backend = ScriptedBackend([{"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("k",), backend, model="gemini-2.5-flash"
    )

    envelope = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="battle:2"
    )

    assert envelope.provider == "google"
    assert envelope.model == "gemini-2.5-flash"


def test_metricas_de_latencia_calculan_p50_p95_y_max():
    metrics = DecisionMetrics()

    for latency in [10.0, 20.0, 30.0, 40.0, 50.0, 100.0, 200.0]:
        metrics.completion_latency(latency)

    snapshot = metrics.snapshot()
    assert snapshot["completion_latency_ms_count"] == 7
    assert snapshot["completion_latency_ms_total"] == 450
    assert snapshot["completion_latency_ms_max"] == 200
    assert snapshot["completion_latency_ms_p50"] == 40
    assert snapshot["completion_latency_ms_p95"] == 170
    assert snapshot["decision_latency_ms_count"] == 0


def test_metricas_de_latencia_rechazan_valores_negativos():
    with pytest.raises(ValueError, match="latency cannot be negative"):
        DecisionMetrics().completion_latency(-1.0)
    with pytest.raises(ValueError, match="latency cannot be negative"):
        DecisionMetrics().decision_latency(-1.0)


def test_metricas_de_latencia_no_truncan_99999_ni_redondean_a_cero():
    """L-01 (R2): la politica de redondeo es entero mas cercano via
    `round()`; truncar con `int()` dejaria 99.999... en 99 y es una
    regresion prohibida (mutacion dedicada)."""
    metrics = DecisionMetrics()
    metrics.completion_latency(99.99999)
    snapshot = metrics.snapshot()
    assert snapshot["completion_latency_ms_total"] == 100
    assert snapshot["completion_latency_ms_max"] == 100
    assert snapshot["completion_latency_ms_p50"] == 100
    assert snapshot["completion_latency_ms_p95"] == 100


def test_percentiles_sin_muestras_son_none_nunca_cero_comparable():
    """L-01 (R2): una poblacion sin muestras deja total/p50/p95/max en None
    (null en artefactos, blanco en el ledger), nunca 0/0/0."""
    snapshot = DecisionMetrics().snapshot()
    assert snapshot["completion_latency_ms_count"] == 0
    assert snapshot["completion_latency_ms_total"] is None
    assert snapshot["completion_latency_ms_p50"] is None
    assert snapshot["completion_latency_ms_p95"] is None
    assert snapshot["completion_latency_ms_max"] is None
    assert snapshot["decision_latency_ms_count"] == 0
    assert snapshot["decision_latency_ms_total"] is None
    assert snapshot["decision_latency_ms_p50"] is None
    assert snapshot["decision_latency_ms_p95"] is None
    assert snapshot["decision_latency_ms_max"] is None


def test_completion_y_decision_son_poblaciones_disjuntas():
    """L-01 (R2): ninguna muestra entra en ambas poblaciones. Una completion
    de 100 ms y una decision de 250 ms son dos poblaciones de una muestra
    cada una, no una poblacion de dos muestras."""
    metrics = DecisionMetrics()
    metrics.completion_latency(100.0)
    metrics.decision_latency(250.0)
    snapshot = metrics.snapshot()
    assert snapshot["completion_latency_ms_count"] == 1
    assert snapshot["completion_latency_ms_total"] == 100
    assert snapshot["completion_latency_ms_max"] == 100
    assert snapshot["decision_latency_ms_count"] == 1
    assert snapshot["decision_latency_ms_total"] == 250
    assert snapshot["decision_latency_ms_max"] == 250


async def test_envelope_latencia_mide_la_llamada_con_reloj_inyectable():
    clock = FakeClock(start=1000.0)

    class AdvancingBackend:
        async def complete(self, prompt, *, api_key, deadline):
            clock.advance(0.25)
            return ProviderCompletion(
                payload={"ok": True},
                usage=CompletionUsage(input_tokens=0, output_tokens=0),
            )

    provider = KeyRotatingProvider(
        "google", ("k",), AdvancingBackend(), model="m", clock=clock
    )

    envelope = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="battle:3"
    )

    assert envelope.latency_ms == 250.0


async def test_envelopes_concurrentes_no_se_cruzan_metadata():
    """El runner ejecuta batallas CONCURRENTES sobre UNA SOLA instancia de
    `KeyRotatingProvider` compartida: dos decisiones en paralelo dentro del
    mismo proceso llaman al MISMO provider. Cada envelope tiene que traer la
    metadata de SU propia llamada.

    El patron rechazado `last_*` (estado mutable por instancia leido despues
    de un await) cruza metadata: la llamada lenta queda suspendida, la rapida
    completa primero y, cuando la lenta se reanuda, lee el estado del vecino.
    Aca el envelope se construye DENTRO de `complete`, con datos locales de
    esa llamada, y no hay ninguna lectura posterior de estado compartido: la
    mutacion `last_*` por instancia pone este test rojo por payload/model
    cruzados (verificacion de la revision R1)."""
    started = asyncio.Event()
    release_first = asyncio.Event()

    class PromptGatedBackend:
        """Un unico backend bloqueante que distingue prompt A y B: A queda
        suspendida hasta que el test la libera; B completa de una."""

        async def complete(self, prompt, *, api_key, deadline):
            if prompt == "prompt A":
                started.set()
                await release_first.wait()
                return ProviderCompletion(
                    payload={"turn": "A"},
                    usage=CompletionUsage(
                        input_tokens=1, output_tokens=1, model="gemini-2.5-flash",
                    ),
                )
            return ProviderCompletion(
                payload={"turn": "B"},
                usage=CompletionUsage(
                    input_tokens=2, output_tokens=2, model="kimi-k2.6",
                ),
            )

    provider = KeyRotatingProvider(
        "google", ("k-a",), PromptGatedBackend(), model="gemini-2.5-flash",
    )

    deadline = time.monotonic() + 5
    task_a = asyncio.create_task(provider.complete(
        "prompt A", deadline=deadline, turn_id="battle:a:1"
    ))
    await started.wait()          # A suspendida dentro del backend
    task_b = asyncio.create_task(provider.complete(
        "prompt B", deadline=deadline, turn_id="battle:b:1"
    ))
    envelope_b = await task_b     # B termina primero
    release_first.set()           # A se reanuda y completa despues
    envelope_a = await task_a

    # Asserts independientes por decision: payload, provider, model y usage.
    assert envelope_a.payload == {"turn": "A"}
    assert envelope_a.provider == "google"
    assert envelope_a.model == "gemini-2.5-flash"
    assert envelope_a.usage.input_tokens == 1
    assert envelope_b.payload == {"turn": "B"}
    assert envelope_b.provider == "google"
    assert envelope_b.model == "kimi-k2.6"
    assert envelope_b.usage.input_tokens == 2


@pytest.mark.asyncio
async def test_pinned_auditor_aborta_si_la_respuesta_efectiva_difiere_del_pin():
    """F2-10/D28: un benchmark fija provider/model al inicio y jamás mezcla.
    Si una respuesta efectiva devuelve otro provider o modelo, la corrida
    aborta con ProviderMixError en vez de contaminar el winrate."""

    class ShapeshiftingProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            return CompletionEnvelope(
                payload={"ok": True},
                provider="google",
                model="gemini-2.5-flash",
                usage=CompletionUsage(input_tokens=1, output_tokens=1),
                latency_ms=0.0,
            )

    resolver = PinnedResolver(
        ShapeshiftingProvider(),
        "kimi",
        "kimi-k2.6",
        enforce_pin=True,
    )
    resolved = await resolver.resolve()

    with pytest.raises(ProviderMixError, match="mezclado"):
        await resolved.provider.complete(
            "p", deadline=time.monotonic() + 1, turn_id="t"
        )


@pytest.mark.asyncio
async def test_pinned_auditor_permite_respuesta_que_coincide_con_el_pin():
    class HonestProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            return CompletionEnvelope(
                payload={"ok": True},
                provider="google",
                model="gemini-2.5-flash",
                usage=CompletionUsage(input_tokens=1, output_tokens=1),
                latency_ms=0.0,
            )

    resolver = PinnedResolver(
        HonestProvider(),
        "google",
        "gemini-2.5-flash",
        enforce_pin=True,
    )
    resolved = await resolver.resolve()
    envelope = await resolved.provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    )
    assert envelope.model == "gemini-2.5-flash"


# --- F2-10B (MON-20): cuarentena de credenciales, pool de 11 claves y
# protocolos declarativos ------------------------------------------------


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/x")
    return httpx.HTTPStatusError(
        "request failed",
        request=request,
        response=httpx.Response(status, request=request),
    )


@pytest.mark.asyncio
async def test_401_cuarentena_solo_esa_clave_y_sigue_con_la_siguiente():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([_http_error(401), {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-bad", "key-ok"), backend, metrics=metrics
    )

    result = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t1"
    )

    assert result.payload == {"ok": True}
    assert backend.calls == [("p", "key-bad"), ("p", "key-ok")]
    assert metrics.snapshot()["keys_quarantined"] == 1
    assert metrics.snapshot()["key_rotations"] == 0
    # La clave en cuarentena NO se vuelve a probar en el turno siguiente.
    backend.responses.append({"ok2": True})
    await provider.complete("p", deadline=time.monotonic() + 1, turn_id="t2")
    assert backend.calls == [
        ("p", "key-bad"), ("p", "key-ok"), ("p", "key-ok"),
    ]


def _http_error_with_body(status: int, body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/x")
    return httpx.HTTPStatusError(
        "request failed",
        request=request,
        response=httpx.Response(status, request=request, json=body),
    )


def _google_403(reason: str) -> httpx.HTTPStatusError:
    return _http_error_with_body(403, {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": reason,
                "domain": "googleapis.com",
            }],
        },
    })


def _openai_403(code: str) -> httpx.HTTPStatusError:
    return _http_error_with_body(403, {
        "error": {"message": "forbidden", "type": "permission_error", "code": code},
    })


@pytest.mark.asyncio
async def test_403_sin_senal_estructurada_es_model_wide_y_detiene():
    """Un 403 sin senal estructurada (o model-wide: ACCESS_DENIED,
    PERMISSION_DENIED, model_not_accessible...) es un error de
    modelo/proyecto/region: se detiene en la PRIMERA clave y no quema el
    pool de 11 (canario del blocker R3 de MON-20)."""
    for error in (_http_error(403), _google_403("ACCESS_DENIED"),
                  _openai_403("model_not_accessible")):
        keys = tuple(f"k{i}" for i in range(11))
        backend = ScriptedBackend([error])
        provider = KeyRotatingProvider(
            "google", keys, backend, metrics=DecisionMetrics()
        )
        with pytest.raises(FatalProviderError):
            await provider.complete(
                "p", deadline=time.monotonic() + 5, turn_id="t"
            )
        assert len(backend.calls) == 1, f"pool quemado para {error}"
        assert backend.calls[0][1] == keys[0]
        assert provider._metrics.snapshot()["keys_quarantined"] == 0


@pytest.mark.asyncio
async def test_403_credential_specific_google_rota_solo_esa_clave():
    """reason=API_KEY_INVALID (senal estructurada de Google) demuestra
    rechazo de credencial: cuarentena de esa clave y sigue con la
    siguiente."""
    metrics = DecisionMetrics()
    backend = ScriptedBackend([_google_403("API_KEY_INVALID"), {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-bad", "key-ok"), backend, metrics=metrics
    )
    result = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    )
    assert result.payload == {"ok": True}
    assert metrics.snapshot()["keys_quarantined"] == 1
    assert metrics.snapshot()["turns_quota_affected"] == 0


@pytest.mark.asyncio
async def test_403_credential_specific_openai_rota_solo_esa_clave():
    metrics = DecisionMetrics()
    backend = ScriptedBackend([_openai_403("invalid_api_key"), {"ok": True}])
    provider = KeyRotatingProvider(
        "open_code_zen", ("key-bad", "key-ok"), backend, metrics=metrics
    )
    result = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    )
    assert result.payload == {"ok": True}
    assert metrics.snapshot()["keys_quarantined"] == 1
    assert metrics.snapshot()["turns_quota_affected"] == 0


@pytest.mark.asyncio
async def test_403_credential_specific_google_a_traves_del_wrapper_langchain():
    """langchain_google_genai envuelve el APIError de Google en
    ChatGoogleGenerativeAIError: la senal estructurada (reason) vive en la
    cadena de causas. La clasificacion debe caminarla."""
    from google.genai.errors import APIError as GoogleAPIError

    api_error = GoogleAPIError(
        code=403,
        response_json={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "details": [{
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_EXPIRED",
                    "domain": "googleapis.com",
                }],
            },
        },
    )
    try:
        raise RuntimeError(
            f"Error calling model 'gemini-2.5-flash' "
            f"(PERMISSION_DENIED): {api_error}"
        ) from api_error
    except RuntimeError as exc:
        wrapper = exc
    metrics = DecisionMetrics()
    backend = ScriptedBackend([wrapper, {"ok": True}])
    provider = KeyRotatingProvider(
        "google", ("key-bad", "key-ok"), backend, metrics=metrics
    )
    result = await provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="t"
    )
    assert result.payload == {"ok": True}
    assert metrics.snapshot()["keys_quarantined"] == 1
    assert metrics.snapshot()["key_rotations"] == 0


@pytest.mark.asyncio
async def test_todas_las_claves_401_terminan_en_pool_exhausted_sin_quemar():
    keys = tuple(f"k{i}" for i in range(11))
    backend = ScriptedBackend([_http_error(401)] * 11)
    provider = KeyRotatingProvider(
        "google", keys, backend, metrics=DecisionMetrics()
    )
    with pytest.raises(ProviderPoolExhausted, match="quarantined"):
        await provider.complete(
            "p", deadline=time.monotonic() + 5, turn_id="t"
        )
    # Exactamente un intento por clave: ninguna se repite.
    assert len(backend.calls) == 11
    assert {call[1] for call in backend.calls} == set(keys)
    assert provider._metrics.snapshot()["keys_quarantined"] == 11


@pytest.mark.asyncio
async def test_error_model_wide_no_quema_el_pool_de_11_claves():
    """Canario MON-20: un error model-wide (404 de modelo, fatal) detiene
    la corrida en la PRIMERA clave. Si el codigo rotara sobre las 11, el
    pool se quemaria en vano y este test fallaria."""
    keys = tuple(f"k{i}" for i in range(11))
    backend = ScriptedBackend([_http_error(404)])
    provider = KeyRotatingProvider(
        "google", keys, backend, metrics=DecisionMetrics()
    )
    with pytest.raises(FatalProviderError):
        await provider.complete(
            "p", deadline=time.monotonic() + 5, turn_id="t"
        )
    assert len(backend.calls) == 1
    assert backend.calls[0][1] == keys[0]


@pytest.mark.asyncio
async def test_pool_de_11_claves_rota_429_hasta_encontrar_una_disponible():
    metrics = DecisionMetrics()
    responses: list = [QuotaExceeded("quota") for _ in range(10)]
    responses.append({"ok": True})
    backend = ScriptedBackend(responses)
    provider = KeyRotatingProvider(
        "google", tuple(f"k{i}" for i in range(11)), backend, metrics=metrics
    )
    result = await provider.complete(
        "p", deadline=time.monotonic() + 5, turn_id="t"
    )
    assert result.payload == {"ok": True}
    assert len(backend.calls) == 11
    assert backend.calls[-1][1] == "k10"
    assert metrics.snapshot()["key_rotations"] == 10
    assert metrics.snapshot()["turns_quota_affected"] == 1


@pytest.mark.asyncio
async def test_rotacion_comparte_un_mismo_deadline():
    """Todos los intentos (rotacion incluida) comparten el deadline de la
    decision: una rotacion no resetea el reloj ni lo extiende."""
    clock_values = iter([0.0, 1.0, 2.0, 3.0, 99.0, 99.0])
    metrics = DecisionMetrics()

    class ScriptedClock:
        def __call__(self) -> float:
            return next(clock_values)

    backend = ScriptedBackend([QuotaExceeded("q")] * 11)
    provider = KeyRotatingProvider(
        "google", tuple(f"k{i}" for i in range(11)), backend,
        metrics=metrics, clock=ScriptedClock(),
        quota_cooldown_seconds=60,
    )
    with pytest.raises(DecisionDeadlineExceeded):
        await provider.complete(
            "p", deadline=2.5, turn_id="t"
        )
    # deadline 2.5: t=0 pasa el pase, t=1 intenta la clave 0, t=2 fija el
    # cooldown de la clave 0, t=3 evalua la clave 1 con remaining<0: la
    # rotacion NUNCA extiende el deadline compartido.
    assert len(backend.calls) == 1
    assert backend.calls[0][1] == "k0"
    assert metrics.snapshot()["turns_deadline_affected"] == 1


def _routes_with(provider: str, model: str, protocol: str, **extra) -> dict:
    from ludex_agent.graph.provider import ModelRoute
    return {(provider, model): ModelRoute(protocol=protocol, **extra)}


def test_protocolo_responses_elige_backend_responses():
    from ludex_agent.graph.provider import (
        OpenAIResponsesDecisionProvider,
        _ResponsesBackend,
        build_route_provider,
    )
    provider = build_route_provider(
        "open_code_zen", "gpt-5.5",
        base_url="https://opencode.ai/zen/v1", keys=("k",),
        metrics=DecisionMetrics(), timeout_seconds=30,
        route=ModelRoute(protocol="responses", temperature=0.2),
    )
    assert isinstance(provider, OpenAIResponsesDecisionProvider)
    assert isinstance(provider._backend, _ResponsesBackend)


def test_protocolo_google_zen_usa_gemini_con_base_url():
    from ludex_agent.graph.provider import (
        GeminiDecisionProvider,
        build_route_provider,
    )
    provider = build_route_provider(
        "open_code_zen", "gemini-3.6-flash",
        base_url="https://opencode.ai/zen/v1", keys=("k",),
        metrics=DecisionMetrics(), timeout_seconds=30,
        route=ModelRoute(protocol="google"),
    )
    assert isinstance(provider, GeminiDecisionProvider)
    assert provider._backend.base_url == "https://opencode.ai/zen/v1"


def test_protocolo_messages_usa_anthropic_y_chat_completions_openai():
    from ludex_agent.graph.provider import (
        AnthropicDecisionProvider,
        OpenAICompatibleDecisionProvider,
        build_route_provider,
    )
    messages = build_route_provider(
        "open_code_zen", "qwen3.6-plus",
        base_url="https://opencode.ai/zen/v1", keys=("k",),
        metrics=DecisionMetrics(), timeout_seconds=30,
        route=ModelRoute(protocol="messages"),
    )
    assert isinstance(messages, AnthropicDecisionProvider)
    chat = build_route_provider(
        "open_code_zen", "deepseek-v4-flash",
        base_url="https://opencode.ai/zen/v1", keys=("k",),
        metrics=DecisionMetrics(), timeout_seconds=30,
        route=ModelRoute(protocol="chat_completions"),
    )
    assert isinstance(chat, OpenAICompatibleDecisionProvider)


@pytest.mark.asyncio
async def test_backend_google_usa_google_api_key_timeout_y_base_url(monkeypatch):
    """Los campos reales de ChatGoogleGenerativeAI son `google_api_key`,
    `max_retries`, `timeout` y `base_url`. Los nombres `api_key`/`retries`/
    `request_timeout` NO existen: pydantic los ignora en silencio y la
    rotacion de claves jamas rotaba credenciales reales. Este test fija la
    regresion: si alguien revierte a `api_key=`, el kwargs capturado no
    contiene `google_api_key` y el test falla."""
    captured: dict[str, object] = {}

    class FakeMessage:
        content = '{"action": "move"}'
        usage_metadata = {
            "input_tokens": 1, "output_tokens": 1,
            "input_token_details": {}, "output_token_details": {},
        }
        response_metadata = {"model_name": "gemini-2.5-flash"}

    class FakeModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def ainvoke(self, prompt):
            return FakeMessage()

    import langchain_google_genai
    monkeypatch.setattr(
        langchain_google_genai, "ChatGoogleGenerativeAI", FakeModel
    )
    backend = _LangChainBackend(
        kind="google", model="gemini-2.5-flash",
        response_schema=provider_response_schema(DecisionResponse),
        timeout_seconds=30, base_url="https://opencode.ai/zen/v1",
        route=ModelRoute(protocol="google", structured_output="text_json"),
    )
    result = await backend.complete(
        "p", api_key="AIza-pool-key", deadline=time.monotonic() + 5
    )
    assert captured.get("google_api_key") == "AIza-pool-key"
    assert captured.get("max_retries") == 0
    assert captured.get("timeout") == 30
    assert captured.get("base_url") == "https://opencode.ai/zen/v1"
    assert "api_key" not in captured
    assert result.usage.model == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_backend_responses_posta_exactamente_al_endpoint(monkeypatch):
    """CONTRATO DE ENDPOINT (D43): `endpoint` es la URL COMPLETA y el
    backend postea exactamente ahi. Debe fallar si la URL terminara en
    `/responses/responses` o en cualquier otro endpoint."""
    captured: dict[str, object] = {}
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            calls.append((url, json))
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {
                    "model": "gpt-5.5",
                    "output": [{
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": '{"action": "switch", "target": "pikachu"}',
                        }],
                    }],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "output_tokens_details": {"reasoning_tokens": 5},
                    },
                },
            )

    import httpx as httpx_module
    monkeypatch.setattr(httpx_module, "AsyncClient", FakeClient)

    from ludex_agent.graph.provider import _ResponsesBackend
    backend = _ResponsesBackend(
        model="gpt-5.5",
        endpoint="https://opencode.ai/zen/v1/responses",
        timeout_seconds=60,
        route=ModelRoute(
            protocol="responses", temperature=0.2, max_tokens=8000,
        ),
    )
    result = await backend.complete(
        "p", api_key="sk-zen", deadline=time.monotonic() + 5
    )
    assert [url for url, _ in calls] == [
        "https://opencode.ai/zen/v1/responses"
    ]
    body = calls[0][1]
    assert body["model"] == "gpt-5.5"
    assert body["input"] == "p"
    assert body["temperature"] == 0.2
    assert body["max_output_tokens"] == 8000
    assert result.payload == {"action": "switch", "target": "pikachu"}
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.reasoning_tokens == 5
    assert result.usage.model == "gpt-5.5"


@pytest.mark.asyncio
async def test_responses_endpoint_derivado_no_duplica_el_path(monkeypatch):
    """Sin `endpoint` en la ruta, se deriva `{base_url}/responses`: nunca
    `/responses/responses`."""
    captured: dict[str, object] = {}
    calls: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            calls.append(url)
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {
                    "model": "gpt-5.5",
                    "output": [{"type": "message", "content": []}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

    import httpx as httpx_module
    monkeypatch.setattr(httpx_module, "AsyncClient", FakeClient)

    from ludex_agent.graph.provider import OpenAIResponsesDecisionProvider
    provider = OpenAIResponsesDecisionProvider(
        "open_code_zen", ("k",), model="gpt-5.5",
        base_url="https://opencode.ai/zen/v1", timeout_seconds=60,
        route=ModelRoute(protocol="responses"),
    )
    await provider.complete("p", deadline=time.monotonic() + 5, turn_id="t")
    assert calls == ["https://opencode.ai/zen/v1/responses"]


def test_responses_protocolo_por_defecto_usa_text_json():
    from ludex_agent.graph.provider import route_structured_output
    assert route_structured_output(
        ModelRoute(protocol="responses"), "openai", None
    ) == "text_json"
    assert route_structured_output(
        ModelRoute(protocol="chat_completions"), "openai", None
    ) == "json_schema"
    assert route_structured_output(
        ModelRoute(protocol="messages", structured_output="json_schema"),
        "openai", "https://zen/v1",
    ) == "json_schema"
    assert route_structured_output(
        ModelRoute(protocol="google"), "google", None
    ) == "json_schema"
