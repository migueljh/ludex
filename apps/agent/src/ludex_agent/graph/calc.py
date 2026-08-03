"""Cliente de daño y ranking determinista del respaldo."""

from __future__ import annotations

import asyncio
import json
import time
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


class CalcSemanticError(Exception):
    """HTTP 400 con el schema real del servicio (`{"error":{"code","message"}}`).

    Es la única excepción que se captura por acción: un matchup inválido de esa
    acción queda diagnosticado sin abortar el resto. Todo lo demás (JSON/shape
    inválido, 5xx, timeout, RequestError, bugs de programación) propaga.
    """

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status

    def as_entry(self) -> dict[str, Any]:
        return {
            "kind": "semantic_error",
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }


class CalcProtocolError(Exception):
    """Respuesta del calc que no cumple el contrato (JSON inválido o shape
    inválido): es un fallo de protocolo/infraestructura y debe propagar."""


def _parse_semantic_error(response: httpx.Response) -> CalcSemanticError:
    """Valida el 400 contra el schema real del servicio.

    El servicio devuelve `{"error":{"code":string,"message":string}}` (medido
    contra el server real). Un 400 con JSON inválido o shape distinto NO es un
    error semántico: es un fallo de protocolo y propaga como
    ``CalcProtocolError``.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise CalcProtocolError(
            f"calc respondió HTTP 400 sin JSON válido: {response.text[:200]!r}"
        ) from exc
    if not isinstance(body, dict):
        raise CalcProtocolError(
            f"calc respondió HTTP 400 con shape inválido: {response.text[:200]!r}"
        )
    error = body.get("error")
    if not isinstance(error, dict):
        raise CalcProtocolError(
            f"calc respondió HTTP 400 con shape inválido: {response.text[:200]!r}"
        )
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise CalcProtocolError(
            f"calc respondió HTTP 400 con shape inválido: {response.text[:200]!r}"
        )
    return CalcSemanticError(code, message)


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
        if response.status_code == 400:
            raise _parse_semantic_error(response)
        # 4xx/5xx restantes (404, 405, 500, 502...) no son semánticos: propagan
        # como fallo de infraestructura/protocolo.
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise CalcProtocolError(
                f"calc respondió 200 con JSON inválido: {response.text[:200]!r}"
            ) from exc


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


def _rival_possible_moves(
    context: dict[str, Any] | None, rival_species: str
) -> list[dict[str, Any]]:
    """Devuelve los descriptores completos del learnset visible del rival."""
    if context is None:
        return []
    for mon in context.get("opponent", []):
        if mon.get("showdown_id") == rival_species:
            return [
                move
                for move in mon.get("moves", [])
                if isinstance(move, dict) and move.get("showdown_id")
            ]
    return []


# Campos que @smogon/calc resuelve con un default cuando el request los omite
# (ver D35 y la inspección del constructor de Pokemon del paquete).
_ASSUMED_FIELDS = (
    "ability", "item", "nature", "evs", "ivs", "level",
    "gender", "status", "boosts", "curHP",
)


def _matchup_assumptions(
    descriptor: dict[str, Any],
    effective: dict[str, Any],
) -> dict[str, Any]:
    """Clasifica un lado del matchup en observado / desconocido / asumido.

    ``observed`` es lo que el adaptador envió (evidencia pública de la
    batalla). ``unknown`` son los campos de juego que la batalla no expuso.
    ``assumed`` son los valores efectivos que @smogon/calc aplicó para esos
    campos omitidos (defaults medidos del paquete, reportados por el server).
    """
    observed = {
        key: value for key, value in descriptor.items()
        if key != "species"
    }
    hp_known = "curHP" in observed or "hpFraction" in observed
    unknown: list[str] = []
    assumed: dict[str, Any] = {}
    for field in _ASSUMED_FIELDS:
        if field in observed:
            continue
        if field == "curHP" and hp_known:
            continue
        if field in effective:
            unknown.append(field)
            assumed[field] = effective[field]
    return {
        "observed": observed,
        "unknown": unknown,
        "assumed": assumed,
    }


_DAMAGE_RELEVANT_FIELDS = (
    "ability", "item", "nature", "evs", "ivs", "level", "boosts", "status",
)


def _attach_assumptions(
    entry: dict[str, Any],
    attacker_desc: dict[str, Any],
    defender_desc: dict[str, Any],
    result: CalcResult,
) -> None:
    effective = result.get("effective", {})
    entry["assumptions"] = {
        "attacker": _matchup_assumptions(
            attacker_desc, effective.get("attacker", {})
        ),
        "defender": _matchup_assumptions(
            defender_desc, effective.get("defender", {})
        ),
    }


def _depends_on_assumptions(entry: dict[str, Any]) -> bool:
    """True si el daño depende de algún default asumido por calc.

    Un KO presentado bajo ability/item/nature/EVs/IVs asumidos no es certeza:
    el fallback no debe tratarlo como tal.
    """
    assumptions = entry.get("assumptions", {})
    for side in ("attacker", "defender"):
        assumed = assumptions.get(side, {}).get("assumed", {})
        if any(field in assumed for field in _DAMAGE_RELEVANT_FIELDS):
            return True
    return False


_MAX_CONCURRENCY = 8
_TOP_N_POSSIBLE = 3


def _percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)

    def at(p: float) -> float:
        return ordered[min(n - 1, int(p * n))]

    return {
        "median": at(0.5),
        "p90": at(0.9),
        "p99": at(0.99),
        "max": ordered[-1],
    }


def _reduce_incoming_batch(
    entries: list[dict[str, Any]], *, top_n: int
) -> list[dict[str, Any]]:
    """Reduce un batch de movimientos entrantes de UN candidato.

    Preserva todos los revelados y los posibles con error (diagnósticos), y
    deja las top_n mayores amenazas posibles medidas (por daño máximo).
    """
    revealed = [entry for entry in entries if entry.get("revealed")]
    possible_ok = sorted(
        (
            entry for entry in entries
            if entry.get("possible") and "result" in entry
        ),
        key=lambda entry: entry["result"]["max_damage"],
        reverse=True,
    )
    possible_error = [
        entry for entry in entries
        if entry.get("possible") and "result" not in entry
    ]
    return revealed + possible_ok[:top_n] + possible_error


async def _calc_matchup(
    calculator: DamageCalculator,
    request: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[CalcResult | None, dict[str, Any] | None]:
    """Una llamada al calc. Devuelve (result, error_entry).

    Mide calls/bytes/latencia en ``metrics`` (mutación segura: asyncio es
    single-threaded). Todo lo que no es ``CalcSemanticError`` propaga.
    """
    payload_bytes = len(json.dumps(request, separators=(",", ":")))
    start = time.perf_counter()
    try:
        result = await calculator.calculate(request)
        return result, None
    except CalcSemanticError as exc:
        return None, exc.as_entry()
    finally:
        metrics["calls"] += 1
        metrics["bytes"] += payload_bytes
        metrics["latencies"].append(time.perf_counter() - start)


async def _concurrent_matchups(
    calculator: DamageCalculator,
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    metrics: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Ejecuta matchups con concurrencia acotada y orden determinista.

    ``items`` es una lista de (entry, request). El orden del resultado es el
    orden de entrada (``asyncio.gather`` preserva el orden de los coros).
    """
    if not items:
        return []
    semaphore = asyncio.Semaphore(limit)

    async def run(item: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        entry, request = item
        async with semaphore:
            result, error = await _calc_matchup(
                calculator, request, metrics
            )
        if error is not None:
            entry["error"] = error
        else:
            assert result is not None
            entry["result"] = result
        return entry

    return list(await asyncio.gather(*(run(item) for item in items)))


def _incoming_field(battle: dict[str, Any]) -> dict[str, Any] | None:
    """Field para un switch-in: hazards aplican y el lado se invierte
    (attackerSide = opponent_side, defenderSide = my_side)."""
    base_field = _build_field(battle, include_hazards=True)
    if base_field is None:
        return None
    incoming: dict[str, Any] = dict(base_field)
    attacker_side = base_field.get("defenderSide")
    defender_side = base_field.get("attackerSide")
    if attacker_side:
        incoming["attackerSide"] = attacker_side
    else:
        incoming.pop("attackerSide", None)
    if defender_side:
        incoming["defenderSide"] = defender_side
    else:
        incoming.pop("defenderSide", None)
    return incoming


def _union_revealed_possible(
    revealed_ids: list[str],
    possible_moves: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], bool, dict[str, Any] | None]]:
    """Unión deduplicada revelados+posibles con procedencia y descriptor.

    Devuelve (descriptor, is_possible, descriptor_possible). Si un id está en
    ambos, revealed gana (posible se omite). Sólo los movimientos posibles con
    ``category`` distinto de status entran: los status no calculan daño.
    """
    seen: set[str] = set()
    result: list[tuple[dict[str, Any], bool, dict[str, Any] | None]] = []
    for move_id in revealed_ids:
        if move_id not in seen:
            seen.add(move_id)
            result.append(({"id": move_id}, False, None))
    for move in possible_moves:
        move_id = move.get("showdown_id")
        if not isinstance(move_id, str) or not move_id:
            continue
        if move_id in seen:
            continue
        if str(move.get("category", "")).lower() == "status":
            continue
        seen.add(move_id)
        result.append(({"id": move_id}, True, move))
    return result


