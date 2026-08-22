"""`EventHub`: secuencia monotonica por stream y ring buffer con replay
exacto (spec Fase 3 S7.2).

Cada stream (`lobby`, `battle:{tag}`) tiene su propia secuencia `seq`
monotonica, arrancando en 1 e independiente de la de cualquier otro stream.
`resume(stream_id, last_seq)` devuelve el sufijo exacto de eventos
posteriores a `last_seq`. Si el ring buffer ya roto mas alla de `last_seq`
la garantia de sufijo exacto se pierde y se levanta `ReplayGapError`: el
cliente debe recargar el estado proyectado en vez de recibir un backlog
parcial.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping

_DEFAULT_RING_BUFFER_SIZE = 256


@dataclass(frozen=True)
class StreamEvent:
    seq: int
    payload: Mapping[str, object]


class ReplayGapError(Exception):
    """`last_seq` ya salio del ring buffer: no hay sufijo exacto disponible."""

    def __init__(
        self, stream_id: str, requested_seq: int, earliest_available_seq: int,
    ) -> None:
        super().__init__(
            f"replay gap en stream {stream_id!r}: pidio desde seq="
            f"{requested_seq}, el buffer solo retiene desde "
            f"{earliest_available_seq}"
        )
        self.stream_id = stream_id
        self.requested_seq = requested_seq
        self.earliest_available_seq = earliest_available_seq


class EventHub:
    """Dueno de la secuencia por stream y del ring buffer de replay."""

    def __init__(self, *, ring_buffer_size: int = _DEFAULT_RING_BUFFER_SIZE) -> None:
        if ring_buffer_size <= 0:
            raise ValueError("ring_buffer_size debe ser positivo")
        self._ring_buffer_size = ring_buffer_size
        self._buffers: dict[str, "deque[StreamEvent]"] = {}
        self._next_seq: dict[str, int] = {}

    def publish(self, stream_id: str, payload: Mapping[str, object]) -> StreamEvent:
        seq = self._next_seq.get(stream_id, 0) + 1
        self._next_seq[stream_id] = seq
        event = StreamEvent(seq=seq, payload=payload)
        buffer = self._buffers.setdefault(
            stream_id, deque(maxlen=self._ring_buffer_size),
        )
        buffer.append(event)
        return event

    def resume(self, stream_id: str, last_seq: int) -> list[StreamEvent]:
        buffer = self._buffers.get(stream_id, deque())
        if not buffer:
            if last_seq != 0:
                raise ReplayGapError(stream_id, last_seq, 0)
            return []
        earliest = buffer[0].seq
        if last_seq < earliest - 1:
            raise ReplayGapError(stream_id, last_seq, earliest)
        return [event for event in buffer if event.seq > last_seq]
