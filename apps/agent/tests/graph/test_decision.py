import time

import pytest
from pydantic import ValidationError



from ludex_agent.graph.decision import (
    DecisionAction,
    DecisionResponse,
    decide,
    normalize_action,
    validate_action,
)
from ludex_agent.graph.provider import (
    CompletionEnvelope,
    CompletionUsage,
    DecisionMetrics,
    FakeDecisionProvider,
    KeyRotatingProvider,
    ProviderCompletion,
    ProviderPoolExhausted,
    QuotaExceeded,
    TransientProviderError,
)


def _state():
    legal = [
        {"kind": "move", "id": "thunderbolt"},
        {"kind": "move", "id": "icebeam", "mega": True},
    ]
    return {
        "battle_state": {
            "schema_version": 1, "turn": 3, "player_role": "p1",
            "format": "gen6randombattle", "gen": 6, "field": {},
            "me": {"pokemon": []}, "opponent": {"pokemon": []},
            "legal_actions": legal,
        },
        "damage": [
            {
                "action": legal[0], "direction": "outgoing",
                "remaining_hp": 50,
                "result": {
                    "damage_rolls": [[10, 100]],
                    "min_damage": 10, "max_damage": 100,
                    "defender_hp": {"cur": 50, "max": 100},
                },
            },
            {
                "action": legal[1], "direction": "outgoing",
                "remaining_hp": 50,
                "result": {
                    "damage_rolls": [[50, 50]],
                    "min_damage": 50, "max_damage": 50,
                    "defender_hp": {"cur": 50, "max": 100},
                },
            },
        ],
        "turn_id": "battle-1:3",
        "deadline": time.monotonic() + 5,
    }


def test_mega_false_y_ausente_son_semanticamente_iguales():
    assert normalize_action(
        {"kind": "move", "id": "x", "mega": False}
    ) == {"kind": "move", "id": "x"}
    assert validate_action(
        {"kind": "move", "id": "x", "mega": False},
        [{"kind": "move", "id": "x"}],
    ) == {"kind": "move", "id": "x"}


def test_true_ids_distintos_y_claves_desconocidas_siguen_siendo_estrictos():
    legal = [{"kind": "move", "id": "x"}]
    with pytest.raises(ValueError):
        validate_action({"kind": "move", "id": "y"}, legal)
    with pytest.raises(ValueError):
        validate_action({"kind": "move", "id": "x", "mega": True}, legal)
    with pytest.raises(ValueError):
        validate_action({"kind": "move", "id": "x", "unknown": False}, legal)


@pytest.mark.asyncio
async def test_dos_respuestas_ilegales_usan_fallback_y_corrigen_prompt():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        {"action": {"kind": "move", "id": "illegal"}, "rationale": "a"},
        {"action": {"kind": "switch", "species": "missing"}, "rationale": "b"},
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action"] == {"kind": "move", "id": "icebeam", "mega": True}
    assert result["action_path"] == "fallback"
    assert "acción ilegal" in provider.prompts[1]
    assert metrics.snapshot()["turns_model_invalid"] == 1
    assert metrics.snapshot()["turns_fallback"] == 1


@pytest.mark.asyncio
async def test_segunda_respuesta_legal_registra_llm_retry():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        {"action": {"kind": "move", "id": "illegal"}, "rationale": "a"},
        {
            "action": {"kind": "move", "id": "thunderbolt"},
            "rationale": "b",
            "confidence": 0.8,
            "alternatives": [],
        },
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    assert result["action"] == {"kind": "move", "id": "thunderbolt"}
    assert metrics.snapshot()["turns_total"] == 1
    assert metrics.snapshot()["turns_model_invalid"] == 1
    assert metrics.snapshot()["turns_fallback"] == 0


class ScriptedBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(self, prompt, *, api_key, deadline):
        self.prompts.append(prompt)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return ProviderCompletion(
            payload=value,
            usage=CompletionUsage(
                input_tokens=1, output_tokens=1, model="fake-model"
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [QuotaExceeded(), TransientProviderError()])
async def test_infraestructura_no_gasta_reintento_semantico_ni_hace_fallback(failure):
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        failure,
        {
            "action": {"kind": "move", "id": "thunderbolt"},
            "rationale": "ok",
            "confidence": 0.8,
            "alternatives": [],
        },
    ])
    keys = ("a", "b") if isinstance(failure, QuotaExceeded) else ("a",)
    provider = KeyRotatingProvider(
        "google", keys, backend, metrics=metrics, transient_retries=1
    )

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm"
    assert len(set(backend.prompts)) == 1
    assert metrics.snapshot()["calls_total"] == 1
    assert metrics.snapshot()["turns_model_invalid"] == 0
    assert metrics.snapshot()["turns_fallback"] == 0


