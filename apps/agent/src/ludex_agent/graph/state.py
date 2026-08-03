"""Estado puro y allowlisted que consume el grafo."""

from __future__ import annotations

from typing import Any, TypedDict


class GraphInput(TypedDict):
    battle_state: dict[str, Any]


class GraphState(TypedDict, total=False):
    raw_state: dict[str, Any]
    battle_state: dict[str, Any]
    context: dict[str, Any]
    prompt_context: dict[str, Any]
    damage: list[dict[str, Any]]
    damage_metrics: dict[str, Any]
    action: dict[str, Any]
    action_path: str
    reasoning: str
    rationale: str
    confidence: float | None
    alternatives: list[dict[str, Any]]
    target: dict[str, Any] | None
    provider: str | None
    model: str | None
    decision_latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    turn_id: str
    deadline: float


_TOP_LEVEL = (
    "schema_version", "turn", "player_role", "format", "gen",
)
_FIELD = ("weather", "field_effects", "my_side", "opponent_side")
_POKEMON = (
    "species", "hp_fraction", "active", "fainted", "status", "level",
    "item", "ability", "types", "boosts",
)
_MOVE = ("id", "pp", "max_pp")
_ACTION = (
    "kind", "id", "species", "mega", "z_move", "dynamax", "terastallize",
)


def _copy_known(raw: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: raw[key] for key in keys if key in raw}


def _pokemon(raw: dict[str, Any], *, mine: bool) -> dict[str, Any]:
    result = _copy_known(raw, _POKEMON)
    result["moves"] = [
        _copy_known(move, _MOVE) for move in raw.get("moves", [])
        if isinstance(move, dict)
    ]
    if mine and "stats" in raw:
        result["stats"] = dict(raw["stats"])
    return result


def _side(raw: dict[str, Any], *, mine: bool) -> dict[str, Any]:
    return {
        "pokemon": [
            _pokemon(mon, mine=mine) for mon in raw.get("pokemon", [])
            if isinstance(mon, dict)
        ]
    }


def allowlisted_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Copia únicamente campos observables nombrados explícitamente."""
    result = _copy_known(raw, _TOP_LEVEL)
    field = raw.get("field", {})
    result["field"] = _copy_known(field, _FIELD) if isinstance(field, dict) else {}
    me = raw.get("me", {})
    opponent = raw.get("opponent", {})
    result["me"] = _side(me, mine=True) if isinstance(me, dict) else {"pokemon": []}
    result["opponent"] = (
        _side(opponent, mine=False)
        if isinstance(opponent, dict) else {"pokemon": []}
    )
    result["legal_actions"] = [
        _copy_known(action, _ACTION)
        for action in raw.get("legal_actions", [])
        if isinstance(action, dict)
    ]
    return result
