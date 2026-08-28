"""WebSockets de la API de control (spec Fase 3 S7.2, D65/D66 MON-33
Task 4).

`/ws/battle/{battle_tag}` y `/ws/lobby` reenvian lo que ya publico el
`EventHub` inyectado (Task 2): cada stream tiene su propio `seq` monotonico
y un ring buffer, asi que esta capa nunca calcula secuencia propia.

Dos loops concurrentes por conexion (D66 T-01, spec S7.2 "publica"):

- **fan-out live**: un loop SERVER-SIDE sondea `event_hub.resume(stream_id,
  cursor)` cada tick y reenvia al socket cada evento nuevo. Un cliente
  conectado recibe `decision_proposed`/`decision_resolved`/etc. sin mandar
  `resume` -- el polling del cliente NO es el canal live.
- **receive**: atiende `resume(last_seq)` con el sufijo exacto o
  `REPLAY_GAP` si el ring buffer ya roto mas alla del cursor pedido.

Ambos loops comparten el cursor y un `asyncio.Lock` para no duplicar
eventos entre el canal live y el replay. El cursor de fan-out arranca en el
seq mas reciente ya publicado en el momento de `accept()`: un connect
fresco no recibe backlog (el backlog se sirve SOLO via `resume`, que es el
canal de reconexion).

El timeout de una decision pendiente avanza por el reloj inyectado del gate
(D42, Task 2), nunca por accion de este modulo: una conexion WS que nunca se
abre, o que se cae, no impide ni retrasa que `PendingApproval.await_resolution()`
resuelva `timeout_auto`.

Origin/loopback (D65 S6.3): un handshake con un `Origin` de browser
no-loopback se rechaza ANTES de `accept()`, mismo criterio para ambos
streams. Un cliente sin header `Origin` (curl, httpx, tests, o cualquier
cliente no-browser) siempre pasa: el header solo lo manda un browser.

Nota de ownership (D66): `EventHub` es de Task 2 (ya aceptada) y no se
modifica; el cursor inicial se lee de su atributo privado `_next_seq`
(mismo repo, unica via para saber el seq vigente sin tocar Task 2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..hitl.events import EventHub, ReplayGapError

_ALLOWED_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost"})
_FAN_OUT_TICK_SECONDS = 0.05


def is_allowed_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    return urlsplit(origin).hostname in _ALLOWED_ORIGIN_HOSTS


@dataclass
class _StreamState:
    hub: EventHub
    stream_id: str
    last_sent: int
    lock: asyncio.Lock


def _latest_seq(hub: EventHub, stream_id: str) -> int:
    return hub._next_seq.get(stream_id, 0)


def _event_message(event) -> dict:
    return {"seq": event.seq, **dict(event.payload)}


async def _receive_loop(websocket: WebSocket, state: _StreamState) -> None:
    """Atiende `resume`/`REPLAY_GAP` en paralelo con el fan-out live."""
    while True:
        message = await websocket.receive_json()
        if message.get("type") != "resume":
            continue
        last_seq = int(message.get("last_seq", 0))
        try:
            events = state.hub.resume(state.stream_id, last_seq)
        except ReplayGapError:
            try:
                async with state.lock:
                    await websocket.send_json({
                        "type": "REPLAY_GAP", "stream": state.stream_id,
                    })
            except (WebSocketDisconnect, RuntimeError):
                return
            continue
        if not events:
            continue
        async with state.lock:
            if events[-1].seq > state.last_sent:
                state.last_sent = events[-1].seq
            for event in events:
                try:
                    await websocket.send_json(_event_message(event))
                except (WebSocketDisconnect, RuntimeError):
                    return


async def _fan_out_loop(websocket: WebSocket, state: _StreamState) -> None:
    """Sondea server-side el `EventHub` y reenvia al socket cada evento
    nuevo. El unico canal live: el cliente nunca tiene que poll-ea."""
    while True:
        await asyncio.sleep(_FAN_OUT_TICK_SECONDS)
        async with state.lock:
            events = state.hub.resume(state.stream_id, state.last_sent)
            if events:
                state.last_sent = events[-1].seq
            for event in events:
                try:
                    await websocket.send_json(_event_message(event))
                except (WebSocketDisconnect, RuntimeError):
                    return


async def _serve_stream(websocket: WebSocket, stream_id: str) -> None:
    if not is_allowed_origin(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    event_hub = websocket.app.state.event_hub
    await websocket.send_json({"type": "hello", "stream": stream_id})
    state = _StreamState(
        hub=event_hub,
        stream_id=stream_id,
        last_sent=_latest_seq(event_hub, stream_id),
        lock=asyncio.Lock(),
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_receive_loop(websocket, state))
            group.create_task(_fan_out_loop(websocket, state))
    except* WebSocketDisconnect:
        pass


def register_websocket_routes(app: FastAPI) -> None:
    @app.websocket("/ws/battle/{battle_tag}")
    async def battle_ws(websocket: WebSocket, battle_tag: str) -> None:
        await _serve_stream(websocket, f"battle:{battle_tag}")

    @app.websocket("/ws/lobby")
    async def lobby_ws(websocket: WebSocket) -> None:
        await _serve_stream(websocket, "lobby")
