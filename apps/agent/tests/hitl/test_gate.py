"""Gate exact-once de aprobacion humana (spec Fase 3 S3-4, D42, D65).

Cubre: records base, ganador unico bajo resolvers concurrentes ("Test
concurrent resolvers"), override ilegal que no consume el gate, timeout con
reloj falso demostrando que el Future estuvo `pending`, modo skip que no
crea ningun Future, toggle autonomo que resuelve sin reabrir, y un canario
de fuente que prohibe los primitivos de timeout de asyncio sobre el Future
del CAS.
"""

from __future__ import annotations

import inspect
import threading

import pytest

from ludex_agent.hitl import gate as gate_module
from ludex_agent.hitl.gate import (
    AlreadyResolved,
    ApprovalKey,
    ApprovalProposal,
    ApprovalResolution,
    IllegalOverrideError,
    PendingApproval,
    create_gate,
)
from ludex_agent.hitl.policy import (
    AlwaysGateApprovalPolicy,
    NeverGateApprovalPolicy,
    ProductionApprovalPolicy,
)

_LEGAL_ACTIONS = [{"id": "move-1"}, {"id": "move-2"}]


def _proposal(**overrides: object) -> ApprovalProposal:
    defaults: dict[str, object] = {
        "action": {"id": "move-1"},
        "legal_actions": _LEGAL_ACTIONS,
        "model_envelope": {"provider": "google", "model": "gemini-test"},
    }
    defaults.update(overrides)
    return ApprovalProposal(**defaults)  # type: ignore[arg-type]


def _key(attempt_index: int = 0) -> ApprovalKey:
    return ApprovalKey(
        battle_tag="battle-1", decision_index=0, attempt_index=attempt_index,
    )


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Records (Step 1)
# ---------------------------------------------------------------------------


def test_records_are_frozen_and_structurally_comparable():
    key_a = _key()
    key_b = ApprovalKey(battle_tag="battle-1", decision_index=0, attempt_index=0)
    assert key_a == key_b
    with pytest.raises(AttributeError):
        key_a.attempt_index = 1  # type: ignore[misc]

    resolution = ApprovalResolution(
        outcome="human_approved", action={"id": "move-1"}, resolved_by="operator",
    )
    assert resolution.resolved_reason is None


# ---------------------------------------------------------------------------
# CAS ganador unico y resolvers concurrentes
# ---------------------------------------------------------------------------


def test_second_resolver_gets_already_resolved_with_the_winning_outcome():
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    winner = gate.resolve_approved()

    with pytest.raises(AlreadyResolved) as exc_info:
        gate.resolve_override({"id": "move-2"})

    assert exc_info.value.winner == winner


def test_concurrent_resolvers_exact_once_winner():
    """Dos hilos compiten por el mismo gate: exactamente uno gana el CAS y
    el otro recibe `AlreadyResolved` apuntando al mismo ganador."""
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def approve() -> None:
        barrier.wait()
        try:
            results["approve"] = gate.resolve_approved()
        except AlreadyResolved as exc:
            results["approve"] = exc

    def override() -> None:
        barrier.wait()
        try:
            results["override"] = gate.resolve_override({"id": "move-2"})
        except AlreadyResolved as exc:
            results["override"] = exc

    threads = [threading.Thread(target=approve), threading.Thread(target=override)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [
        value for value in results.values() if isinstance(value, ApprovalResolution)
    ]
    losers = [
        value for value in results.values() if isinstance(value, AlreadyResolved)
    ]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].winner == winners[0]

    # el gate sigue siendo exact-once: un tercer intento tambien pierde
    # contra el mismo ganador, nunca reabre la decision.
    with pytest.raises(AlreadyResolved) as exc_info:
        gate.resolve_approved()
    assert exc_info.value.winner == winners[0]


# ---------------------------------------------------------------------------
# Override ilegal
# ---------------------------------------------------------------------------


def test_illegal_override_raises_typed_error_and_does_not_consume_gate():
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    with pytest.raises(IllegalOverrideError) as exc_info:
        gate.resolve_override({"id": "does-not-exist"})
    assert exc_info.value.action == {"id": "does-not-exist"}

    # el gate NO se consumio: un resolver legitimo todavia puede ganar el CAS.
    resolution = gate.resolve_approved()
    assert resolution.outcome == "human_approved"


def test_legal_override_uses_exactly_one_action_from_the_captured_mask():
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    resolution = gate.resolve_override({"id": "move-2"})
    assert resolution.outcome == "human_override"
    assert resolution.action in _LEGAL_ACTIONS
    assert resolution.resolved_by == "operator"


