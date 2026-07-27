"""Captura del stream crudo de protocolo, agrupado por turno.

El protocolo es la FUENTE DE VERDAD del estado (ver D17): el estado derivado es
una vista materializada que se puede volver a calcular desde aca. Por eso se
guarda tal como llega, incluido el |request|, que trae el equipo propio.

IMPORTANTE: el stream es POR JUGADOR. El |request| de p1 contiene el equipo de
p1 y el de p2 el de p2. Un recorder por jugador, nunca compartido.
"""

from __future__ import annotations

from collections import defaultdict


class ProtocolRecorder:
    def __init__(self) -> None:
        self._by_turn: dict[int, list[str]] = defaultdict(list)
        self._order: list[str] = []
        self._current_turn = 0

    def record(self, split_messages: list[list[str]]) -> None:
        """Recibe las lineas ya separadas por `|`, tal como las da poke-env."""
        for parts in split_messages:
            line = "|".join(parts)
            # `|turn|N` ABRE el turno N: la linea pertenece al turno nuevo.
            if len(parts) > 2 and parts[1] == "turn":
                try:
                    self._current_turn = int(parts[2])
                except ValueError:
                    pass
            self._by_turn[self._current_turn].append(line)
            self._order.append(line)

    def lines_for_turn(self, turn: int) -> list[str]:
        return list(self._by_turn.get(turn, []))

    def turns(self) -> list[int]:
        return sorted(self._by_turn)

    @property
    def all_lines(self) -> list[str]:
        return list(self._order)
