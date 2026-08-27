"""WebSockets de la API de control (spec Fase 3 S7.2, D65 MON-33 Task 4).

`/ws/battle/{battle_tag}` y `/ws/lobby` reenvian lo que ya publico el
`EventHub` inyectado (Task 2): cada stream tiene su propio `seq` monotonico
y un ring buffer, asi que esta capa nunca calcula secuencia propia ni
retiene eventos por su cuenta. `resume` devuelve el sufijo exacto o, si el
buffer ya roto mas alla del cursor pedido, `REPLAY_GAP` (el cliente debe
recargar el estado proyectado en vez de aceptar un backlog parcial).

El timeout de una decision pendiente avanza por el reloj inyectado del gate
(D42, Task 2), nunca por accion de este modulo: una conexion WS que nunca se
abre, o que se cae, no impide ni retrasa que `PendingApproval.await_resolution()`
resuelva `timeout_auto` (ver `hitl/registry.py` y su test de "disconnect
independent timeout").

Origin/loopback (D65 S6.3): un handshake con un `Origin` de browser
no-loopback se rechaza ANTES de `accept()`, mismo criterio para ambos
streams. Un cliente sin header `Origin` (curl, httpx, tests, o cualquier
cliente no-browser) siempre pasa: el header solo lo manda un browser.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..hitl.events import ReplayGapError

_ALLOWED_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost"})


def is_allowed_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    return urlsplit(origin).hostname in _ALLOWED_ORIGIN_HOSTS


async def _serve_stream(websocket: WebSocket, stream_id: str) -> None:
    if not is_allowed_origin(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    event_hub = websocket.app.state.event_hub
    await websocket.send_json({"type": "hello", "stream": stream_id})
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") != "resume":
                continue
            last_seq = int(message.get("last_seq", 0))
            try:
                events = event_hub.resume(stream_id, last_seq)
            except ReplayGapError:
                await websocket.send_json(
                    {"type": "REPLAY_GAP", "stream": stream_id}
                )
                continue
            for event in events:
                await websocket.send_json({"seq": event.seq, **dict(event.payload)})
    except WebSocketDisconnect:
        return


def register_websocket_routes(app: FastAPI) -> None:
    @app.websocket("/ws/battle/{battle_tag}")
    async def battle_ws(websocket: WebSocket, battle_tag: str) -> None:
        await _serve_stream(websocket, f"battle:{battle_tag}")

    @app.websocket("/ws/lobby")
    async def lobby_ws(websocket: WebSocket) -> None:
        await _serve_stream(websocket, "lobby")
