"""`ApprovalRegistry`: mapea decisiones vivas a su `PendingApproval` actual
(Fase 3 S3-4, MON-33 Task 4). Cubre stale attempt (409), CAS ganador/perdedor
via el registry, override ilegal, y timeout independiente de cualquier
suscriptor (WS o no): el registry nunca condiciona `await_resolution()` a que
alguien este escuchando.
"""

from __future__ import annotations

import pytest

from ludex_agent.hitl.gate import (
    AlreadyResolved,
    ApprovalKey,
    ApprovalProposal,
    ApprovalResolution,
    IllegalOverrideError,
    PendingApproval,
)
from ludex_agent.hitl.registry import (
    ApprovalRegistry,
    StaleAttemptError,
    UnknownDecisionError,
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


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _open(
    registry: ApprovalRegistry,
    *,
    battle_tag: str = "battle-1",
    decision_index: int = 0,
    attempt_index: int = 0,
    approval_timeout_seconds: float = 10.0,
    decision_deadline: float = 100.0,
    clock: object | None = None,
    tick: object | None = None,
) -> PendingApproval:
    pending = PendingApproval.open(
        key=ApprovalKey(
            battle_tag=battle_tag,
            decision_index=decision_index,
            attempt_index=attempt_index,
        ),
        proposal=_proposal(),
        approval_timeout_seconds=approval_timeout_seconds,
        decision_deadline=decision_deadline,
        clock=clock or _FakeClock(),
        tick=tick,
    )
    registry.open(pending)
    return pending


def test_get_returns_none_for_unknown_decision():
    registry = ApprovalRegistry()
    assert registry.get("battle-1", 0) is None


def test_open_makes_the_decision_visible_via_get():
    registry = ApprovalRegistry()
    pending = _open(registry)
    assert registry.get("battle-1", 0) is pending


def test_resolve_approved_returns_the_winning_resolution():
    registry = ApprovalRegistry()
    _open(registry)
    resolution = registry.resolve_approved("battle-1", 0, 0)
    assert isinstance(resolution, ApprovalResolution)
    assert resolution.outcome == "human_approved"


def test_resolve_override_with_illegal_action_raises_and_does_not_consume():
    registry = ApprovalRegistry()
    _open(registry)
    with pytest.raises(IllegalOverrideError):
        registry.resolve_override("battle-1", 0, 0, {"id": "does-not-exist"})
    # el gate sigue vivo: un approve legitimo todavia puede ganar.
    resolution = registry.resolve_approved("battle-1", 0, 0)
    assert resolution.outcome == "human_approved"


def test_resolve_against_unknown_decision_raises_typed_error():
    registry = ApprovalRegistry()
    with pytest.raises(UnknownDecisionError):
        registry.resolve_approved("battle-1", 0, 0)


def test_resolve_with_stale_attempt_index_raises_typed_409():
    """Un `attempt_index` viejo (la decision fue superseded a un intento
    nuevo) tiene que fallar distinto de "no existe": es un 409, no un 404."""
    registry = ApprovalRegistry()
    _open(registry, attempt_index=1)
    with pytest.raises(StaleAttemptError) as exc_info:
        registry.resolve_approved("battle-1", 0, 0)
    assert exc_info.value.requested_attempt == 0
    assert exc_info.value.current_attempt == 1


def test_open_with_a_new_attempt_supersedes_the_previous_one_in_the_registry():
    registry = ApprovalRegistry()
    _open(registry, attempt_index=0)
    _open(registry, attempt_index=1)
    assert registry.get("battle-1", 0).key.attempt_index == 1
    with pytest.raises(StaleAttemptError):
        registry.resolve_approved("battle-1", 0, 0)


def test_second_resolver_gets_already_resolved_with_the_winning_outcome():
    registry = ApprovalRegistry()
    _open(registry)
    winner = registry.resolve_approved("battle-1", 0, 0)
    with pytest.raises(AlreadyResolved) as exc_info:
        registry.resolve_override("battle-1", 0, 0, {"id": "move-2"})
    assert exc_info.value.winner == winner


def test_discard_removes_the_decision_from_the_registry():
    registry = ApprovalRegistry()
    _open(registry)
    registry.discard("battle-1", 0)
    assert registry.get("battle-1", 0) is None
    with pytest.raises(UnknownDecisionError):
        registry.resolve_approved("battle-1", 0, 0)


async def test_timeout_resolves_without_any_subscriber_ever_registered():
    """El timeout tiene que vencer por reloj/tick, nunca por accion de un
    cliente WS: el registry no expone ningun hook que un subscriber deba
    llamar para que el gate avance."""
    clock = _FakeClock(start=0.0)

    async def fake_tick() -> None:
        clock.advance(1.0)

    registry = ApprovalRegistry()
    pending = _open(
        registry,
        approval_timeout_seconds=3.0,
        decision_deadline=100.0,
        clock=clock,
        tick=fake_tick,
    )
    assert pending.was_pending is False

    resolution = await pending.await_resolution()

    assert resolution.outcome == "timeout_auto"
    assert resolution.resolved_by == "timer"
    assert pending.was_pending is True
    # el registry sigue devolviendo el mismo pending (ya resuelto); un
    # resolve tardio contra el mismo attempt ve el ganador, no un error de
    # "no existe".
    with pytest.raises(AlreadyResolved) as exc_info:
        registry.resolve_approved("battle-1", 0, 0)
    assert exc_info.value.winner == resolution
