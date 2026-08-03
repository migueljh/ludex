import asyncio

import pytest

from ludex_agent.graph.calc import (
    CalcSemanticError,
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
        raise CalcSemanticError(
            "invalid_request", "unknown move"
        )


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

    assert update["damage"][0]["error"]["code"] == "invalid_request"
    assert update["damage"][0]["error"]["status"] == 400
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
async def test_request_omit_stealth_rock_y_spikes_en_outgoing_contra_rival_activo():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {}, "field_effects": {}, "my_side": {}, "opponent_side": {"STEALTH_ROCK": 3, "SPIKES": 2}})
    await calc_damage({"battle_state": battle}, calculator)
    defender_side = calculator.requests[0]["field"].get("defenderSide", {})
    assert "isSR" not in defender_side
    assert "spikes" not in defender_side


@pytest.mark.asyncio
async def test_request_incluye_hazards_en_switch_incoming():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["opponent"]["pokemon"][0]["moves"] = [{"id": "surf"}]
    battle["legal_actions"] = [{"kind": "switch", "species": "charizard"}]
    battle["me"]["pokemon"].append({
        "species": "charizard", "level": 80, "active": False,
        "hp_fraction": 1.0, "moves": [], "boosts": {},
        "item": None, "ability": None, "status": None,
    })
    battle["field"]["my_side"] = {"STEALTH_ROCK": 3, "SPIKES": 2}
    await calc_damage({"battle_state": battle}, calculator)
    # En incoming, el field se invierte: defenderSide = my_side (con hazards)
    defender_side = calculator.requests[0]["field"].get("defenderSide", {})
    assert defender_side["isSR"] is True
    assert defender_side["spikes"] == 2


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
    battle["me"]["pokemon"][0]["item"] = "charizarditex"
    battle["legal_actions"] = [{"kind": "move", "id": "flareblitz", "mega": True}]
    context = {
        "own": [],
        "opponent": [],
        "mega_forms": {
            "charizarditex": {
                "mega_species": "charizardmegax",
                "mega_ability": "Tough Claws",
                "mega_evolves": "Charizard",
            },
        },
    }
    await calc_damage({"battle_state": battle, "context": context}, calculator)
    assert calculator.requests[0]["attacker"]["species"] == "charizardmegax"
    assert calculator.requests[0]["attacker"]["ability"] == "Tough Claws"


@pytest.mark.asyncio
async def test_request_mega_charizarditey_usa_forma_y():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["me"]["pokemon"][0]["species"] = "charizard"
    battle["me"]["pokemon"][0]["item"] = "charizarditey"
    battle["legal_actions"] = [{"kind": "move", "id": "flamethrower", "mega": True}]
    context = {
        "own": [],
        "opponent": [],
        "mega_forms": {
            "charizarditey": {
                "mega_species": "charizardmegay",
                "mega_ability": "Drought",
                "mega_evolves": "Charizard",
            },
        },
    }
    await calc_damage({"battle_state": battle, "context": context}, calculator)
    assert calculator.requests[0]["attacker"]["species"] == "charizardmegay"
    assert calculator.requests[0]["attacker"]["ability"] == "Drought"


@pytest.mark.asyncio
async def test_request_mega_venusaurite_forma_unica():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["me"]["pokemon"][0]["species"] = "venusaur"
    battle["me"]["pokemon"][0]["item"] = "venusaurite"
    battle["legal_actions"] = [{"kind": "move", "id": "sludgebomb", "mega": True}]
    context = {
        "own": [],
        "opponent": [],
        "mega_forms": {
            "venusaurite": {
                "mega_species": "venusaurmega",
                "mega_ability": "Thick Fat",
                "mega_evolves": "Venusaur",
            },
        },
    }
    await calc_damage({"battle_state": battle, "context": context}, calculator)
    assert calculator.requests[0]["attacker"]["species"] == "venusaurmega"
    assert calculator.requests[0]["attacker"]["ability"] == "Thick Fat"


@pytest.mark.asyncio
async def test_request_mega_piedra_equivocada_falla_ruidosamente():
    battle = _battle()
    battle["me"]["pokemon"][0]["species"] = "charizard"
    battle["me"]["pokemon"][0]["item"] = "venusaurite"
    battle["legal_actions"] = [{"kind": "move", "id": "flareblitz", "mega": True}]
    context = {
        "own": [],
        "opponent": [],
        "mega_forms": {
            "venusaurite": {
                "mega_species": "venusaurmega",
                "mega_ability": "Thick Fat",
                "mega_evolves": "Venusaur",
            },
        },
    }
    with pytest.raises(LookupError, match="megaEvolve"):
        await calc_damage(
            {"battle_state": battle, "context": context}, RecordingCalculator()
        )


