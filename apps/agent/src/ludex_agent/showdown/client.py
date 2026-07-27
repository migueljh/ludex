"""Jugador que graba mientras juega.

Elige al azar entre las acciones legales: el entregable de esta rebanada no es
que juegue bien, es que grabe bien.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from poke_env import ServerConfiguration
from poke_env.player import RandomPlayer

from ..state.actions import action_from_order
from ..state.serializer import serialize_battle
from .protocol import ProtocolRecorder


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
        # El battle_tag llega como primera linea con formato `>battle-...`.
        tag = None
        for parts in split_messages:
            if parts and parts[0].startswith(">"):
                tag = parts[0][1:].strip()
                break
        if tag is None and len(self.recorders) == 1:
            tag = next(iter(self.recorders))
        if tag:
            self.recorders[tag].record(split_messages)
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
