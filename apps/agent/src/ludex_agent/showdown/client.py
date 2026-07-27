"""Jugador que graba mientras juega.

Elige al azar entre las acciones legales: el entregable de esta rebanada no es
que juegue bien, es que grabe bien.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from poke_env import ServerConfiguration
from poke_env.player import RandomPlayer

from ..state.actions import action_from_order
from ..state.serializer import serialize_battle
from .protocol import ProtocolRecorder


logger = logging.getLogger(__name__)


def battle_tag_from(split_messages: list[list[str]]) -> str | None:
    """El battle_tag llega como una linea `>battle-...`.

    Funcion pura y separada a proposito: es la unica logica de este modulo que
    se puede testear sin levantar un WebSocket, y de ella depende que un lote
    de protocolo se guarde o se pierda.
    """
    for parts in split_messages:
        if parts and parts[0].startswith(">"):
            return parts[0][1:].strip()
    return None


def local_server_configuration(ws_url: str) -> ServerConfiguration:
    """El server local corre con --no-security: la URL de auth no se usa."""
    return ServerConfiguration(
        ws_url, "https://play.pokemonshowdown.com/action.php?"
    )


class LudexPlayer(RandomPlayer):
    """RandomPlayer que captura protocolo crudo y estado por turno.

    Un ProtocolRecorder por battle_tag, nunca compartido entre jugadores: el
    |request| de cada uno trae su propio equipo.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorders: dict[str, ProtocolRecorder] = defaultdict(ProtocolRecorder)
        self.steps: dict[str, list[dict]] = defaultdict(list)

    def _handle_battle_message(self, split_messages: list[list[str]]) -> Any:
        tag = battle_tag_from(split_messages)
        if tag is None and len(self.recorders) == 1:
            # Rama de respaldo: hoy inalcanzable, porque poke-env garantiza el
            # tag en la primera linea. Se conserva por si esa garantia cambia.
            tag = next(iter(self.recorders))
        if tag:
            self.recorders[tag].record(split_messages)
        else:
            # Nunca descartar en silencio: el protocolo es la fuente de verdad
            # y perder un lote rompe la re-derivacion del estado.
            logger.warning(
                "lote de protocolo sin battle_tag, %d lineas descartadas",
                len(split_messages),
            )
        return super()._handle_battle_message(split_messages)

    def choose_move(self, battle: Any) -> Any:
        order = super().choose_move(battle)
        self.steps[battle.battle_tag].append(
            {
                "turn": battle.turn,
                "state": serialize_battle(battle),
                "action_taken": action_from_order(order),
            }
        )
        return order
