"""F2-09 (MON-14): contrato del adapter de ejecucion grafo -> poke-env.

La funcion es pura (sin poke-env, sin showdown): traduce el `action` del
grafo al `BattleOrder` del mapa capturado y devuelve None fuera de la
mascara. El cableado real dentro de `choose_move` queda pendiente de la
liberacion de MON-18 (territorio del recorder); la equivalencia del contrato
se prueba aca.
"""

import pytest

from ludex_agent.graph.execute import execute_action


def test_execute_action_traduce_a_order():
    order = object()
    orders = {
        tuple(sorted({"kind": "move", "id": "tackle"}.items())): order,
    }

    assert execute_action({"kind": "move", "id": "tackle"}, orders) is order


def test_execute_action_ignora_flags_false_equivalentes():
    """Los flags de mecanicas especiales false/ausente son equivalentes (D25):
    el action del grafo ya viene normalizado; el adapter compara contra el
    mapa capturado con la misma normalizacion."""
    order = object()
    orders = {
        tuple(sorted({"kind": "move", "id": "tackle"}.items())): order,
    }

    assert execute_action({"kind": "move", "id": "tackle"}, orders) is order
    assert execute_action({"kind": "move", "id": "tackle", "mega": True}, orders) is None


def test_execute_action_devuelve_none_fuera_de_mascara():
    orders = {
        tuple(sorted({"kind": "move", "id": "tackle"}.items())): object(),
    }

    assert execute_action({"kind": "move", "id": "surf"}, orders) is None
    assert execute_action({"kind": "switch", "species": "pikachu"}, orders) is None
    assert execute_action({}, orders) is None


def test_execute_action_con_mapa_vacio_no_ejecuta_nada():
    # Canario: un mapa vacio (nunca deberia pasar en el caller) devuelve None
    # para cualquier accion; nunca un order por accidente.
    assert execute_action({"kind": "move", "id": "tackle"}, {}) is None
