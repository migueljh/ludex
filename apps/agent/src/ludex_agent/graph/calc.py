"""Cliente de daño y ranking determinista del respaldo."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from .state import GraphState


CalcResult = dict[str, Any]


class DamageCalculator(Protocol):
    async def calculate(self, request: dict[str, Any]) -> CalcResult: ...


class CalcClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        response = await self._client.get("/health")
        response.raise_for_status()
        return response.json().get("status") == "ok"

    async def calculate(self, request: dict[str, Any]) -> CalcResult:
        response = await self._client.post("/calc", json=request)
        response.raise_for_status()
        return response.json()


def _active(side: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (mon for mon in side.get("pokemon", []) if mon.get("active")),
        None,
    )


def _pokemon_descriptor(mon: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "species": "species",
        "level": "level",
        "item": "item",
        "ability": "ability",
        "status": "status",
        "boosts": "boosts",
    }
    return {
        target: mon[source]
        for source, target in mapping.items()
        if mon.get(source) is not None
    }


def _request(
    *, gen: int, attacker: dict[str, Any], defender: dict[str, Any], move_id: str
) -> dict[str, Any]:
    return {
        "gen": gen,
        "attacker": _pokemon_descriptor(attacker),
        "defender": _pokemon_descriptor(defender),
        "move": {"name": move_id},
    }


def _remaining_hp(result: CalcResult, fraction: float | None) -> float:
    maximum = result["defender_hp"]["max"]
    return maximum * (fraction if fraction is not None else 1)


async def calc_damage(
    state: GraphState, calculator: DamageCalculator
) -> dict[str, list[dict[str, Any]]]:
    """Calcula salidas disponibles; un matchup inválido queda diagnosticado."""
    battle = state["battle_state"]
    me = battle.get("me", {})
    opponent = battle.get("opponent", {})
    mine = _active(me)
    rival = _active(opponent)
    damage: list[dict[str, Any]] = []
    if mine is None or rival is None:
        return {"damage": damage}

    gen = battle["gen"]
    for action in battle.get("legal_actions", []):
        if action.get("kind") == "move":
            entry = {
                "action": dict(action),
                "direction": "outgoing",
            }
            try:
                result = await calculator.calculate(_request(
                    gen=gen, attacker=mine, defender=rival, move_id=action["id"]
                ))
                entry["result"] = result
                entry["remaining_hp"] = _remaining_hp(
                    result, rival.get("hp_fraction")
                )
            except Exception as exc:
                entry["error"] = str(exc)
            damage.append(entry)

        elif action.get("kind") == "switch":
            candidate = next(
                (
                    mon for mon in me.get("pokemon", [])
                    if mon.get("species") == action.get("species")
                ),
                None,
            )
            if candidate is None:
                continue
            for move in rival.get("moves", []):
                entry = {
                    "action": dict(action),
                    "direction": "incoming",
                    "move_id": move.get("id"),
                }
                try:
                    result = await calculator.calculate(_request(
                        gen=gen,
                        attacker=rival,
                        defender=candidate,
                        move_id=move["id"],
                    ))
                    entry["result"] = result
                    entry["defender_max_hp"] = result["defender_hp"]["max"]
                except Exception as exc:
                    entry["error"] = str(exc)
                damage.append(entry)
    return {"damage": damage}


def _roll_totals(result: CalcResult) -> list[float]:
    rolls = result.get("damage_rolls", [])
    if not rolls:
        return []
    return [sum(position) for position in zip(*rolls)]


def _expected_capped(result: CalcResult, remaining_hp: float) -> float:
    totals = _roll_totals(result)
    if not totals:
        return 0
    return sum(min(value, remaining_hp) for value in totals) / len(totals)


def rank_move_fallback(
    legal_actions: list[dict[str, Any]],
    damage: list[dict[str, Any]],
) -> dict[str, Any] | None:
    entries = {
        tuple(sorted(entry["action"].items())): entry
        for entry in damage
        if entry.get("direction") == "outgoing" and "result" in entry
    }
    best: dict[str, Any] | None = None
    best_score: tuple[bool, float] = (False, -1)
    for action in legal_actions:
        if action.get("kind") != "move":
            continue
        entry = entries.get(tuple(sorted(action.items())))
        if entry is None:
            continue
        remaining = entry["remaining_hp"]
        result = entry["result"]
        score = (
            result["min_damage"] >= remaining,
            _expected_capped(result, remaining),
        )
        if score > best_score:
            best, best_score = action, score
    return best


def rank_switch_fallback(
    legal_actions: list[dict[str, Any]],
    damage: list[dict[str, Any]],
) -> dict[str, Any] | None:
    switches = [action for action in legal_actions if action.get("kind") == "switch"]
    if not switches:
        return None
    scores: dict[tuple[tuple[str, Any], ...], list[float]] = {}
    for entry in damage:
        if entry.get("direction") != "incoming" or "result" not in entry:
            continue
        maximum = entry.get("defender_max_hp") or entry["result"]["defender_hp"]["max"]
        totals = _roll_totals(entry["result"])
        if maximum <= 0 or not totals:
            continue
        expected_fraction = (sum(totals) / len(totals)) / maximum
        key = tuple(sorted(entry["action"].items()))
        scores.setdefault(key, []).append(expected_fraction)

    best = switches[0]
    best_worst = float("inf")
    found = False
    for action in switches:
        matchups = scores.get(tuple(sorted(action.items())), [])
        if matchups and max(matchups) < best_worst:
            best, best_worst, found = action, max(matchups), True
    return best if found else switches[0]
