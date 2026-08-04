"""Contrato del adapter de ejecucion grafo -> poke-env (F2-09/MON-14).

El grafo conceptual del plan termina en `execute`; la ejecucion real con
poke-env no puede ser un nodo LangGraph: el mapa accion->BattleOrder se
captura SINCRONO antes del primer await en `choose_move` (D31/D22, la
disciplina que garantiza `action_taken in legal_actions` por construccion) y
poke-env exige el `BattleOrder` como retorno de `choose_move`; el grafo corre
despues de awaits. Por eso la correspondencia se cierra con un adapter
explícito FUERA del grafo.

Este modulo es PURA: no importa poke-env ni showdown. `execute_action`
traduce el `action` canonico del grafo al `BattleOrder` del mapa capturado y
devuelve `None` si el action no esta en el mapa (fuera de la mascara
capturada); el caller lo convierte en RuntimeError.

CABLEADO PENDIENTE (actualizacion de territorio, MON-18 reabierta): la
llamada real a `execute_action` dentro de `run_graph` (showdown/client.py)
queda pendiente de la liberacion de Latwan — `client.py` es territorio de
MON-18 hasta nuevo aviso. La equivalencia del contrato se prueba en
`tests/graph/test_execute.py`; la aceptacion end-to-end dentro de
`choose_move` se verifica en la integracion final, sin simularse cerrada.
"""

from __future__ import annotations

from typing import Any


def execute_action(
    action: dict[str, Any], action_orders: dict[tuple[tuple[str, Any], ...], Any]
) -> Any:
    """Traduce el `action` canonico del grafo al `BattleOrder` capturado.

    Devuelve `None` si el action no esta en el mapa: el caller debe
    convertirlo en un error ruidoso (una accion fuera de la mascara capturada
    nunca debe ejecutarse). El mapa se construye una sola vez, sincrono, en
    `choose_move` (D31/D22).
    """
    return action_orders.get(tuple(sorted(action.items())))
