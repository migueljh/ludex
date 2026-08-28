"""WebSockets de la API de control (spec Fase 3 S7.2, D65 MON-33 Task 4).

Cubre: `hello` al conectar, rechazo de `Origin` no-loopback ANTES de
`accept()`, `seq` monotonico por stream, `resume(last_seq)` devolviendo el
sufijo exacto, `REPLAY_GAP` cuando el ring buffer ya roto mas alla del
cursor pedido (canario ACOTADO: nunca cuelga `receive_json`), el fan-out
live (un evento publicado DESPUES de conectar llega al cliente sin que el
cliente mande `resume`), y que el timeout de una decision pendiente
resuelve independientemente de cualquier cliente WS (nunca conectado, o
conectado y desconectado).
"""

from __future__ import annotations

import queue
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ludex_agent.api.app import create_app
from ludex_agent.db.model_repository import ProviderModel
from ludex_agent.hitl.events import EventHub
from ludex_agent.hitl.gate import ApprovalKey, ApprovalProposal, PendingApproval
from ludex_agent.hitl.registry import ApprovalRegistry


class _FakeSettingsRepo:
    async def active_selection(self):
        return ProviderModel("google", "gemini-test")


class _EmptyReadRepository:
    async def get_battle_by_tag(self, battle_tag: str):
        return None


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(*, event_hub: EventHub | None = None):
    registry = ApprovalRegistry()
    event_hub = event_hub or EventHub()
    app = create_app(
        registry=registry, event_hub=event_hub, settings_repo=_FakeSettingsRepo(),
        historical_repo_factory=lambda: _EmptyReadRepository(),
    )
    return TestClient(app), registry, event_hub


# ---------------------------------------------------------------------------
# hello / Origin
# ---------------------------------------------------------------------------


def test_battle_ws_sends_hello_on_connect():
    client, _, _ = _client()
    with client.websocket_connect("/ws/battle/battle-1") as ws:
        hello = ws.receive_json()
    assert hello == {"type": "hello", "stream": "battle:battle-1"}


def test_battle_ws_accepts_loopback_origin():
    client, _, _ = _client()
    with client.websocket_connect(
        "/ws/battle/battle-1", headers={"origin": "http://127.0.0.1:5173"},
    ) as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello"


def test_battle_ws_rejects_a_foreign_origin_before_accept():
    client, _, _ = _client()
    try:
        with client.websocket_connect(
            "/ws/battle/battle-1", headers={"origin": "https://evil.example"},
        ) as ws:
            ws.receive_json()
        raised = False
    except WebSocketDisconnect:
        raised = True
    assert raised, "un Origin no-loopback tiene que cerrar antes de aceptar"


def test_lobby_ws_also_enforces_loopback_origin():
    client, _, _ = _client()
    try:
        with client.websocket_connect(
            "/ws/lobby", headers={"origin": "https://evil.example"},
        ) as ws:
            ws.receive_json()
        raised = False
    except WebSocketDisconnect:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# seq monotonico y resume exacto
# ---------------------------------------------------------------------------


def test_resume_from_zero_returns_every_published_event_in_monotonic_seq_order():
    event_hub = EventHub()
    event_hub.publish("battle:battle-1", {"type": "decision_proposed", "decision_index": 0})
    event_hub.publish("battle:battle-1", {"type": "decision_resolved", "decision_index": 0})
    event_hub.publish("battle:battle-1", {"type": "decision_proposed", "decision_index": 1})

    client, _, _ = _client(event_hub=event_hub)
    with client.websocket_connect("/ws/battle/battle-1") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "resume", "last_seq": 0})
        messages = [ws.receive_json() for _ in range(3)]

    assert [m["seq"] for m in messages] == [1, 2, 3]
    assert [m["type"] for m in messages] == [
        "decision_proposed", "decision_resolved", "decision_proposed",
    ]


def test_resume_from_a_partial_cursor_returns_only_the_exact_suffix():
    event_hub = EventHub()
    event_hub.publish("battle:battle-1", {"type": "decision_proposed", "decision_index": 0})
    event_hub.publish("battle:battle-1", {"type": "decision_resolved", "decision_index": 0})
    event_hub.publish("battle:battle-1", {"type": "decision_proposed", "decision_index": 1})

    client, _, _ = _client(event_hub=event_hub)
    with client.websocket_connect("/ws/battle/battle-1") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "resume", "last_seq": 1})
        messages = [ws.receive_json() for _ in range(2)]

    assert [m["seq"] for m in messages] == [2, 3]


