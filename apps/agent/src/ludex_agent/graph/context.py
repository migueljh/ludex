"""Extracción allowlisted y nodo de contexto local del grafo."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Protocol

from .state import GraphState


class ContextRepository(Protocol):
    async def load_battle_context(
        self,
        *,
        gen_number: int,
        own_species: tuple[str, ...],
        opponent_species: tuple[str, ...],
    ) -> dict[str, object]: ...

    async def load_moves(
        self,
        *,
        gen_number: int,
        move_ids: tuple[str, ...],
    ) -> dict[str, dict[str, object]]: ...

    async def load_mega_forms(
        self,
        *,
        gen_number: int,
        item_ids: tuple[str, ...],
    ) -> dict[str, dict[str, object]]: ...


def _side_pokemon(side: object) -> list[dict[str, Any]]:
    if not isinstance(side, dict):
        return []
    pokemon = side.get("pokemon", [])
    if not isinstance(pokemon, list):
        return []
    return [entry for entry in pokemon if isinstance(entry, dict)]


def _side_species(side: object) -> tuple[str, ...]:
    pokemon = _side_pokemon(side)

    seen: set[str] = set()
    species: list[str] = []
    for entry in pokemon:
        showdown_id = entry.get("species")
        if (
            isinstance(showdown_id, str)
            and showdown_id
            and showdown_id not in seen
        ):
            seen.add(showdown_id)
            species.append(showdown_id)
    return tuple(species)


def extract_species_ids(
    state: GraphState,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extrae sólo especies visibles desde la salida de ``parse_state``."""
    battle = state["battle_state"]
    return (
        _side_species(battle.get("me")),
        _side_species(battle.get("opponent")),
    )


def _entry_move_ids(entry: dict[str, Any]) -> list[str]:
    moves = entry.get("moves", [])
    if not isinstance(moves, list):
        return []
    return [
        move_id
        for move in moves
        if isinstance(move, dict)
        and isinstance((move_id := move.get("id")), str)
        and move_id
    ]


def extract_observed_move_ids(state: GraphState) -> tuple[str, ...]:
    """Extrae IDs públicos conocidos/revelados, sin consultar ``raw_state``."""
    battle = state["battle_state"]
    seen: set[str] = set()
    observed: list[str] = []
    for side_name in ("me", "opponent"):
        for pokemon in _side_pokemon(battle.get(side_name)):
            for move_id in _entry_move_ids(pokemon):
                if move_id not in seen:
                    seen.add(move_id)
                    observed.append(move_id)
    return tuple(observed)


def _showdown_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _ability_ids(abilities: object) -> list[str]:
    if not isinstance(abilities, dict):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in abilities.values():
        values = value if isinstance(value, list) else [value]
        for ability in values:
            if not isinstance(ability, str):
                continue
            ability_id = _showdown_id(ability)
            if ability_id and ability_id not in seen:
                seen.add(ability_id)
                result.append(ability_id)
    return result


def _base_move_descriptor(move: dict[str, Any]) -> dict[str, object]:
    return {
        "type": move["type"],
        "category": move["category"],
        "power": move["power"],
        "power_kind": move["power_kind"],
        "accuracy": (
            "never_misses"
            if move.get("accuracy") is None
            else move["accuracy"]
        ),
        "priority": move["priority"],
        "target": move["target"],
    }


def _observed_move_descriptor(move: dict[str, Any]) -> dict[str, object]:
    return {
        **_base_move_descriptor(move),
        "description": move["description"],
        "flags": deepcopy(move["flags"]),
    }


