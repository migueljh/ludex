"""Gate exact-once de aprobacion humana (spec Fase 3 S3-4, D42, D65).

Cubre: records base, ganador unico bajo resolvers concurrentes ("Test
concurrent resolvers"), override ilegal que no consume el gate, timeout con
reloj falso demostrando que el Future estuvo `pending`, modo skip que no
crea ningun Future, toggle autonomo que resuelve sin reabrir, y un canario
de fuente que prohibe los primitivos de timeout de asyncio sobre el Future
del CAS.
"""

from __future__ import annotations

import ast
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
#
# Un canario de substring (MON-32 T-02) no distingue una llamada real de una
# mencion en un docstring o comentario, y no ve dos rutas de evasion triviales:
# un alias de import (`from asyncio import wait_for as wf`) o un acceso via
# `getattr(asyncio, "wait_for")`. Este canario parsea el AST del modulo y
# resuelve ambas rutas, ademas de la llamada calificada directa
# (`asyncio.wait_for(...)`). El `.cancel()` prohibido se acota al Future de
# aprobacion (`self._future` o un alias local asignado directamente desde el)
# para no marcar un `.cancel()` legitimo sobre otro objeto que el modulo
# pueda ganar despues.

_FORBIDDEN_ASYNCIO_ATTRS = {"wait_for", "timeout", "wrap_future"}


class _ForbiddenTimeoutPrimitiveVisitor(ast.NodeVisitor):
    """Detecta, en el AST de `gate.py`, usos prohibidos de primitivos de
    timeout de asyncio y `.cancel()` sobre el Future de aprobacion (D42/D65
    S4.1)."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.future_attr_refs = 0
        self._asyncio_aliases: set[str] = set()
        self._forbidden_name_aliases: dict[str, str] = {}
        self._future_local_aliases: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "asyncio":
                self._asyncio_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "asyncio":
            for alias in node.names:
                if alias.name in _FORBIDDEN_ASYNCIO_ATTRS:
                    local = alias.asname or alias.name
                    self._forbidden_name_aliases[local] = alias.name
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "_future":
            self.future_attr_refs += 1
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_approval_future_ref(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._future_local_aliases.add(target.id)
        self.generic_visit(node)

    def _is_asyncio_ref(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self._asyncio_aliases

    def _is_approval_future_ref(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "_future":
            return isinstance(node.value, ast.Name) and node.value.id == "self"
        if isinstance(node, ast.Name):
            return node.id in self._future_local_aliases
        return False

    def _forbidden_attr_target(self, node: ast.AST) -> str | None:
        """Nombre prohibido si `node` referencia `asyncio.<forbidden>`,
        directo o via `getattr` literal."""
        if isinstance(node, ast.Attribute) and self._is_asyncio_ref(node.value):
            if node.attr in _FORBIDDEN_ASYNCIO_ATTRS:
                return node.attr
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and self._is_asyncio_ref(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in _FORBIDDEN_ASYNCIO_ATTRS
        ):
            return node.args[1].value
        return None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        forbidden = self._forbidden_attr_target(func)
        if forbidden is not None:
            self.violations.append(
                f"linea {node.lineno}: llamada a asyncio.{forbidden} "
                "(directa o via getattr)"
            )
        elif isinstance(func, ast.Name) and func.id in self._forbidden_name_aliases:
            target = self._forbidden_name_aliases[func.id]
            self.violations.append(
                f"linea {node.lineno}: llamada via alias de import {func.id!r} "
                f"-> asyncio.{target}"
            )
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "cancel"
            and self._is_approval_future_ref(func.value)
        ):
            self.violations.append(
                f"linea {node.lineno}: cancel() sobre el Future de aprobacion"
            )
        self.generic_visit(node)


def test_gate_source_never_uses_forbidden_timeout_primitives():
    """D42/D65 S4.1: prohibido sobre el Future del CAS de aprobacion usar
    asyncio.wait_for/timeout/wrap_future -- llamada directa, alias de import,
    o `getattr` literal -- y prohibido cancelar el Future de aprobacion (o un
    alias local asignado directamente desde `self._future`)."""
    source = inspect.getsource(gate_module)
    tree = ast.parse(source)
    visitor = _ForbiddenTimeoutPrimitiveVisitor()
    visitor.visit(tree)

    # Canario no vacuo: si `_future` deja de existir como nombre de atributo
    # en el modulo (p.ej. un rename), la deteccion de `.cancel()` sobre el
    # Future de aprobacion queda ciega en silencio. Esto lo hace fallar en
    # vez de pasar sin haber verificado nada.
    assert visitor.future_attr_refs > 0, (
        "no se encontro ninguna referencia a `_future` en gate.py: si el "
        "atributo se renombro, este canario quedo vacuo y hay que actualizarlo"
    )

    assert visitor.violations == [], "primitivos prohibidos encontrados:\n" + "\n".join(
        visitor.violations
    )
