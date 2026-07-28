"""Contrato semántico del modelo, validación y respaldo determinista."""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .calc import rank_move_fallback, rank_switch_fallback
from .provider import DecisionMetrics, DecisionProvider
from .state import GraphState


_FALSE_EQUIVALENT_FLAGS = ("mega", "z_move", "dynamax", "terastallize")


class DecisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["move", "switch"]
    id: str | None = None
    species: str | None = None
    mega: bool | None = None
    z_move: bool | None = None
    dynamax: bool | None = None
    terastallize: bool | None = None

    @model_validator(mode="after")
    def correct_identifier(self) -> "DecisionAction":
        if self.kind == "move" and (not self.id or self.species is not None):
            raise ValueError("move requires id and forbids species")
        if self.kind == "switch" and (not self.species or self.id is not None):
            raise ValueError("switch requires species and forbids id")
        return self


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    reasoning: str


def normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(action)
    for flag in _FALSE_EQUIVALENT_FLAGS:
        if normalized.get(flag) is False or normalized.get(flag) is None:
            normalized.pop(flag, None)
    return normalized


def validate_action(
    action: dict[str, Any], legal: list[dict[str, Any]]
) -> dict[str, Any]:
    normalized = normalize_action(action)
    normalized_legal = [normalize_action(candidate) for candidate in legal]
    if normalized not in normalized_legal:
        raise ValueError(f"acción ilegal: {normalized!r}")
    return legal[normalized_legal.index(normalized)]


def _prompt(
    state: GraphState, *, previous_error: str | None = None
) -> str:
    payload = {
        "battle": state["battle_state"],
        "damage": state.get("damage", []),
        "legal_actions": state["battle_state"].get("legal_actions", []),
    }
    instructions = (
        "Elegí exactamente una acción de legal_actions. Respondé con action "
        "y un reasoning breve. Ausente y false son equivalentes únicamente "
        "para los flags de mecánicas especiales."
    )
    if previous_error:
        instructions += (
            f"\nLa respuesta anterior fue una acción ilegal o inválida: "
            f"{previous_error}. Corregila usando la máscara."
        )
    return instructions + "\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def _fallback(state: GraphState) -> dict[str, Any]:
    legal = state["battle_state"].get("legal_actions", [])
    action = rank_move_fallback(legal, state.get("damage", []))
    if action is None:
        action = rank_switch_fallback(legal, state.get("damage", []))
    if action is None and legal:
        action = legal[0]
    if action is None:
        raise ValueError("no legal action available for deterministic fallback")
    return action


async def decide(
    state: GraphState,
    provider: DecisionProvider,
    metrics: DecisionMetrics,
) -> dict[str, Any]:
    legal = state["battle_state"].get("legal_actions", [])
    turn_id = str(state.get(
        "turn_id",
        f"{state['battle_state'].get('player_role')}:{state['battle_state'].get('turn')}",
    ))
    deadline = float(state.get("deadline", time.monotonic() + 240))
    previous_error: str | None = None
    metrics.turn(turn_id)

    for semantic_attempt in range(2):
        raw = await provider.complete(
            _prompt(state, previous_error=previous_error),
            deadline=deadline,
            turn_id=turn_id,
        )
        try:
            parsed = DecisionResponse.model_validate(raw)
            action = validate_action(
                parsed.action.model_dump(exclude_none=True), legal
            )
            return {
                "action": action,
                "action_path": "llm" if semantic_attempt == 0 else "llm_retry",
                "reasoning": parsed.reasoning,
            }
        except (ValueError, TypeError) as exc:
            metrics.model_invalid(turn_id)
            previous_error = str(exc)

    metrics.fallback(turn_id)
    return {
        "action": _fallback(state),
        "action_path": "fallback",
        "reasoning": "deterministic fallback after two invalid model responses",
    }
