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


def test_con_mega_disponible_cada_movimiento_tiene_variante_mega():
    """I4: sin esto, mega-evolucionar y no mega-evolucionar con el mismo
    movimiento son indistinguibles como acciones legales."""
    battle = SimpleNamespace(
        available_moves=[_move("meteormash")],
        available_switches=[],
        can_mega_evolve=True,
    )
    assert legal_actions(battle) == [
        {"kind": "move", "id": "meteormash"},
        {"kind": "move", "id": "meteormash", "mega": True},
    ]


def test_sin_mega_disponible_no_agrega_variante():
    battle = SimpleNamespace(
        available_moves=[_move("meteormash")],
        available_switches=[],
        can_mega_evolve=False,
    )
    assert legal_actions(battle) == [{"kind": "move", "id": "meteormash"}]


def test_battle_sin_atributo_can_mega_evolve_no_revienta():
    """El fake de Battle en otros tests puede no tener el atributo: default
    False, no AttributeError."""
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


def test_traduce_una_orden_de_movimiento_con_mega_evolucion():
    """I4: sin el flag, un Meteor Mash con mega y sin mega colapsan en la
    misma etiqueta; con el, son distinguibles."""
    order = SimpleNamespace(order=_move("meteormash"), mega=True)
    assert action_from_order(order) == {
        "kind": "move", "id": "meteormash", "mega": True
    }


def test_traduce_una_orden_de_movimiento_sin_mega_no_agrega_la_clave():
    order = SimpleNamespace(order=_move("meteormash"), mega=False)
    assert action_from_order(order) == {"kind": "move", "id": "meteormash"}


def test_traduce_una_orden_con_terastallize():
    order = SimpleNamespace(order=_move("flamethrower"), terastallize=True)
    assert action_from_order(order) == {
        "kind": "move", "id": "flamethrower", "terastallize": True
    }


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
