"""Lectura generation-scoped de especies, movimientos y learnsets."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


def _showdown_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


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

_MOVES_CONTEXT = text("""
    SELECT
      m.showdown_id,
      m.name,
      m.type,
      m.category,
      m.power,
      m.power_kind,
      m.accuracy,
      m.pp,
      m.priority,
      m.target,
      m.flags,
      m.description
    FROM moves m
    WHERE m.gen_id = :gen_id
      AND m.showdown_id = ANY(CAST(:move_ids AS text[]))
    ORDER BY m.showdown_id
""")


def _standalone_move(row: Any) -> dict[str, object]:
    return {
        "showdown_id": row["showdown_id"],
        "name": row["name"],
        "type": row["type"],
        "category": row["category"],
        "power": row["power"],
        "power_kind": row["power_kind"],
        "accuracy": row["accuracy"],
        "never_misses": row["accuracy"] is None,
        "pp": row["pp"],
        "priority": row["priority"],
        "target": row["target"],
        "flags": dict(row["flags"]),
        "description": row["description"],
    }


class SpeciesVocabulary(Protocol):
    """Resuelve formas cosmeticas a su especie base sin heuristica de prefijo.

    Las formas cosmeticas (Vivillon-Tundra, Sawsbuck-Summer, Unown-O,
    Florges-Yellow/Orange) comparten stats y learnset con la especie base:
    solo cambia el sprite. Una forma con stats propios (Mega, floetteeternal)
    NO es cosmetica y debe LookupError ruidosamente. La generacion es siempre
    un parametro (ver .claude/showdown-data/SKILL.md y AGENTS.md).
    """

    def cosmetic_base_species(
        self, visible_id: str, gen_number: int
    ) -> str | None: ...


class PokeEnvSpeciesVocabulary:
    """Impl con ``GenData`` de poke-env, generation-scoped y cacheado por gen.

    La fuente es exclusivamente local (el paquete empaquetado), sin internet.
    """

    def __init__(self) -> None:
        self._data_by_gen: dict[int, Any] = {}

    def _data(self, gen_number: int) -> Any:
        from poke_env.data import GenData

        if gen_number not in self._data_by_gen:
            self._data_by_gen[gen_number] = GenData.from_gen(gen_number)
        return self._data_by_gen[gen_number]

    def cosmetic_base_species(
        self, visible_id: str, gen_number: int
    ) -> str | None:
        pokedex = self._data(gen_number).pokedex
        entry = pokedex.get(visible_id)
        if entry is None:
            return None
        base = entry.get("baseSpecies")
        if not isinstance(base, str) or not base:
            return None
        base_id = _showdown_id(base)
        if base_id == visible_id:
            return None
        base_entry = pokedex.get(base_id)
        if base_entry is None:
            return None
        # Una forma es cosmetica solo si sus stats son identicos a los de la
        # base. Mega y floetteeternal tienen stats propios: no degradan.
        if entry.get("baseStats") != base_entry.get("baseStats"):
            return None
        return base_id


class PostgresContextRepository:
    """El engine vive dentro del loop donde corre ``retrieve_context``.

    poke-env ejecuta ``choose_move`` (y por ende el grafo) en el loop del
    listener, que puede ser otro hilo distinto del loop que orquesta la
    batalla. Un ``AsyncEngine`` creado por el caller bindea su pool de
    asyncpg a ESE loop, y usarlo desde el listener cruza loops ("Future
    attached to a different loop"). Creando el engine perezosamente en el
    primer ``await`` garantizamos que el pool se bindea al loop correcto,
    sin acoplar al ``BattleRepository`` (que persiste en el loop principal).
    """

    def __init__(
        self,
        database_url: str,
        vocabulary: SpeciesVocabulary | None = None,
    ) -> None:
        self._database_url = database_url
        self._engine = None
        self._factory = None
        self._vocabulary = (
            vocabulary if vocabulary is not None else PokeEnvSpeciesVocabulary()
        )

    def _ensure_factory(self) -> Any:
        if self._factory is None:
            # NullPool: el repositorio corre en el loop del listener de
            # poke-env, que puede ser otro hilo distinto del loop que lo
            # dispone. Un pool que retiene conexiones (QueuePool) bindea sus
            # conexiones asyncpg al loop donde se crearon, y dispose() desde
            # otro loop cruza ("Future attached to a different loop"). NullPool
            # no retiene conexiones entre sesiones: cada checkout abre una
            # nueva y la devuelve cerrada, asi que dispose es trivial y no
            # necesita cerrar Futures en loop ajeno. El overhead de abrir una
            # conexion por query es despreciable para dos lecturas por
            # decision.
            from sqlalchemy.pool import NullPool

            self._engine = create_async_engine(
                self._database_url, poolclass=NullPool,
            )
            self._factory = async_sessionmaker(
                self._engine, expire_on_commit=False,
            )
        return self._factory

    async def aclose(self) -> None:
        engine = self._engine
        self._engine = None
        self._factory = None
        if engine is not None:
            await engine.dispose()

    async def load_battle_context(
        self,
        *,
        gen_number: int,
        own_species: tuple[str, ...],
        opponent_species: tuple[str, ...],
    ) -> dict[str, object]:
        requested = tuple(dict.fromkeys(own_species + opponent_species))
        # Resolver alias cosmeticos antes de tocar la base: las formas
        # cosmeticas (vivillontundra, florgesyellow, ...) no tienen fila en
        # `pokemon`, asi que se consultan por su especie base (vivillon,
        # florges...). El showdown_id VISIBLE se conserva en el resultado
        # para que la proyeccion correlacione por especie revelada. Una forma
        # ausente que no sea cosmética (floetteeternal: stats propios) no
        # degrada: LookupError ruidoso, nunca descarte silencioso.
        visible_to_db_id: dict[str, str] = {}
        query_ids: list[str] = []
        for visible_id in requested:
            if visible_id in visible_to_db_id:
                continue
            base = self._vocabulary.cosmetic_base_species(
                visible_id, gen_number
            )
            db_id = base if base is not None else visible_id
            visible_to_db_id[visible_id] = db_id
            if db_id not in query_ids:
                query_ids.append(db_id)

        factory = self._ensure_factory()
        async with factory() as session:
            generation = (
                await session.execute(
                    _GENERATION,
                    {"gen_number": gen_number},
                )
            ).mappings().one_or_none()
            if generation is None:
                raise LookupError(f"generación no seedeada: {gen_number}")

            rows = []
            if query_ids:
                rows = (
                    await session.execute(
                        _SPECIES_CONTEXT,
                        {
                            "gen_id": generation["id"],
                            "species_ids": query_ids,
                        },
                    )
                ).mappings().all()

        by_db_id: dict[str, dict[str, object]] = {}
        for row in rows:
            pokemon_id = row["pokemon_showdown_id"]
            pokemon = by_db_id.setdefault(
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

        by_visible_id: dict[str, dict[str, object]] = {}
        for visible_id in requested:
            db_id = visible_to_db_id[visible_id]
            if db_id not in by_db_id:
                raise LookupError(
                    f"especie visible no seedeada ni forma cosmética: "
                    f"{visible_id}"
                )
            labeled = deepcopy(by_db_id[db_id])
            labeled["showdown_id"] = visible_id
            by_visible_id[visible_id] = labeled

        return {
            "generation": {
                "gen_number": gen_number,
                "label": generation["label"],
            },
            "own": [
                deepcopy(by_visible_id[showdown_id])
                for showdown_id in own_species
                if showdown_id in by_visible_id
            ],
            "opponent": [
                deepcopy(by_visible_id[showdown_id])
                for showdown_id in opponent_species
                if showdown_id in by_visible_id
            ],
        }

    async def load_moves(
        self,
        *,
        gen_number: int,
        move_ids: tuple[str, ...],
    ) -> dict[str, dict[str, object]]:
        requested = tuple(dict.fromkeys(move_ids))
        if not requested:
            return {}

        factory = self._ensure_factory()
        async with factory() as session:
            generation = (
                await session.execute(
                    _GENERATION,
                    {"gen_number": gen_number},
                )
            ).mappings().one_or_none()
            if generation is None:
                raise LookupError(f"generación no seedeada: {gen_number}")
            rows = (
                await session.execute(
                    _MOVES_CONTEXT,
                    {
                        "gen_id": generation["id"],
                        "move_ids": list(requested),
                    },
                )
            ).mappings().all()

        by_id = {
            row["showdown_id"]: _standalone_move(row)
            for row in rows
        }
        missing = [
            move_id
            for move_id in requested
            if move_id not in by_id
        ]
        if missing:
            raise LookupError(
                "movimientos no seedeados para "
                f"Gen {gen_number}: {', '.join(missing)}"
            )
        return {
            move_id: by_id[move_id]
            for move_id in requested
        }
