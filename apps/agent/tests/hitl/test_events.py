"""`EventHub`: secuencia monotonica por stream y replay exacto o
`ReplayGapError` (spec Fase 3 S7.2)."""

from __future__ import annotations

import pytest

from ludex_agent.hitl.events import EventHub, ReplayGapError


def test_publish_assigns_strictly_monotonic_sequence_per_stream():
    hub = EventHub()
    first = hub.publish("battle:t1", {"type": "hello"})
    second = hub.publish("battle:t1", {"type": "decision_proposed"})
    third = hub.publish("battle:t1", {"type": "decision_resolved"})
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]


def test_sequence_is_independent_per_stream():
    """La secuencia es dueña de CADA stream, no global: dos streams no
    comparten contador."""
    hub = EventHub()
    hub.publish("battle:t1", {"type": "hello"})
    hub.publish("battle:t1", {"type": "protocol"})
    lobby_first = hub.publish("lobby", {"type": "connection"})
    assert lobby_first.seq == 1


def test_resume_returns_exact_suffix_after_last_seq():
    hub = EventHub()
    hub.publish("battle:t1", {"type": "hello"})
    hub.publish("battle:t1", {"type": "protocol"})
    third = hub.publish("battle:t1", {"type": "decision_proposed"})
    fourth = hub.publish("battle:t1", {"type": "decision_resolved"})

    suffix = hub.resume("battle:t1", last_seq=2)

    assert [event.seq for event in suffix] == [3, 4]
    assert suffix[0].payload == third.payload
    assert suffix[1].payload == fourth.payload


def test_resume_from_zero_returns_full_unrotated_buffer():
    hub = EventHub()
    hub.publish("lobby", {"type": "connection"})
    hub.publish("lobby", {"type": "challenge"})

    suffix = hub.resume("lobby", last_seq=0)

    assert [event.seq for event in suffix] == [1, 2]


def test_resume_at_latest_seq_returns_empty_no_new_events():
    hub = EventHub()
    hub.publish("lobby", {"type": "connection"})

    assert hub.resume("lobby", last_seq=1) == []


def test_resume_raises_replay_gap_error_after_ring_buffer_rotates():
    """Una vez que el ring buffer rota mas alla de `last_seq`, no hay sufijo
    exacto: se exige `ReplayGapError`, nunca un backlog parcial."""
    hub = EventHub(ring_buffer_size=3)
    for index in range(5):
        hub.publish("battle:t1", {"type": "turn", "index": index})
    # seq 1 y 2 ya rotaron; el buffer retiene solo seq 3, 4, 5.

    with pytest.raises(ReplayGapError) as exc_info:
        hub.resume("battle:t1", last_seq=1)

    assert exc_info.value.stream_id == "battle:t1"
    assert exc_info.value.requested_seq == 1
    assert exc_info.value.earliest_available_seq == 3


def test_resume_at_earliest_available_boundary_does_not_raise():
    """`last_seq == earliest - 1` es exactamente el limite retenido: debe
    devolver el sufijo completo, no levantar `ReplayGapError`."""
    hub = EventHub(ring_buffer_size=3)
    for index in range(5):
        hub.publish("battle:t1", {"type": "turn", "index": index})

    suffix = hub.resume("battle:t1", last_seq=2)

    assert [event.seq for event in suffix] == [3, 4, 5]


def test_resume_unknown_stream_with_last_seq_zero_returns_empty():
    hub = EventHub()
    assert hub.resume("never-published", last_seq=0) == []


def test_resume_unknown_stream_with_nonzero_last_seq_raises_replay_gap():
    hub = EventHub()
    with pytest.raises(ReplayGapError):
        hub.resume("never-published", last_seq=5)
