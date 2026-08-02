import pytest

from ludex_agent.graph.calc import (
    calc_damage,
    rank_move_fallback,
    rank_switch_fallback,
)


def _result(rolls, *, hp=100):
    totals = [sum(values) for values in zip(*rolls)]
    return {
        "damage_rolls": rolls,
        "min_damage": min(totals),
        "max_damage": max(totals),
        "defender_hp": {"cur": hp, "max": hp},
    }


def test_movimiento_prioriza_ko_garantizado_sobre_promedio_bruto():
    legal = [
        {"kind": "move", "id": "thunderbolt"},
        {"kind": "move", "id": "icebeam"},
    ]
    damage = [
        {"action": legal[0], "direction": "outgoing", "remaining_hp": 50,
         "result": _result([[10, 100]])},
        {"action": legal[1], "direction": "outgoing", "remaining_hp": 50,
         "result": _result([[50, 50]])},
    ]

    assert rank_move_fallback(legal, damage) == legal[1]


def test_promedio_capa_overkill_al_hp_restante():
    legal = [
        {"kind": "move", "id": "overkill"},
        {"kind": "move", "id": "steady"},
    ]
    damage = [
        {"action": legal[0], "direction": "outgoing", "remaining_hp": 50,
         "result": _result([[0, 200]])},
        {"action": legal[1], "direction": "outgoing", "remaining_hp": 50,
         "result": _result([[49, 49]])},
    ]

    assert rank_move_fallback(legal, damage) == legal[1]


def test_multigolpe_suma_cada_posicion_y_empate_respeta_mascara():
    legal = [
        {"kind": "move", "id": "doublehit"},
        {"kind": "move", "id": "singlehit"},
    ]
    damage = [
        {"action": legal[0], "direction": "outgoing", "remaining_hp": 100,
         "result": _result([[20, 30], [20, 30]])},
        {"action": legal[1], "direction": "outgoing", "remaining_hp": 100,
         "result": _result([[40, 60]])},
    ]

    assert rank_move_fallback(legal, damage) == legal[0]


def test_cambio_forzado_elige_el_menor_peor_dano_relativo():
    legal = [
        {"kind": "switch", "species": "blissey"},
        {"kind": "switch", "species": "skarmory"},
    ]
    damage = [
        {"action": legal[0], "direction": "incoming", "move_id": "closecombat",
         "defender_max_hp": 300, "result": _result([[120, 180]])},
        {"action": legal[0], "direction": "incoming", "move_id": "thunderbolt",
         "defender_max_hp": 300, "result": _result([[60, 90]])},
        {"action": legal[1], "direction": "incoming", "move_id": "closecombat",
         "defender_max_hp": 100, "result": _result([[20, 30]])},
        {"action": legal[1], "direction": "incoming", "move_id": "thunderbolt",
         "defender_max_hp": 100, "result": _result([[35, 40]])},
    ]

    assert rank_switch_fallback(legal, damage) == legal[1]


def test_cambio_sin_matchup_calculable_usa_primero_de_la_mascara():
    legal = [
        {"kind": "switch", "species": "pikachu"},
        {"kind": "switch", "species": "raichu"},
    ]
    assert rank_switch_fallback(legal, []) == legal[0]


class RecordingCalculator:
    def __init__(self):
        self.requests = []

    async def calculate(self, request):
        self.requests.append(request)
        return _result([[10, 20]], hp=200)


