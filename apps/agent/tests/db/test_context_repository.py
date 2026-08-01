import json
import os
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text

from ludex_agent.config import load_settings


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)


@asynccontextmanager
async def _repository():
    from ludex_agent.db.context_repository import PostgresContextRepository

    repo = PostgresContextRepository(load_settings().database_url)
    try:
        yield repo
    finally:
        await repo.aclose()


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
        with pytest.raises(LookupError, match="gholdengo"):
            await repository.load_battle_context(
                gen_number=6,
                own_species=("gholdengo",),
                opponent_species=(),
            )
        gen9 = await repository.load_battle_context(
            gen_number=9,
            own_species=("gholdengo",),
            opponent_species=(),
        )

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


@pytest.mark.asyncio
async def test_load_moves_tiene_frontera_generation_scoped_propia():
    async with _repository() as repository:
        gen6 = await repository.load_moves(
            gen_number=6,
            move_ids=("tackle",),
        )
        gen9 = await repository.load_moves(
            gen_number=9,
            move_ids=("tackle",),
        )

    assert gen6["tackle"]["power"] == 50
    assert gen9["tackle"]["power"] == 40
    assert gen6["tackle"]["showdown_id"] == "tackle"
    assert gen9["tackle"]["showdown_id"] == "tackle"


@pytest.mark.asyncio
async def test_load_moves_vacio_no_ejecuta_un_any_invalido():
    async with _repository() as repository:
        assert await repository.load_moves(
            gen_number=6,
            move_ids=(),
        ) == {}


@pytest.mark.asyncio
async def test_load_moves_falla_si_un_id_observado_no_existe():
    async with _repository() as repository:
        with pytest.raises(LookupError, match="movimientoausente"):
            await repository.load_moves(
                gen_number=6,
                move_ids=("tackle", "movimientoausente"),
            )


@pytest.mark.asyncio
async def test_ursaring_observa_sludge_bomb_sin_inferir_zoroark():
    from ludex_agent.graph.context import retrieve_context

    state = {
        "battle_state": {
            "gen": 6,
            "me": {"pokemon": []},
            "opponent": {
                "pokemon": [{
                    "species": "ursaring",
                    "moves": [{"id": "sludgebomb"}],
                }],
            },
        },
    }
    async with _repository() as repository:
        result = await retrieve_context(state, repository)

    rich = result["context"]
    projected = result["prompt_context"]
    ursaring = _species(rich, "opponent", "ursaring")
    possible = {
        move["showdown_id"]
        for move in ursaring["moves"]
    }

    assert "sludgebomb" not in possible
    assert "sludgebomb" in rich["observed_moves"]
    assert rich["observed_moves"]["sludgebomb"]["power"] == 90
    assert [entry["showdown_id"] for entry in rich["opponent"]] == [
        "ursaring",
    ]
    assert "zoroark" not in json.dumps(result)
    assert projected["opponent"] == [{
        "species": "ursaring",
        "base_types": ["Normal"],
        "base_stats": {
            "hp": 90,
            "atk": 130,
            "def": 75,
            "spa": 75,
            "spd": 75,
            "spe": 55,
        },
        "possible_abilities": ["guts", "quickfeet", "unnerve"],
        "revealed_moves": ["sludgebomb"],
        "possible_moves": [
            move["showdown_id"]
            for move in ursaring["moves"]
        ],
    }]
    assert projected["moves"]["sludgebomb"]["description"]
    assert projected["moves"]["sludgebomb"]["flags"]


def _compact_bytes(value):
    return len(json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode())


def _learn_method_count(context):
    return sum(
        len(move["learn_methods"])
        for side in ("own", "opponent")
        for pokemon in context[side]
        for move in pokemon["moves"]
    )


