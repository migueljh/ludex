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
