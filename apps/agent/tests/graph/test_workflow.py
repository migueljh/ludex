import json
import time

import pytest

from ludex_agent.graph.provider import DecisionMetrics
from ludex_agent.graph.provider import CompletionEnvelope, CompletionUsage
from ludex_agent.graph.workflow import build_decision_graph


@pytest.mark.asyncio
async def test_grafo_ejecuta_todos_los_nodos_en_orden_y_devuelve_contexto():
    events = []
    rich_only_sentinel = "rich-only-sentinel-source-species"

    def parser(raw):
        events.append("parse_state")
        clean = dict(raw)
        clean.pop("chat", None)
        return clean

    class Calculator:
        async def calculate(self, request):
            events.append("calc_damage")
            return {
                "damage_rolls": [[10]], "min_damage": 10, "max_damage": 10,
                "defender_hp": {"cur": 100, "max": 100},
            }

    class Provider:
        async def complete(self, prompt, *, deadline, turn_id):
            events.append("decide")
            assert "rendite" not in prompt
            assert rich_only_sentinel not in prompt
            assert "learn_methods" not in prompt
            assert "sourceSpecies" not in prompt
            assert "observed_moves" not in prompt
            payload = json.loads(prompt.splitlines()[-1])
            assert payload["battle"]["context"] == {
                "generation": {
                    "gen_number": 6,
                    "label": "XY/ORAS",
                },
                "own": [{
                    "species": "pikachu",
                    "known_moves": ["tackle"],
                }],
                "opponent": [{
                    "species": "eevee",
                    "base_types": ["Normal"],
                    "base_stats": {},
                    "possible_abilities": ["runaway"],
                    "revealed_moves": [],
                    "possible_moves": [],
                }],
                "moves": {
                    "tackle": {
                        "type": "Normal",
                        "category": "Physical",
                        "power": 50,
                        "power_kind": "standard",
                        "accuracy": 100,
                        "priority": 0,
                        "target": "normal",
                        "description": "Hits the target.",
                        "flags": {"contact": 1},
                    },
                },
            }
            return CompletionEnvelope(
                payload={
                    "action": {"kind": "move", "id": "tackle"},
                    "rationale": "legal",
                    "confidence": 0.9,
                    "alternatives": [],
                },
                provider="fake", model="fake-model",
                usage=CompletionUsage(input_tokens=0, output_tokens=0),
                latency_ms=0.0,
            )

    class Repository:
        async def load_battle_context(
            self, *, gen_number, own_species, opponent_species
        ):
            events.append("retrieve_context")
            assert gen_number == 6
            assert own_species == ("pikachu",)
            assert opponent_species == ("eevee",)
            return {
                "generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [{
                    "showdown_id": "pikachu",
                    "types": ["Electric"],
                    "base_stats": {},
                    "abilities": {"0": "Static"},
                    "moves": [{
                        "showdown_id": "surf",
                        "learn_methods": [{
                            "sourceSpecies": rich_only_sentinel,
                        }],
                    }],
                }],
                "opponent": [{
                    "showdown_id": "eevee",
                    "types": ["Normal"],
                    "base_stats": {},
                    "abilities": {"0": "Run Away"},
                    "moves": [],
                }],
            }

        async def load_moves(self, *, gen_number, move_ids):
            assert gen_number == 6
            assert move_ids == ("tackle",)
            return {
                "tackle": {
                    "showdown_id": "tackle",
                    "name": "Tackle",
                    "type": "Normal",
                    "category": "Physical",
                    "power": 50,
                    "power_kind": "standard",
                    "accuracy": 100,
                    "never_misses": False,
                    "pp": 35,
                    "priority": 0,
                    "target": "normal",
                    "flags": {"contact": 1},
                    "description": "Hits the target.",
                },
            }

    raw = {
        "gen": 6,
        "me": {"pokemon": [{
            "species": "pikachu", "active": True, "hp_fraction": 1,
            "moves": [{"id": "tackle"}],
        }]},
        "opponent": {"pokemon": [{
            "species": "eevee", "active": True, "hp_fraction": 1, "moves": [],
        }]},
        "field": {},
        "legal_actions": [{"kind": "move", "id": "tackle"}],
        "chat": "rendite",
    }
    graph = build_decision_graph(
        Calculator(), Provider(), DecisionMetrics(), Repository(), parser=parser
    )

    result = await graph.ainvoke({
        "raw_state": raw,
        "turn_id": "battle:1",
        "deadline": time.monotonic() + 5,
    })

    assert events == [
        "parse_state", "retrieve_context", "calc_damage", "decide",
    ]
    assert result["context"]["generation"]["gen_number"] == 6
    assert result["context"]["own"][0]["moves"][0]["learn_methods"] == [{
        "sourceSpecies": rich_only_sentinel,
    }]
    assert result["prompt_context"]["own"][0]["known_moves"] == ["tackle"]
    assert "context" not in result["battle_state"]
    assert result["action"] == {"kind": "move", "id": "tackle"}
    assert result["action_path"] == "llm"


def test_grafo_exige_context_repository():
    class Never:
        async def calculate(self, request):
            raise AssertionError

        async def complete(self, prompt, *, deadline, turn_id):
            raise AssertionError

    with pytest.raises(TypeError):
        build_decision_graph(Never(), Never(), DecisionMetrics())


def test_grafo_se_compila_una_vez_al_construir():
    class Never:
        async def calculate(self, request):
            raise AssertionError

        async def complete(self, prompt, *, deadline, turn_id):
            raise AssertionError

        async def load_battle_context(
            self, *, gen_number, own_species, opponent_species
        ):
            raise AssertionError

        async def load_moves(self, *, gen_number, move_ids):
            raise AssertionError

    graph = build_decision_graph(
        Never(), Never(), DecisionMetrics(), Never()
    )
    assert hasattr(graph, "ainvoke")