@pytest.mark.asyncio
async def test_request_mega_item_no_megastone_falla_ruidosamente():
    battle = _battle()
    battle["me"]["pokemon"][0]["species"] = "charizard"
    battle["me"]["pokemon"][0]["item"] = "leftovers"
    battle["legal_actions"] = [{"kind": "move", "id": "flareblitz", "mega": True}]
    context = {"own": [], "opponent": [], "mega_forms": {}}
    with pytest.raises(LookupError, match="leftovers"):
        await calc_damage(
            {"battle_state": battle, "context": context}, RecordingCalculator()
        )


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
    battle = _battle(legal_actions=[
        {"kind": "move", "id": "thunderbolt"},
        {"kind": "move", "id": "absolutelynotreal"},
    ])

    class SelectiveCalculator:
        async def calculate(self, request):
            if "absolutelynotreal" in request["move"]["name"]:
                raise CalcSemanticError(
                    "unknown_move", "movimiento inexistente"
                )
            return _result([[10, 20]], hp=200)

    update = await calc_damage({"battle_state": battle}, SelectiveCalculator())
    assert update["damage"][0].get("result") is not None
    assert update["damage"][1]["error"]["code"] == "unknown_move"
    assert update["damage"][1]["error"]["status"] == 400
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
async def test_calc_http_500_propaga_ruidosamente():
    import httpx
    battle = _battle()

    class ServerErrorCalculator:
        async def calculate(self, request):
            raise httpx.HTTPStatusError(
                "internal error",
                request=httpx.Request("POST", "http://test/calc"),
                response=httpx.Response(500, text="internal"),
            )

    with pytest.raises(httpx.HTTPStatusError):
        await calc_damage({"battle_state": battle}, ServerErrorCalculator())


@pytest.mark.asyncio
async def test_calc_json_invalido_propaga_ruidosamente():
    import httpx
    battle = _battle()

    class JsonErrorCalculator:
        async def calculate(self, request):
            raise ValueError("invalid JSON response")

    with pytest.raises(ValueError, match="invalid JSON"):
        await calc_damage({"battle_state": battle}, JsonErrorCalculator())


@pytest.mark.asyncio
async def test_revealed_y_possible_coinciden_en_union_deduplicada():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["opponent"]["pokemon"][0]["moves"] = [
        {"id": "surf"},
    ]
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
                {"showdown_id": "earthquake"},
            ],
        }],
        "mega_forms": {},
    }
    update = await calc_damage({"battle_state": battle, "context": context}, calculator)
    outgoing = [e for e in update["damage"] if e["direction"] == "incoming"]
    assert outgoing, "canario: el test debe iterar"
    revealed = [e for e in outgoing if e.get("revealed")]
    possible = [e for e in outgoing if e.get("possible")]
    assert len(revealed) == 1
    assert revealed[0]["move_id"] == "surf"
    assert {e["move_id"] for e in possible} == {"icebeam", "earthquake"}
    assert all("possible" not in e for e in revealed)
    assert all("revealed" not in e for e in possible)


@pytest.mark.asyncio
async def test_rival_hp_fraction_se_envia_al_calc():
    calculator = RecordingCalculator()
    battle = _battle()
    battle["opponent"]["pokemon"][0]["hp_fraction"] = 0.25
    await calc_damage({"battle_state": battle}, calculator)
    assert "hpFraction" in calculator.requests[0]["defender"]
    assert calculator.requests[0]["defender"]["hpFraction"] == 0.25


@pytest.mark.asyncio
async def test_singles_doubles_se_mapea_desde_format():
    calculator = RecordingCalculator()
    battle = _battle(format="gen6doublesou")
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["gameType"] == "Doubles"


@pytest.mark.asyncio
async def test_wonder_room_se_mapea_a_isWonderRoom():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {}, "field_effects": {"WONDER_ROOM": 3}, "my_side": {}, "opponent_side": {}})
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["isWonderRoom"] is True


@pytest.mark.asyncio
async def test_magic_room_se_mapea_a_isMagicRoom():
    calculator = RecordingCalculator()
    battle = _battle(field={"weather": {}, "field_effects": {"MAGIC_ROOM": 3}, "my_side": {}, "opponent_side": {}})
    await calc_damage({"battle_state": battle}, calculator)
    assert calculator.requests[0]["field"]["isMagicRoom"] is True


# --- L-01: observado / desconocido / asumido por matchup ---

from ludex_agent.graph.calc import (
    _concurrent_matchups,
    _MAX_CONCURRENCY,
    _depends_on_assumptions,
    _matchup_assumptions,
    _reduce_incoming_batch,
    _union_revealed_possible,
)


