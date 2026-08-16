"""Orquestación local del flujo de decisión."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from .calc import DamageCalculator, calc_damage
from .context import ContextRepository, retrieve_context
from .decision import decide
from .provider import DecisionMetrics, ProviderResolver
from .state import GraphState, allowlisted_state


def build_decision_graph(
    calculator: DamageCalculator,
    resolver: ProviderResolver,
    metrics: DecisionMetrics,
    repository: ContextRepository,
    *,
    parser: Callable[[dict[str, Any]], dict[str, Any]] = allowlisted_state,
    clock: Callable[[], float] = time.monotonic,
    on_stage: Callable[[str], None] | None = None,
):
    """Construye el grafo de decision.

    ``on_stage`` (MON-20 DIAG-A) es observabilidad, no comportamiento: cada
    nodo async reporta su etapa ANTES de su await, para que
    `LudexPlayer.decision_snapshot()` pueda localizar una decision atascada
    (un await SQL sin deadline, el proveedor, el calc). Corre dentro de la
    task de la decision; el callback decide como mapearla a su contexto.
    """
    async def resolve_node(state: GraphState) -> dict[str, Any]:
        # F2-09 (MON-14): la seleccion activa se resuelve en CADA invocacion
        # del grafo, nunca al compilarlo ni al arrancar la batalla. Cambiar
        # el modelo activo en la DB surte efecto en la decision siguiente.
        if on_stage is not None:
            on_stage("resolve_provider")
        return {"resolved_provider": await resolver.resolve()}

    async def parse_node(state: GraphState) -> dict[str, Any]:
        return {"battle_state": parser(state["raw_state"])}

    async def calc_node(state: GraphState) -> dict[str, Any]:
        if on_stage is not None:
            on_stage("calc_damage")
        return await calc_damage(state, calculator)

    async def context_node(state: GraphState) -> dict[str, Any]:
        if on_stage is not None:
            on_stage("retrieve_context")
        return await retrieve_context(state, repository)

    async def decide_node(state: GraphState) -> dict[str, Any]:
        decision_state: GraphState = {
            "battle_state": {
                **state["battle_state"],
                "context": state["prompt_context"],
            },
            "damage": state.get("damage", []),
        }
        if "turn_id" in state:
            decision_state["turn_id"] = state["turn_id"]
        if "deadline" in state:
            decision_state["deadline"] = state["deadline"]
        resolved = state["resolved_provider"]
        if on_stage is not None:
            on_stage("decide")
        return await decide(decision_state, resolved.provider, metrics, clock=clock)

    builder = StateGraph(GraphState)
    builder.add_node("resolve_provider", resolve_node)
    builder.add_node("parse_state", parse_node)
    builder.add_node("retrieve_context", context_node)
    builder.add_node("calc_damage", calc_node)
    builder.add_node("decide", decide_node)
    builder.add_edge(START, "resolve_provider")
    builder.add_edge("resolve_provider", "parse_state")
    builder.add_edge("parse_state", "retrieve_context")
    builder.add_edge("retrieve_context", "calc_damage")
    builder.add_edge("calc_damage", "decide")
    builder.add_edge("decide", END)
    return builder.compile()