async def calc_damage(
    state: GraphState, calculator: DamageCalculator
) -> dict[str, Any]:
    """Calcula salidas disponibles; un matchup inválido queda diagnosticado.

    - ``CalcSemanticError`` (400 con schema real): capturado por acción con
      kind/code/status/message.
    - JSON/shape inválido, 5xx, timeout, RequestError, errores de programación:
      propagan ruidosamente (``CalcProtocolError`` u original).
    - Los possible_moves se calculan con concurrencia acotada, orden
      determinista, y se reducen post-cálculo a las mayores amenazas por
      candidato preservando los revelados. Se reporta `damage_metrics` con
      calls/bytes y latencia mediana/p90/p99/máximo.
    """
    battle = state["battle_state"]
    me = battle.get("me", {})
    opponent = battle.get("opponent", {})
    mine = _active(me)
    rival = _active(opponent)
    damage: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"calls": 0, "bytes": 0, "latencies": []}
    if mine is None or rival is None:
        return {"damage": damage, "damage_metrics": _final_metrics(metrics)}

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
            result, error = await _calc_matchup(calculator, _request(
                gen=gen,
                attacker=attacker_desc,
                defender=defender_desc,
                move_id=action["id"],
                field=field,
            ), metrics)
            if error is not None:
                entry["error"] = error
            else:
                assert result is not None
                entry["result"] = result
                entry["remaining_hp"] = _remaining_hp(
                    result, rival.get("hp_fraction")
                )
                _attach_assumptions(entry, attacker_desc, defender_desc, result)
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
            incoming_field = _incoming_field(battle)

            # Unión deduplicada: revelados conservan categoría revealed;
            # posibles adicionales conservan possible=True y su descriptor.
            rival_moves = [
                m["id"] for m in rival.get("moves", [])
                if isinstance(m, dict) and m.get("id")
            ]
            rival_species = rival.get("species", "")
            possible_moves = _rival_possible_moves(context, rival_species)
            union = _union_revealed_possible(rival_moves, possible_moves)

            batch: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for move_desc, is_possible, possible_desc in union:
                move_id = move_desc["id"]
                entry: dict[str, Any] = {
                    "action": dict(action),
                    "direction": "incoming",
                    "move_id": move_id,
                }
                if is_possible:
                    entry["possible"] = True
                    if possible_desc is not None:
                        entry["descriptor"] = {
                            key: value
                            for key, value in possible_desc.items()
                            if key != "learn_methods"
                        }
                else:
                    entry["revealed"] = True
                batch.append((entry, _request(
                    gen=gen,
                    attacker=attacker_desc,
                    defender=defender_desc,
                    move_id=move_id,
                    field=incoming_field,
                )))

            computed = await _concurrent_matchups(
                calculator, batch, metrics, limit=_MAX_CONCURRENCY
            )
            for entry in computed:
                if "result" in entry:
                    entry["defender_max_hp"] = entry["result"]["defender_hp"]["max"]
                    _attach_assumptions(
                        entry, attacker_desc, defender_desc, entry["result"]
                    )
            damage.extend(_reduce_incoming_batch(
                computed, top_n=_TOP_N_POSSIBLE
            ))
    return {"damage": damage, "damage_metrics": _final_metrics(metrics)}


def _final_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    latencies = metrics["latencies"]
    return {
        "calls": metrics["calls"],
        "bytes": metrics["bytes"],
        "latency_ms": _percentiles(
            [latency * 1000 for latency in latencies]
        ),
    }


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
        # Un KO solo cuenta como certeza si el daño no depende de defaults
        # asumidos por calc (ability/item/nature/EVs/IVs omitidos). Bajo
        # supuestos, se rankea por valor esperado, nunca como KO garantizado.
        ko_certain = (
            result["min_damage"] >= remaining
            and not _depends_on_assumptions(entry)
        )
        score = (
            ko_certain,
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