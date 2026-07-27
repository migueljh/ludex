from enum import Enum
from types import SimpleNamespace

from ludex_agent.state.serializer import serialize_battle


class FakeType(Enum):
    BUG = 1
    FLYING = 2


class FakeStatus(Enum):
    BRN = 1


def _move(mid, pp=15, maxpp=16):
    return SimpleNamespace(id=mid, current_pp=pp, max_pp=maxpp)


def _mon(species, *, hp_frac=1.0, active=False, fainted=False, item="leftovers",
         ability="swarm", level=99, status=None, moves=None, secreto="NO DEBE APARECER"):
    return SimpleNamespace(
        species=species, current_hp_fraction=hp_frac, active=active, fainted=fainted,
        item=item, ability=ability, level=level, status=status,
        boosts={"atk": 1, "def": 0}, stats={"hp": 279, "atk": 143},
        types=[FakeType.BUG, FakeType.FLYING],
        moves=moves if moves is not None else {"bugbuzz": _move("bugbuzz")},
        atributo_privado=secreto,
    )


def _battle(**kw):
    base = dict(
        turn=3, player_role="p1", format="gen6randombattle", gen=6,
        team={"p1: Beautifly": _mon("beautifly", active=True)},
        opponent_team={"p2: Magnezone": _mon("magnezone", active=True)},
        weather={}, fields={}, side_conditions={}, opponent_side_conditions={},
        available_moves=[_move("bugbuzz")], available_switches=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_incluye_version_turno_y_metadatos():
    s = serialize_battle(_battle())
    assert s["schema_version"] == 1
    assert s["turn"] == 3
    assert s["player_role"] == "p1"
    assert s["format"] == "gen6randombattle"
    assert s["gen"] == 6


def test_serializa_mi_lado_completo():
    s = serialize_battle(_battle())
    mine = s["me"]["pokemon"][0]
    assert mine["species"] == "beautifly"
    assert mine["hp_fraction"] == 1.0
    assert mine["item"] == "leftovers"
    assert mine["ability"] == "swarm"
    assert mine["stats"] == {"hp": 279, "atk": 143}
    assert mine["moves"] == [{"id": "bugbuzz", "pp": 15, "max_pp": 16}]


def test_el_lado_rival_no_expone_stats():
    # Los stats del rival no son observables: poke-env los estima o los deja
    # en None. Incluirlos seria inventar informacion que un jugador no tiene.
    s = serialize_battle(_battle())
    opp = s["opponent"]["pokemon"][0]
    assert opp["species"] == "magnezone"
    assert "stats" not in opp


def test_es_lista_blanca_no_copia():
    # El fake trae un atributo que el serializador nunca nombra. Si aparece,
    # alguien esta recorriendo atributos en vez de nombrarlos uno por uno.
    s = serialize_battle(_battle())
    assert "NO DEBE APARECER" not in str(s)
    assert "atributo_privado" not in str(s)


def test_el_rival_vacio_en_el_turno_cero():
    s = serialize_battle(_battle(turn=0, opponent_team={}))
    assert s["opponent"]["pokemon"] == []


def test_normaliza_enums_a_nombre():
    s = serialize_battle(_battle())
    assert s["me"]["pokemon"][0]["types"] == ["BUG", "FLYING"]


def test_status_none_y_status_presente():
    assert serialize_battle(_battle())["me"]["pokemon"][0]["status"] is None
    b = _battle(team={"p1: X": _mon("beautifly", status=FakeStatus.BRN, active=True)})
    assert serialize_battle(b)["me"]["pokemon"][0]["status"] == "BRN"


def test_incluye_las_acciones_legales():
    s = serialize_battle(_battle())
    assert s["legal_actions"] == [{"kind": "move", "id": "bugbuzz"}]


def test_es_serializable_a_json():
    import json
    json.dumps(serialize_battle(_battle()))


def test_campo_de_efectos_de_campo_se_llama_field_effects():
    # battle.fields no son solo terrenos: incluye Trick Room, Gravity y Wonder
    # Room. La clave se llama field_effects para no mentirle a quien consuma
    # el dataset en la fase de entrenamiento. Se compara el set completo de
    # claves para que un rename de vuelta al nombre viejo no pase inadvertido.
    s = serialize_battle(_battle())
    assert set(s["field"].keys()) == {
        "weather", "field_effects", "my_side", "opponent_side",
    }
