"""`ApprovalRegistry`: estado vivo de decisiones pendientes (spec Fase 3 S3.3,
S7.1, D65 MON-33 Task 4).

El registry vive en el loop de FastAPI y mapea `(battle_tag, decision_index)`
a su `PendingApproval` de attempt ACTUAL. `PendingApproval` ya es el gate
exact-once (Task 2); este modulo solo agrega la frontera de lookup que
necesita la API: 404 si nunca hubo gate para esa decision, 409 si el attempt
que trae el request quedo `superseded` por uno mas nuevo (D65 S4.3: un
rechazo de Showdown invalida el intento e incrementa `attempt_index`).

El timeout de un `PendingApproval` avanza por su propio `await_resolution()`
(reloj inyectado D42, Task 2) sin que este registry ni ningun cliente
WebSocket tengan que intervenir: no hay ningun metodo aca que un subscriber
deba llamar para que el gate progrese.
"""

from __future__ import annotations

from .gate import ApprovalResolution, PendingApproval


class UnknownDecisionError(Exception):
    """Nunca se abrio (o ya se descarto) un gate para esta decision."""

    def __init__(self, battle_tag: str, decision_index: int) -> None:
        super().__init__(
            f"no hay decision pendiente para battle_tag={battle_tag!r} "
            f"decision_index={decision_index!r}"
        )
        self.battle_tag = battle_tag
        self.decision_index = decision_index


class StaleAttemptError(Exception):
    """El `attempt_index` del request ya no es el vigente: fue superseded."""

    def __init__(
        self,
        battle_tag: str,
        decision_index: int,
        requested_attempt: int,
        current_attempt: int,
    ) -> None:
        super().__init__(
            f"attempt_index={requested_attempt!r} quedo obsoleto para "
            f"battle_tag={battle_tag!r} decision_index={decision_index!r}: "
            f"el attempt vigente es {current_attempt!r}"
        )
        self.battle_tag = battle_tag
        self.decision_index = decision_index
        self.requested_attempt = requested_attempt
        self.current_attempt = current_attempt


class ApprovalRegistry:
    """Dueno del mapa `(battle_tag, decision_index) -> PendingApproval`.

    No es duenio de ninguna persistencia: `pending_decisions` (Task 3) es
    auditoria escrita exclusivamente por `POKE_LOOP`. Este registry es
    memoria pura, vive y muere con el proceso de la API.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, int], PendingApproval] = {}

    def open(self, pending: PendingApproval) -> None:
        """Registra `pending` como el attempt vigente de su decision.

        Un `open` con un `attempt_index` nuevo reemplaza silenciosamente el
        anterior en el mapa (D65 S4.3: superseded); el `PendingApproval`
        viejo sigue existiendo como objeto (su Future puede seguir
        resolviendose por quien ya lo tenia referenciado) pero deja de ser
        alcanzable via el registry, que es la unica via de la API.
        """
        key = (pending.key.battle_tag, pending.key.decision_index)
        self._pending[key] = pending

    def get(self, battle_tag: str, decision_index: int) -> PendingApproval | None:
        return self._pending.get((battle_tag, decision_index))

    def discard(self, battle_tag: str, decision_index: int) -> None:
        self._pending.pop((battle_tag, decision_index), None)

    def _require_current(
        self, battle_tag: str, decision_index: int, attempt_index: int,
    ) -> PendingApproval:
        current = self._pending.get((battle_tag, decision_index))
        if current is None:
            raise UnknownDecisionError(battle_tag, decision_index)
        if current.key.attempt_index != attempt_index:
            raise StaleAttemptError(
                battle_tag, decision_index, attempt_index,
                current.key.attempt_index,
            )
        return current

    def resolve_approved(
        self, battle_tag: str, decision_index: int, attempt_index: int,
    ) -> ApprovalResolution:
        """Puede levantar `UnknownDecisionError`, `StaleAttemptError` o
        `AlreadyResolved` (Task 2, gate ya resuelto por otro CAS)."""
        pending = self._require_current(battle_tag, decision_index, attempt_index)
        return pending.resolve_approved()

    def resolve_override(
        self,
        battle_tag: str,
        decision_index: int,
        attempt_index: int,
        action: dict[str, object],
    ) -> ApprovalResolution:
        """Ademas de lo anterior, puede levantar `IllegalOverrideError`
        (validado ANTES del CAS por Task 2: no consume el gate)."""
        pending = self._require_current(battle_tag, decision_index, attempt_index)
        return pending.resolve_override(action)
