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
# Side conditions con valor nmerico (stacks).
_SIDE_INT_MAP = {
    "SPIKES": "spikes",
}
# Conditions que son hazards de entrada (solo aplican a switch-in, no a
# un defensor ya activo en un outgoing request).
_HAZARD_KEYS = {"STEALTH_ROCK", "SPIKES"}


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
    """Construye el descriptor de pokemon para el calc, sin inventar datos."""
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
    # HP: si conocemos maxHP (stats propios), materializamos curHP.
    # Si no (rival sin stats), enviamos hpFraction para que el calc lo
    # derive internamente. Nunca inventamos un maxHP.
    hp_fraction = mon.get("hp_fraction")
    stats = mon.get("stats")
    if (
        hp_fraction is not None
        and isinstance(stats, dict)
        and isinstance(stats.get("hp"), (int, float))
    ):
        descriptor["curHP"] = round(hp_fraction * stats["hp"])
    elif hp_fraction is not None:
        descriptor["hpFraction"] = hp_fraction
    return descriptor


def _build_side(raw: dict[str, Any], *, include_hazards: bool) -> dict[str, Any]:
    """Mapea side_conditions de poke-env a flags del Side del calc.

    Los hazards (Stealth Rock, Spikes) solo se incluyen cuando
    include_hazards=True (switch-in). En outgoing contra un rival ya
    activo, los hazards se omiten: el activo ya los recibió al entrar.
    """
    side: dict[str, Any] = {}
    for key, value in raw.items():
        is_hazard = key in _HAZARD_KEYS
        if is_hazard and not include_hazards:
            continue
        if key in _SIDE_BOOL_MAP:
            side[_SIDE_BOOL_MAP[key]] = True
        elif key in _SIDE_INT_MAP:
            if isinstance(value, (int, float)):
                side[_SIDE_INT_MAP[key]] = int(value)
    return side


def _build_field(
    battle: dict[str, Any], *, include_hazards: bool
) -> dict[str, Any] | None:
    """Construye field descriptor desde battle_state.field.

    include_hazards controla si los hazards de entrada se incluyen en
    el defenderSide (solo para switch-in, no para rival ya activo).
    """
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
        if "WONDER_ROOM" in effects:
            field["isWonderRoom"] = True
        if "MAGIC_ROOM" in effects:
            field["isMagicRoom"] = True

    attacker_side = _build_side(
        field_raw.get("my_side", {}), include_hazards=include_hazards
    )
    defender_side = _build_side(
        field_raw.get("opponent_side", {}), include_hazards=include_hazards
    )
    if attacker_side:
        field["attackerSide"] = attacker_side
    if defender_side:
        field["defenderSide"] = defender_side

    return field if field else None


def _resolve_mega(
    mon: dict[str, Any], context: dict[str, Any] | None
) -> dict[str, Any]:
    """Resuelve la forma Mega desde context.mega_forms usando el item del activo.

    Si action.mega=True, el item del pokemon activo (observable en
    battle_state) se busca en context["mega_forms"], que retrieve_context
    pobló batch desde la tabla items. mega_forms mapea item_id →
    {mega_species, mega_ability, mega_evolves}. Si no hay item o no es
    megastone, falla ruidosamente (LookupError).
    """
    item = mon.get("item")
    if not isinstance(item, str) or not item or context is None:
        raise LookupError("mega solicitada pero no hay item observable")

    mega_forms = context.get("mega_forms", {})
    if not isinstance(mega_forms, dict):
        raise LookupError("context.mega_forms inválido")

    mega_info = mega_forms.get(item)
    if mega_info is None:
        raise LookupError(f"item no es megastone o no existe para esta gen: {item}")

    mega_species = mega_info.get("mega_species")
    mega_ability = mega_info.get("mega_ability")
    mega_evolves = mega_info.get("mega_evolves")

    if not mega_species:
        raise LookupError(f"resolved megastone sin especie: {item}")

    if mega_evolves and _showdown_id(mega_evolves) != mon.get("species"):
        raise LookupError("megaEvolve no corresponde a la especie del activo")

    descriptor = _pokemon_descriptor(mon)
    descriptor["species"] = mega_species
    if mega_ability:
        descriptor["ability"] = mega_ability
    return descriptor


