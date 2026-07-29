"""Extracción allowlisted y nodo de contexto local del grafo."""

from __future__ import annotations

from typing import Protocol

from .state import GraphState


class ContextRepository(Protocol):
    async def load_battle_context(
        self,
        *,
        gen_number: int,
        own_species: tuple[str, ...],
        opponent_species: tuple[str, ...],
    ) -> dict[str, object]: ...


def _side_species(side: object) -> tuple[str, ...]:
    if not isinstance(side, dict):
        return ()
    pokemon = side.get("pokemon", [])
    if not isinstance(pokemon, list):
        return ()

    seen: set[str] = set()
    species: list[str] = []
    for entry in pokemon:
        if not isinstance(entry, dict):
            continue
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
    context = await repository.load_battle_context(
        gen_number=gen_number,
        own_species=own_species,
        opponent_species=opponent_species,
    )
    return {"context": context}
