import json
import os
from contextlib import asynccontextmanager

import pytest

from ludex_agent.config import load_settings
from ludex_agent.db.session import make_engine, session_factory


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)


@asynccontextmanager
async def _repository():
    from ludex_agent.db.context_repository import PostgresContextRepository

    engine = make_engine(load_settings().database_url)
    repo = PostgresContextRepository(session_factory(engine))
    try:
        yield repo
    finally:
        await engine.dispose()


def _species(context, side, showdown_id):
    return next(
        pokemon
        for pokemon in context[side]
        if pokemon["showdown_id"] == showdown_id
    )


def _move(pokemon, showdown_id):
    return next(
        move
        for move in pokemon["moves"]
        if move["showdown_id"] == showdown_id
    )


@pytest.mark.asyncio
async def test_lookup_usa_gen_id_y_showdown_id():
    async with _repository() as repository:
        gen6 = await repository.load_battle_context(
            gen_number=6,
            own_species=("bulbasaur",),
            opponent_species=(),
        )
        gen9 = await repository.load_battle_context(
            gen_number=9,
            own_species=("bulbasaur",),
            opponent_species=(),
        )

    bulbasaur6 = _species(gen6, "own", "bulbasaur")
    bulbasaur9 = _species(gen9, "own", "bulbasaur")
    assert gen6["generation"] == {"gen_number": 6, "label": "XY/ORAS"}
    assert gen9["generation"] == {"gen_number": 9, "label": "SV"}
    assert _move(bulbasaur6, "tackle")["power"] == 50
    assert _move(bulbasaur9, "tackle")["power"] == 40
    json.dumps(gen6)
    json.dumps(gen9)


@pytest.mark.asyncio
async def test_frontera_real_gholdengo_existe_en_gen9_pero_no_en_gen6():
    async with _repository() as repository:
        gen6 = await repository.load_battle_context(
            gen_number=6,
            own_species=("gholdengo",),
            opponent_species=(),
        )
        gen9 = await repository.load_battle_context(
            gen_number=9,
            own_species=("gholdengo",),
            opponent_species=(),
        )

    assert gen6["own"] == []
    assert [
        pokemon["showdown_id"] for pokemon in gen9["own"]
    ] == ["gholdengo"]
    assert _move(
        _species(gen9, "own", "gholdengo"), "makeitrain"
    )["power"] == 120


@pytest.mark.asyncio
async def test_accuracy_null_se_expone_como_nunca_falla():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=6,
            own_species=("pikachu",),
            opponent_species=(),
        )

    swift = _move(_species(context, "own", "pikachu"), "swift")
    assert swift["accuracy"] is None
    assert swift["never_misses"] is True


@pytest.mark.asyncio
async def test_power_kind_distingue_variable_fijo_y_no_ofensivo():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=6,
            own_species=("ferrothorn", "machamp", "magikarp"),
            opponent_species=(),
        )

    gyro_ball = _move(_species(context, "own", "ferrothorn"), "gyroball")
    seismic_toss = _move(_species(context, "own", "machamp"), "seismictoss")
    splash = _move(_species(context, "own", "magikarp"), "splash")
    assert (gyro_ball["power"], gyro_ball["power_kind"]) == (0, "variable")
    assert (seismic_toss["power"], seismic_toss["power_kind"]) == (
        0,
        "fixed_damage",
    )
    assert (splash["power"], splash["power_kind"]) == (0, "status")


@pytest.mark.asyncio
async def test_learn_methods_conserva_source_species_y_campos():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=6,
            own_species=("charizardmegax",),
            opponent_species=(),
        )

    methods = _move(
        _species(context, "own", "charizardmegax"), "flamethrower"
    )["learn_methods"]
    assert {
        "gen": 6,
        "method": "machine",
        "sourceSpecies": "charizard",
    } in methods
    assert {
        "gen": 6,
        "level": 43,
        "method": "level",
        "sourceSpecies": "charmeleon",
    } in methods
    assert {
        "gen": 6,
        "level": 37,
        "method": "level",
        "sourceSpecies": "charmander",
    } in methods