@pytest.mark.asyncio
async def test_pool_agotado_propaga_y_nunca_cae_en_fallback():
    metrics = DecisionMetrics()
    provider = KeyRotatingProvider(
        "google", ("a",), ScriptedBackend([QuotaExceeded()]), metrics=metrics
    )

    with pytest.raises(ProviderPoolExhausted):
        await decide(_state(), provider, metrics)
    assert metrics.snapshot()["turns_fallback"] == 0


# --- F2-08 (MON-13): contrato completo de decision -------------------------


def _respuesta_completa(**overrides):
    payload = {
        "action": {"kind": "move", "id": "thunderbolt"},
        "target": None,
        "rationale": "brief rationale",
        "confidence": 0.9,
        "alternatives": [],
    }
    payload.update(overrides)
    return payload


def test_confidence_fuera_de_rango_es_rechazada():
    for valor in (1.5, -0.1, 1.0000001):
        with pytest.raises(ValidationError):
            DecisionResponse.model_validate(_respuesta_completa(confidence=valor))


def test_confidence_obligatoria_para_respuesta_llm():
    payload = _respuesta_completa()
    del payload["confidence"]
    with pytest.raises(ValidationError):
        DecisionResponse.model_validate(payload)


def test_faltan_campos_obligatorios_rechazada():
    for campo in ("action", "rationale", "alternatives"):
        payload = _respuesta_completa()
        del payload[campo]
        with pytest.raises(ValidationError):
            DecisionResponse.model_validate(payload)


def test_alternatives_vacias_es_valido():
    parsed = DecisionResponse.model_validate(_respuesta_completa())
    assert parsed.alternatives == []
    assert parsed.target is None


def test_target_null_en_singles_es_valido_y_esperado():
    parsed = DecisionResponse.model_validate(_respuesta_completa(target=None))
    assert parsed.target is None


def test_target_con_esquema_invalido_rechazado():
    with pytest.raises(ValidationError):
        DecisionResponse.model_validate(_respuesta_completa(target={"kind": "zzz"}))


@pytest.mark.asyncio
async def test_target_no_nulo_sin_targets_en_mask_consume_reintento():
    """Mientras la mascara legal no exponga targets explicitos, un target
    no-NULL es una respuesta invalida: consume el reintento semantico."""
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(
            target={"kind": "active_opponent", "id": "eevee"},
        ),
        _respuesta_completa(),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    assert result["target"] is None
    assert metrics.snapshot()["turns_model_invalid"] == 1


@pytest.mark.asyncio
async def test_alternativa_ilegal_consume_reintento_semantico():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(alternatives=[
            {"kind": "switch", "species": "missing"},
        ]),
        _respuesta_completa(),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    assert result["alternatives"] == []
    assert metrics.snapshot()["turns_model_invalid"] == 1


@pytest.mark.asyncio
async def test_alternativas_duplicadas_tras_normalizacion_consume_reintento():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(alternatives=[
            {"kind": "move", "id": "icebeam", "mega": True},
            {"kind": "move", "id": "icebeam", "mega": True},
        ]),
        _respuesta_completa(),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    assert metrics.snapshot()["turns_model_invalid"] == 1


@pytest.mark.asyncio
async def test_alternativa_que_repite_la_principal_consume_reintento():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(alternatives=[
            {"kind": "move", "id": "thunderbolt"},
        ]),
        _respuesta_completa(),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    assert metrics.snapshot()["turns_model_invalid"] == 1


@pytest.mark.asyncio
async def test_dos_respuestas_invalidas_por_alternativas_usan_fallback():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(alternatives=[
            {"kind": "move", "id": "missing"},
        ]),
        _respuesta_completa(alternatives=[
            {"kind": "switch", "species": "missing"},
        ]),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "fallback"
    assert result["confidence"] is None
    assert result["provider"] is None
    assert result["model"] is None
    assert result["alternatives"] == []
    assert metrics.snapshot()["turns_fallback"] == 1


@pytest.mark.asyncio
async def test_respuesta_llm_aceptada_expone_metadata_completa():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(
            confidence=0.7,
            alternatives=[
                {"kind": "move", "id": "icebeam", "mega": True},
            ],
            rationale="corto y user-facing",
        ),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm"
    assert result["confidence"] == 0.7
    assert result["alternatives"] == [
        {"kind": "move", "id": "icebeam", "mega": True},
    ]
    assert result["rationale"] == "corto y user-facing"
    # Alias interno para consumidores que ya leian `reasoning`: deriva del
    # rationale validado; nunca forma parte del schema del proveedor.
    assert result["reasoning"] == "corto y user-facing"
    assert result["provider"] == "fake"
    assert result["model"] == "fake-model"
    assert result["decision_latency_ms"] >= 0
    assert result["input_tokens"] == 1
    assert result["output_tokens"] == 1
    assert result["cached_input_tokens"] == 0
    assert result["reasoning_tokens"] == 0


