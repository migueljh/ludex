"""Modelos Pydantic de request/response de la API de control (spec Fase 3
S7.1, D65 MON-33 Task 4).

Nunca modelan secretos: `ModelRepository` (F2-09) ya excluye valores de API
key de toda fila que expone, y estos schemas heredan esa restriccion sin
agregar ningun campo nuevo que pudiera filtrarlos.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DecisionAttemptRequest(BaseModel):
    attempt_index: int = Field(ge=0)


class OverrideRequest(DecisionAttemptRequest):
    action: dict[str, object]


class ResolutionResponse(BaseModel):
    outcome: Literal["human_approved", "human_override", "timeout_auto"]
    action: dict[str, object]
    resolved_by: Literal["operator", "timer", "system"]
    resolved_reason: str | None = None


class ModelSelectionRequest(BaseModel):
    provider: str
    model: str


class ModelSelectionResponse(BaseModel):
    provider: str
    model: str


class ApprovalModeRequest(BaseModel):
    approval_mode: Literal["hitl", "autonomous"]


class SettingsResponse(BaseModel):
    active_model: ModelSelectionResponse | None
    approval_mode: Literal["hitl", "autonomous"] | None


class ProviderSummaryResponse(BaseModel):
    """Solo `name` y `enabled`: nunca `base_url` (puede embeder
    credenciales) ni `api_key_env` (nombra la variable del secreto)."""

    name: str
    enabled: bool


class BattleSummaryResponse(BaseModel):
    battle_tag: str
    format: str
    p1: str
    p2: str
    winner: str | None
    played_by: str
    source: str


class PendingDecisionResponse(BaseModel):
    attempt_index: int
    action: dict[str, object]
    legal_actions: list[dict[str, object]]


class ChallengeResponse(BaseModel):
    """Un challenge entrante conocido (Fase 3 Task 7, D65 S5/S7.1)."""

    user: str
    format: str


class ChallengeActionResponse(BaseModel):
    user: str
    action: Literal["accept", "reject", "outgoing"]


class OutgoingChallengeRequest(BaseModel):
    user: str
    format: str


class SessionRequest(BaseModel):
    """Solicitud de sesion (Fase 3 Task 8, D65 S6.2/S7.1).

    `confirm` es el interlock por llamada: sin `confirm=true` el ladder se
    rechaza antes de abrir socket o enviar `/search`, por mas que el resto
    de los interlocks esten abiertos. Fail-closed: nunca hay un default que
    lo habilite.
    """

    n_battles: int = Field(default=1, ge=1)
    confirm: bool = False
