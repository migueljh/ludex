import time

import pytest

from ludex_agent.graph.decision import decide, normalize_action, validate_action
from ludex_agent.graph.provider import (
    DecisionMetrics,
    FakeDecisionProvider,
    KeyRotatingProvider,
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
        {"action": {"kind": "move", "id": "illegal"}, "reasoning": "a"},
        {"action": {"kind": "switch", "species": "missing"}, "reasoning": "b"},
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
        {"action": {"kind": "move", "id": "illegal"}, "reasoning": "a"},
        {"action": {"kind": "move", "id": "thunderbolt"}, "reasoning": "b"},
    ])

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm_retry"
    assert result["action"] == {"kind": "move", "id": "thunderbolt"}
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
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [QuotaExceeded(), TransientProviderError()])
async def test_infraestructura_no_gasta_reintento_semantico_ni_hace_fallback(failure):
    metrics = DecisionMetrics()
    backend = ScriptedBackend([
        failure,
        {"action": {"kind": "move", "id": "thunderbolt"}, "reasoning": "ok"},
    ])
    keys = ("a", "b") if isinstance(failure, QuotaExceeded) else ("a",)
    provider = KeyRotatingProvider(
        "google", keys, backend, metrics=metrics, transient_retries=1
    )

    result = await decide(_state(), provider, metrics)

    assert result["action_path"] == "llm"
    assert len(set(backend.prompts)) == 1
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
