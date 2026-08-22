"""Politicas de aprobacion humana inyectables (spec Fase 3 S1.1, D65)."""

from __future__ import annotations

from ludex_agent.hitl.policy import (
    AlwaysGateApprovalPolicy,
    NeverGateApprovalPolicy,
    ProductionApprovalPolicy,
)


def test_production_policy_skips_local_regardless_of_approval_mode():
    policy = ProductionApprovalPolicy()
    assert (
        policy.requires_gate(connection_mode="local", approval_mode="hitl")
        is False
    )
    assert (
        policy.requires_gate(connection_mode="local", approval_mode="autonomous")
        is False
    )


def test_production_policy_skips_official_autonomous():
    policy = ProductionApprovalPolicy()
    assert (
        policy.requires_gate(
            connection_mode="official", approval_mode="autonomous",
        )
        is False
    )


def test_production_policy_gates_official_hitl():
    policy = ProductionApprovalPolicy()
    assert (
        policy.requires_gate(connection_mode="official", approval_mode="hitl")
        is True
    )


def test_always_gate_policy_gates_local_hitl_for_tests():
    """Los tests pueden habilitar HITL contra Showdown local sin tocar la
    politica de produccion (spec S1.1)."""
    policy = AlwaysGateApprovalPolicy()
    assert (
        policy.requires_gate(connection_mode="local", approval_mode="hitl")
        is True
    )
    assert (
        policy.requires_gate(connection_mode="local", approval_mode="autonomous")
        is False
    )


def test_never_gate_policy_never_blocks_run_benchmark_matrix_run():
    """`run`, `benchmark` y `matrix-run` nunca deben bloquear por un setting
    de HITL, incluso en `official`/`hitl` (spec S1.1)."""
    policy = NeverGateApprovalPolicy()
    assert (
        policy.requires_gate(connection_mode="official", approval_mode="hitl")
        is False
    )
    assert (
        policy.requires_gate(connection_mode="local", approval_mode="hitl")
        is False
    )
