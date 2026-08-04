import asyncio
import logging
import time
from dataclasses import FrozenInstanceError

import httpx
import pytest
from anthropic import APITimeoutError as AnthropicAPITimeoutError
from openai import APITimeoutError as OpenAIAPITimeoutError
from pydantic import BaseModel, Field, ValidationError

from ludex_agent.graph.provider import (
    CompletionEnvelope,
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
    ) == ModelRoute(protocol="chat_completions")
    assert model_route(
        routes, "open_code_zen", "deepseek-v4-pro"
    ) == ModelRoute(protocol="chat_completions")
    assert model_route(
        routes, "open_code_zen", "deepseek-v4-flash"
    ) == ModelRoute(protocol="chat_completions")
    assert model_route(
        routes, "open_code_zen", "qwen3.5-plus"
    ) == ModelRoute(
        protocol="messages",
        max_tokens=1024,
        timeout_seconds=60,
    )
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
