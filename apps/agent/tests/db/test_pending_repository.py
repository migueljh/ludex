"""D65 (MON-31/Fase 3 S2): `PendingDecisionRepository`, auditoria durable del
gate exact-once.

MON-11 (E/R2): `TEST_DATABASE_URL`, nunca `DATABASE_URL` -- este archivo
nunca corre contra la base compartida. Cada test recibe una base
descartable nueva via la fixture `test_database_url` (ver conftest.py /
_disposable.py).
"""

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from _disposable import verified_engine
from ludex_agent.db.pending_repository import (
    PendingDecisionNotFoundError,
    PendingDecisionRecord,
    PendingDecisionRepository,
)
from ludex_agent.db.session import session_factory
from ludex_agent.hitl import ApprovalKey, ApprovalProposal, ApprovalResolution

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)",
)

_MODEL_ENVELOPE = {
    "rationale": "el rival esta en rango de OHKO, priorizo el golpe",
    "confidence": 0.87,
    "alternatives": [{"kind": "switch", "species": "charizard"}],
    "provider": "google",
    "model": "gemini-2.5-flash",
    "decision_latency_ms": 812.5,
    "input_tokens": 400,
    "output_tokens": 60,
    "cached_input_tokens": 100,
    "reasoning_tokens": 0,
    "target": None,
}

_LEGAL_ACTIONS = [
    {"kind": "move", "id": "shadowball"},
    {"kind": "switch", "species": "charizard"},
]


def _proposal(action: dict | None = None) -> ApprovalProposal:
    return ApprovalProposal(
        action=action or {"kind": "move", "id": "shadowball"},
        legal_actions=_LEGAL_ACTIONS,
        model_envelope=_MODEL_ENVELOPE,
    )


def _key(battle_tag: str = "battle-test-pending-1", decision_index: int = 0,
         attempt_index: int = 0) -> ApprovalKey:
    return ApprovalKey(
        battle_tag=battle_tag, decision_index=decision_index,
        attempt_index=attempt_index,
    )


# loop_scope="function" explicito: ver el mismo comentario en
# test_repository.py -- el default del proyecto es scope module.
@pytest_asyncio.fixture(loop_scope="function")
async def repo(test_database_url):
    pending = PendingDecisionRepository(test_database_url)
    yield pending
    await pending.aclose()


@pytest_asyncio.fixture(loop_scope="function")
async def reader(test_database_url):
    engine = await verified_engine(test_database_url)
    yield session_factory(engine)
    await engine.dispose()


async def test_insert_awaiting_persiste_fila_completa(repo, reader):
    key = _key()
    proposal = _proposal()
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=proposal))

    async with reader() as s:
        fila = (await s.execute(text("""
            SELECT status, action, legal_actions, model_envelope,
                   resolved_action, resolved_by, resolved_reason,
                   approval_wait_ms, resolved_at
            FROM pending_decisions
            WHERE battle_tag=:bt AND decision_index=:di AND attempt_index=:ai
        """), {"bt": key.battle_tag, "di": key.decision_index, "ai": key.attempt_index})).one()

    assert fila[0] == "awaiting"
    assert fila[1] == proposal.action
    assert fila[2] == list(proposal.legal_actions)
    assert fila[3] == proposal.model_envelope
    assert fila[4] is None
    assert fila[5] is None
    assert fila[6] is None
    assert fila[7] is None
    assert fila[8] is None


async def test_insert_awaiting_dos_intentos_de_la_misma_decision_son_filas_distintas(repo, reader):
    """La clave incluye `attempt_index`: un reintento tras un rechazo de
    Showdown (misma battle_tag/decision_index) es una fila propia, nunca
    pisa a la anterior."""
    await repo.insert_awaiting(PendingDecisionRecord(
        key=_key(attempt_index=0), proposal=_proposal({"kind": "move", "id": "a"}),
    ))
    await repo.insert_awaiting(PendingDecisionRecord(
        key=_key(attempt_index=1), proposal=_proposal({"kind": "move", "id": "b"}),
    ))

    async with reader() as s:
        filas = (await s.execute(text(
            "SELECT attempt_index, action->>'id' FROM pending_decisions "
            "WHERE battle_tag=:bt ORDER BY attempt_index"
        ), {"bt": _key().battle_tag})).all()
    assert [tuple(f) for f in filas] == [(0, "a"), (1, "b")]


async def test_insert_awaiting_misma_clave_dos_veces_revienta(repo):
    key = _key()
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=_proposal()))
    with pytest.raises(IntegrityError):
        await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=_proposal()))


