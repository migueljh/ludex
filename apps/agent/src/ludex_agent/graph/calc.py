"""Cliente de daño y ranking determinista del respaldo."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from .state import GraphState


CalcResult = dict[str, Any]

# --- Mapeos: enums serializados de poke-env → strings exactos de @smogon/calc ---

_WEATHER_MAP = {
    "RAINDANCE": "Rain",
    "SUNNYDAY": "Sun",
    "SANDSTORM": "Sand",
    "HAIL": "Hail",
    "SNOW": "Snow",
    "SNOWSCAPE": "Snow",
    "HEAVY_RAIN": "Heavy Rain",
    "DESOLATELAND": "Harsh Sunshine",
    "HARSH_SUNSHINE": "Harsh Sunshine",
    "PRIMORDIALSEA": "Heavy Rain",
    "DELTASTREAM": "Strong Winds",
    "STRONG_WINDS": "Strong Winds",
}

_TERRAIN_MAP = {
    "ELECTRIC_TERRAIN": "Electric",
    "GRASSY_TERRAIN": "Grassy",
    "PSYCHIC_TERRAIN": "Psychic",
    "MISTY_TERRAIN": "Misty",
}

_STATUS_MAP = {
    "BRN": "brn",
    "PAR": "par",
    "SLP": "slp",
    "FRZ": "frz",
    "PSN": "psn",
    "TOX": "tox",
}

# Side conditions que mapean a flags booleanos del Side del calc.
_SIDE_BOOL_MAP = {
    "REFLECT": "isReflect",
    "LIGHT_SCREEN": "isLightScreen",
    "AURORA_VEIL": "isAuroraVeil",
    "STEALTH_ROCK": "isSR",
    "TAILWIND": "isTailwind",
}
# Side conditions con valor numérico (stacks).
_SIDE_INT_MAP = {
    "SPIKES": "spikes",
}


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
    """Construye el descriptor de pokemon para el calc, sin inventar datos.

    Solo se incluyen campos observables conocidos. ability/item/nature/EVs/IVs
    no revelados se omiten: el calc los asume por defecto si están ausentes.
    status se normaliza de enum name a string lowercase del calc.
    curHP se calcula solo cuando se conoce el maxHP (stats propios).
    """
    descriptor: dict[str, Any] = {"species": mon["species"]}
    if mon.get("level") is not None:
        descriptor["level"] = mon["level"]
    if mon.get("item"):
        descriptor["item"] = mon["item"]
    if mon.get("ability"):
        descriptor["ability"] = mon["ability"]
    status = mon.get("status")
    if status and status in _STATUS_MAP:
        descriptor["status"] = _STATUS_MAP[status]
    boosts = mon.get("boosts")
    if boosts and isinstance(boosts, dict) and any(v for v in boosts.values() if v):
        descriptor["boosts"] = dict(boosts)
    hp_fraction = mon.get("hp_fraction")
    stats = mon.get("stats")
    if (
        hp_fraction is not None
        and isinstance(stats, dict)
        and isinstance(stats.get("hp"), (int, float))
    ):
        descriptor["curHP"] = round(hp_fraction * stats["hp"])
    return descriptor


def _build_side(raw: dict[str, Any]) -> dict[str, Any]:
    """Mapea side_conditions de poke-env a flags del Side del calc."""
    side: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SIDE_BOOL_MAP:
            side[_SIDE_BOOL_MAP[key]] = True
        elif key in _SIDE_INT_MAP:
            if isinstance(value, (int, float)):
                side[_SIDE_INT_MAP[key]] = int(value)
    return side


def _build_field(battle: dict[str, Any]) -> dict[str, Any] | None:
    """Construye field descriptor desde battle_state.field, o None si vacío."""
    field_raw = battle.get("field", {})
    if not isinstance(field_raw, dict):
        return None

    field: dict[str, Any] = {}

    fmt = battle.get("format") or ""
    if "double" in fmt:
        field["gameType"] = "Doubles"
    else:
        field["gameType"] = "Singles"

    weather = field_raw.get("weather", {})
    if isinstance(weather, dict):
        for name in weather:
            mapped = _WEATHER_MAP.get(name)
            if mapped is not None:
                field["weather"] = mapped
                break

    effects = field_raw.get("field_effects", {})
    if isinstance(effects, dict):
        for name in effects:
            mapped = _TERRAIN_MAP.get(name)
            if mapped is not None:
                field["terrain"] = mapped
                break
        if "GRAVITY" in effects:
            field["isGravity"] = True

    attacker_side = _build_side(field_raw.get("my_side", {}))
    defender_side = _build_side(field_raw.get("opponent_side", {}))
    if attacker_side:
        field["attackerSide"] = attacker_side
    if defender_side:
        field["defenderSide"] = defender_side

    return field if field else None


def _resolve_mega_species(
    mon: dict[str, Any], context: dict[str, Any] | None, *, side: str
) -> dict[str, Any]:
    """Si el pokemon tiene una forma Mega disponible en el context, la usa."""
    if context is None:
        return _pokemon_descriptor(mon)
    species = mon.get("species")
    if not species:
        return _pokemon_descriptor(mon)
    own_context = context.get(side, [])
    mon_entry = next(
        (p for p in own_context if p.get("showdown_id") == species),
        None,
    )
    if mon_entry is None:
        return _pokemon_descriptor(mon)
    base_species = mon_entry.get("base_species") or species
    mega_entry = next(
        (
            p
            for p in own_context
            if isinstance(p, dict)
            and isinstance(p.get("forme"), str)
            and "Mega" in p["forme"]
            and (p.get("base_species") or "") == base_species
            and p.get("showdown_id") != species
        ),
        None,
    )
    if mega_entry is None:
        return _pokemon_descriptor(mon)
    descriptor = _pokemon_descriptor(mon)
    descriptor["species"] = mega_entry["showdown_id"]
    abilities = mega_entry.get("abilities", {})
    if isinstance(abilities, dict):
        primary = abilities.get("0")
        if primary:
            descriptor["ability"] = primary
    return descriptor


def _request(
    *,
    gen: int,
    attacker: dict[str, Any],
    defender: dict[str, Any],
    move_id: str,
    field: dict[str, Any] | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "gen": gen,
        "attacker": attacker,
        "defender": defender,
        "move": {"name": move_id},
    }
    if field is not None:
        request["field"] = field
    return request


def _remaining_hp(result: CalcResult, fraction: float | None) -> float:
    maximum = result["defender_hp"]["max"]
    return maximum * (fraction if fraction is not None else 1)


def _rival_possible_moves(context: dict[str, Any] | None, rival_species: str) -> list[str]:
    if context is None:
        return []
    for mon in context.get("opponent", []):
        if mon.get("showdown_id") == rival_species:
            return [
                m["showdown_id"]
                for m in mon.get("moves", [])
                if isinstance(m, dict) and m.get("showdown_id")
            ]
    return []


async def calc_damage(
    state: GraphState, calculator: DamageCalculator
) -> dict[str, list[dict[str, Any]]]:
    """Calcula salidas disponibles; un matchup inválido queda diagnosticado.

    Errores de infraestructura (ConnectError, Timeout) PROPAGAN ruidosamente.
    Errores semánticos de una acción individual quedan diagnosticados por entry.
    """
    battle = state["battle_state"]
    me = battle.get("me", {})
    opponent = battle.get("opponent", {})
    mine = _active(me)
    rival = _active(opponent)
    damage: list[dict[str, Any]] = []
    if mine is None or rival is None:
        return {"damage": damage}

    gen = battle["gen"]
    context = state.get("context")
    field = _build_field(battle)

    for action in battle.get("legal_actions", []):
        if action.get("kind") == "move":
            is_mega = action.get("mega") is True
            if is_mega:
                attacker_desc = _resolve_mega_species(
                    mine, context, side="own"
                )
            else:
                attacker_desc = _pokemon_descriptor(mine)
            defender_desc = _pokemon_descriptor(rival)
            entry: dict[str, Any] = {
                "action": dict(action),
                "direction": "outgoing",
            }
            try:
                result = await calculator.calculate(_request(
                    gen=gen,
                    attacker=attacker_desc,
                    defender=defender_desc,
                    move_id=action["id"],
                    field=field,
                ))
                entry["result"] = result
                entry["remaining_hp"] = _remaining_hp(
                    result, rival.get("hp_fraction")
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                raise
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
            attacker_desc = _pokemon_descriptor(rival)
            defender_desc = _pokemon_descriptor(candidate)
            # Incoming: el rival ataca a nuestro candidato.
            # El field se invierte: attackerSide = opponent_side, defenderSide = my_side.
            incoming_field: dict[str, Any] | None = None
            if field is not None:
                incoming_field = dict(field)
                attacker_side = field.get("defenderSide")
                defender_side = field.get("attackerSide")
                if attacker_side:
                    incoming_field["attackerSide"] = attacker_side
                else:
                    incoming_field.pop("attackerSide", None)
                if defender_side:
                    incoming_field["defenderSide"] = defender_side
                else:
                    incoming_field.pop("defenderSide", None)

            rival_moves = [
                m["id"] for m in rival.get("moves", [])
                if isinstance(m, dict) and m.get("id")
            ]
            rival_species = rival.get("species", "")
            possible_moves = _rival_possible_moves(context, rival_species)
            use_possible = not rival_moves and bool(possible_moves)
            move_ids = possible_moves if use_possible else rival_moves

            for move_id in move_ids:
                entry = {
                    "action": dict(action),
                    "direction": "incoming",
                    "move_id": move_id,
                }
                if use_possible:
                    entry["possible"] = True
                try:
                    result = await calculator.calculate(_request(
                        gen=gen,
                        attacker=attacker_desc,
                        defender=defender_desc,
                        move_id=move_id,
                        field=incoming_field,
                    ))
                    entry["result"] = result
                    entry["defender_max_hp"] = result["defender_hp"]["max"]
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    raise
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
