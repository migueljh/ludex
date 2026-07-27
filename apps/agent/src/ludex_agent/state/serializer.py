"""Battle de poke-env -> dict serializable.

REGLA DURA: esto es una LISTA BLANCA. Cada campo que entra se nombra a mano.
Esta prohibido recorrer los atributos del objeto Battle o serializarlo
genericamente, porque poke-env expone mi equipo completo y el del rival con la
misma forma, y del rival solo debe verse lo revelado. Con lista blanca, un
campo nuevo en una version futura de la libreria no se cuela solo.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .actions import legal_actions
from .schema import STATE_SCHEMA_VERSION


def _name(value: Any) -> Any:
    """Los enums de poke-env se persisten por nombre, no por valor numerico."""
    return value.name if isinstance(value, Enum) else value


def _moves(mon: Any) -> list[dict]:
    return [
        {"id": mid, "pp": mv.current_pp, "max_pp": mv.max_pp}
        for mid, mv in (mon.moves or {}).items()
    ]


def _pokemon(mon: Any, *, mine: bool) -> dict:
    """Campos observables de un pokemon.

    `mine=False` omite `stats`: los stats del rival no son observables, poke-env
    los estima o los deja en None, y persistirlos seria inventar informacion que
    el jugador no tiene.
    """
    out = {
        "species": mon.species,
        "hp_fraction": mon.current_hp_fraction,
        "active": mon.active,
        "fainted": mon.fainted,
        "status": _name(mon.status),
        "level": mon.level,
        "item": mon.item,
        "ability": mon.ability,
        "types": [_name(t) for t in (mon.types or []) if t is not None],
        "boosts": dict(mon.boosts or {}),
        "moves": _moves(mon),
    }
    if mine:
        out["stats"] = dict(mon.stats or {})
    return out


def _side(team: dict, *, mine: bool) -> dict:
    return {"pokemon": [_pokemon(m, mine=mine) for m in (team or {}).values()]}


def _conditions(raw: dict) -> dict:
    return {_name(k): _name(v) for k, v in (raw or {}).items()}


def serialize_battle(battle: Any) -> dict:
    """Estado observable del turno, desde el punto de vista de ESTE jugador."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "turn": battle.turn,
        "player_role": battle.player_role,
        "format": battle.format,
        "gen": battle.gen,
        "field": {
            "weather": _conditions(battle.weather),
            "terrain": _conditions(battle.fields),
            "my_side": _conditions(battle.side_conditions),
            "opponent_side": _conditions(battle.opponent_side_conditions),
        },
        "me": _side(battle.team, mine=True),
        "opponent": _side(battle.opponent_team, mine=False),
        "legal_actions": legal_actions(battle),
    }
