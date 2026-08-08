"""Contrato semántico del modelo, validación y respaldo determinista."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .calc import rank_move_fallback, rank_switch_fallback
from .provider import CompletionUsage, DecisionMetrics, DecisionProvider
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


class DecisionTarget(BaseModel):
    """Objetivo de la accion. NULL es valido y esperado en singles; un target
    no-NULL solo es valido si la mascara legal expone targets explicitos
    (design verdict F2-08/MON-13)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["active_opponent", "slot"] | None = None
    id: str | None = None
    species: str | None = None


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    target: DecisionTarget | None = None
    rationale: str
    confidence: float
    alternatives: list[DecisionAction]

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        return value


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


def _action_key(action: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(action.items()))


def validate_alternatives(
    alternatives: list[DecisionAction],
    action: dict[str, Any],
    legal: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cada alternativa atraviesa el MISMO `normalize_action` +
    `validate_action` que la accion principal, contra la misma mascara
    capturada. Deben ser unicas tras la normalizacion y ninguna puede repetir
    la accion principal. `[]` es valido. Cualquier violacion consume el
    reintento semantico (ValueError), igual que una principal ilegal
    (design verdict F2-08/MON-13).
    """
    canonizadas = [
        validate_action(candidate.model_dump(exclude_none=True), legal)
        for candidate in alternatives
    ]
    if len({_action_key(c) for c in canonizadas}) != len(canonizadas):
        raise ValueError("alternatives duplicadas tras normalizacion")
    principal = _action_key(action)
    if any(_action_key(c) == principal for c in canonizadas):
        raise ValueError("una alternative repite la accion principal")
    return canonizadas


def validate_target(
    target: DecisionTarget | None, legal: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """NULL es valido y esperado en singles. Un target no-NULL solo es valido
    si la MISMA mascara legal expone targets explicitos; mientras no los
    exponga, se rechaza en vez de inventarse (design verdict F2-08/MON-13)."""
    if target is None:
        return None
    mask_targets = [
        item["target"]
        for item in legal
        if isinstance(item, dict) and "target" in item
    ]
    if not mask_targets:
        raise ValueError("target no permitido: la mascara legal no expone targets")
    serialized = target.model_dump(exclude_none=True)
    if serialized not in mask_targets:
        raise ValueError(f"target fuera de la mascara legal: {serialized!r}")
    return serialized


def _add_usage(
    total: CompletionUsage | None, usage: CompletionUsage
) -> CompletionUsage:
    """Suma de las respuestas facturables del camino de una decision.

    F2-08: el usage de una decision canonica acumula TODAS las respuestas
    facturables, incluidos los retries semanticos (y los de infraestructura
    cuando exista usage en el envelope de la llamada que termino
    exitosamente).
    """
    if total is None:
        return usage
    return CompletionUsage(
        input_tokens=total.input_tokens + usage.input_tokens,
        output_tokens=total.output_tokens + usage.output_tokens,
        cached_input_tokens=total.cached_input_tokens + usage.cached_input_tokens,
        reasoning_tokens=total.reasoning_tokens + usage.reasoning_tokens,
        model=usage.model or total.model,
    )


def _prompt(
    state: GraphState, *, previous_error: str | None = None
) -> str:
    payload = {
        "battle": state["battle_state"],
        "damage": state.get("damage", []),
        "legal_actions": state["battle_state"].get("legal_actions", []),
    }
    instructions = (
        "Elegí exactamente una acción de legal_actions. Respondé con action, "
        "un rationale breve (user-facing, sin cadena de razonamiento), "
        "confidence en [0,1] y alternatives (puede ser []). target solo si la "
        "máscara lo expone. Ausente y false son equivalentes únicamente para "
        "los flags de mecánicas especiales."
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
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    legal = state["battle_state"].get("legal_actions", [])
    turn_id = str(state.get(
        "turn_id",
        f"{state['battle_state'].get('player_role')}:{state['battle_state'].get('turn')}",
    ))
    deadline = float(state.get("deadline", clock() + 240))
    previous_error: str | None = None
    metrics.turn(turn_id)
    # F2-08: la latencia de la decision va desde el primer intento LLM hasta
    # la respuesta aceptada o el fallback, retries incluidos. Se mide aca, en
    # el camino de la decision, no por llamada.
    # F2-10: el reloj es inyectable para tests con reloj falso/scripted.
    started_at = clock()
    usage: CompletionUsage | None = None

    for semantic_attempt in range(2):
        envelope = await provider.complete(
            _prompt(state, previous_error=previous_error),
            deadline=deadline,
            turn_id=turn_id,
        )
        usage = _add_usage(usage, envelope.usage)
        if envelope.model is None:
            # Sin model efectivo no hay forma honesta de atribuir la decision:
            # es un fallo del pipeline (backend sin reporte y sin model
            # configurado), no del contrato del modelo. Falla ruidoso, nunca
            # se persiste metadata inventada.
            raise RuntimeError(
                f"provider {envelope.provider!r} no expuso un model efectivo: "
                "no se puede persistir metadata de decision sin inventar"
            )
        try:
            parsed = DecisionResponse.model_validate(envelope.payload)
            action = validate_action(
                parsed.action.model_dump(exclude_none=True), legal
            )
            alternatives = validate_alternatives(
                parsed.alternatives, action, legal
            )
            target = validate_target(parsed.target, legal)
            decision_latency_ms = (clock() - started_at) * 1000
            metrics.latency(decision_latency_ms)
            return {
                "action": action,
                "action_path": "llm" if semantic_attempt == 0 else "llm_retry",
                # `rationale` es el campo canonico (design verdict F2-08). El
                # alias `reasoning` NO forma parte del schema del proveedor:
                # es solo para los consumidores internos que ya lo leen
                # (run_graph en client.py), derivado del rationale validado.
                "rationale": parsed.rationale,
                "reasoning": parsed.rationale,
                "confidence": parsed.confidence,
                "alternatives": alternatives,
                "target": target,
                "provider": envelope.provider,
                "model": envelope.model,
                "decision_latency_ms": decision_latency_ms,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            }
        except (ValueError, TypeError) as exc:
            metrics.model_invalid(turn_id)
            previous_error = str(exc)

    metrics.fallback(turn_id)
    fallback_rationale = "deterministic fallback after two invalid model responses"
    decision_latency_ms = (clock() - started_at) * 1000
    metrics.latency(decision_latency_ms)
    return {
        "action": _fallback(state),
        "action_path": "fallback",
        "rationale": fallback_rationale,
        "reasoning": fallback_rationale,
        "confidence": None,
        "alternatives": [],
        "target": None,
        "provider": None,
        "model": None,
        "decision_latency_ms": decision_latency_ms,
        "input_tokens": usage.input_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "cached_input_tokens": usage.cached_input_tokens if usage is not None else None,
        "reasoning_tokens": usage.reasoning_tokens if usage is not None else None,
    }
