from types import SimpleNamespace

from ludex_agent.state.actions import action_from_order, legal_actions


def _move(mid):
    return SimpleNamespace(id=mid)


def _mon(species):
    return SimpleNamespace(species=species)


def test_lista_movimientos_y_cambios():
    battle = SimpleNamespace(
        available_moves=[_move("bugbuzz"), _move("roost")],
        available_switches=[_mon("magnezone")],
    )
    assert legal_actions(battle) == [
        {"kind": "move", "id": "bugbuzz"},
        {"kind": "move", "id": "roost"},
        {"kind": "switch", "species": "magnezone"},
    ]


def test_sin_acciones_devuelve_lista_vacia():
    battle = SimpleNamespace(available_moves=[], available_switches=[])
    assert legal_actions(battle) == []


def test_traduce_una_orden_de_movimiento():
    order = SimpleNamespace(order=_move("bugbuzz"))
    assert action_from_order(order) == {"kind": "move", "id": "bugbuzz"}


def test_traduce_una_orden_de_cambio():
    order = SimpleNamespace(order=_mon("magnezone"))
    assert action_from_order(order) == {"kind": "switch", "species": "magnezone"}


def test_orden_vacia_es_none():
    assert action_from_order(None) is None