def test_replay_gap_when_the_ring_buffer_already_rotated_past_the_cursor():
    event_hub = EventHub(ring_buffer_size=2)
    for i in range(4):
        event_hub.publish("battle:battle-1", {"type": "decision_proposed", "decision_index": i})
    # ring buffer_size=2: solo quedan seq=3,4. Pedir desde seq=0 (o desde
    # cualquier cursor < 2) ya no tiene sufijo exacto disponible.

    client, _, _ = _client(event_hub=event_hub)

    # Canario ACOTADO (D65 canario 8 + regla de verificacion): `receive_json`
    # bloquea para siempre si el handler traga `ReplayGapError` sin enviar
    # nada. El receive corre en un hilo separado y el assert espera en una
    # queue con timeout: si el mensaje no llega, el test FALLA acotado en
    # vez de colgar la suite.
    received: queue.Queue = queue.Queue()

    def worker() -> None:
        with client.websocket_connect("/ws/battle/battle-1") as ws:
            ws.receive_json()  # hello
            ws.send_json({"type": "resume", "last_seq": 0})
            received.put(ws.receive_json())

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        message = received.get(timeout=2.0)
    except queue.Empty:
        pytest.fail(
            "REPLAY_GAP nunca llego en 2 s: el handler colgo el canario "
            "en vez de enviar el frame acotado"
        )

    assert message == {"type": "REPLAY_GAP", "stream": "battle:battle-1"}


# ---------------------------------------------------------------------------
# Fan-out live (spec S7.2, D65 canario 19): el servidor publica, el cliente
# recibe SIN mandar resume. resume/replay sigue siendo el canal de reconexion.
# ---------------------------------------------------------------------------


def test_live_fan_out_delivers_events_published_after_connect_without_resume():
    """Connect -> hello -> `publish` DESPUES de conectar -> el cliente
    recibe el evento sin haber enviado `resume`. Si se quita el fan-out
    server-side, el receive bloquea y el canario falla por timeout acotado
    (mismo patron bounded que el de REPLAY_GAP)."""
    client, _, event_hub = _client()

    received: queue.Queue = queue.Queue()

    def worker() -> None:
        with client.websocket_connect("/ws/battle/battle-1") as ws:
            hello = ws.receive_json()
            assert hello == {"type": "hello", "stream": "battle:battle-1"}
            event_hub.publish(
                "battle:battle-1",
                {"type": "decision_proposed", "decision_index": 7},
            )
            received.put(ws.receive_json())

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        message = received.get(timeout=2.0)
    except queue.Empty:
        pytest.fail(
            "el fan-out no entrego en 2 s el evento publicado despues de "
            "conectar (sin resume): el WS depende de polling del cliente"
        )

    assert message["seq"] == 1
    assert message["type"] == "decision_proposed"
    assert message["decision_index"] == 7


# ---------------------------------------------------------------------------
# Timeout independiente de cualquier suscriptor WS
# ---------------------------------------------------------------------------


def _open_pending(registry: ApprovalRegistry, *, clock, tick=None) -> PendingApproval:
    pending = PendingApproval.open(
        key=ApprovalKey(battle_tag="battle-1", decision_index=0, attempt_index=0),
        proposal=ApprovalProposal(
            action={"id": "move-1"}, legal_actions=[{"id": "move-1"}],
            model_envelope={"provider": "google", "model": "gemini-test"},
        ),
        approval_timeout_seconds=3.0,
        decision_deadline=100.0,
        clock=clock,
        tick=tick,
    )
    registry.open(pending)
    return pending


async def test_timeout_resolves_even_though_no_ws_client_ever_connected():
    client, registry, _ = _client()
    clock = _FakeClock(start=0.0)

    async def fake_tick() -> None:
        clock.advance(1.0)

    pending = _open_pending(registry, clock=clock, tick=fake_tick)

    # Ningun websocket se conecto jamas para este battle_tag: el timeout
    # tiene que resolver igual, via el reloj inyectado del gate. `was_pending`
    # y `resolved_by` demuestran que resolvio por vencimiento real (observado
    # pendiente, `timer`), no porque alguien lo cerro antes por su cuenta.
    resolution = await pending.await_resolution()

    assert resolution.outcome == "timeout_auto"
    assert resolution.resolved_by == "timer"
    assert pending.was_pending is True
    assert client is not None  # la app existe; nunca se abrio un WS sobre ella


async def test_timeout_resolves_after_a_ws_client_connects_and_disconnects():
    client, registry, _ = _client()
    clock = _FakeClock(start=0.0)

    async def fake_tick() -> None:
        clock.advance(1.0)

    pending = _open_pending(registry, clock=clock, tick=fake_tick)

    with client.websocket_connect("/ws/battle/battle-1") as ws:
        ws.receive_json()  # hello
    # la conexion ya se cerro aca (el `with` sale del contexto).

    resolution = await pending.await_resolution()
    assert resolution.outcome == "timeout_auto"
    assert resolution.resolved_by == "timer"
    assert pending.was_pending is True