def _showdown_id(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", value.lower())


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


def _is_calc_error(exc: Exception) -> bool:
    """True si exc es un error semantico 4xx del calc (httpx.HTTPStatusError
    con status 400). Falso para 5xx, timeouts, connect errors, JSON invalido,
    bugs de programacion."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 400
    return False


def _calc_error_entry(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    """Estructura un error semantico 4xx del calc."""
    entry: dict[str, Any] = {
        "kind": "semantic_error",
        "status": exc.response.status_code,
    }
    try:
        body = exc.response.json()
        entry["code"] = body.get("error", body.get("code", "invalid_request"))
        entry["message"] = str(body.get("message", exc.response.text))
    except Exception:
        entry["code"] = "invalid_request"
        entry["message"] = exc.response.text
    return entry


async def _do_calc(
    calculator: DamageCalculator,
    request: dict[str, Any],
) -> CalcResult:
    """Ejecuta una llamada al calc, propagando todo menos 4xx semantic errors."""
    try:
        return await calculator.calculate(request)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            raise
        raise
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        raise


def _rival_possible_moves(
    context: dict[str, Any] | None, rival_species: str
) -> list[str]:
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

    - 4xx semantico: capturado por accion con kind/code/status/message.
    - 5xx, timeout, RequestError, JSON invalido: propagan ruidosamente.
    - Errores de programacion (TypeError, etc.): propagan.
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

    for action in battle.get("legal_actions", []):
        if action.get("kind") == "move":
            is_mega = action.get("mega") is True
            if is_mega:
                attacker_desc = _resolve_mega(mine, context)
            else:
                attacker_desc = _pokemon_descriptor(mine)
            defender_desc = _pokemon_descriptor(rival)
            # Outgoing: sin hazards en defenderSide (rival ya activo).
            field = _build_field(battle, include_hazards=False)
            entry: dict[str, Any] = {
                "action": dict(action),
                "direction": "outgoing",
            }
            try:
                result = await _do_calc(calculator, _request(
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
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400:
                    entry["error"] = _calc_error_entry(exc)
                else:
                    raise
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
            # Incoming: el rival ataca a nuestro candidato que ENTRA.
            # Hazards SI aplican (switch-in). El field se invierte:
            # attackerSide = opponent_side, defenderSide = my_side.
            base_field = _build_field(battle, include_hazards=True)
            incoming_field: dict[str, Any] | None = None
            if base_field is not None:
                incoming_field = dict(base_field)
                attacker_side = base_field.get("defenderSide")
                defender_side = base_field.get("attackerSide")
                if attacker_side:
                    incoming_field["attackerSide"] = attacker_side
                else:
                    incoming_field.pop("attackerSide", None)
                if defender_side:
                    incoming_field["defenderSide"] = defender_side
                else:
                    incoming_field.pop("defenderSide", None)

            # Union deduplicada: revelados + posibles adicionales.
            # Revealed conserva categoria revealed; possible adicionales
            # conservan possible=True.
            rival_moves = [
                m["id"] for m in rival.get("moves", [])
                if isinstance(m, dict) and m.get("id")
            ]
            rival_species = rival.get("species", "")
            possible_moves = _rival_possible_moves(context, rival_species)

            # Union deduplicada: revealed first, then possible not in revealed.
            seen: set[str] = set()
            move_entries: list[tuple[str, bool]] = []
            for move_id in rival_moves:
                if move_id not in seen:
                    seen.add(move_id)
                    move_entries.append((move_id, False))
            for move_id in possible_moves:
                if move_id not in seen:
                    seen.add(move_id)
                    move_entries.append((move_id, True))

            for move_id, is_possible in move_entries:
                entry = {
                    "action": dict(action),
                    "direction": "incoming",
                    "move_id": move_id,
                }
                if is_possible:
                    entry["possible"] = True
                else:
                    entry["revealed"] = True
                try:
                    result = await _do_calc(calculator, _request(
                        gen=gen,
                        attacker=attacker_desc,
                        defender=defender_desc,
                        move_id=move_id,
                        field=incoming_field,
                    ))
                    entry["result"] = result
                    entry["defender_max_hp"] = result["defender_hp"]["max"]
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400:
                        entry["error"] = _calc_error_entry(exc)
                    else:
                        raise
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