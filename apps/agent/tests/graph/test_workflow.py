import time

import pytest

from ludex_agent.graph.provider import DecisionMetrics
from ludex_agent.graph.workflow import build_decision_graph


@pytest.mark.asyncio
async def test_grafo_ejecuta_parse_calc_decide_en_orden_y_devuelve_camino():
    events = []

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
            return {
                "action": {"kind": "move", "id": "tackle"},
                "reasoning": "legal",
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
        Calculator(), Provider(), DecisionMetrics(), parser=parser
    )

    result = await graph.ainvoke({
        "raw_state": raw,
        "turn_id": "battle:1",
        "deadline": time.monotonic() + 5,
    })

    assert events == ["parse_state", "calc_damage", "decide"]
    assert result["action"] == {"kind": "move", "id": "tackle"}
    assert result["action_path"] == "llm"


def test_grafo_se_compila_una_vez_al_construir():
    class Never:
        async def calculate(self, request):
            raise AssertionError

        async def complete(self, prompt, *, deadline, turn_id):
            raise AssertionError

    graph = build_decision_graph(Never(), Never(), DecisionMetrics())
    assert hasattr(graph, "ainvoke")
