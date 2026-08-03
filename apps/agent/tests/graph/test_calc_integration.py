import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from ludex_agent.graph.calc import (
    CalcClient,
    CalcProtocolError,
    CalcSemanticError,
    calc_damage,
)


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


def _mon(species, *, level=80, active=True, hp_fraction=1.0, ability=None,
         item=None, status=None, moves=()):
    return {
        "species": species, "level": level, "active": active,
        "hp_fraction": hp_fraction, "moves": [{"id": m} for m in moves],
        "boosts": {}, "item": item, "ability": ability, "status": status,
    }


def _battle(*, me, rival, legal, field=None, gen=6, format="gen6randombattle",
            extra_me=()):
    return {
        "gen": gen,
        "format": format,
        "field": field if field is not None else {
            "weather": {}, "field_effects": {}, "my_side": {}, "opponent_side": {},
        },
        "me": {"pokemon": [me, *extra_me]},
        "opponent": {"pokemon": [rival]},
        "legal_actions": legal,
    }


async def _run_damage(battle, context=None):
    async with CalcClient(BASE_URL, timeout_seconds=5) as calc:
        state = {"battle_state": battle}
        if context is not None:
            state["context"] = context
        return await calc_damage(state, calc)


# --- Oráculos pineados: valores medidos contra @smogon/calc@0.11.0 (server
# real en 8200). Cada uno atraviesa calc_damage (nunca _request a mano),
# tiene control con/sin efecto Y el valor exacto pinado.


