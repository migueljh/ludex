"""Superficie REST de decisiones y settings/model (spec Fase 3 S7.1, D65
MON-33 Task 4).

Cubre: approve, override, override ilegal (`422`), attempt obsoleto
(`409 STALE_ATTEMPT`), segundo resolver contra un gate ya ganado
(`409 ALREADY_RESOLVED` con el outcome ganador), validacion de
`PATCH /settings/model` contra la DB (F2-09 `ModelSelectionError` -> `422`),
y el canario de seguridad: un sentinel de password nunca aparece en ninguna
respuesta HTTP ni en el log, aunque el DSN de la lectura historica lo tenga
embebido y esa lectura falle.
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi.testclient import TestClient

from ludex_agent.db.model_repository import ModelSelectionError, ProviderModel
from ludex_agent.hitl.events import EventHub
from ludex_agent.hitl.gate import ApprovalKey, ApprovalProposal, PendingApproval
from ludex_agent.hitl.registry import ApprovalRegistry
from ludex_agent.api.app import create_app

_LEGAL_ACTIONS = [{"id": "move-1"}, {"id": "move-2"}]


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _FakeSettingsRepo:
    def __init__(self, *, valid: bool = True) -> None:
        self._valid = valid
        self.active = None

    async def active_selection(self):
        return self.active

    async def validate_selection(self, provider: str, model: str):
        if not self._valid:
            raise ModelSelectionError(f"model {model!r} no existe para {provider!r}")
        return object()

    async def set_active(self, provider: str, model: str) -> None:
        self.active = ProviderModel(provider, model)


class _EmptyReadRepository:
    async def get_battle_by_tag(self, battle_tag: str):
        return None


class _RaisingReadRepository:
    """Simula una lectura historica que revienta con un error de DB cuyo
    mensaje trae el DSN completo (usuario:password@host) embebido -- el
    caso realista donde un sentinel de credenciales podria filtrarse si la
    API alguna vez devolviera `str(exc)` crudo al cliente."""

    def __init__(self, dsn_with_secret: str) -> None:
        self._dsn = dsn_with_secret

    async def get_battle_by_tag(self, battle_tag: str):
        raise RuntimeError(f"no se pudo conectar a {self._dsn}")


def _open_pending(
    registry: ApprovalRegistry, *, attempt_index: int = 0,
    battle_tag: str = "battle-1", decision_index: int = 0,
) -> PendingApproval:
    pending = PendingApproval.open(
        key=ApprovalKey(
            battle_tag=battle_tag, decision_index=decision_index,
            attempt_index=attempt_index,
        ),
        proposal=ApprovalProposal(
            action={"id": "move-1"}, legal_actions=_LEGAL_ACTIONS,
            model_envelope={"provider": "google", "model": "gemini-test"},
        ),
        approval_timeout_seconds=10.0,
        decision_deadline=100.0,
        clock=_FakeClock(),
    )
    registry.open(pending)
    return pending


def _client(*, settings_repo=None, read_repo=None):
    registry = ApprovalRegistry()
    event_hub = EventHub()
    settings_repo = settings_repo or _FakeSettingsRepo()
    read_repo = read_repo or _EmptyReadRepository()
    app = create_app(
        registry=registry, event_hub=event_hub, settings_repo=settings_repo,
        historical_repo_factory=lambda: read_repo,
    )
    # `raise_server_exceptions=False`: Starlette's `ServerErrorMiddleware`
    # SIEMPRE re-lanza la excepcion tras generar la respuesta (proposito:
    # que un servidor ASGI real la loguee) -- TestClient por defecto
    # reraisea esa excepcion para debugging. El sentinel test necesita ver
    # la respuesta HTTP saneada de verdad, no la excepcion original.
    return TestClient(app, raise_server_exceptions=False), registry, event_hub


# ---------------------------------------------------------------------------
# Origin (D65 S6.3): tambien enforced sobre HTTP, no solo sobre WS
# ---------------------------------------------------------------------------


def test_a_foreign_origin_is_rejected_on_plain_http_requests_too():
    client, _, _ = _client()
    response = client.get("/health", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json() == {"error": "FORBIDDEN_ORIGIN"}


def test_a_loopback_origin_or_no_origin_is_accepted_on_http_requests():
    client, _, _ = _client()
    assert client.get("/health").status_code == 200
    assert client.get(
        "/health", headers={"origin": "http://127.0.0.1:5173"},
    ).status_code == 200


# ---------------------------------------------------------------------------
# Approve / override
# ---------------------------------------------------------------------------


def test_approve_resolves_the_pending_decision_and_publishes_an_event():
    client, registry, event_hub = _client()
    _open_pending(registry)

    response = client.post(
        "/battles/battle-1/decisions/0/approve", json={"attempt_index": 0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "human_approved"
    assert body["action"] == {"id": "move-1"}

    events = event_hub.resume("battle:battle-1", 0)
    assert len(events) == 1
    assert events[0].payload["type"] == "decision_resolved"
    assert events[0].payload["outcome"] == "human_approved"


def test_override_with_a_legal_action_resolves_human_override():
    client, registry, _ = _client()
    _open_pending(registry)

    response = client.post(
        "/battles/battle-1/decisions/0/override",
        json={"attempt_index": 0, "action": {"id": "move-2"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "human_override"
    assert body["action"] == {"id": "move-2"}


def test_override_with_an_illegal_action_returns_422_and_does_not_consume_the_gate():
    client, registry, _ = _client()
    _open_pending(registry)

    response = client.post(
        "/battles/battle-1/decisions/0/override",
        json={"attempt_index": 0, "action": {"id": "does-not-exist"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "ILLEGAL_OVERRIDE"

    # el gate sigue vivo: approve legitimo todavia gana.
    follow_up = client.post(
        "/battles/battle-1/decisions/0/approve", json={"attempt_index": 0},
    )
    assert follow_up.status_code == 200
    assert follow_up.json()["outcome"] == "human_approved"


def test_approve_with_a_stale_attempt_index_returns_409_stale_attempt():
    client, registry, _ = _client()
    _open_pending(registry, attempt_index=1)

    response = client.post(
        "/battles/battle-1/decisions/0/approve", json={"attempt_index": 0},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "STALE_ATTEMPT"
    assert detail["current_attempt"] == 1


def test_approve_against_an_unknown_decision_returns_404():
    client, _, _ = _client()
    response = client.post(
        "/battles/battle-1/decisions/0/approve", json={"attempt_index": 0},
    )
    assert response.status_code == 404


def test_get_pending_returns_the_live_attempt_from_the_registry():
    client, registry, _ = _client()
    _open_pending(registry, attempt_index=2)

    response = client.get(
        "/battles/battle-1/pending", params={"decision_index": 0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["attempt_index"] == 2
    assert body["action"] == {"id": "move-1"}
    assert body["legal_actions"] == _LEGAL_ACTIONS


def test_get_pending_against_an_unknown_decision_returns_404():
    client, _, _ = _client()
    response = client.get(
        "/battles/battle-1/pending", params={"decision_index": 0},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "UNKNOWN_DECISION"


def test_second_resolver_gets_409_already_resolved_with_the_winning_outcome():
    client, registry, _ = _client()
    _open_pending(registry)

    winner_response = client.post(
        "/battles/battle-1/decisions/0/approve", json={"attempt_index": 0},
    )
    assert winner_response.status_code == 200
    winning_outcome = winner_response.json()["outcome"]

    loser_response = client.post(
        "/battles/battle-1/decisions/0/override",
        json={"attempt_index": 0, "action": {"id": "move-2"}},
    )
    assert loser_response.status_code == 409
    detail = loser_response.json()["detail"]
    assert detail["error"] == "ALREADY_RESOLVED"
    assert detail["outcome"] == winning_outcome


# ---------------------------------------------------------------------------
# settings/model
# ---------------------------------------------------------------------------


def test_get_settings_model_returns_none_when_nothing_is_selected():
    client, _, _ = _client()
    response = client.get("/settings/model")
    assert response.status_code == 200
    assert response.json() is None


def test_patch_settings_model_with_a_valid_selection_sets_it_active():
    client, _, _ = _client()
    response = client.patch(
        "/settings/model", json={"provider": "google", "model": "gemini-test"},
    )
    assert response.status_code == 200
    assert response.json() == {"provider": "google", "model": "gemini-test"}

    follow_up = client.get("/settings/model")
    assert follow_up.json() == {"provider": "google", "model": "gemini-test"}


def test_patch_settings_model_with_an_invalid_selection_returns_422():
    client, _, _ = _client(settings_repo=_FakeSettingsRepo(valid=False))
    response = client.patch(
        "/settings/model", json={"provider": "google", "model": "does-not-exist"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "INVALID_MODEL_SELECTION"


# ---------------------------------------------------------------------------
# Password sentinel: nunca aparece en respuesta ni en log
# ---------------------------------------------------------------------------


def test_password_sentinel_never_leaks_through_events_logs_or_responses(caplog):
    """Planta un sentinel unico en el "DSN" que usaria una lectura historica
    (simulando password embebido en `DATABASE_URL`, D65 S6.3) y fuerza que
    esa lectura falle. La API nunca debe devolver `str(exc)` crudo al
    cliente ni loguearlo con el sentinel visible."""
    sentinel = "SENTINEL-PASSWORD-3f9a7c21"
    dsn_with_secret = f"postgresql+asyncpg://ludex:{sentinel}@127.0.0.1:15432/ludex"
    client, registry, event_hub = _client(
        read_repo=_RaisingReadRepository(dsn_with_secret),
    )
    _open_pending(registry)

    with caplog.at_level(logging.DEBUG):
        health = client.get("/health")
        model_get = client.get("/settings/model")
        model_patch = client.patch(
            "/settings/model", json={"provider": "google", "model": "gemini-test"},
        )
        approve = client.post(
            "/battles/battle-1/decisions/0/approve", json={"attempt_index": 0},
        )
        # La lectura historica revienta (RuntimeError con el DSN adentro):
        # FastAPI la convierte en un 500 generico. El body de ESE 500 nunca
        # puede traer el sentinel.
        battle_read = client.get("/battles/battle-1", headers={
            "Accept": "application/json",
        })

    responses_text = "".join(
        response.text for response in (health, model_get, model_patch, approve, battle_read)
    )
    assert sentinel not in responses_text

    for record in caplog.records:
        assert sentinel not in record.getMessage()

    for event in event_hub.resume("battle:battle-1", 0):
        assert sentinel not in str(event.payload)
