"""Repositorio de auditoria durable del gate exact-once (D65, MON-31 Fase 3).

`PendingDecisionRepository` es el UNICO escritor de `pending_decisions`: el
plan de Fase 3 exige que solo corra dentro de `POKE_LOOP`, nunca en el loop
de FastAPI -- la API nunca recibe una instancia de este repositorio (Task 4
lee historial via `ApiReadRepository`, no via este modulo).

El engine se crea perezosamente con `NullPool`, mismo motivo que
`PostgresContextRepository` (`context_repository.py`): este repositorio
puede correr en el loop del listener de poke-env, que puede ser otro hilo
distinto del loop que lo construye, y un pool que retiene conexiones bindea
sus conexiones asyncpg al loop donde se crearon -- `dispose()` desde otro
loop cruza ("Future attached to a different loop").

La fila `awaiting` se persiste ANTES de publicar la propuesta por WebSocket
(D65): el `Future` del gate (`hitl/gate.py`) sigue siendo la fuente de
verdad de `/choose`; esta tabla es auditoria, nunca el mecanismo. Un
`human_override` deja las 11 columnas D38 de `trajectory_steps` NULL como
grupo (`BattleRepository.save_step`); la propuesta DESCARTADA del modelo
permanece COMPLETA aca -- el costo real de una batalla con overrides es
`trajectory_steps` (la accion ejecutada) MAS `pending_decisions` (lo que el
LLM propuso y el humano piso).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ludex_agent.hitl import ApprovalKey, ApprovalProposal, ApprovalResolution


@dataclass(frozen=True)
class PendingDecisionRecord:
    """La propuesta que `insert_awaiting` persiste, JSON-safe (Task 2).

    `proposal` es el `ApprovalProposal` completo del dominio HITL puro
    (`action`, `legal_actions`, `model_envelope` -- el envelope D38 entero
    como un solo dict); `status` arranca siempre en `"awaiting"`.
    """

    key: ApprovalKey
    proposal: ApprovalProposal
    status: Literal["awaiting"] = "awaiting"


_INSERT_AWAITING_SQL = text("""
    INSERT INTO pending_decisions
      (battle_tag, decision_index, attempt_index, status, action,
       legal_actions, model_envelope)
    VALUES (:battle_tag, :decision_index, :attempt_index, :status,
            CAST(:action AS jsonb), CAST(:legal_actions AS jsonb),
            CAST(:model_envelope AS jsonb))
""")

_RESOLVE_SQL = text("""
    UPDATE pending_decisions
    SET status = :status,
        resolved_action = CAST(:resolved_action AS jsonb),
        resolved_by = :resolved_by,
        resolved_reason = :resolved_reason,
        approval_wait_ms = :approval_wait_ms,
        resolved_at = now()
    WHERE battle_tag = :battle_tag
      AND decision_index = :decision_index
      AND attempt_index = :attempt_index
    RETURNING battle_tag
""")

_ABORT_STALE_SQL = text("""
    UPDATE pending_decisions
    SET status = 'aborted',
        resolved_by = 'system',
        resolved_reason = :reason,
        resolved_at = now()
    WHERE status = 'awaiting'
    RETURNING battle_tag
""")


class PendingDecisionNotFoundError(LookupError):
    """`resolve()` no encontro ninguna fila para esta clave.

    `resolve()` nunca crea filas: `insert_awaiting()` es el unico camino de
    alta (la fila `awaiting` se persiste ANTES de publicar la propuesta,
    D65). Si esto se lanza, el caller invoco `resolve()` sin haber llamado
    `insert_awaiting()` primero para la misma clave -- un bug de
    integracion, no un caso esperado en produccion."""

    def __init__(self, key: ApprovalKey) -> None:
        super().__init__(
            f"no existe pending_decisions para battle_tag={key.battle_tag!r} "
            f"decision_index={key.decision_index} attempt_index={key.attempt_index}: "
            "insert_awaiting() no se llamo antes de resolve()"
        )
        self.key = key


class PendingDecisionRepository:
    """El engine vive dentro del loop donde corre `POKE_LOOP`.

    Ver el docstring del modulo: crear el engine en el primer `await`
    garantiza que su pool bindea al loop correcto, igual que
    `PostgresContextRepository`.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._engine = None
        self._factory = None

    def _ensure_factory(self) -> Any:
        if self._factory is None:
            # NullPool: mismo motivo que PostgresContextRepository -- este
            # repositorio puede correr en el loop del listener de poke-env,
            # distinto del loop donde se construyo. NullPool no retiene
            # conexiones entre sesiones, asi que dispose() nunca necesita
            # cerrar un Future de otro loop.
            from sqlalchemy.pool import NullPool

            self._engine = create_async_engine(
                self._database_url, poolclass=NullPool,
            )
            self._factory = async_sessionmaker(
                self._engine, expire_on_commit=False,
            )
        return self._factory

    async def aclose(self) -> None:
        engine = self._engine
        self._engine = None
        self._factory = None
        if engine is not None:
            await engine.dispose()

    async def insert_awaiting(self, proposal: PendingDecisionRecord) -> None:
        factory = self._ensure_factory()
        async with factory() as session:
            await session.execute(_INSERT_AWAITING_SQL, {
                "battle_tag": proposal.key.battle_tag,
                "decision_index": proposal.key.decision_index,
                "attempt_index": proposal.key.attempt_index,
                "status": proposal.status,
                "action": json.dumps(proposal.proposal.action),
                "legal_actions": json.dumps(list(proposal.proposal.legal_actions)),
                "model_envelope": json.dumps(proposal.proposal.model_envelope),
            })
            await session.commit()

    async def resolve(
        self,
        key: ApprovalKey,
        resolution: ApprovalResolution,
        approval_wait_ms: int,
    ) -> None:
        factory = self._ensure_factory()
        async with factory() as session:
            result = await session.execute(_RESOLVE_SQL, {
                "battle_tag": key.battle_tag,
                "decision_index": key.decision_index,
                "attempt_index": key.attempt_index,
                "status": resolution.outcome,
                "resolved_action": json.dumps(resolution.action),
                "resolved_by": resolution.resolved_by,
                "resolved_reason": resolution.resolved_reason,
                "approval_wait_ms": approval_wait_ms,
            })
            if result.first() is None:
                await session.rollback()
                raise PendingDecisionNotFoundError(key)
            await session.commit()

    async def abort_stale(self, reason: str = "process_restart") -> int:
        """Sweep de arranque (D65): toda fila huerfana `awaiting` pasa a
        `aborted`. Devuelve la cantidad de filas afectadas -- `0` es un
        resultado legitimo (nada quedo colgado), no un error."""
        factory = self._ensure_factory()
        async with factory() as session:
            result = await session.execute(_ABORT_STALE_SQL, {"reason": reason})
            rows = result.all()
            await session.commit()
            return len(rows)
