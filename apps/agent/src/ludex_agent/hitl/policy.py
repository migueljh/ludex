"""Politicas de aprobacion humana, inyectables (spec Fase 3 S1.1, D65).

`local` siempre saltea el gate humano en produccion. `official` permite
`hitl` o `autonomous`. La politica de produccion nunca gatea en `local`; los
tests pueden inyectar una politica que tambien gatea en `local` para probar
HITL contra un Showdown local sin tocar el comportamiento de produccion.
`run`, `benchmark` y `matrix-run` deben usar una politica que nunca gatea,
sin importar el setting de HITL en `Settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ConnectionMode = Literal["local", "official"]
ApprovalMode = Literal["hitl", "autonomous"]


class ApprovalPolicy(Protocol):
    def requires_gate(
        self, *, connection_mode: ConnectionMode, approval_mode: ApprovalMode,
    ) -> bool:
        ...


@dataclass(frozen=True)
class ProductionApprovalPolicy:
    """Politica por defecto: saltea `local` y saltea `official`/`autonomous`."""

    def requires_gate(
        self, *, connection_mode: ConnectionMode, approval_mode: ApprovalMode,
    ) -> bool:
        if connection_mode == "local":
            return False
        return approval_mode == "hitl"


@dataclass(frozen=True)
class AlwaysGateApprovalPolicy:
    """Solo para tests: gatea tambien en `local` cuando `approval_mode=hitl`."""

    def requires_gate(
        self, *, connection_mode: ConnectionMode, approval_mode: ApprovalMode,
    ) -> bool:
        return approval_mode == "hitl"


@dataclass(frozen=True)
class NeverGateApprovalPolicy:
    """`run`, `benchmark` y `matrix-run`: nunca bloquean por HITL."""

    def requires_gate(
        self, *, connection_mode: ConnectionMode, approval_mode: ApprovalMode,
    ) -> bool:
        return False
