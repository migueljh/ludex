"""RED tests for Task 6: LobbyInbox (MON-36 S6).

`LobbyInbox` es el canal pre-lock donde `LudexPlayer` publica
challenges/estado de conexion/sesion para que `/ws/lobby` (spec 7.2) los
consuma sin tocar `Battle` ni el `ProtocolRecorder`.
"""

from __future__ import annotations

import pytest

from ludex_agent.showdown.lobby import LobbyInbox


def test_publish_and_drain_preserves_order() -> None:
    inbox = LobbyInbox()
    inbox.publish({"type": "connection", "status": "connected"})
    inbox.publish({"type": "challenge", "user": "rival1"})
    events = inbox.snapshot()
    assert [event["type"] for event in events] == ["connection", "challenge"]


def test_each_event_gets_monotonic_seq() -> None:
    inbox = LobbyInbox()
    inbox.publish({"type": "connection", "status": "connected"})
    inbox.publish({"type": "connection", "status": "disconnected"})
    events = inbox.snapshot()
    assert events[0]["seq"] < events[1]["seq"]


@pytest.mark.asyncio
async def test_wait_for_next_returns_new_event_without_polling() -> None:
    inbox = LobbyInbox()

    async def _publisher() -> None:
        inbox.publish({"type": "session", "status": "searching"})

    import asyncio

    waiter = asyncio.ensure_future(inbox.wait_for_next(after_seq=0))
    await asyncio.sleep(0)
    await _publisher()
    event = await asyncio.wait_for(waiter, timeout=1.0)
    assert event["type"] == "session"


def test_resume_after_rotation_reports_replay_gap() -> None:
    inbox = LobbyInbox(max_size=2)
    inbox.publish({"type": "a"})
    inbox.publish({"type": "b"})
    inbox.publish({"type": "c"})
    with pytest.raises(LobbyInbox.ReplayGapError):
        inbox.resume(last_seq=0)