async def test_resolve_human_approved_actualiza_status_y_resolucion_sin_tocar_la_propuesta(repo, reader):
    key = _key()
    proposal = _proposal()
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=proposal))

    resolution = ApprovalResolution(
        outcome="human_approved", action=proposal.action, resolved_by="operator",
    )
    await repo.resolve(key, resolution, approval_wait_ms=1500)

    async with reader() as s:
        fila = (await s.execute(text("""
            SELECT status, action, resolved_action, resolved_by, resolved_reason,
                   approval_wait_ms, resolved_at IS NOT NULL
            FROM pending_decisions
            WHERE battle_tag=:bt AND decision_index=:di AND attempt_index=:ai
        """), {"bt": key.battle_tag, "di": key.decision_index, "ai": key.attempt_index})).one()

    assert fila[0] == "human_approved"
    assert fila[1] == proposal.action, "la propuesta original no se toca al resolver"
    assert fila[2] == proposal.action
    assert fila[3] == "operator"
    assert fila[4] is None
    assert float(fila[5]) == 1500
    assert fila[6] is True


async def test_resolve_human_override_conserva_la_propuesta_descartada_completa(repo, reader):
    """D65: el costo de un override se calcula con `trajectory_steps` MAS
    `pending_decisions` -- la propuesta que el LLM hizo y el humano piso
    tiene que seguir completa (accion, legal_actions, model_envelope), no
    reemplazada por la accion que gano."""
    key = _key()
    proposta_llm = _proposal({"kind": "move", "id": "shadowball"})
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=proposta_llm))

    accion_humana = {"kind": "switch", "species": "charizard"}
    resolution = ApprovalResolution(
        outcome="human_override", action=accion_humana, resolved_by="operator",
        resolved_reason="rival en rango de setup, mejor cambiar",
    )
    await repo.resolve(key, resolution, approval_wait_ms=4200)

    async with reader() as s:
        fila = (await s.execute(text("""
            SELECT status, action, model_envelope, resolved_action, resolved_by,
                   resolved_reason, approval_wait_ms
            FROM pending_decisions
            WHERE battle_tag=:bt AND decision_index=:di AND attempt_index=:ai
        """), {"bt": key.battle_tag, "di": key.decision_index, "ai": key.attempt_index})).one()

    assert fila[0] == "human_override"
    assert fila[1] == proposta_llm.action, "la propuesta ORIGINAL del LLM sigue completa"
    assert fila[2] == proposta_llm.model_envelope, "el envelope D38 del LLM no se pierde"
    assert fila[3] == accion_humana, "resolved_action es la que efectivamente gano"
    assert fila[4] == "operator"
    assert fila[5] == "rival en rango de setup, mejor cambiar"
    assert float(fila[6]) == 4200


async def test_resolve_timeout_auto_resolved_by_timer(repo, reader):
    key = _key()
    proposal = _proposal()
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=proposal))

    resolution = ApprovalResolution(
        outcome="timeout_auto", action=proposal.action, resolved_by="timer",
    )
    await repo.resolve(key, resolution, approval_wait_ms=10_000)

    async with reader() as s:
        fila = (await s.execute(text(
            "SELECT status, resolved_by FROM pending_decisions "
            "WHERE battle_tag=:bt AND decision_index=:di AND attempt_index=:ai"
        ), {"bt": key.battle_tag, "di": key.decision_index, "ai": key.attempt_index})).one()
    assert tuple(fila) == ("timeout_auto", "timer")


async def test_resolve_sin_insert_previo_revienta(repo):
    key = _key(battle_tag="battle-nunca-insertada")
    resolution = ApprovalResolution(
        outcome="human_approved", action={"kind": "move", "id": "x"},
        resolved_by="operator",
    )
    with pytest.raises(PendingDecisionNotFoundError) as exc:
        await repo.resolve(key, resolution, approval_wait_ms=100)
    assert exc.value.key == key


async def test_abort_stale_cambia_solo_awaiting_y_devuelve_el_conteo(repo, reader):
    huerfana_1 = _key(battle_tag="battle-huerfana-1")
    huerfana_2 = _key(battle_tag="battle-huerfana-2")
    resuelta = _key(battle_tag="battle-ya-resuelta")

    for key in (huerfana_1, huerfana_2, resuelta):
        await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=_proposal()))
    await repo.resolve(
        resuelta,
        ApprovalResolution(outcome="human_approved", action={"kind": "move", "id": "x"},
                          resolved_by="operator"),
        approval_wait_ms=50,
    )

    afectadas = await repo.abort_stale()

    assert afectadas == 2
    async with reader() as s:
        estados = dict((await s.execute(text(
            "SELECT battle_tag, status FROM pending_decisions"
        ))).all())
    assert estados[huerfana_1.battle_tag] == "aborted"
    assert estados[huerfana_2.battle_tag] == "aborted"
    assert estados[resuelta.battle_tag] == "human_approved", (
        "abort_stale no debe tocar una fila ya resuelta"
    )


async def test_abort_stale_reason_por_defecto_y_explicito(repo, reader):
    key = _key()
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=_proposal()))
    await repo.abort_stale()

    async with reader() as s:
        fila = (await s.execute(text(
            "SELECT resolved_reason, resolved_by FROM pending_decisions WHERE battle_tag=:bt"
        ), {"bt": key.battle_tag})).one()
    assert fila[0] == "process_restart"
    assert fila[1] == "system"