def test_matchup_assumptions_clasifica_observado_desconocido_asumido():
    descriptor = {
        "species": "blastoise", "level": 80, "hpFraction": 0.5,
    }
    effective = {
        "level": 80, "nature": "Serious", "ability": "Torrent", "item": None,
        "evs": {"hp": 0}, "ivs": {"hp": 31}, "status": "", "gender": "M",
        "boosts": {}, "curHP": 121,
    }
    classified = _matchup_assumptions(descriptor, effective)
    assert classified["observed"] == {"level": 80, "hpFraction": 0.5}
    assert "curHP" not in classified["assumed"]  # derivado de hpFraction, no asumido
    assert classified["assumed"]["ability"] == "Torrent"
    assert classified["assumed"]["nature"] == "Serious"
    assert classified["assumed"]["item"] is None
    assert "ability" in classified["unknown"]
    assert "nature" in classified["unknown"]


def test_rank_move_fallback_no_claim_ko_bajo_supuestos():
    legal = [
        {"kind": "move", "id": "asumido"},
        {"kind": "move", "id": "real"},
    ]
    assumed_entry = {
        "action": legal[0], "direction": "outgoing", "remaining_hp": 50,
        "result": _result([[100, 100]]),
        "assumptions": {
            "attacker": {"observed": {}, "unknown": ["ability"],
                         "assumed": {"ability": "Blaze"}},
            "defender": {"observed": {}, "unknown": [], "assumed": {}},
        },
    }
    real_entry = {
        "action": legal[1], "direction": "outgoing", "remaining_hp": 50,
        "result": _result([[100, 100]]),
        "assumptions": {
            "attacker": {"observed": {"ability": "No Guard"}, "unknown": [],
                         "assumed": {}},
            "defender": {"observed": {}, "unknown": [], "assumed": {}},
        },
    }
    assert _depends_on_assumptions(assumed_entry) is True
    assert _depends_on_assumptions(real_entry) is False
    # Ambos KO por min_damage, pero el KO del "asumido" no es certeza: gana el real.
    assert rank_move_fallback(legal, [assumed_entry, real_entry]) == legal[1]


# --- L-02: possible_moves con categoría, dedupe, concurrencia, reducción ---


class BlockingProbeCalculator:
    """Fake bloqueante que mide la concurrencia real y el orden de finalización.

    `calculate` duerme de forma que un request mantiene su slot del semáforo
    ocupado, exponiendo `max_in_flight`. Con `inverted=True` el que EMPIEZA
    tarde termina primero, para poder demostrar que el orden de salida sigue
    siendo el de entrada (determinista) aunque el de finalización se invierta.
    """

    def __init__(self, *, total: int, inverted: bool = False) -> None:
        self._total = total
        self._inverted = inverted
        self.in_flight = 0
        self.max_in_flight = 0
        self.start_order: list[str] = []
        self.finish_order: list[str] = []

    async def calculate(self, request):
        move_id = request["move"]["name"]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        index = len(self.start_order)
        self.start_order.append(move_id)
        # Latencia invertida: el último en empezar termina primero.
        delay = (self._total - index) * 0.02 if self._inverted else 0.02
        await asyncio.sleep(delay)
        self.in_flight -= 1
        self.finish_order.append(move_id)
        return _result([[10, 20]], hp=200)


@pytest.mark.asyncio
async def test_semaphore_cota_max_in_flight_a_8_con_fake_bloqueante():
    """T-03: con 12 requests y un fake bloqueante, max_in_flight debe ser 8."""
    calculator = BlockingProbeCalculator(total=12)
    battle = _battle()
    battle["opponent"]["pokemon"][0]["moves"] = []
    battle["legal_actions"] = [{"kind": "switch", "species": "charizard"}]
    battle["me"]["pokemon"].append({
        "species": "charizard", "level": 80, "active": False,
        "hp_fraction": 1.0, "moves": [], "boosts": {},
        "item": None, "ability": None, "status": None,
    })
    possible = [_move(f"move{i}", "Physical") for i in range(12)]
    context = {
        "own": [],
        "opponent": [{"showdown_id": "blastoise", "moves": possible}],
        "mega_forms": {},
    }
    update = await calc_damage(
        {"battle_state": battle, "context": context}, calculator
    )
    assert len(calculator.start_order) == 12
    assert calculator.max_in_flight == 8
    assert update["damage_metrics"]["calls"] == 12