@pytest.mark.asyncio
async def test_calc_damage_recibe_contexto_rico_no_prompt_context(monkeypatch):
    import ludex_agent.graph.workflow as workflow

    rich_sentinel = {
        "learn_methods": [{
            "sourceSpecies": "rich-context-for-f2-07",
        }],
    }

    async def recording_calc(state, calculator):
        assert state["context"]["rich_sentinel"] == rich_sentinel
        assert state["prompt_context"] != state["context"]
        return {"damage": []}

    monkeypatch.setattr(workflow, "calc_damage", recording_calc)

    class Repository:
        async def load_battle_context(
            self, *, gen_number, own_species, opponent_species
        ):
            return {
                "generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [],
                "opponent": [],
                "rich_sentinel": rich_sentinel,
            }

        async def load_moves(self, *, gen_number, move_ids):
            return {}

    class Provider:
        async def complete(self, prompt, *, deadline, turn_id):
            return CompletionEnvelope(
                payload={
                    "action": {"kind": "switch", "species": "pikachu"},
                    "rationale": "legal",
                    "confidence": 0.8,
                    "alternatives": [],
                },
                provider="fake", model="fake-model",
                usage=CompletionUsage(input_tokens=0, output_tokens=0),
                latency_ms=0.0,
            )

    graph = build_decision_graph(
        object(),
        Provider(),
        DecisionMetrics(),
        Repository(),
    )
    result = await graph.ainvoke({
        "raw_state": {
            "gen": 6,
            "me": {"pokemon": []},
            "opponent": {"pokemon": []},
            "field": {},
            "legal_actions": [{
                "kind": "switch",
                "species": "pikachu",
            }],
        },
        "turn_id": "battle:1",
        "deadline": time.monotonic() + 5,
    })

    assert result["context"]["rich_sentinel"] == rich_sentinel


# --- L-02 metrics: damage_metrics debe sobrevivir al workflow productivo ---


@pytest.mark.asyncio
async def test_grafo_conserva_damage_metrics_en_la_salida():
    """T-02: calc_damage devuelve damage_metrics, pero GraphState no la
    declaraba y StateGraph la descartaba en el merge. El contrato la conserva
    en la salida del workflow (sin ampliar persistencia)."""

    class Calculator:
        async def calculate(self, request):
            return {
                "damage_rolls": [[86, 104]],
                "min_damage": 86,
                "max_damage": 104,
                "min_percent": 35.6,
                "max_percent": 43.1,
                "ko_chance": {"n": 5, "text": "possible 5HKO"},
                "description": "Pikachu Thunderbolt vs. Blastoise",
                "defender_hp": {"cur": 241, "max": 241},
                "effective": {
                    "attacker": {
                        "species": "Pikachu", "level": 80, "nature": "Serious",
                        "ability": "Static", "item": None,
                        "evs": {"hp": 0}, "ivs": {"hp": 31},
                        "boosts": {"hp": 0}, "status": "", "curHP": 170,
                        "gender": "M",
                    },
                    "defender": {
                        "species": "Blastoise", "level": 80,
                        "nature": "Serious", "ability": "Torrent",
                        "item": None,
                        "evs": {"hp": 0}, "ivs": {"hp": 31},
                        "boosts": {"hp": 0}, "status": "", "curHP": 241,
                        "gender": "M",
                    },
                },
            }

    class Provider:
        async def complete(self, prompt, *, deadline, turn_id):
            return CompletionEnvelope(
                payload={
                    "action": {"kind": "move", "id": "tackle"},
                    "rationale": "legal",
                    "confidence": 0.9,
                    "alternatives": [],
                },
                provider="fake", model="fake-model",
                usage=CompletionUsage(input_tokens=0, output_tokens=0),
                latency_ms=0.0,
            )

    class Repository:
        async def load_battle_context(
            self, *, gen_number, own_species, opponent_species
        ):
            return {
                "generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [],
                "opponent": [],
                "mega_forms": {},
            }

        async def load_moves(self, *, gen_number, move_ids):
            return {
                "tackle": {
                    "showdown_id": "tackle", "name": "Tackle",
                    "type": "Normal", "category": "Physical", "power": 50,
                    "power_kind": "standard", "accuracy": 100,
                    "never_misses": False, "pp": 35, "priority": 0,
                    "target": "normal", "flags": {"contact": 1},
                    "description": "Hits the target.",
                },
            }

        async def load_mega_forms(self, *, gen_number, item_ids):
            return {}

    graph = build_decision_graph(
        Calculator(), Provider(), DecisionMetrics(), Repository()
    )
    result = await graph.ainvoke({
        "raw_state": {
            "gen": 6,
            "me": {"pokemon": [{
                "species": "pikachu", "active": True, "hp_fraction": 1,
                "moves": [{"id": "tackle"}],
            }]},
            "opponent": {"pokemon": [{
                "species": "eevee", "active": True, "hp_fraction": 1,
                "moves": [],
            }]},
            "field": {},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        },
        "turn_id": "battle:1",
        "deadline": time.monotonic() + 5,
    })

    assert result["action"] == {"kind": "move", "id": "tackle"}
    metrics = result["damage_metrics"]
    assert metrics["calls"] == 1
    assert metrics["bytes"] > 0
    assert set(metrics["latency_ms"]) == {"median", "p90", "p99", "max"}
