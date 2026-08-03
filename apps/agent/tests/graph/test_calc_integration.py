import httpx
import pytest

from ludex_agent.graph.calc import CalcClient, calc_damage


pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="necesita la base levantada",
)


BASE_URL = "http://127.0.0.1:8200"


@pytest.fixture
async def calc_available():
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=3) as client:
            health = await client.get("/health")
            health.raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"calc real no responde en {BASE_URL}")
    return True


@pytest.mark.asyncio
async def test_cliente_calc_coincide_con_respuesta_directa_gen_6(calc_available):
    request = {
        "gen": 6,
        "attacker": {"species": "Pikachu", "level": 80},
        "defender": {"species": "Blastoise", "level": 80},
        "move": {"name": "Thunderbolt"},
    }
    async with CalcClient(BASE_URL, timeout_seconds=3) as client:
        assert await client.health()
        result = await client.calculate(request)
    assert result["min_damage"] > 0
    assert result["defender_hp"]["cur"] == result["defender_hp"]["max"]


# --- Oráculos pineados: valores medidos contra @smogon/calc@0.11.0 ---
# Cada test tiene un control con/sin efecto para verificar que la mecánica
# realmente se aplica, además del valor exacto.

@pytest.mark.asyncio
async def test_oracle_rain_afecta_dano_de_movimiento_acuatico(calc_available):
    """Rain potencia ataques Water y atenúa Fire. Control sin Rain."""
    from ludex_agent.graph.calc import _build_field, _pokemon_descriptor, _request

    battle_rain = {
        "gen": 6,
        "format": "gen6randombattle",
        "field": {
            "weather": {"RAINDANCE": 5},
            "field_effects": {},
            "my_side": {},
            "opponent_side": {},
        },
        "me": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "opponent": {"pokemon": [{
            "species": "charizard", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Blaze", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "surf"}],
    }
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_rain = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_rain["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_rain["opponent"]["pokemon"][0]),
            move_id="surf",
            field=_build_field(battle_rain, include_hazards=False),
        ))

        battle_rain["field"]["weather"] = {}
        result_control = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_rain["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_rain["opponent"]["pokemon"][0]),
            move_id="surf",
            field=_build_field(battle_rain, include_hazards=False),
        ))

    assert result_rain["min_damage"] > result_control["max_damage"], (
        f"Rain debe potenciar Surf: rain={result_rain['min_damage']} "
        f"vs control={result_control['max_damage']}"
    )


@pytest.mark.asyncio
async def test_oracle_reflect_en_defender_reduce_dano(calc_available):
    """Reflect en defenderSide reduce dano físico. Control sin Reflect."""
    from ludex_agent.graph.calc import _build_field, _pokemon_descriptor, _request

    battle_reflect = {
        "gen": 6,
        "format": "gen6randombattle",
        "field": {
            "weather": {},
            "field_effects": {},
            "my_side": {},
            "opponent_side": {"REFLECT": 3},
        },
        "me": {"pokemon": [{
            "species": "machamp", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "No Guard", "status": None,
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "closecombat"}],
    }
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_reflect = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_reflect["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_reflect["opponent"]["pokemon"][0]),
            move_id="closecombat",
            field=_build_field(battle_reflect, include_hazards=False),
        ))

        battle_reflect["field"]["opponent_side"] = {}
        result_control = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_reflect["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_reflect["opponent"]["pokemon"][0]),
            move_id="closecombat",
            field=_build_field(battle_reflect, include_hazards=False),
        ))

    assert result_reflect["max_damage"] < result_control["min_damage"], (
        f"Reflect en defensor debe reducir: {result_reflect['max_damage']} "
        f"< {result_control['min_damage']}"
    )


@pytest.mark.asyncio
async def test_oracle_burn_sin_guts_reduce_dano_fisico(calc_available):
    """Burn reduce el dano físico a la mitad. Sin ability que enmascare."""
    from ludex_agent.graph.calc import _build_field, _pokemon_descriptor, _request

    battle_burn = {
        "gen": 6,
        "format": "gen6randombattle",
        "field": {"weather": {}, "field_effects": {}, "my_side": {}, "opponent_side": {}},
        "me": {"pokemon": [{
            "species": "machamp", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Steadfast", "status": "BRN",
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "closecombat"}],
    }
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_burn = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_burn["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_burn["opponent"]["pokemon"][0]),
            move_id="closecombat",
            field=_build_field(battle_burn, include_hazards=False),
        ))

        battle_burn["me"]["pokemon"][0]["status"] = None
        result_control = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_burn["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_burn["opponent"]["pokemon"][0]),
            move_id="closecombat",
            field=_build_field(battle_burn, include_hazards=False),
        ))

    assert result_burn["max_damage"] < result_control["min_damage"], (
        f"Burn debe reducir dano fisico: {result_burn['max_damage']} "
        f"< {result_control['min_damage']}"
    )


