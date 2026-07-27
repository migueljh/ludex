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
        # (turno, linea) en el orden EXACTO de llegada. Ademas de `_by_turn`
        # (para lines_for_turn/turns), esto permite buscar una linea con un
        # cursor global que solo avanza (ver `entries_from`): dos decisiones
        # pueden mencionar la MISMA especie/movimiento (p.ej. Outrage dos
        # turnos seguidos por el bloqueo del movimiento), y sin un cursor que
        # nunca retrocede, la segunda busqueda podria reusar por error la
        # linea que ya le pertenecia a la primera.
        self._entries: list[tuple[int, str]] = []
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
            self._entries.append((self._current_turn, line))

    def lines_for_turn(self, turn: int) -> list[str]:
        return list(self._by_turn.get(turn, []))

    def turns(self) -> list[int]:
        return sorted(self._by_turn)

    @property
    def all_lines(self) -> list[str]:
        return [line for _, line in self._entries]

    @property
    def line_count(self) -> int:
        """Cuantas lineas de protocolo se grabaron hasta ahora, para este tag.

        Barato de leer en un loop de espera (a diferencia de `all_lines`, que
        copia la lista entera). Lo usa `LudexPlayer._materialize_step` (C1)
        para detectar que llego una lote nueva sin depender del numero de
        turno: un cambio forzado tras un debilitamiento trae narracion nueva
        sin mover `_current_turn`.
        """
        return len(self._entries)

    def entries_from(self, index: int) -> list[tuple[int, str]]:
        """Pares (turno, linea) desde una posicion GLOBAL en el orden de
        llegada. Usado por `LudexPlayer` para corregir la etiqueta de turno
        de cada decision con un cursor que solo avanza (ver D20)."""
        return list(self._entries[index:])
