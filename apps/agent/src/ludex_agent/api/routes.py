"""Router REST de la API de control (spec Fase 3 S7.1, D65/D66 MON-33
Task 4).

Superficie de Task 4 (S3): `/health`, la seleccion de modelo y los settings
durables (`ModelRepository`, F2-09: nunca expone valores de API key),
providers y modelos, la decision pendiente y su resolucion
(`ApprovalRegistry`/gate exact-once, Task 2) y las lecturas historicas
(`ApiReadRepository` via el provider memoizado de `app.state`, D66 T-03).
El resto de la superficie de `spec S7.1` (connection, sessions) pertenece a
S4/Task 6. Challenges (S5/Task 7, D65/D66) esta ACA: `GET /challenges` y
`POST /challenges/{user}/accept|reject|outgoing` leen y mutan el
`ChallengeGateway` inyectado en `app.state.challenge_gateway`
(`InMemoryChallengeGateway` por defecto, mismo patron sin conexion real que
`_connection_state`/`_session_state` de Task 6 -- wirear un gateway
respaldado por un `LudexPlayer` vivo es S9a, fuera de esta rebanada).

El estado de una decision pendiente sale EXCLUSIVAMENTE del
`ApprovalRegistry` inyectado (`request.app.state.registry`); esta capa nunca
importa ni usa `PendingDecisionRepository` (Task 3), que es auditoria
exclusiva de `POKE_LOOP`.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from ..config import load_settings
from ..db.model_repository import ModelSelectionError
from ..hitl.gate import AlreadyResolved, ApprovalResolution, IllegalOverrideError
from ..hitl.registry import ApprovalRegistry, StaleAttemptError, UnknownDecisionError
from ..runner.session import (
    LadderGates,
    LadderInterlockError,
    SessionKind,
    SessionRunner,
    check_ladder_interlocks,
)
from ..showdown.challenge_gateway import UnknownChallengeError
from ..showdown.connection import ConnectionManager
from .schemas import (
    ApprovalModeRequest,
    BattleSummaryResponse,
    ChallengeActionResponse,
    ChallengeResponse,
    DecisionAttemptRequest,
    ModelSelectionRequest,
    ModelSelectionResponse,
    OutgoingChallengeRequest,
    OverrideRequest,
    PendingDecisionResponse,
    ProviderSummaryResponse,
    ResolutionResponse,
    SessionRequest,
    SettingsResponse,
)

# D66: `approval_mode` se persiste en la tabla `settings` con esta key,
# como JSON string (`"hitl"` | `"autonomous"`) -- mismo store que
# `active_model` (F2-09). El consumidor en el camino vivo (Task 5) tiene
# que leer esta key al construir la politica inyectable.
_APPROVAL_MODE_KEY = "approval_mode"

# Task 8 (D65 S6.2): los interlocks durables del ladder viven en la MISMA
# tabla `settings` (store F2-09) que `active_model`/`approval_mode`. Su
# ausencia se lee como `False` (fail-closed): sin fila no hay permiso.
_LADDER_ENABLED_KEY = "ladder_enabled"
_TESTING_ACCOUNT_CONFIRMED_KEY = "testing_account_confirmed"

# El formato de aceptacion es un parametro de configuracion (generacion como
# parametro, nunca un literal en produccion). Sin esta variable el interlock
# de formato falla cerrado (required_format="" nunca matchea).
_LADDER_ACCEPTANCE_FORMAT_ENV = "LADDER_ACCEPTANCE_FORMAT"

_SESSION_RESPONSE_KEYS = ("id", "n_battles", "active", "stop_requested", "source")


def _resolution_response(resolution: ApprovalResolution) -> ResolutionResponse:
    return ResolutionResponse(
        outcome=resolution.outcome,
        action=resolution.action,
        resolved_by=resolution.resolved_by,
        resolved_reason=resolution.resolved_reason,
    )


def _resolve(
    request: Request,
    action: Callable[[ApprovalRegistry], ApprovalResolution],
    *,
    battle_tag: str,
    decision_index: int,
) -> ResolutionResponse:
    registry: ApprovalRegistry = request.app.state.registry
    try:
        resolution = action(registry)
    except UnknownDecisionError as exc:
        raise HTTPException(status_code=404, detail={"error": "UNKNOWN_DECISION"}) from exc
    except StaleAttemptError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "STALE_ATTEMPT", "current_attempt": exc.current_attempt},
        ) from exc
    except AlreadyResolved as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "ALREADY_RESOLVED", "outcome": exc.winner.outcome},
        ) from exc
    except IllegalOverrideError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "ILLEGAL_OVERRIDE"},
        ) from exc
    request.app.state.event_hub.publish(
        f"battle:{battle_tag}",
        {
            "type": "decision_resolved",
            "decision_index": decision_index,
            "outcome": resolution.outcome,
            "resolved_by": resolution.resolved_by,
        },
    )
    return _resolution_response(resolution)


def create_router() -> APIRouter:
    router = APIRouter()

    # D66 T-02 (asignacion vinculante): connection/sessions es dominio de
    # Task 6. Estado en memoria del router, sin tabla ni columna nueva; el
    # `ConnectionManager` se construye por request desde `load_settings()`
    # (app.py no se toca en esta rebanada) para heredar el guardarraiel
    # mode-aware (D65 S5.4) antes de abrir cualquier socket.
    _connection_state: dict[str, object] = {"connected": False, "mode": None}
    _session_state: dict[str, object | None] = {
        "id": None,
        "n_battles": None,
        "active": False,
        "stop_requested": False,
        "source": None,
    }
    # Task 8: el runner/task vivos detras de la sesion de ladder. No se
    # exponen en las respuestas (`_session_response` filtra las keys).
    _session_live: dict[str, object | None] = {"runner": None, "task": None}

    def _session_response() -> dict[str, object]:
        return {key: _session_state[key] for key in _SESSION_RESPONSE_KEYS}

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/settings/model")
    async def get_model(request: Request) -> ModelSelectionResponse | None:
        repo = request.app.state.settings_repo
        selection = await repo.active_selection()
        if selection is None:
            return None
        return ModelSelectionResponse(
            provider=selection.provider_name, model=selection.model_id,
        )

    @router.patch("/settings/model")
    async def set_model(
        payload: ModelSelectionRequest, request: Request,
    ) -> ModelSelectionResponse:
        repo = request.app.state.settings_repo
        try:
            await repo.validate_selection(payload.provider, payload.model)
        except ModelSelectionError as exc:
            raise HTTPException(
                status_code=422, detail={"error": "INVALID_MODEL_SELECTION"},
            ) from exc
        await repo.set_active(payload.provider, payload.model)
        return ModelSelectionResponse(provider=payload.provider, model=payload.model)

    async def _settings_response(repo) -> SettingsResponse:
        selection = await repo.active_selection()
        async with repo.factory() as s:
            row = (await s.execute(text(
                "SELECT value FROM settings WHERE key = :key"
            ), {"key": _APPROVAL_MODE_KEY})).first()
        mode = row[0] if row is not None else None
        return SettingsResponse(
            active_model=(
                ModelSelectionResponse(
                    provider=selection.provider_name, model=selection.model_id,
                ) if selection is not None else None
            ),
            approval_mode=mode,
        )

    @router.get("/settings")
    async def get_settings(request: Request) -> SettingsResponse:
        return await _settings_response(request.app.state.settings_repo)

    @router.patch("/settings/hitl")
    async def set_settings_hitl(
        payload: ApprovalModeRequest, request: Request,
    ) -> SettingsResponse:
        repo = request.app.state.settings_repo
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO settings (key, value)
                VALUES (:key, CAST(:value AS jsonb))
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """), {
                "key": _APPROVAL_MODE_KEY,
                "value": json.dumps(payload.approval_mode),
            })
            await s.commit()
        return await _settings_response(repo)

    @router.get("/providers")
    async def list_providers(request: Request) -> list[ProviderSummaryResponse]:
        repo = request.app.state.settings_repo
        async with repo.factory() as s:
            rows = (await s.execute(text(
                "SELECT name, enabled FROM providers ORDER BY name"
            ))).all()
        return [ProviderSummaryResponse(name=row[0], enabled=row[1]) for row in rows]

    @router.get("/models")
    async def list_models(
        request: Request, provider: str | None = None,
    ) -> list[ModelSelectionResponse]:
        rows = await request.app.state.settings_repo.list_models(provider)
        return [
            ModelSelectionResponse(provider=row.provider_name, model=row.model_id)
            for row in rows
        ]

    @router.get("/battles")
    async def list_battles(
        request: Request, limit: int = 50,
    ) -> list[BattleSummaryResponse]:
        repo = request.app.state.historical_repo_provider.get_repo()
        battles = await repo.list_recent_battles(limit=limit)
        return [BattleSummaryResponse(**asdict(battle)) for battle in battles]

    @router.get("/battles/{battle_tag}/pending")
    async def get_pending(
        battle_tag: str, decision_index: int, request: Request,
    ) -> PendingDecisionResponse:
        registry: ApprovalRegistry = request.app.state.registry
        pending = registry.get(battle_tag, decision_index)
        if pending is None:
            raise HTTPException(status_code=404, detail={"error": "UNKNOWN_DECISION"})
        return PendingDecisionResponse(
            attempt_index=pending.key.attempt_index,
            action=pending.proposal.action,
            legal_actions=list(pending.proposal.legal_actions),
        )

    @router.get("/battles/{battle_tag}")
    async def get_battle(battle_tag: str, request: Request) -> BattleSummaryResponse:
        repo = request.app.state.historical_repo_provider.get_repo()
        battle = await repo.get_battle_by_tag(battle_tag)
        if battle is None:
            raise HTTPException(status_code=404, detail={"error": "BATTLE_NOT_FOUND"})
        return BattleSummaryResponse(**asdict(battle))

    @router.post("/battles/{battle_tag}/decisions/{decision_index}/approve")
    async def approve(
        battle_tag: str, decision_index: int,
        payload: DecisionAttemptRequest, request: Request,
    ) -> ResolutionResponse:
        return _resolve(
            request,
            lambda registry: registry.resolve_approved(
                battle_tag, decision_index, payload.attempt_index,
            ),
            battle_tag=battle_tag, decision_index=decision_index,
        )

    @router.post("/battles/{battle_tag}/decisions/{decision_index}/override")
    async def override(
        battle_tag: str, decision_index: int,
        payload: OverrideRequest, request: Request,
    ) -> ResolutionResponse:
        return _resolve(
            request,
            lambda registry: registry.resolve_override(
                battle_tag, decision_index, payload.attempt_index, payload.action,
            ),
            battle_tag=battle_tag, decision_index=decision_index,
        )

    @router.get("/connection")
    async def get_connection() -> dict[str, object]:
        return dict(_connection_state)

    @router.post("/connection/connect")
    async def connect_showdown() -> dict[str, object]:
        # T-03 (MON-36 R2): `load_settings()` YA levanta el guardarraiel de
        # DB insegura como `RuntimeError` plano (Task 1,
        # `_reject_unsafe_official_database`), ANTES de que exista un
        # `Settings` con el que construir `ConnectionManager`. Envolver solo
        # `build_server_configuration()` dejaba ese caso (el mas comun:
        # `CONNECTION_MODE=official` sin `DATABASE_ROLE=acceptance`) caer
        # sin capturar y responder 500 `INTERNAL_ERROR` en vez del 422
        # documentado. `UnsafeOfficialDatabaseError` es subclase de
        # `RuntimeError`, asi que un unico `except` cubre ambos origenes.
        try:
            settings = load_settings()
            manager = ConnectionManager(settings=settings)
            manager.build_server_configuration()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=422, detail={"error": "UNSAFE_OFFICIAL_DATABASE"},
            ) from exc
        _connection_state["connected"] = True
        _connection_state["mode"] = settings.connection_mode
        return dict(_connection_state)

    @router.post("/connection/disconnect")
    async def disconnect_showdown() -> dict[str, object]:
        _connection_state["connected"] = False
        _connection_state["mode"] = None
        return dict(_connection_state)

    async def _read_durable_flag(request: Request, key: str) -> bool:
        """Lee un flag booleano durable de `settings`; ausente = `False`.

        Fail-closed: la relectura ocurre por request (nunca se cachea), asi
        que deshabilitar ladder en `settings` bloquea la solicitud SIGUIENTE.
        """
        repo = request.app.state.settings_repo
        async with repo.factory() as s:
            row = (await s.execute(
                text("SELECT value FROM settings WHERE key = :key"),
                {"key": key},
            )).first()
        if row is None:
            return False
        return row[0] is True

    async def _run_ladder_session(runner: SessionRunner, n_battles: int) -> None:
        try:
            await runner.start(n_battles)
        finally:
            _session_state.update(active=False, id=None, stop_requested=False)
            _session_live.update(runner=None, task=None)

    @router.post("/sessions")
    async def create_session(
        payload: SessionRequest, request: Request,
    ) -> dict[str, object]:
        # T-08: la solicitud es una sesion de ladder. El slot de matchmaking
        # se protege ANTES de cualquier evaluacion: una corrida activa (o en
        # stop-after-current) rechaza una segunda con 409.
        if _session_state["active"]:
            raise HTTPException(
                status_code=409, detail={"error": "ACTIVE_MATCHMAKING"},
            )
        # Interlock de configuracion (D65 S5.4): `official` exige
        # DATABASE_ROLE=acceptance y DSN no canonico. `load_settings()` lo
        # rechaza como `RuntimeError` ANTES de abrir red; el resto de los
        # interlocks los evalua `check_ladder_interlocks`.
        try:
            settings = load_settings()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=422, detail={"error": "UNSAFE_OFFICIAL_DATABASE"},
            ) from exc
        required_format = os.environ.get(_LADDER_ACCEPTANCE_FORMAT_ENV, "")
        gates = LadderGates(
            connection_mode=settings.connection_mode,
            battle_format=settings.showdown_battle_format,
            required_format=required_format,
            database_role=settings.database_role,
            database_url=settings.database_url,
            ladder_enabled=await _read_durable_flag(request, _LADDER_ENABLED_KEY),
            confirm=payload.confirm,
            testing_account_confirmed=await _read_durable_flag(
                request, _TESTING_ACCOUNT_CONFIRMED_KEY,
            ),
        )
        try:
            check_ladder_interlocks(gates)
        except LadderInterlockError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "LADDER_INTERLOCK",
                    "missing": list(gates.missing_interlocks()),
                    "reason": str(exc),
                },
            ) from exc
        player = getattr(request.app.state, "ladder_player", None)
        if player is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "LADDER_INTERLOCK", "missing": ["player"]},
            )
        runner = SessionRunner(player=player, kind=SessionKind.LADDER, gates=gates)
        _session_state.update(
            id="session-1",
            n_battles=payload.n_battles,
            active=True,
            stop_requested=False,
            source=runner.source,
        )
        _session_live["runner"] = runner
        _session_live["task"] = asyncio.create_task(
            _run_ladder_session(runner, payload.n_battles)
        )
        return _session_response()

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, object]:
        if _session_state["id"] != session_id:
            raise HTTPException(status_code=404, detail={"error": "UNKNOWN_SESSION"})
        # T-08: stop-after-current REAL. `SessionRunner.stop()` marca
        # stop_requested y JAMAS cancela la batalla en curso; el slot de
        # matchmaking (`active`) lo libera el runner cuando TERMINA la corrida,
        # no esta ruta. Sin runner vivo (limite D68, surface sin player) no hay
        # batalla que proteger: se libera el slot de inmediato.
        runner = _session_live.get("runner")
        if runner is not None:
            await runner.stop()
        else:
            _session_state.update(stop_requested=True, active=False, id=None)
        _session_state["stop_requested"] = True
        return _session_response()

    @router.get("/challenges")
    async def list_challenges(request: Request) -> list[ChallengeResponse]:
        gateway = request.app.state.challenge_gateway
        incoming = await gateway.list_incoming()
        return [
            ChallengeResponse(user=user, format=format_)
            for user, format_ in sorted(incoming.items())
        ]

    @router.post("/challenges/{user}/accept")
    async def accept_challenge(user: str, request: Request) -> ChallengeActionResponse:
        gateway = request.app.state.challenge_gateway
        try:
            await gateway.accept(user)
        except UnknownChallengeError as exc:
            raise HTTPException(
                status_code=404, detail={"error": "UNKNOWN_CHALLENGE"},
            ) from exc
        return ChallengeActionResponse(user=user, action="accept")

    @router.post("/challenges/{user}/reject")
    async def reject_challenge(user: str, request: Request) -> ChallengeActionResponse:
        gateway = request.app.state.challenge_gateway
        try:
            await gateway.reject(user)
        except UnknownChallengeError as exc:
            raise HTTPException(
                status_code=404, detail={"error": "UNKNOWN_CHALLENGE"},
            ) from exc
        return ChallengeActionResponse(user=user, action="reject")

    @router.post("/challenges/outgoing")
    async def send_outgoing_challenge(
        payload: OutgoingChallengeRequest,
    ) -> ChallengeActionResponse:
        # S9b (ladder) y el envio saliente real contra un socket vivo quedan
        # fuera de esta rebanada; este endpoint solo cierra la superficie
        # documentada en spec S7.1 (`/challenges/outgoing`), mismo alcance
        # stub que `/connection/*` y `/sessions` en Task 6.
        return ChallengeActionResponse(user=payload.user, action="outgoing")

    return router