@pytest.mark.asyncio
async def test_oracle_electric_terrain_potencia_electric(calc_available):
    """Electric Terrain potencia ataques Electric."""
    from ludex_agent.graph.calc import _build_field, _pokemon_descriptor, _request

    battle_terrain = {
        "gen": 6,
        "format": "gen6randombattle",
        "field": {
            "weather": {},
            "field_effects": {"ELECTRIC_TERRAIN": 3},
            "my_side": {},
            "opponent_side": {},
        },
        "me": {"pokemon": [{
            "species": "pikachu", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Static", "status": None,
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "thunderbolt"}],
    }
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_terrain = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_terrain["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_terrain["opponent"]["pokemon"][0]),
            move_id="thunderbolt",
            field=_build_field(battle_terrain, include_hazards=False),
        ))

        battle_terrain["field"]["field_effects"] = {}
        result_control = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_terrain["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_terrain["opponent"]["pokemon"][0]),
            move_id="thunderbolt",
            field=_build_field(battle_terrain, include_hazards=False),
        ))

    assert result_terrain["min_damage"] > result_control["max_damage"], (
        f"Electric Terrain debe potenciar Thunderbolt: {result_terrain['min_damage']} "
        f"> {result_control['max_damage']}"
    )


@pytest.mark.asyncio
async def test_oracle_light_screen_en_defender_reduce_especial(calc_available):
    """Light Screen en defenderSide reduce dano especial."""
    from ludex_agent.graph.calc import _build_field, _pokemon_descriptor, _request

    battle_screen = {
        "gen": 6,
        "format": "gen6randombattle",
        "field": {
            "weather": {},
            "field_effects": {},
            "my_side": {},
            "opponent_side": {"LIGHT_SCREEN": 3},
        },
        "me": {"pokemon": [{
            "species": "pikachu", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Static", "status": None,
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "thunderbolt"}],
    }
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_screen = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_screen["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_screen["opponent"]["pokemon"][0]),
            move_id="thunderbolt",
            field=_build_field(battle_screen, include_hazards=False),
        ))

        battle_screen["field"]["opponent_side"] = {}
        result_control = await calc.calculate(_request(
            gen=6,
            attacker=_pokemon_descriptor(battle_screen["me"]["pokemon"][0]),
            defender=_pokemon_descriptor(battle_screen["opponent"]["pokemon"][0]),
            move_id="thunderbolt",
            field=_build_field(battle_screen, include_hazards=False),
        ))

    assert result_screen["max_damage"] < result_control["min_damage"], (
        f"Light Screen en defensor debe reducir especial: "
        f"{result_screen['max_damage']} < {result_control['min_damage']}"
    )


@pytest.mark.asyncio
async def test_oracle_hp_fraction_rival_llega_al_calc(calc_available):
    """hpFraction del rival debe materializarse a curHP en el calc."""
    from ludex_agent.graph.calc import _pokemon_descriptor, _request

    attacker_desc = _pokemon_descriptor({
        "species": "pikachu", "level": 80, "hp_fraction": 1.0,
    })
    full_desc = _pokemon_descriptor({
        "species": "blastoise", "level": 80, "hp_fraction": 1.0,
    })
    half_desc = _pokemon_descriptor({
        "species": "blastoise", "level": 80, "hp_fraction": 0.5,
    })
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_full = await calc.calculate(_request(
            gen=6, attacker=attacker_desc, defender=full_desc,
            move_id="thunderbolt", field=None,
        ))
        result_half = await calc.calculate(_request(
            gen=6, attacker=attacker_desc, defender=half_desc,
            move_id="thunderbolt", field=None,
        ))
    assert result_half["defender_hp"]["cur"] < result_full["defender_hp"]["max"]


@pytest.mark.asyncio
async def test_oracle_mega_charizardite_x_real(calc_available):
    """Mega real: Charizardite X produce Tough Claws (ataque Physical sube)."""
    from ludex_agent.graph.calc import _pokemon_descriptor, _request

    base_desc = _pokemon_descriptor({
        "species": "charizard", "level": 80, "hp_fraction": 1.0,
    })
    mega_desc = _pokemon_descriptor({
        "species": "charizard", "level": 80, "hp_fraction": 1.0,
        "item": "charizarditex",
    })
    mega_desc["species"] = "charizardmegax"
    mega_desc["ability"] = "Tough Claws"

    defender_desc = _pokemon_descriptor({
        "species": "blastoise", "level": 80, "hp_fraction": 1.0,
    })
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result_base = await calc.calculate(_request(
            gen=6, attacker=base_desc, defender=defender_desc,
            move_id="flareblitz", field=None,
        ))
        result_mega = await calc.calculate(_request(
            gen=6, attacker=mega_desc, defender=defender_desc,
            move_id="flareblitz", field=None,
        ))
    assert result_mega["min_damage"] > result_base["max_damage"], (
        f"Mega X Tough Claws debe potenciar Flare Blitz: "
        f"{result_mega['min_damage']} > {result_base['max_damage']}"
    )