@pytest.mark.asyncio
async def test_calc_damage_pasa_generacion_y_construye_matchups_de_movimiento():
    calculator = RecordingCalculator()
    battle = {
        "gen": 6,
        "field": {},
        "me": {"pokemon": [{
            "species": "pikachu", "level": 80, "active": True,
            "hp_fraction": 1, "moves": [{"id": "thunderbolt"}],
            "boosts": {}, "item": None, "ability": None, "status": None,
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 0.25, "moves": [], "boosts": {},
            "item": None, "ability": None, "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "thunderbolt"}],
    }

    update = await calc_damage({"battle_state": battle}, calculator)

    assert calculator.requests[0]["gen"] == 6
    assert calculator.requests[0]["move"] == {"name": "thunderbolt"}
    assert update["damage"][0]["remaining_hp"] == 50


class FailingCalculator:
    async def calculate(self, request):
        raise RuntimeError("unknown move")


@pytest.mark.asyncio
async def test_calc_damage_conserva_error_diagnostico_no_lo_convierte_en_cero():
    state = {
        "battle_state": {
            "gen": 6, "field": {},
            "me": {"pokemon": [{"species": "x", "active": True, "moves": []}]},
            "opponent": {"pokemon": [{"species": "y", "active": True, "moves": []}]},
            "legal_actions": [{"kind": "move", "id": "bad"}],
        }
    }

    update = await calc_damage(state, FailingCalculator())

    assert update["damage"][0]["error"] == "unknown move"
    assert "result" not in update["damage"][0]


# --- F2-07: contexto observable completo en requests de daño ---


def _battle(**overrides):
    base = {
        "gen": 6,
        "field": {
            "weather": {},
            "field_effects": {},
            "my_side": {},
            "opponent_side": {},
        },
        "me": {"pokemon": [{
            "species": "pikachu", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [{"id": "thunderbolt"}],
            "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0},
            "item": None, "ability": None, "status": None,
            "stats": {"hp": 180, "atk": 100, "def": 80, "spa": 90, "spd": 80, "spe": 150},
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 0.25, "moves": [], "boosts": {},
            "item": None, "ability": None, "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "thunderbolt"}],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_request_incluye_rain_mapeado_desde_RAINDANCE():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {"RAINDANCE": 5}, "field_effects": {}, "my_side": {}, "opponent_side": {}})
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["weather"] == "Rain"


@pytest.mark.asyncio
async def test_request_incluye_reflect_en_attacker_side():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {}, "field_effects": {}, "my_side": {"REFLECT": 3}, "opponent_side": {}})
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["attackerSide"]["isReflect"] is True


@pytest.mark.asyncio
async def test_request_incluye_boosts_y_status_burn():
    calculator = RecordingCalculator()
    mon = _battle()
    mon["me"]["pokemon"][0]["boosts"] = {"atk": 2, "def": 0, "spa": 0, "spd": 0, "spe": 1}
    mon["me"]["pokemon"][0]["status"] = "BRN"
    await calc_damage({"battle_state": mon}, calculator)
    assert calculator.requests[0]["attacker"]["boosts"] == {"atk": 2, "def": 0, "spa": 0, "spd": 0, "spe": 1}
    assert calculator.requests[0]["attacker"]["status"] == "brn"


@pytest.mark.asyncio
async def test_request_incluye_gravity_mapeado_a_isGravity():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {}, "field_effects": {"GRAVITY": 3}, "my_side": {}, "opponent_side": {}})
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["isGravity"] is True


@pytest.mark.asyncio
async def test_request_incluye_stealth_rock_y_spikes_en_defender_side():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {}, "field_effects": {}, "my_side": {}, "opponent_side": {"STEALTH_ROCK": 3, "SPIKES": 2}})
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["defenderSide"]["isSR"] is True
    assert calculator.requests[0]["field"]["defenderSide"]["spikes"] == 2


@pytest.mark.asyncio
async def test_hp_actual_propio_se_convierte_a_curHP():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["me"]["pokemon"][0]["hp_fraction"] = 0.5
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["attacker"]["curHP"] == 90  # 0.5 * 180


@pytest.mark.asyncio
async def test_rival_sin_stats_no_recibe_curHP_inventado():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["opponent"]["pokemon"][0]["hp_fraction"] = 0.25
    await calc_damage({"battle_state": battle}, calculator)
    assert "curHP" not in calculator.requests[0]["defender"]


@pytest.mark.asyncio
async def test_request_mega_true_usa_forma_post_mega_del_context():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["me"]["pokemon"][0]["species"] = "charizard"
    battle["legal_actions"] = [{"kind": "move", "id": "flareblitz", "mega": True}]
    context = {
        "own": [{
            "showdown_id": "charizard",
            "base_species": "charizard",
            "forme": None,
            "types": ["Fire", "Flying"],
            "abilities": {"0": "Blaze"},
            "moves": [],
        }, {
            "showdown_id": "charizardmegax",
            "base_species": "charizard",
            "forme": "Mega-X",
            "types": ["Fire", "Dragon"],
            "abilities": {"0": "Tough Claws"},
            "moves": [],
        }],
        "opponent": [],
    }
    await calc_damage({"battle_state": battle, "context": context}, calculator)
    assert calculator.requests[0]["attacker"]["species"] == "charizardmegax"
    assert calculator.requests[0]["attacker"]["ability"] == "Tough Claws"


@pytest.mark.asyncio
async def test_cambio_forzado_usa_possible_moves_sin_presentarlos_como_revelados():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["opponent"]["pokemon"][0]["moves"] = []  # sin revealed moves
    battle["legal_actions"] = [{"kind": "switch", "species": "charizard"}]
    battle["me"]["pokemon"].append({
        "species": "charizard", "level": 80, "active": False,
        "hp_fraction": 1.0, "moves": [], "boosts": {},
        "item": None, "ability": None, "status": None,
    })
    context = {
        "own": [],
        "opponent": [{
            "showdown_id": "blastoise",
            "moves": [
                {"showdown_id": "surf"},
                {"showdown_id": "icebeam"},
            ],
        }],
    }
    update = await calc_damage({"battle_state": battle, "context": context}, calculator)
    outgoing = [e for e in update["damage"] if e["direction"] == "incoming"]
    assert outgoing, "canario: el test debe iterar possible_moves"
    assert all(e.get("possible") is True for e in outgoing)
    assert {e["move_id"] for e in outgoing} == {"surf", "icebeam"}


@pytest.mark.asyncio
async def test_request_semanticamente_invalido_queda_diagnosticado_por_accion():
    calculator = RecordingCalculator()
    battle = _battle(legal_actions=[
        {"kind": "move", "id": "thunderbolt"},
        {"kind": "move", "id": "absolutelynotreal"},
    ])
    call_count = 0

    class SelectiveCalculator:
        async def calculate(self, request):
            nonlocal call_count
            call_count += 1
            if "absolutelynotreal" in request["move"]["name"]:
                raise RuntimeError("unknown move")
            return _result([[10, 20]], hp=200)

    update = await calc_damage({"battle_state": battle}, SelectiveCalculator())
    assert update["damage"][0].get("result") is not None
    assert update["damage"][1].get("error") is not None
    assert "result" not in update["damage"][1]


class InfrastructureFailureCalculator:
    async def calculate(self, request):
        raise ConnectionError("calc service unavailable")


@pytest.mark.asyncio
async def test_calc_infraestructura_no_disponible_propaga_ruidosamente():
    import httpx
    battle = _battle()

    class UnreachableCalculator:
        async def calculate(self, request):
            raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.ConnectError, match="connection refused"):
        await calc_damage({"battle_state": battle}, UnreachableCalculator())


@pytest.mark.asyncio
async def test_singles_doubles_se_mapea_desde_format():
    calculator = RecordingCalculator()
    battle = _battle(format="gen6doublesou")
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["gameType"] == "Doubles"
