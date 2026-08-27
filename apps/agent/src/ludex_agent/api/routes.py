"""Router REST de la API de control (spec Fase 3 S7.1, D65 MON-33 Task 4).

Superficie minima que ejerce Task 4: `/health`, seleccion de modelo
(`ModelRepository`, F2-09: nunca expone valores de API key), la decision
pendiente y su resolucion (`ApprovalRegistry`/gate exact-once, Task 2) y una
lectura historica minima (`ApiReadRepository`). El resto de la superficie de
`spec S7.1` (challenges, ladder, conexion oficial, sesiones) pertenece a
S5/S6 y no se declara aca.

El estado de una decision pendiente sale EXCLUSIVAMENTE del
`ApprovalRegistry` inyectado (`request.app.state.registry`); esta capa nunca
importa ni usa `PendingDecisionRepository` (Task 3), que es auditoria
exclusiva de `POKE_LOOP`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from ..db.model_repository import ModelSelectionError
from ..hitl.gate import AlreadyResolved, ApprovalResolution, IllegalOverrideError
from ..hitl.registry import ApprovalRegistry, StaleAttemptError, UnknownDecisionError
from .schemas import (
    BattleSummaryResponse,
    DecisionAttemptRequest,
    ModelSelectionRequest,
    ModelSelectionResponse,
    OverrideRequest,
    PendingDecisionResponse,
    ResolutionResponse,
)


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
        repo = request.app.state.historical_repo_factory()
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

    return router