# ---------------------------------------------------------------------------
# Timeout con reloj falso
# ---------------------------------------------------------------------------


async def test_timeout_resolves_via_injected_clock_and_observes_pending_first():
    clock = _FakeClock(start=0.0)
    tick_calls = 0

    async def fake_tick() -> None:
        nonlocal tick_calls
        tick_calls += 1
        clock.advance(1.0)

    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=3.0,
        decision_deadline=100.0,
        clock=clock,
        tick=fake_tick,
    )
    assert gate.was_pending is False

    resolution = await gate.await_resolution()

    assert resolution.outcome == "timeout_auto"
    assert resolution.resolved_by == "timer"
    assert resolution.action == gate.proposal.action
    # El Future estuvo pending al menos una vuelta antes de resolverse: no
    # es un gate que ya vino resuelto desde el primer chequeo.
    assert gate.was_pending is True
    assert tick_calls >= 1

    # Prueba de "Future no cancelado": un Future cancelado levanta
    # CancelledError en result(); await_resolution ya devolvio el resultado
    # sin excepcion, lo cual solo es posible si nadie lo canceló.
    second_call = await gate.await_resolution()
    assert second_call == resolution


async def test_deadline_is_clamped_by_the_shorter_decision_deadline():
    """`approval_deadline = min(gate_start + approval_timeout,
    decision_deadline)` (D42/D65 S4.1): un decision_deadline mas corto que
    el approval_timeout debe ganar."""
    clock = _FakeClock(start=10.0)

    async def fake_tick() -> None:
        clock.advance(0.5)

    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=100.0,
        decision_deadline=11.0,
        clock=clock,
        tick=fake_tick,
    )
    assert gate.deadline == 11.0

    resolution = await gate.await_resolution()
    assert resolution.outcome == "timeout_auto"
    assert clock.now >= 11.0


# ---------------------------------------------------------------------------
# Modo skip: la politica decide si se crea el gate
# ---------------------------------------------------------------------------


def test_create_gate_skip_mode_returns_none_without_building_a_future():
    result = create_gate(
        NeverGateApprovalPolicy(),
        connection_mode="official",
        approval_mode="hitl",
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    assert result is None


def test_create_gate_production_policy_skips_local():
    result = create_gate(
        ProductionApprovalPolicy(),
        connection_mode="local",
        approval_mode="hitl",
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    assert result is None


def test_create_gate_builds_pending_approval_when_policy_requires_it():
    result = create_gate(
        AlwaysGateApprovalPolicy(),
        connection_mode="local",
        approval_mode="hitl",
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    assert isinstance(result, PendingApproval)


# ---------------------------------------------------------------------------
# Toggle autonomo
# ---------------------------------------------------------------------------


def test_autonomous_toggle_resolves_pending_gate_as_timeout_auto_system():
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    resolution = gate.resolve_autonomous_toggle()

    assert resolution.outcome == "timeout_auto"
    assert resolution.resolved_by == "system"
    assert resolution.resolved_reason == "autonomous_toggle"
    assert resolution.action == gate.proposal.action


def test_autonomous_toggle_never_reopens_a_resolved_gate():
    """Un unico /choose: una vez resuelto (por cualquier via), el toggle
    autonomo tambien pierde el CAS en vez de reabrir la decision."""
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    winner = gate.resolve_approved()

    with pytest.raises(AlreadyResolved) as exc_info:
        gate.resolve_autonomous_toggle()
    assert exc_info.value.winner == winner


async def test_pending_await_resolution_sees_autonomous_toggle_immediately():
    clock = _FakeClock(start=0.0)
    gate = PendingApproval.open(
        key=_key(),
        proposal=_proposal(),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=clock,
    )
    toggled = gate.resolve_autonomous_toggle()
    resolution = await gate.await_resolution()
    assert resolution == toggled


# ---------------------------------------------------------------------------
# Canario de fuente: prohibidos los primitivos de timeout de asyncio
# ---------------------------------------------------------------------------


def test_gate_source_never_uses_forbidden_timeout_primitives():
    """D42/D65 S4.1: prohibido sobre el Future del CAS esperar con un
    timeout de asyncio, envolverlo bajo timeout equivalente, o cancelarlo."""
    source = inspect.getsource(gate_module)
    forbidden = ("wait_for", "asyncio.timeout", "wrap_future", ".cancel(")
    for token in forbidden:
        assert token not in source, f"token prohibido encontrado: {token!r}"