@pytest.mark.asyncio
async def test_batalla_real_preserva_completitud_bajo_techo_compacto():
    from ludex_agent.db.session import make_engine, session_factory

    engine = make_engine(load_settings().database_url)
    factory = session_factory(engine)
    try:
        async with factory() as session:
            rows = (await session.execute(text("""
                SELECT ts.decision_index, ts.state, ts.legal_actions
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = :battle_tag
                  AND ts.decision_index IN (1, 25)
                ORDER BY ts.decision_index
            """), {
                "battle_tag": "battle-gen6randombattle-397",
            })).mappings().all()

        assert [row["decision_index"] for row in rows] == [1, 25]
        from ludex_agent.db.context_repository import (
            PostgresContextRepository,
        )
        from ludex_agent.graph.context import retrieve_context
        from ludex_agent.graph.state import allowlisted_state

        repository = PostgresContextRepository(load_settings().database_url)
        # Proveniencia de los conteos: seed pokemon-showdown@0.11.10 sobre
        # 20260725000002_game_data.sql y
        # 20260726000001_base_species_id_y_power_kind.sql.
        expected = {
            1: {
                "own_known": 24,
                "opponent_candidates": 91,
                "catalog": 106,
                "learn_methods": 2811,
            },
            25: {
                "own_known": 24,
                "opponent_candidates": 495,
                "catalog": 243,
                "learn_methods": 4800,
            },
        }
        for row in rows:
            raw = dict(row["state"])
            raw["legal_actions"] = list(row["legal_actions"])
            battle_state = allowlisted_state(raw)
            result = await retrieve_context(
                {"battle_state": battle_state},
                repository,
            )
            rich = result["context"]
            projected = result["prompt_context"]
            counts = expected[row["decision_index"]]

            assert sum(
                len(pokemon["known_moves"])
                for pokemon in projected["own"]
            ) == counts["own_known"]
            assert sum(
                len(pokemon["possible_moves"])
                for pokemon in projected["opponent"]
            ) == counts["opponent_candidates"]
            assert len(projected["moves"]) == counts["catalog"]
            assert _learn_method_count(rich) == counts["learn_methods"]
            observed_ids = {
                move_id
                for pokemon in projected["own"]
                for move_id in pokemon["known_moves"]
            } | {
                move_id
                for pokemon in projected["opponent"]
                for move_id in pokemon["revealed_moves"]
            }
            assert observed_ids
            assert all(
                "description" in projected["moves"][move_id]
                and "flags" in projected["moves"][move_id]
                for move_id in observed_ids
            )
            assert _compact_bytes(projected) <= 65_536
            assert _compact_bytes(projected) <= _compact_bytes(rich) * 0.10
    finally:
        await repository.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_vivillontundra_resuelve_a_vivillon_y_permanece_visible():
    async with _repository() as repository:
        base = await repository.load_battle_context(
            gen_number=6,
            own_species=("vivillon",),
            opponent_species=(),
        )
        cosmetica = await repository.load_battle_context(
            gen_number=6,
            own_species=("vivillontundra",),
            opponent_species=(),
        )

    mon_base = _species(base, "own", "vivillon")
    mon_cosmetica = _species(cosmetica, "own", "vivillontundra")

    assert mon_cosmetica["showdown_id"] == "vivillontundra"
    assert mon_cosmetica["types"] == mon_base["types"]
    assert mon_cosmetica["base_stats"] == mon_base["base_stats"]
    assert mon_cosmetica["abilities"] == mon_base["abilities"]
    assert {m["showdown_id"] for m in mon_cosmetica["moves"]} == {
        m["showdown_id"] for m in mon_base["moves"]
    }


@pytest.mark.asyncio
async def test_charizardmegax_usa_fila_directa_no_charizard():
    async with _repository() as repository:
        charizard = await repository.load_battle_context(
            gen_number=6,
            own_species=("charizard",),
            opponent_species=(),
        )
        megax = await repository.load_battle_context(
            gen_number=6,
            own_species=("charizardmegax",),
            opponent_species=(),
        )

    mon_charizard = _species(charizard, "own", "charizard")
    mon_megax = _species(megax, "own", "charizardmegax")

    assert mon_megax["showdown_id"] == "charizardmegax"
    assert mon_megax["base_stats"] == {
        "hp": 78, "atk": 130, "def": 111,
        "spa": 130, "spd": 85, "spe": 100,
    }
    assert mon_charizard["base_stats"]["atk"] == 84
    assert mon_megax["base_stats"]["atk"] != 84