def project_prompt_context(
    battle_state: dict[str, Any],
    rich_context: dict[str, Any],
) -> dict[str, object]:
    """Proyecta contexto compacto sin mutar el estado ni el contexto rico."""
    own_state = _side_pokemon(battle_state.get("me"))
    opponent_state = _side_pokemon(battle_state.get("opponent"))
    rich_opponent = {
        pokemon["showdown_id"]: pokemon
        for pokemon in rich_context.get("opponent", [])
        if isinstance(pokemon, dict)
        and isinstance(pokemon.get("showdown_id"), str)
    }
    observed = rich_context.get("observed_moves", {})
    if not isinstance(observed, dict):
        raise TypeError("context.observed_moves debe ser un dict")

    own: list[dict[str, object]] = []
    opponent: list[dict[str, object]] = []
    observed_ids: list[str] = []
    possible_by_id: dict[str, dict[str, Any]] = {}

    for pokemon in own_state:
        species = pokemon.get("species")
        if not isinstance(species, str) or not species:
            continue
        known_moves = _entry_move_ids(pokemon)
        observed_ids.extend(known_moves)
        own.append({
            "species": species,
            "known_moves": known_moves,
        })

    for pokemon in opponent_state:
        species = pokemon.get("species")
        if not isinstance(species, str) or not species:
            continue
        metadata = rich_opponent.get(species)
        if metadata is None:
            continue
        revealed_moves = _entry_move_ids(pokemon)
        observed_ids.extend(revealed_moves)
        possible_moves: list[str] = []
        for move in metadata.get("moves", []):
            if (
                not isinstance(move, dict)
                or not isinstance(move.get("showdown_id"), str)
            ):
                continue
            move_id = move["showdown_id"]
            possible_moves.append(move_id)
            possible_by_id.setdefault(move_id, move)
        opponent.append({
            "species": species,
            "base_types": deepcopy(metadata.get("types", [])),
            "base_stats": deepcopy(metadata.get("base_stats", {})),
            "possible_abilities": _ability_ids(metadata.get("abilities")),
            "revealed_moves": revealed_moves,
            "possible_moves": possible_moves,
        })

    observed_set = set(observed_ids)
    catalog_ids = tuple(dict.fromkeys(
        observed_ids + list(possible_by_id)
    ))
    missing = [
        move_id
        for move_id in observed_set
        if move_id not in observed
    ]
    if missing:
        raise LookupError(
            "movimientos observados no seedeados: "
            + ", ".join(sorted(missing))
        )

    moves: dict[str, dict[str, object]] = {}
    for move_id in catalog_ids:
        if move_id in observed_set:
            move = observed[move_id]
            if not isinstance(move, dict):
                raise TypeError(f"descriptor inválido para {move_id}")
            moves[move_id] = _observed_move_descriptor(move)
        else:
            moves[move_id] = _base_move_descriptor(possible_by_id[move_id])

    return {
        "generation": deepcopy(rich_context["generation"]),
        "own": own,
        "opponent": opponent,
        "moves": moves,
    }


async def retrieve_context(
    state: GraphState,
    repository: ContextRepository,
) -> dict[str, object]:
    """Devuelve contexto JSON-serializable para las especies visibles."""
    battle = state["battle_state"]
    gen_number = battle.get("gen")
    if not isinstance(gen_number, int) or isinstance(gen_number, bool):
        raise ValueError("battle_state.gen debe ser un entero")
    own_species, opponent_species = extract_species_ids(state)
    observed_ids = extract_observed_move_ids(state)
    battle_context = await repository.load_battle_context(
        gen_number=gen_number,
        own_species=own_species,
        opponent_species=opponent_species,
    )
    observed_moves = await repository.load_moves(
        gen_number=gen_number,
        move_ids=observed_ids,
    )
    missing = [
        move_id
        for move_id in observed_ids
        if move_id not in observed_moves
    ]
    if missing:
        raise LookupError(
            "movimientos observados no seedeados: "
            + ", ".join(missing)
        )

    # Resolver megastones: recoger items visibles del lado propio y
    # rival, batch consultar la tabla items para obtener la forma Mega
    # y su ability. El resultado entra en el contexto rico para que
    # calc_damage lo use sin queries adicionales (D32 reserva context
    # para calc).
    mega_item_ids: list[str] = []
    for side_name in ("me", "opponent"):
        for mon in _side_pokemon(battle.get(side_name)):
            item = mon.get("item")
            if isinstance(item, str) and item:
                mega_item_ids.append(item)
    mega_forms = await repository.load_mega_forms(
        gen_number=gen_number,
        item_ids=tuple(mega_item_ids),
    ) if mega_item_ids else {}

    context = {
        **battle_context,
        "observed_moves": {
            move_id: deepcopy(observed_moves[move_id])
            for move_id in observed_ids
        },
        "mega_forms": mega_forms,
    }
    return {
        "context": context,
        "prompt_context": project_prompt_context(battle, context),
    }
