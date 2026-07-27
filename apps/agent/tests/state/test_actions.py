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


def test_orden_sin_contenido_es_none():
    assert action_from_order(SimpleNamespace(order=None)) is None


def test_orden_con_contenido_inesperado_es_none():
    # Ni .id ni .species: no revienta, devuelve None.
    assert action_from_order(SimpleNamespace(order=SimpleNamespace(raro=1))) is None


def test_la_desambiguacion_vale_contra_los_objetos_reales():
    """Fija el supuesto del que depende `action_from_order`.

    La funcion distingue Move de Pokemon por hasattr, sin importar poke_env,
    porque state/ es puro. Este test SI importa la libreria — los tests pueden,
    src/ no — para que si una version futura le diera `.id` a Pokemon, falle
    ruidosamente en vez de clasificar todos los cambios como movimientos.
    """
    from poke_env.battle import Move, Pokemon

    mon = Pokemon(gen=6, species="charizard")
    mv = Move("flamethrower", gen=6)
    assert not hasattr(mon, "id") and hasattr(mon, "species")
    assert hasattr(mv, "id") and not hasattr(mv, "species")
