"""Orquestación local del flujo de decisión."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from .calc import DamageCalculator, calc_damage
from .decision import decide
from .provider import DecisionMetrics, DecisionProvider
from .state import GraphState, allowlisted_state


def build_decision_graph(
    calculator: DamageCalculator,
    provider: DecisionProvider,
    metrics: DecisionMetrics,
    *,
    parser: Callable[[dict[str, Any]], dict[str, Any]] = allowlisted_state,
):
    async def parse_node(state: GraphState) -> dict[str, Any]:
        return {"battle_state": parser(state["raw_state"])}

    async def calc_node(state: GraphState) -> dict[str, Any]:
        return await calc_damage(state, calculator)

    async def decide_node(state: GraphState) -> dict[str, Any]:
        return await decide(state, provider, metrics)

    builder = StateGraph(GraphState)
    builder.add_node("parse_state", parse_node)
    builder.add_node("calc_damage", calc_node)
    builder.add_node("decide", decide_node)
    builder.add_edge(START, "parse_state")
    builder.add_edge("parse_state", "calc_damage")
    builder.add_edge("calc_damage", "decide")
    builder.add_edge("decide", END)
    return builder.compile()
