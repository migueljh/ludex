"""Acciones legales del turno.

Sin la mascara de acciones legales no se puede entrenar una politica, asi que
esto se persiste en cada paso junto al estado.
"""

from __future__ import annotations

from typing import Any


def legal_actions(battle: Any) -> list[dict]:
    """Movimientos y cambios disponibles, en ese orden.

    I4 (review final): si se puede mega-evolucionar este turno, cada
    movimiento tiene una variante "con mega" ademas de la variante pelada:
    son dos decisiones distintas (mega-evolucionar cambia stats/tipo/habilidad
    antes de que el movimiento se resuelva), y sin esto el espacio de acciones
    del dataset queda incompleto para gen 6, donde la mega es del jugador.
    """
    actions: list[dict] = [
        {"kind": "move", "id": move.id} for move in battle.available_moves
    ]
    if getattr(battle, "can_mega_evolve", False):
        actions += [
            {"kind": "move", "id": move.id, "mega": True}
            for move in battle.available_moves
        ]
    actions += [
        {"kind": "switch", "species": mon.species} for mon in battle.available_switches
    ]
    return actions


def action_from_order(order: Any) -> dict | None:
    """Traduce una BattleOrder de poke-env a la forma que se persiste.

    Una orden envuelve un Move (que tiene `.id`) o un Pokemon (que tiene
    `.species`); se distinguen por que atributo esta presente.

    I4 (review final): `SingleBattleOrder` trae ademas `mega`, `z_move`,
    `dynamax` y `terastallize` como flags DEL ORDEN (no del Move/Pokemon
    interno). Sin esto, un Meteor Mash con mega-evolucion y el mismo Meteor
    Mash sin ella colapsan en la MISMA etiqueta ("move", "meteormash"), pese a
    ser decisiones distintas del jugador. Se agregan solo cuando estan en
    `True`, para no ensuciar el 99% de las acciones que no las usan.
    """
    if order is None:
        return None
    inner = getattr(order, "order", None)
    if inner is None:
        return None
    if hasattr(inner, "id"):
        action = {"kind": "move", "id": inner.id}
    elif hasattr(inner, "species"):
        action = {"kind": "switch", "species": inner.species}
    else:
        return None
    for flag in ("mega", "z_move", "dynamax", "terastallize"):
        if getattr(order, flag, False):
            action[flag] = True
    return action
