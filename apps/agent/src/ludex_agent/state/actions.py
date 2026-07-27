"""Acciones legales del turno.

Sin la mascara de acciones legales no se puede entrenar una politica, asi que
esto se persiste en cada paso junto al estado.
"""

from __future__ import annotations

from typing import Any


def legal_actions(battle: Any) -> list[dict]:
    """Movimientos y cambios disponibles, en ese orden."""
    actions: list[dict] = [
        {"kind": "move", "id": move.id} for move in battle.available_moves
    ]
    actions += [
        {"kind": "switch", "species": mon.species} for mon in battle.available_switches
    ]
    return actions


def action_from_order(order: Any) -> dict | None:
    """Traduce una BattleOrder de poke-env a la forma que se persiste.

    Una orden envuelve un Move (que tiene `.id`) o un Pokemon (que tiene
    `.species`); se distinguen por que atributo esta presente.
    """
    if order is None:
        return None
    inner = getattr(order, "order", None)
    if inner is None:
        return None
    if hasattr(inner, "id"):
        return {"kind": "move", "id": inner.id}
    if hasattr(inner, "species"):
        return {"kind": "switch", "species": inner.species}
    return None
