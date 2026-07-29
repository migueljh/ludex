"""Lectura generation-scoped de especies, movimientos y learnsets."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import text


_GENERATION = text("""
    SELECT id, label
    FROM generations
    WHERE gen_number = :gen_number
""")

_SPECIES_CONTEXT = text("""
    SELECT
      p.showdown_id AS pokemon_showdown_id,
      p.name AS pokemon_name,
      p.base_species,
      p.forme,
      p.types,
      p.base_stats,
      p.abilities,
      m.showdown_id AS move_showdown_id,
      m.name AS move_name,
      m.type AS move_type,
      m.category AS move_category,
      m.power,
      m.power_kind,
      m.accuracy,
      m.pp,
      m.priority,
      m.target,
      m.flags,
      m.description,
      l.learn_methods
    FROM pokemon p
    LEFT JOIN learnsets l
      ON l.pokemon_id = p.id
    LEFT JOIN moves m
      ON m.id = l.move_id
     AND m.gen_id = p.gen_id
    WHERE p.gen_id = :gen_id
      AND p.showdown_id = ANY(CAST(:species_ids AS text[]))
    ORDER BY p.showdown_id, m.showdown_id
""")


class PostgresContextRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def load_battle_context(
        self,
        *,
        gen_number: int,
        own_species: tuple[str, ...],
        opponent_species: tuple[str, ...],
    ) -> dict[str, object]:
        requested = tuple(dict.fromkeys(own_species + opponent_species))
        async with self.factory() as session:
            generation = (
                await session.execute(
                    _GENERATION,
                    {"gen_number": gen_number},
                )
            ).mappings().one_or_none()
            if generation is None:
                raise LookupError(f"generación no seedeada: {gen_number}")

            rows = []
            if requested:
                rows = (
                    await session.execute(
                        _SPECIES_CONTEXT,
                        {
                            "gen_id": generation["id"],
                            "species_ids": list(requested),
                        },
                    )
                ).mappings().all()

        by_id: dict[str, dict[str, object]] = {}
        for row in rows:
            pokemon_id = row["pokemon_showdown_id"]
            pokemon = by_id.setdefault(
                pokemon_id,
                {
                    "showdown_id": pokemon_id,
                    "name": row["pokemon_name"],
                    "base_species": row["base_species"],
                    "forme": row["forme"],
                    "types": list(row["types"]),
                    "base_stats": dict(row["base_stats"]),
                    "abilities": dict(row["abilities"]),
                    "moves": [],
                },
            )
            if row["move_showdown_id"] is None:
                continue
            pokemon["moves"].append({
                "showdown_id": row["move_showdown_id"],
                "name": row["move_name"],
                "type": row["move_type"],
                "category": row["move_category"],
                "power": row["power"],
                "power_kind": row["power_kind"],
                "accuracy": row["accuracy"],
                "never_misses": row["accuracy"] is None,
                "pp": row["pp"],
                "priority": row["priority"],
                "target": row["target"],
                "flags": dict(row["flags"]),
                "description": row["description"],
                "learn_methods": list(row["learn_methods"]),
            })

        return {
            "generation": {
                "gen_number": gen_number,
                "label": generation["label"],
            },
            "own": [
                deepcopy(by_id[showdown_id])
                for showdown_id in own_species
                if showdown_id in by_id
            ],
            "opponent": [
                deepcopy(by_id[showdown_id])
                for showdown_id in opponent_species
                if showdown_id in by_id
            ],
        }