@pytest.mark.asyncio
async def test_floetteeternal_no_es_cosmetica_y_falla_ruidosamente():
    async with _repository() as repository:
        with pytest.raises(LookupError, match="floetteeternal"):
            await repository.load_battle_context(
                gen_number=6,
                own_species=("floetteeternal",),
                opponent_species=(),
            )


@pytest.mark.asyncio
async def test_rival_cosmetico_no_desaparece_de_prompt_context():
    from ludex_agent.graph.context import retrieve_context

    state = {
        "battle_state": {
            "gen": 6,
            "me": {"pokemon": [{"species": "charmander"}]},
            "opponent": {"pokemon": [{"species": "vivillontundra"}]},
        },
    }
    async with _repository() as repository:
        result = await retrieve_context(state, repository)

    rich = result["context"]
    projected = result["prompt_context"]

    assert [p["showdown_id"] for p in rich["opponent"]] == ["vivillontundra"]
    assert [p["species"] for p in projected["opponent"]] == ["vivillontundra"]
    assert projected["opponent"][0]["base_types"] == rich["opponent"][0]["types"]
    assert projected["opponent"][0]["possible_moves"] == [
        m["showdown_id"] for m in rich["opponent"][0]["moves"]
    ]


# --- Cambios Requested: fila directa siempre gana, cosmeticFormes explícito ---


@pytest.mark.asyncio
async def test_arceuspoison_gen6_conserva_tipo_poison_por_fila_directa():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=6, own_species=("arceuspoison",), opponent_species=(),
        )
    mon = _species(context, "own", "arceuspoison")
    assert mon["showdown_id"] == "arceuspoison"
    assert mon["types"] == ["Poison"], (
        "tiene fila directa con tipo Poison; no debe degradar a Arceus Normal"
    )


@pytest.mark.asyncio
async def test_castformsunny_gen6_conserva_tipo_fire_por_fila_directa():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=6, own_species=("castformsunny",), opponent_species=(),
        )
    mon = _species(context, "own", "castformsunny")
    assert mon["showdown_id"] == "castformsunny"
    assert mon["types"] == ["Fire"], (
        "tiene fila directa con tipo Fire; no debe degradar a Castform Normal"
    )


@pytest.mark.asyncio
async def test_pikachucosplay_gen6_conserva_fila_directa_y_ability():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=6, own_species=("pikachucosplay",), opponent_species=(),
        )
    mon = _species(context, "own", "pikachucosplay")
    assert mon["showdown_id"] == "pikachucosplay"
    assert mon["abilities"]["0"] == "Lightning Rod", (
        "pikachucosplay tiene su propia ability, no la de pikachu"
    )


@pytest.mark.asyncio
async def test_ogerponwellspring_gen9_conserva_tipos_y_ability():
    async with _repository() as repository:
        context = await repository.load_battle_context(
            gen_number=9, own_species=("ogerponwellspring",), opponent_species=(),
        )
    mon = _species(context, "own", "ogerponwellspring")
    assert mon["showdown_id"] == "ogerponwellspring"
    assert mon["types"] == ["Grass", "Water"]
    assert mon["abilities"]["0"] == "Water Absorb"


@pytest.mark.asyncio
async def test_pikachupartner_gen6_no_disponible_falla_ruidosamente():
    async with _repository() as repository:
        with pytest.raises(LookupError, match="pikachupartner"):
            await repository.load_battle_context(
                gen_number=6, own_species=("pikachupartner",),
                opponent_species=(),
            )


@pytest.mark.asyncio
async def test_pikachuworld_gen6_no_disponible_falla_ruidosamente():
    async with _repository() as repository:
        with pytest.raises(LookupError, match="pikachuworld"):
            await repository.load_battle_context(
                gen_number=6, own_species=("pikachuworld",),
                opponent_species=(),
            )


@pytest.mark.asyncio
async def test_charizardgmax_gen6_no_disponible_falla_ruidosamente():
    async with _repository() as repository:
        with pytest.raises(LookupError, match="charizardgmax"):
            await repository.load_battle_context(
                gen_number=6, own_species=("charizardgmax",),
                opponent_species=(),
            )