@pytest.mark.asyncio
async def test_semaphore_con_latencias_invertidas_preserva_orden_determinista():
    """T-03: aunque el orden de finalización se invierta (el que empieza tarde
    termina primero), el resultado conserva el orden de entrada."""
    calculator = BlockingProbeCalculator(total=12, inverted=True)
    entries = [{"move_id": f"move{i}"} for i in range(12)]
    requests = [{"move": {"name": f"move{i}"}} for i in range(12)]
    metrics = {"calls": 0, "bytes": 0, "latencies": []}
    computed = await _concurrent_matchups(
        calculator, list(zip(entries, requests)), metrics,
        limit=_MAX_CONCURRENCY,
    )
    assert calculator.max_in_flight == 8
    # Las latencias invertidas sí cambian el orden de finalización: el primer
    # request en terminar no es el primero en empezar (move7, no move0).
    assert calculator.finish_order != calculator.start_order
    assert calculator.finish_order[0] != calculator.start_order[0]
    assert [entry["move_id"] for entry in computed] == [
        f"move{i}" for i in range(12)
    ]
    assert metrics["calls"] == 12


def _move(id, category):
    return {
        "showdown_id": id, "name": id, "type": "Normal",
        "category": category, "power": 60, "power_kind": "base", "pp": 10,
    }


def test_union_revelados_preserva_todos_y_excluye_solo_status():
    revealed = ["surf"]
    possible = [
        _move("surf", "Special"),          # coincide con revealed -> revealed gana
        _move("icebeam", "Special"),
        _move("earthquake", "Physical"),
        _move("toxic", "Status"),          # status -> excluido
        _move("protect", "Status"),        # status -> excluido
        _move("scald", "Special"),
    ]
    union = _union_revealed_possible(revealed, possible)
    assert {d["id"] for d, is_possible, _ in union} == {
        "surf", "icebeam", "earthquake", "scald",
    }
    by_id = {d["id"]: (is_possible, desc) for d, is_possible, desc in union}
    assert by_id["surf"] == (False, None)  # revealed gana
    assert by_id["icebeam"][0] is True
    assert by_id["icebeam"][1]["category"] == "Special"


def test_reduce_incoming_batch_conserva_revelados_y_top_n_posibles():
    revealed = [
        {"revealed": True, "move_id": f"r{i}", "result": {"max_damage": 50}}
        for i in range(2)
    ]
    possible = [
        {"possible": True, "move_id": f"p{i}", "result": {"max_damage": d}}
        for i, d in enumerate([100, 90, 80, 70, 60, 5])
    ]
    reduced = _reduce_incoming_batch(revealed + possible, top_n=3)
    assert {e["move_id"] for e in reduced if e.get("revealed")} == {"r0", "r1"}
    top_possible = [e["move_id"] for e in reduced if e.get("possible")]
    assert top_possible == ["p0", "p1", "p2"]


@pytest.mark.asyncio
async def test_possible_moves_102_con_29_status_se_filtran_y_se_reducen():
    """Canario unitario: Blastoise Gen 6 trae 102 posibles, 29 status."""
    calculator = RecordingCalculator()
    battle = _battle()
    battle["opponent"]["pokemon"][0]["moves"] = []
    battle["legal_actions"] = [{"kind": "switch", "species": "charizard"}]
    battle["me"]["pokemon"].append({
        "species": "charizard", "level": 80, "active": False,
        "hp_fraction": 1.0, "moves": [], "boosts": {},
        "item": None, "ability": None, "status": None,
    })
    possible = []
    for i in range(102):
        category = "Status" if i < 29 else ("Special" if i % 2 else "Physical")
        possible.append(_move(f"move{i}", category))
    context = {
        "own": [],
        "opponent": [{"showdown_id": "blastoise", "moves": possible}],
        "mega_forms": {},
    }
    update = await calc_damage(
        {"battle_state": battle, "context": context}, calculator
    )
    # 102 posibles - 29 status = 73 requests; ninguno de status se calcula.
    assert len(calculator.requests) == 73
    assert all(
        "status" not in request["move"]["name"] for request in calculator.requests
    )
    incoming = [e for e in update["damage"] if e["direction"] == "incoming"]
    assert len(incoming) <= 73
    # Tras la reducción: solo los top-3 posibles por daño (no los 73).
    assert len(incoming) == 3
    metrics = update["damage_metrics"]
    assert metrics["calls"] == 73
    assert metrics["latency_ms"] is not None
    assert set(metrics["latency_ms"]) == {"median", "p90", "p99", "max"}


@pytest.mark.asyncio
async def test_damage_metrics_reporta_calls_bytes_y_percentiles():
    calculator = RecordingCalculator()
    battle = _battle()
    update = await calc_damage({"battle_state": battle}, calculator)
    metrics = update["damage_metrics"]
    assert metrics["calls"] == 1
    assert metrics["bytes"] > 0
    assert metrics["latency_ms"]["max"] >= metrics["latency_ms"]["median"]
