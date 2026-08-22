"""Gate exact-once de aprobacion humana (D65, spec Fase 3 S3-4).

Cada intento de decision usa una clave unica `(battle_tag, decision_index,
attempt_index)`. `PendingApproval` posee un `concurrent.futures.Future`
compartido entre dos loops (D65 S3.3): el loop de FastAPI resuelve el CAS a
traves de los metodos `resolve_*`, el POKE_LOOP espera con
`await_resolution()`. El primer `set_result()` gana; cualquier intento
posterior recibe `AlreadyResolved` con el resultado que gano. Un override se
valida contra la mascara legal capturada ANTES de intentar el CAS, para que
un override invalido no consuma el gate.

Regla de reloj (D42/D65 S4.1): el waiter usa unicamente el `clock()`
inyectado, nunca el reloj real del sistema ni el del event loop. Queda
prohibido, sobre el Future del CAS: esperar con un timeout del modulo
asyncio, envolverlo bajo un timeout equivalente, o cancelarlo -- verificado
en CPython 3.12 que esa combinacion cancela el Future fuente y hace
imposible escribir el outcome de vencimiento. El waiter sondea con
`_tick()` (por defecto un `asyncio.sleep` corto) en vez de bloquear sobre el
Future.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from typing import Literal

from .policy import ApprovalMode, ApprovalPolicy, ConnectionMode

_PRODUCTION_TICK_SECONDS = 0.05


async def _default_tick() -> None:
    await asyncio.sleep(_PRODUCTION_TICK_SECONDS)


@dataclass(frozen=True)
class ApprovalKey:
    battle_tag: str
    decision_index: int
    attempt_index: int


@dataclass(frozen=True)
class ApprovalProposal:
    action: dict[str, object]
    legal_actions: Sequence[dict[str, object]]
    model_envelope: dict[str, object]


@dataclass(frozen=True)
class ApprovalResolution:
    outcome: Literal["human_approved", "human_override", "timeout_auto"]
    action: dict[str, object]
    resolved_by: Literal["operator", "timer", "system"]
    resolved_reason: str | None = None


class AlreadyResolved(Exception):
    """El gate ya fue resuelto por otro resolver antes de este intento de CAS."""

    def __init__(self, winner: ApprovalResolution) -> None:
        super().__init__(
            f"el gate ya fue resuelto: outcome={winner.outcome!r} "
            f"resolved_by={winner.resolved_by!r}"
        )
        self.winner = winner


class IllegalOverrideError(Exception):
    """Un override no pertenece a la mascara legal capturada para el intento."""

    def __init__(
        self,
        action: dict[str, object],
        legal_actions: Sequence[dict[str, object]],
    ) -> None:
        super().__init__(f"override fuera de la mascara legal: {action!r}")
        self.action = action
        self.legal_actions = legal_actions


class PendingApproval:
    """Gate exact-once para un unico intento de decision."""

    def __init__(
        self,
        *,
        key: ApprovalKey,
        proposal: ApprovalProposal,
        clock: Callable[[], float],
        deadline: float,
        tick: Callable[[], Awaitable[None]],
    ) -> None:
        self.key = key
        self.proposal = proposal
        self._clock = clock
        self._deadline = deadline
        self._tick = tick
        self._future: "Future[ApprovalResolution]" = Future()
        self._was_pending = False

    @classmethod
    def open(
        cls,
        *,
        key: ApprovalKey,
        proposal: ApprovalProposal,
        approval_timeout_seconds: float,
        decision_deadline: float,
        clock: Callable[[], float] = time.monotonic,
        tick: Callable[[], Awaitable[None]] | None = None,
    ) -> "PendingApproval":
        gate_start = clock()
        deadline = min(gate_start + approval_timeout_seconds, decision_deadline)
        return cls(
            key=key,
            proposal=proposal,
            clock=clock,
            deadline=deadline,
            tick=tick or _default_tick,
        )

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def was_pending(self) -> bool:
        """`True` si `await_resolution()` observo el Future pendiente al menos
        una vez antes de resolverlo. Distingue un timeout real de un gate que
        ya estaba resuelto desde el primer chequeo."""
        return self._was_pending

    def resolve_approved(self) -> ApprovalResolution:
        resolution = ApprovalResolution(
            outcome="human_approved",
            action=self.proposal.action,
            resolved_by="operator",
        )
        return self._cas(resolution)

    def resolve_override(self, action: dict[str, object]) -> ApprovalResolution:
        if action not in self.proposal.legal_actions:
            raise IllegalOverrideError(action, self.proposal.legal_actions)
        resolution = ApprovalResolution(
            outcome="human_override",
            action=action,
            resolved_by="operator",
        )
        return self._cas(resolution)

    def resolve_autonomous_toggle(self) -> ApprovalResolution:
        resolution = ApprovalResolution(
            outcome="timeout_auto",
            action=self.proposal.action,
            resolved_by="system",
            resolved_reason="autonomous_toggle",
        )
        return self._cas(resolution)

    def _cas(self, resolution: ApprovalResolution) -> ApprovalResolution:
        try:
            self._future.set_result(resolution)
        except InvalidStateError:
            raise AlreadyResolved(self._future.result()) from None
        return resolution

    def _try_resolve_timeout(self) -> None:
        resolution = ApprovalResolution(
            outcome="timeout_auto",
            action=self.proposal.action,
            resolved_by="timer",
        )
        try:
            self._future.set_result(resolution)
        except InvalidStateError:
            pass

    async def await_resolution(self) -> ApprovalResolution:
        while True:
            if self._future.done():
                return self._future.result()
            if self._clock() >= self._deadline:
                self._try_resolve_timeout()
                return self._future.result()
            self._was_pending = True
            await self._tick()


def create_gate(
    policy: ApprovalPolicy,
    *,
    connection_mode: ConnectionMode,
    approval_mode: ApprovalMode,
    key: ApprovalKey,
    proposal: ApprovalProposal,
    approval_timeout_seconds: float,
    decision_deadline: float,
    clock: Callable[[], float] = time.monotonic,
    tick: Callable[[], Awaitable[None]] | None = None,
) -> PendingApproval | None:
    """Punto de entrada inyectable: `None` si la politica no exige gate.

    En modo skip no se construye ningun `PendingApproval` y por lo tanto
    tampoco ningun `Future`: el llamador nunca espera.
    """
    if not policy.requires_gate(
        connection_mode=connection_mode, approval_mode=approval_mode,
    ):
        return None
    return PendingApproval.open(
        key=key,
        proposal=proposal,
        approval_timeout_seconds=approval_timeout_seconds,
        decision_deadline=decision_deadline,
        clock=clock,
        tick=tick,
    )
