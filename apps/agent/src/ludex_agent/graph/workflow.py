"""Orquestación local del flujo de decisión."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from .calc import DamageCalculator, calc_damage
from .context import ContextRepository, retrieve_context
from .decision import decide
from .provider import DecisionMetrics, DecisionProvider
from .state import GraphState, allowlisted_state


def build_decision_graph(
    calculator: DamageCalculator,
    provider: DecisionProvider,
    metrics: DecisionMetrics,
    repository: ContextRepository | None = None,
    *,
    parser: Callable[[dict[str, Any]], dict[str, Any]] = allowlisted_state,
):
    async def parse_node(state: GraphState) -> dict[str, Any]:
        return {"battle_state": parser(state["raw_state"])}

    async def calc_node(state: GraphState) -> dict[str, Any]:
        return await calc_damage(state, calculator)

    async def context_node(state: GraphState) -> dict[str, Any]:
        if repository is None:
            battle = state["battle_state"]
            return {
                "context": {
                    "generation": {
                        "gen_number": battle["gen"],
                        "label": None,
                    },
                    "own": [],
                    "opponent": [],
                },
            }
        return await retrieve_context(state, repository)

    async def decide_node(state: GraphState) -> dict[str, Any]:
        decision_state = dict(state)
        decision_state["battle_state"] = {
            **state["battle_state"],
            "context": state.get("context", {}),
        }
        return await decide(decision_state, provider, metrics)

    builder = StateGraph(GraphState)
    builder.add_node("parse_state", parse_node)
    builder.add_node("retrieve_context", context_node)
    builder.add_node("calc_damage", calc_node)
    builder.add_node("decide", decide_node)
    builder.add_edge(START, "parse_state")
    builder.add_edge("parse_state", "retrieve_context")
    builder.add_edge("retrieve_context", "calc_damage")
    builder.add_edge("calc_damage", "decide")
    builder.add_edge("decide", END)
    return builder.compile()
