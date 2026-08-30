"""Router REST de la API de control (spec Fase 3 S7.1, D65/D66 MON-33
Task 4).

Superficie de Task 4 (S3): `/health`, la seleccion de modelo y los settings
durables (`ModelRepository`, F2-09: nunca expone valores de API key),
providers y modelos, la decision pendiente y su resolucion
(`ApprovalRegistry`/gate exact-once, Task 2) y las lecturas historicas
(`ApiReadRepository` via el provider memoizado de `app.state`, D66 T-03).
El resto de la superficie de `spec S7.1` (connection, sessions) pertenece a
S4/Task 6 y challenges a S5/Task 7 (asignacion vinculante D66).

El estado de una decision pendiente sale EXCLUSIVAMENTE del
`ApprovalRegistry` inyectado (`request.app.state.registry`); esta capa nunca
importa ni usa `PendingDecisionRepository` (Task 3), que es auditoria
exclusiva de `POKE_LOOP`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from ..config import load_settings
from ..db.model_repository import ModelSelectionError
from ..hitl.gate import AlreadyResolved, ApprovalResolution, IllegalOverrideError
from ..hitl.registry import ApprovalRegistry, StaleAttemptError, UnknownDecisionError
from ..showdown.connection import ConnectionManager, UnsafeOfficialDatabaseError
from .schemas import (
    ApprovalModeRequest,
    BattleSummaryResponse,
    DecisionAttemptRequest,
    ModelSelectionRequest,
    ModelSelectionResponse,
    OverrideRequest,
    PendingDecisionResponse,
    ProviderSummaryResponse,
    ResolutionResponse,
    SettingsResponse,
)

# D66: `approval_mode` se persiste en la tabla `settings` con esta key,
# como JSON string (`"hitl"` | `"autonomous"`) -- mismo store que
# `active_model` (F2-09). El consumidor en el camino vivo (Task 5) tiene
# que leer esta key al construir la politica inyectable.
_APPROVAL_MODE_KEY = "approval_mode"


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
        "id": None, "n_battles": None, "active": False, "stop_requested": False,
    }

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
        settings = load_settings()
        manager = ConnectionManager(settings=settings)
        try:
            manager.build_server_configuration()
        except UnsafeOfficialDatabaseError as exc:
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

    @router.post("/sessions")
    async def create_session(payload: dict[str, int]) -> dict[str, object]:
        if _session_state["active"]:
            raise HTTPException(
                status_code=409, detail={"error": "ACTIVE_MATCHMAKING"},
            )
        n_battles = payload.get("n_battles", 1)
        _session_state.update(
            id="session-1", n_battles=n_battles, active=True, stop_requested=False,
        )
        return dict(_session_state)

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, object]:
        if _session_state["id"] != session_id:
            raise HTTPException(status_code=404, detail={"error": "UNKNOWN_SESSION"})
        # Stop-after-current (spec 7.1): jamas cancela la batalla en curso,
        # solo impide que arranque la siguiente.
        _session_state["stop_requested"] = True
        return dict(_session_state)

    return router