async def test_abort_stale_sin_filas_awaiting_devuelve_cero(repo):
    assert await repo.abort_stale() == 0


# --- CHECKs de la migracion (mutados deliberadamente para probar RED, ver
# REVIEW PACKET) ------------------------------------------------------------

async def test_status_check_rechaza_valor_fuera_del_enum_cerrado(repo, reader):
    async with reader() as s:
        with pytest.raises(DBAPIError, match="pending_decisions_status_check") as exc:
            # resolved_at=now() satisface aparte el check de
            # awaiting_has_no_resolution (status<>'awaiting' => resolved_at
            # no nulo) para aislar el CHECK que este test quiere ejercer.
            await s.execute(text("""
                INSERT INTO pending_decisions
                  (battle_tag, decision_index, attempt_index, status, action,
                   legal_actions, model_envelope, resolved_at)
                VALUES ('battle-x', 0, 0, 'inventado', '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, now())
            """))
        assert exc.value.orig.sqlstate == "23514"


async def test_awaiting_has_no_resolution_check_rechaza_awaiting_con_resolved_at(repo, reader):
    async with reader() as s:
        with pytest.raises(
            DBAPIError, match="pending_decisions_awaiting_has_no_resolution_check"
        ) as exc:
            await s.execute(text("""
                INSERT INTO pending_decisions
                  (battle_tag, decision_index, attempt_index, status, action,
                   legal_actions, model_envelope, resolved_at)
                VALUES ('battle-x', 0, 0, 'awaiting', '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, now())
            """))
        assert exc.value.orig.sqlstate == "23514"


async def test_resolution_action_check_rechaza_resuelta_sin_resolved_action(repo, reader):
    async with reader() as s:
        with pytest.raises(
            DBAPIError, match="pending_decisions_resolution_action_check"
        ) as exc:
            await s.execute(text("""
                INSERT INTO pending_decisions
                  (battle_tag, decision_index, attempt_index, status, action,
                   legal_actions, model_envelope, resolved_by, resolved_at)
                VALUES ('battle-x', 0, 0, 'human_approved', '{}'::jsonb, '[]'::jsonb,
                        '{}'::jsonb, 'operator', now())
            """))
        assert exc.value.orig.sqlstate == "23514"


async def test_resolved_by_check_rechaza_valor_fuera_del_enum(repo, reader):
    async with reader() as s:
        with pytest.raises(
            DBAPIError, match="pending_decisions_resolved_by_check"
        ) as exc:
            await s.execute(text("""
                INSERT INTO pending_decisions
                  (battle_tag, decision_index, attempt_index, status, action,
                   legal_actions, model_envelope, resolved_action, resolved_by, resolved_at)
                VALUES ('battle-x', 0, 0, 'human_approved', '{}'::jsonb, '[]'::jsonb,
                        '{}'::jsonb, '{}'::jsonb, 'quien-sabe', now())
            """))
        assert exc.value.orig.sqlstate == "23514"


async def test_legal_actions_type_check_rechaza_no_array(repo, reader):
    async with reader() as s:
        with pytest.raises(
            DBAPIError, match="pending_decisions_legal_actions_type_check"
        ) as exc:
            await s.execute(text("""
                INSERT INTO pending_decisions
                  (battle_tag, decision_index, attempt_index, action, legal_actions, model_envelope)
                VALUES ('battle-x', 0, 0, '{}'::jsonb, '{"a": 1}'::jsonb, '{}'::jsonb)
            """))
        assert exc.value.orig.sqlstate == "23514"


async def test_model_envelope_type_check_rechaza_no_object(repo, reader):
    async with reader() as s:
        with pytest.raises(
            DBAPIError, match="pending_decisions_model_envelope_type_check"
        ) as exc:
            await s.execute(text("""
                INSERT INTO pending_decisions
                  (battle_tag, decision_index, attempt_index, action, legal_actions, model_envelope)
                VALUES ('battle-x', 0, 0, '{}'::jsonb, '[]'::jsonb, '["no es objeto"]'::jsonb)
            """))
        assert exc.value.orig.sqlstate == "23514"


async def test_approval_wait_ms_negativo_viola_check(repo, reader):
    key = _key()
    proposal = _proposal()
    await repo.insert_awaiting(PendingDecisionRecord(key=key, proposal=proposal))
    async with reader() as s:
        with pytest.raises(
            DBAPIError, match="pending_decisions_approval_wait_ms_check"
        ) as exc:
            await s.execute(text("""
                UPDATE pending_decisions SET approval_wait_ms = -1
                WHERE battle_tag=:bt AND decision_index=:di AND attempt_index=:ai
            """), {"bt": key.battle_tag, "di": key.decision_index, "ai": key.attempt_index})
        assert exc.value.orig.sqlstate == "23514"
