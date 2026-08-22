"""Dominio puro HITL exact-once (MON-32 Task 2, spec Fase 3 S3-4 y S7).

Records, gate CAS, politicas de aprobacion inyectables y `EventHub` de
replay. Este paquete no integra con `run_graph`: la integracion vive en una
rebanada posterior (D65 exige no tocar `graph/workflow.py`,
`graph/state.py`, `graph/decision.py` ni `graph/execute.py` para implementar
el gate).
"""

from __future__ import annotations

from .events import EventHub, ReplayGapError, StreamEvent
from .gate import (
    AlreadyResolved,
    ApprovalKey,
    ApprovalProposal,
    ApprovalResolution,
    IllegalOverrideError,
    PendingApproval,
    create_gate,
)
from .policy import (
    AlwaysGateApprovalPolicy,
    ApprovalMode,
    ApprovalPolicy,
    ConnectionMode,
    NeverGateApprovalPolicy,
    ProductionApprovalPolicy,
)

__all__ = [
    "AlreadyResolved",
    "AlwaysGateApprovalPolicy",
    "ApprovalKey",
    "ApprovalMode",
    "ApprovalPolicy",
    "ApprovalProposal",
    "ApprovalResolution",
    "ConnectionMode",
    "EventHub",
    "IllegalOverrideError",
    "NeverGateApprovalPolicy",
    "PendingApproval",
    "ProductionApprovalPolicy",
    "ReplayGapError",
    "StreamEvent",
    "create_gate",
]