@pytest.mark.asyncio
async def test_oracle_rain_pineado_a_traves_de_calc_damage(calc_available):
    rain = _battle(
        me=_mon("blastoise"),
        rival=_mon("charizard"),
        legal=[{"kind": "move", "id": "surf"}],
        field={"weather": {"RAINDANCE": 5}, "field_effects": {},
               "my_side": {}, "opponent_side": {}},
    )
    update = await _run_damage(rain)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (236, 282)

    rain["field"]["weather"] = {}
    control_update = await _run_damage(rain)
    control = control_update["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (158, 188)
    assert result["min_damage"] > control["max_damage"]


@pytest.mark.asyncio
async def test_oracle_reflect_defensor_pineado(calc_available):
    battle = _battle(
        me=_mon("machamp", ability="No Guard"),
        rival=_mon("blastoise"),
        legal=[{"kind": "move", "id": "closecombat"}],
        field={"weather": {}, "field_effects": {}, "my_side": {},
               "opponent_side": {"REFLECT": 3}},
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (66, 78)

    battle["field"]["opponent_side"] = {}
    control = (await _run_damage(battle))["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (132, 156)
    assert result["max_damage"] < control["min_damage"]


@pytest.mark.asyncio
async def test_oracle_light_screen_defensor_pineado(calc_available):
    battle = _battle(
        me=_mon("pikachu", ability="Static"),
        rival=_mon("blastoise"),
        legal=[{"kind": "move", "id": "thunderbolt"}],
        field={"weather": {}, "field_effects": {}, "my_side": {},
               "opponent_side": {"LIGHT_SCREEN": 3}},
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (43, 52)

    battle["field"]["opponent_side"] = {}
    control = (await _run_damage(battle))["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (86, 104)
    assert result["max_damage"] < control["min_damage"]


@pytest.mark.asyncio
async def test_oracle_burn_sin_guts_pineado(calc_available):
    battle = _battle(
        me=_mon("machamp", ability="Steadfast", status="BRN"),
        rival=_mon("blastoise"),
        legal=[{"kind": "move", "id": "closecombat"}],
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (66, 78)

    battle["me"]["pokemon"][0]["status"] = None
    control = (await _run_damage(battle))["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (132, 156)
    assert result["max_damage"] < control["min_damage"]


@pytest.mark.asyncio
async def test_oracle_electric_terrain_pineado(calc_available):
    battle = _battle(
        me=_mon("pikachu", ability="Static"),
        rival=_mon("blastoise"),
        legal=[{"kind": "move", "id": "thunderbolt"}],
        field={"weather": {}, "field_effects": {"ELECTRIC_TERRAIN": 3},
               "my_side": {}, "opponent_side": {}},
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (132, 156)

    battle["field"]["field_effects"] = {}
    control = (await _run_damage(battle))["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (86, 104)
    assert result["min_damage"] > control["max_damage"]


@pytest.mark.asyncio
async def test_oracle_wonder_room_pineado(calc_available):
    battle = _battle(
        me=_mon("machamp", ability="No Guard"),
        rival=_mon("blastoise"),
        legal=[{"kind": "move", "id": "closecombat"}],
        field={"weather": {}, "field_effects": {"WONDER_ROOM": 3},
               "my_side": {}, "opponent_side": {}},
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (127, 150)

    battle["field"]["field_effects"] = {}
    control = (await _run_damage(battle))["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (132, 156)
    assert result["min_damage"] < control["min_damage"]


@pytest.mark.asyncio
async def test_oracle_magic_room_pineado(calc_available):
    battle = _battle(
        me=_mon("machamp", ability="No Guard", item="choiceband"),
        rival=_mon("blastoise"),
        legal=[{"kind": "move", "id": "closecombat"}],
        field={"weather": {}, "field_effects": {"MAGIC_ROOM": 3},
               "my_side": {}, "opponent_side": {}},
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    assert (result["min_damage"], result["max_damage"]) == (132, 156)

    battle["field"]["field_effects"] = {}
    control = (await _run_damage(battle))["damage"][0]["result"]
    assert (control["min_damage"], control["max_damage"]) == (196, 232)
    assert result["max_damage"] < control["min_damage"]


@pytest.mark.asyncio
async def test_oracle_hp_fraction_rival_se_materializa_en_curHP(calc_available):
    battle = _battle(
        me=_mon("pikachu", ability="Static"),
        rival=_mon("blastoise", hp_fraction=0.5),
        legal=[{"kind": "move", "id": "thunderbolt"}],
    )
    update = await _run_damage(battle)
    result = update["damage"][0]["result"]
    # maxHP Blastoise lvl 80 = 241 (medido); 0.5 -> round(120.5) = 121.
    assert result["defender_hp"] == {"cur": 121, "max": 241}
    assert result["effective"]["defender"]["curHP"] == 121


@pytest.mark.asyncio
async def test_oracle_hazards_ko_solo_en_switch_in(calc_available):
    """Stealth Rock aplica solo al candidato que entra: el ko_chance cambia
    de 2HKO a OHKO after Stealth Rock. El rival activo de un outgoing no
    recibe hazards."""
    switch_in = _battle(
        me=_mon("pikachu"),
        rival=_mon("blastoise", moves=["surf"]),
        legal=[{"kind": "switch", "species": "charizard"}],
        field={"weather": {}, "field_effects": {}, "my_side": {"STEALTH_ROCK": 3},
               "opponent_side": {}},
        extra_me=(_mon("charizard", active=False, hp_fraction=0.8),),
    )
    update = await _run_damage(switch_in)
    incoming = [e for e in update["damage"] if e["direction"] == "incoming"]
    assert incoming and incoming[0]["revealed"]
    assert incoming[0]["result"]["ko_chance"]["text"] == (
        "guaranteed OHKO after Stealth Rock"
    )

    # El mismo movimiento outgoing contra el rival activo NO lleva hazards:
    # el ko_chance no incorpora el chip de Stealth Rock.
    outgoing = _battle(
        me=_mon("blastoise", moves=["surf"]),
        rival=_mon("charizard", hp_fraction=0.8),
        legal=[{"kind": "move", "id": "surf"}],
        field={"weather": {}, "field_effects": {}, "my_side": {},
               "opponent_side": {"STEALTH_ROCK": 3}},
    )
    out_update = await _run_damage(outgoing)
    result = out_update["damage"][0]["result"]
    assert result["ko_chance"]["text"] == "guaranteed 2HKO"


# --- L-03: el 400 real, y solo él, es semántico; el resto propaga.
# El camino completo pasa por CalcClient + servidor real (no fixtures planas).


class _StubCalcHandler(BaseHTTPRequestHandler):
    responses: list = []
    delay: float = 0.0
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.delay:
            import time
            time.sleep(self.delay)
        self.rfile.read(int(self.headers.get("content-length", 0)))
        status, body, content_type = self.responses.pop(0)
        if body is None:
            length = 0
        else:
            length = len(body)
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(length))
        self.end_headers()
        if body is not None:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def _stub_calc(responses, *, delay=0.0):
    handler_cls = type("StubCalc", (_StubCalcHandler,), {
        "responses": list(responses),
        "delay": delay,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _error_battle(move_id="surf"):
    return _battle(
        me=_mon("blastoise"),
        rival=_mon("charizard"),
        legal=[{"kind": "move", "id": move_id}],
    )


async def _calc_damage_via(base_url, battle=None, *, timeout=3):
    async with CalcClient(base_url, timeout_seconds=timeout) as calc:
        return await calc_damage({"battle_state": battle or _error_battle()}, calc)


@pytest.mark.asyncio
async def test_400_real_es_semantico_por_accion(calc_available):
    """El schema real es `{"error":{"code":string,"message":string}}`."""
    battle = _error_battle(move_id="totallynotamove")
    update = await _calc_damage_via(BASE_URL, battle)
    error = update["damage"][0]["error"]
    assert error["kind"] == "semantic_error"
    assert error["status"] == 400
    assert error["code"] == "unknown_move"
    assert isinstance(error["message"], str) and error["message"]


@pytest.mark.asyncio
async def test_400_json_invalido_propaga_como_protocolo():
    with _stub_calc([(400, b"not json at all", "text/plain")]) as url:
        with pytest.raises(CalcProtocolError, match="sin JSON"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_400_shape_invalido_propaga_como_protocolo():
    body = json.dumps({"error": "flat_shape"}).encode()
    with _stub_calc([(400, body, "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="shape inválido"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_500_propaga_ruidosamente():
    body = json.dumps({"error": {"code": "internal", "message": "boom"}}).encode()
    with _stub_calc([(500, body, "application/json")]) as url:
        with pytest.raises(httpx.HTTPStatusError):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_200_json_invalido_propaga_como_protocolo():
    with _stub_calc([(200, b"<html>no json</html>", "text/html")]) as url:
        with pytest.raises(CalcProtocolError, match="JSON inválido"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_timeout_propaga_ruidosamente():
    body = json.dumps({"ok": True}).encode()
    with _stub_calc([(200, body, "application/json")], delay=0.8) as url:
        with pytest.raises(httpx.TimeoutException):
            await _calc_damage_via(url, timeout=0.2)


@pytest.mark.asyncio
async def test_connect_error_propaga_ruidosamente():
    with _stub_calc([(200, b"{}", "application/json")]) as url:
        dead_url = url
    # El server ya cerro: conectar al puerto libre devuelve connection refused.
    async with CalcClient(dead_url, timeout_seconds=1) as calc:
        with pytest.raises(httpx.ConnectError):
            await calc_damage({"battle_state": _error_battle()}, calc)


# --- L-03.1: el HTTP 200 exitoso también se valida contra el shape completo ---

_EFFECTIVE_SIDE = {
    "species": "Charizard", "level": 80, "nature": "Serious",
    "ability": "Blaze", "item": None,
    "evs": {"hp": 0}, "ivs": {"hp": 31}, "boosts": {"hp": 0},
    "status": "", "curHP": 239, "gender": "M",
}


def _valid_200():
    return {
        "damage_rolls": [[86, 90, 104]],
        "min_damage": 86,
        "max_damage": 104,
        "min_percent": 35.6,
        "max_percent": 43.1,
        "ko_chance": {"chance": 1, "n": 3, "text": "guaranteed 3HKO"},
        "description": "Pikachu Thunderbolt vs. Blastoise",
        "defender_hp": {"cur": 241, "max": 241},
        "effective": {
            "attacker": _EFFECTIVE_SIDE,
            "defender": dict(_EFFECTIVE_SIDE, species="Blastoise"),
        },
    }


def _mutate_200(**changes):
    body = _valid_200()
    body.update(changes)
    return body


@pytest.mark.asyncio
async def test_200_shape_valido_se_acepta(calc_available):
    async with CalcClient(BASE_URL, timeout_seconds=3) as calc:
        result = await calc.calculate({
            "gen": 6,
            "attacker": {"species": "Pikachu", "level": 80},
            "defender": {"species": "Blastoise", "level": 80},
            "move": {"name": "Thunderbolt"},
        })
    assert result["min_damage"] > 0
    assert result["effective"]["attacker"]["species"] == "Pikachu"


@pytest.mark.asyncio
async def test_200_effective_ausente_propaga_como_protocolo():
    body = _valid_200()
    del body["effective"]
    with _stub_calc([(200, json.dumps(body).encode(), "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="shape incompleto"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_200_effective_attacker_malformado_propaga():
    body = _valid_200()
    attacker = dict(body["effective"]["attacker"])
    del attacker["species"]
    body["effective"]["attacker"] = attacker
    with _stub_calc([(200, json.dumps(body).encode(), "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="effective.attacker"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_200_effective_ivs_tipo_invalido_propaga():
    body = _valid_200()
    defender = dict(body["effective"]["defender"])
    defender["ivs"] = {"hp": "31"}
    body["effective"]["defender"] = defender
    with _stub_calc([(200, json.dumps(body).encode(), "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="effective.defender.ivs"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_200_campo_productivo_faltante_propaga():
    body = _valid_200()
    del body["min_damage"]
    with _stub_calc([(200, json.dumps(body).encode(), "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="shape incompleto"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_200_damage_rolls_tipo_invalido_propaga():
    body = _valid_200()
    body["damage_rolls"] = [[86, "no"], [104]]
    with _stub_calc([(200, json.dumps(body).encode(), "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="damage_rolls"):
            await _calc_damage_via(url)


@pytest.mark.asyncio
async def test_200_ko_chance_malformado_propaga():
    body = _valid_200()
    body["ko_chance"] = {"chance": "no", "n": 1, "text": 5}
    with _stub_calc([(200, json.dumps(body).encode(), "application/json")]) as url:
        with pytest.raises(CalcProtocolError, match="ko_chance"):
            await _calc_damage_via(url)


# --- Contexto real: canario Blastoise Gen 6 (102 posibles, 29 status) ---


@pytest.mark.asyncio
async def test_canario_blastoise_gen6_102_posibles_29_status(calc_available):
    """Canario real sobre el learnset de la base: Blastoise Gen 6 trae 102
    movimientos, 29 de status (medido). El adaptador solo calcula los
    no-status y reduce despues del calculo."""
    from ludex_agent.config import load_settings
    from ludex_agent.db.context_repository import PostgresContextRepository

    repo = PostgresContextRepository(load_settings().database_url)
    try:
        context = await repo.load_battle_context(
            gen_number=6, own_species=(), opponent_species=("blastoise",)
        )
    finally:
        await repo.aclose()

    blastoise = context["opponent"][0]
    moves = blastoise["moves"]
    status = [m for m in moves if str(m["category"]).lower() == "status"]
    non_status = [m for m in moves if str(m["category"]).lower() != "status"]
    assert len(moves) == 102, f"Blastoise Gen 6 tiene {len(moves)} moves"
    assert len(status) == 29, f"Blastoise Gen 6 tiene {len(status)} status"

    battle = _battle(
        me=_mon("pikachu"),
        rival=_mon("blastoise"),
        legal=[{"kind": "switch", "species": "charizard"}],
        extra_me=(_mon("charizard", active=False),),
    )
    battle["opponent"]["pokemon"][0]["moves"] = []

    async with CalcClient(BASE_URL, timeout_seconds=5) as calc:
        update = await calc_damage(
            {"battle_state": battle, "context": context}, calc
        )
    metrics = update["damage_metrics"]
    # 102 - 29 status = 73 requests, ninguno de status.
    assert metrics["calls"] == 73
    incoming = [e for e in update["damage"] if e["direction"] == "incoming"]
    assert len(incoming) == 3  # reduccion post-calculo: top-3 posibles
    assert all(e.get("possible") for e in incoming)
    # El descriptor completo (categoria incluida) viaja con cada posible.
    assert all(e["descriptor"]["category"] in {"Physical", "Special"}
               for e in incoming)
    assert all(e["result"]["max_damage"] >= incoming[-1]["result"]["max_damage"]
               for e in incoming)
    assert metrics["latency_ms"] is not None
    # La latencia maxima cabe ampliamente en el presupuesto de decision.
    assert metrics["latency_ms"]["max"] < 5000  # ms


# --- Mega por el camino completo: retrieve_context -> calc_damage ---


async def _mega_update(*, species, item, move_id, gen=6):
    from ludex_agent.graph.context import retrieve_context
    from ludex_agent.db.context_repository import PostgresContextRepository
    from ludex_agent.config import load_settings

    repo = PostgresContextRepository(load_settings().database_url)
    try:
        battle = _battle(
            me=_mon(species, item=item),
            rival=_mon("blastoise"),
            legal=[{"kind": "move", "id": move_id, "mega": True}],
            gen=gen,
        )
        state = {"battle_state": battle}
        enriched = await retrieve_context(state, repo)
        state["context"] = enriched["context"]
        async with CalcClient(BASE_URL, timeout_seconds=5) as calc:
            return await calc_damage(state, calc)
    finally:
        await repo.aclose()


@pytest.mark.asyncio
async def test_mega_charizardite_x_camino_completo(calc_available):
    update = await _mega_update(
        species="charizard", item="charizarditex", move_id="flareblitz"
    )
    attacker = update["damage"][0]["result"]["effective"]["attacker"]
    assert attacker["species"] == "Charizard-Mega-X"
    assert attacker["ability"] == "Tough Claws"
    assert (update["damage"][0]["result"]["min_damage"],
            update["damage"][0]["result"]["max_damage"]) == (85, 101)


@pytest.mark.asyncio
async def test_mega_charizardite_y_camino_completo(calc_available):
    update = await _mega_update(
        species="charizard", item="charizarditey", move_id="flamethrower"
    )
    attacker = update["damage"][0]["result"]["effective"]["attacker"]
    assert attacker["species"] == "Charizard-Mega-Y"
    assert attacker["ability"] == "Drought"


@pytest.mark.asyncio
async def test_mega_venusaurite_camino_completo(calc_available):
    update = await _mega_update(
        species="venusaur", item="venusaurite", move_id="sludgebomb"
    )
    attacker = update["damage"][0]["result"]["effective"]["attacker"]
    assert attacker["species"] == "Venusaur-Mega"
    assert attacker["ability"] == "Thick Fat"


@pytest.mark.asyncio
async def test_mega_piedra_equivocada_lookup_error(calc_available):
    with pytest.raises(LookupError, match="megaEvolve"):
        await _mega_update(
            species="charizard", item="venusaurite", move_id="flareblitz"
        )


@pytest.mark.asyncio
async def test_mega_item_no_megastone_lookup_error(calc_available):
    with pytest.raises(LookupError, match="leftovers"):
        await _mega_update(
            species="charizard", item="leftovers", move_id="flareblitz"
        )


@pytest.mark.asyncio
async def test_mega_forma_ausente_por_generacion_lookup_error(calc_available):
    """Gen 9 no tiene megas: la piedra no resuelve y falla ruidosamente."""
    with pytest.raises(LookupError):
        await _mega_update(
            species="charizard", item="charizarditex", move_id="flareblitz",
            gen=9,
        )