# --- L-01 (R1): rationale es el campo canonico del contrato --------------

def test_payload_completo_con_rationale_se_acepta():
    """Canario de L-01: el schema productivo acepta el contrato canonico con
    `rationale` y rechaza `reasoning` (extra_forbidden)."""
    payload = _respuesta_completa(rationale="breve y user-facing")
    parsed = DecisionResponse.model_validate(payload)
    assert parsed.rationale == "breve y user-facing"


def test_payload_con_solo_reasoning_se_rechaza_por_missing_y_extra():
    """Un proveedor que emita `reasoning` en vez de `rationale` viola el
    contrato de dos formas: falta el campo canonico y sobra el alias
    (extra_forbidden, D38: el alias nunca viaja en el schema)."""
    payload = {
        "action": {"kind": "move", "id": "thunderbolt"},
        "target": None,
        "reasoning": "breve",
        "confidence": 0.8,
        "alternatives": [],
    }
    with pytest.raises(ValidationError) as exc_info:
        DecisionResponse.model_validate(payload)

    errores = {
        (error["loc"][0] if error["loc"] else None, error["type"])
        for error in exc_info.value.errors()
    }
    assert ("rationale", "missing") in errores, errores
    assert ("reasoning", "extra_forbidden") in errores, errores


@pytest.mark.asyncio
async def test_usage_acumula_las_respuestas_facturables_del_camino():
    metrics = DecisionMetrics()
    provider = FakeDecisionProvider([
        _respuesta_completa(alternatives=[
            {"kind": "move", "id": "missing"},
        ]),
        _respuesta_completa(),
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    # Las DOS respuestas facturables del camino se suman: la invalida y la
    # aceptada (cada una aporta 1 token por categoria en FakeDecisionProvider).
    assert result["input_tokens"] == 2
    assert result["output_tokens"] == 2


@pytest.mark.asyncio
async def test_fallback_sin_llamadas_no_inventa_usage_ni_latencia_modelo():
    """Fallback por dos respuestas invalidas SI registro llamadas facturables:
    usage y latency existen, pero provider/model/confidence nunca se atribuyen
    a un modelo. El caso de zero usage (fallback sin llamadas) se cubre en la
    ruta random, fuera del contrato LLM."""

    class FailingProvider:
        def __init__(self):
            self.prompts = []

        async def complete(self, prompt, *, deadline, turn_id):
            self.prompts.append(prompt)
            return CompletionEnvelope(
                payload={"_invalid_response": "no json"},
                provider="fake", model="fake-model",
                usage=CompletionUsage(input_tokens=0, output_tokens=0),
                latency_ms=0.0,
            )

    metrics = DecisionMetrics()
    provider = FailingProvider()

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "fallback"
    assert result["provider"] is None
    assert result["model"] is None
    assert result["confidence"] is None
    assert result["alternatives"] == []
    assert result["rationale"]
    assert result["decision_latency_ms"] >= 0
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_decide_usa_reloj_inyectado_para_deadline_y_latencia():
    """F2-10: `decide` debe evaluar el deadline y medir la latencia con el
    reloj inyectado, no con `time.monotonic()`. Avanzamos el reloj falso una
    cantidad conocida y verificamos que la latencia refleje exactamente ese
    avance."""
    clock = _FakeClock(start=500.0)
    metrics = DecisionMetrics()

    class AdvancingProvider:
        async def complete(self, prompt, *, deadline, turn_id):
            clock.advance(0.125)
            return CompletionEnvelope(
                payload={
                    "action": {"kind": "move", "id": "thunderbolt"},
                    "rationale": "ok",
                    "confidence": 0.8,
                    "alternatives": [],
                },
                provider="fake",
                model="fake-model",
                usage=CompletionUsage(
                    input_tokens=10, output_tokens=5, model="fake-model"
                ),
                latency_ms=125.0,
            )

    state = _state()
    state["deadline"] = clock.now + 10.0
    result = await decide(state, AdvancingProvider(), metrics, clock=clock)

    assert result["action_path"] == "llm"
    assert result["decision_latency_ms"] == 125.0
    assert metrics.snapshot()["latency_ms_count"] == 1
    assert metrics.snapshot()["latency_ms_total"] == 125
    assert metrics.snapshot()["latency_ms_max"] == 125
