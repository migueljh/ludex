"""Pruebas del runner/matriz dinamica (F2-10B/MON-20)."""

from __future__ import annotations

import inspect
import re
import time
from decimal import Decimal
from pathlib import Path

import pytest

from ludex_agent.graph.provider import ModelRoute
from ludex_agent.matrix import (
    BATTLES_PER_MODEL,
    BudgetSpec,
    CatalogEntry,
    ManifestRow,
    build_manifest,
    delta_catalog,
    plan_budget,
)


def _routes() -> dict[tuple[str, str], ModelRoute]:
    return {
        ("google", "gemini-2.5-flash"): ModelRoute(protocol="google"),
        ("kimi", "kimi-k2.6"): ModelRoute(protocol="chat_completions"),
        ("open_code_zen", "gpt-5.5"): ModelRoute(
            protocol="responses", endpoint="https://opencode.ai/zen/v1/responses"
        ),
        ("open_code_zen", "deepseek-v4-flash"): ModelRoute(
            protocol="chat_completions"
        ),
        ("open_code_zen", "mimo-v2.5-free"): ModelRoute(
            protocol="chat_completions"
        ),
        ("open_code_zen", "gemini-3.6-flash"): ModelRoute(
            protocol="google"
        ),
        ("open_code_zen", "qwen3.6-plus"): ModelRoute(protocol="messages"),
    }


def _tier_prices() -> dict[tuple[str, str], tuple[str, str, str, str]]:
    return {
        ("open_code_zen", "gpt-5.5"): ("paid", "5", "30", "zen-docs"),
        ("open_code_zen", "deepseek-v4-flash"): (
            "paid", "0.14", "0.28", "zen-docs",
        ),
        ("open_code_zen", "mimo-v2.5-free"): (
            "free", "0", "0", "zen-docs",
        ),
        ("open_code_zen", "gemini-3.6-flash"): (
            "paid", "1.5", "7.5", "zen-docs",
        ),
        ("open_code_zen", "qwen3.6-plus"): (
            "paid", "0.5", "3", "zen-docs",
        ),
        ("kimi", "kimi-k2.6"): ("paid", "0.95", "4", "moonshot-docs"),
        ("google", "gemini-2.5-flash"): (
            "unknown", None, None, "sin fuente oficial verificada",
        ),
    }


def _previous_inventory() -> dict:
    return {
        "models": {
            "google": [
                {"id": "gemini-2.5-flash", "in_scope": True},
                {"id": "gemini-2.5-flash-image", "in_scope": False,
                 "exclusion_reason": "generacion de imagenes"},
            ],
            "kimi": [{"id": "kimi-k2.6", "in_scope": True}],
            "open_code_zen": [
                {"id": "gpt-5.5", "in_scope": True},
                {"id": "deepseek-v4-flash", "in_scope": True},
                {"id": "mimo-v2.5-free", "in_scope": True},
                {"id": "gemini-3.6-flash", "in_scope": True},
                {"id": "qwen3.6-plus", "in_scope": True},
            ],
        }
    }


def test_delta_catalog_detecta_altas_y_bajas():
    delta = delta_catalog(
        {"zen": ["a", "b", "c"], "kimi": ["x"]},
        {"zen": ["b", "c", "d"], "kimi": ["x", "y"], "google": ["z"]},
    )
    assert delta["zen"]["added"] == ["d"]
    assert delta["zen"]["removed"] == ["a"]
    assert delta["kimi"]["added"] == ["y"]
    assert delta["kimi"]["removed"] == []
    assert delta["google"]["added"] == ["z"]
    assert delta["google"]["removed"] == []


def test_manifiesto_una_fila_por_modelo_con_pin_estricto():
    fresh = {
        "google": ["gemini-2.5-flash", "gemini-2.5-flash-image"],
        "kimi": ["kimi-k2.6"],
        "open_code_zen": [
            "gpt-5.5", "deepseek-v4-flash", "mimo-v2.5-free",
            "gemini-3.6-flash", "qwen3.6-plus",
        ],
    }
    rows = build_manifest(
        fresh, previous_inventory=_previous_inventory(),
        tier_prices=_tier_prices(),
        routes=_routes(),
    )
    by_key = {(r.provider, r.model): r for r in rows}
    # Una fila por cada modelo descubierto (7) + la excluida (1).
    assert len(rows) == 8
    gpt = by_key[("open_code_zen", "gpt-5.5")]
    assert gpt.status == "ready"
    assert gpt.protocol == "responses"
    assert gpt.endpoint == "https://opencode.ai/zen/v1/responses"
    assert gpt.pin == ("open_code_zen", "gpt-5.5")
    assert gpt.concurrency == 1
    assert gpt.persist is False
    assert gpt.battles == BATTLES_PER_MODEL == 2
    assert by_key[("open_code_zen", "gemini-3.6-flash")].protocol == "google"
    assert by_key[("open_code_zen", "qwen3.6-plus")].protocol == "messages"
    excluded = by_key[("google", "gemini-2.5-flash-image")]
    assert excluded.status.startswith("excluded:")
    assert excluded.battles == 0


def test_modelo_sin_ruta_tiene_clasificacion_explicita_no_silenciosa():
    fresh = {"open_code_zen": ["claude-sonnet-5"]}
    rows = build_manifest(
        fresh, previous_inventory={
            "models": {"open_code_zen": [
                {"id": "claude-sonnet-5", "in_scope": True},
            ]},
        },
        tier_prices={},
        routes=_routes(),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "missing-route"
    assert row.protocol is None
    # Nunca se clasifica incompatible/unsupported: es un missing-route
    # explicito que preserva la fila para cuando exista la ruta.
    assert "unsupported" not in row.status
    assert "incompatible" not in row.status
    assert "no se ensaya" in (row.classification_note or "")


def test_plan_budget_ordena_por_costo_y_aplica_hard_stop():
    routes = _routes()
    fresh = {
        "open_code_zen": [
            "deepseek-v4-flash", "gpt-5.5", "mimo-v2.5-free", "gemini-3.6-flash",
        ],
    }
    rows = build_manifest(
        fresh,
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": model, "in_scope": True} for model in fresh["open_code_zen"]
            ]},
        },
        tier_prices=_tier_prices(),
        routes=routes,
    )
    budgets = {
        "open_code_zen": BudgetSpec(
            balance_usd=Decimal("11"), cap_usd=Decimal("10"),
            leave_usd=Decimal("1"),
        ),
    }
    planned = plan_budget(rows, budgets)
    ready = [r for r in planned if r.status == "ready"]
    # Orden ascendente de costo estimado: free primero, luego flash, qwen...
    assert ready[0].model == "mimo-v2.5-free"
    assert ready[0].tier == "free"
    costs = [r.estimated_cost_usd for r in ready if r.estimated_cost_usd is not None]
    assert costs == sorted(costs)
    # L-02: disponibilidad = min(cap, balance - leave) = min(10, 10) = 10.
    # Ninguna fila ready acumula mas de 10; gpt-5.5 (reserva ~9.56) queda
    # pending-budget si el acumulado previo ya lo empuja sobre 10.
    total = sum(
        (r.cumulative_cost_usd or Decimal("0"))
        for r in ready if r.cumulative_cost_usd is not None
    )
    assert total <= Decimal("10")
    assert any(r.status == "pending-budget" for r in planned)
    assert planned[0].model == "mimo-v2.5-free"


def test_plan_budget_disponibilidad_es_min_cap_balance_menos_leave():
    """L-02: con balance 11 / cap 10 / leave 1 la disponibilidad es
    EXACTAMENTE 10 (min(cap, balance - leave)), no 9 (cap - leave). Una
    reserva de 9.5 entra; una de 10.5 no."""
    rows = [
        ManifestRow(
            provider="open_code_zen", model="a", protocol="chat_completions",
            endpoint=None, structured_output=None, tier="paid",
            status="ready", battles=2, concurrency=1, persist=False,
            pin=("open_code_zen", "a"),
            estimated_cost_usd=Decimal("9.5"), estimated_smoke_usd=Decimal("0"),
        ),
        ManifestRow(
            provider="open_code_zen", model="b", protocol="chat_completions",
            endpoint=None, structured_output=None, tier="paid",
            status="ready", battles=2, concurrency=1, persist=False,
            pin=("open_code_zen", "b"),
            estimated_cost_usd=Decimal("10.5"), estimated_smoke_usd=Decimal("0"),
        ),
    ]
    planned = plan_budget(rows, {
        "open_code_zen": BudgetSpec(
            balance_usd=Decimal("11"), cap_usd=Decimal("10"),
            leave_usd=Decimal("1"),
        ),
    })
    by_model = {r.model: r for r in planned}
    assert by_model["a"].status == "ready"
    assert by_model["b"].status == "pending-budget"
    assert by_model["a"].cumulative_cost_usd == Decimal("9.5")


def test_plan_budget_limites_efectivos_10_y_550():
    """L-02: con la autorizacion real (Zen 11/10/1, Kimi 6/5.50/0.50) los
    limites efectivos son exactamente 10.00 y 5.50."""
    rows = [
        ManifestRow(
            provider="open_code_zen", model="m", protocol="chat_completions",
            endpoint=None, structured_output=None, tier="paid",
            status="ready", battles=2, concurrency=1, persist=False,
            pin=("open_code_zen", "m"),
            estimated_cost_usd=Decimal("9.9"), estimated_smoke_usd=Decimal("0.05"),
        ),
        ManifestRow(
            provider="kimi", model="n", protocol="chat_completions",
            endpoint=None, structured_output=None, tier="paid",
            status="ready", battles=2, concurrency=1, persist=False,
            pin=("kimi", "n"),
            estimated_cost_usd=Decimal("5.4"), estimated_smoke_usd=Decimal("0.05"),
        ),
    ]
    planned = plan_budget(rows, {
        "open_code_zen": BudgetSpec(
            balance_usd=Decimal("11"), cap_usd=Decimal("10"),
            leave_usd=Decimal("1"),
        ),
        "kimi": BudgetSpec(
            balance_usd=Decimal("6"), cap_usd=Decimal("5.5"),
            leave_usd=Decimal("0.5"),
        ),
    })
    by_model = {r.model: r for r in planned}
    # 9.95 <= 10.00 entra; 5.45 <= 5.50 entra.
    assert by_model["m"].status == "ready"
    assert by_model["m"].cumulative_cost_usd == Decimal("9.95")
    assert by_model["n"].status == "ready"
    assert by_model["n"].cumulative_cost_usd == Decimal("5.45")


def test_plan_budget_pending_nunca_es_unsupported():
    rows = [
        ManifestRow(
            provider="open_code_zen", model="claude-opus-5",
            protocol="messages", endpoint=None, structured_output=None,
            tier="paid", status="ready", battles=2, concurrency=1,
            persist=False, pin=("open_code_zen", "claude-opus-5"),
            estimated_cost_usd=Decimal("50"), estimated_smoke_usd=Decimal("0.1"),
        ),
    ]
    planned = plan_budget(rows, {
        "open_code_zen": BudgetSpec(
            balance_usd=Decimal("11"), cap_usd=Decimal("10"),
            leave_usd=Decimal("1"),
        ),
    })
    row = planned[0]
    assert row.status == "pending-budget"
    assert "unsupported" not in row.status
    assert "incompatible" not in row.status
    assert "externally-limited" not in row.status
    assert row.protocol == "messages"  # se preserva protocolo/ruta
    assert row.estimated_cost_usd == Decimal("50")


def test_plan_budget_sin_costo_estimado_queda_pending_no_free():
    rows = [
        ManifestRow(
            provider="google", model="gemini-3.6-flash",
            protocol="google", endpoint=None, structured_output=None,
            tier="unknown", status="ready", battles=2, concurrency=1,
            persist=False, pin=("google", "gemini-3.6-flash"),
            estimated_cost_usd=None, estimated_smoke_usd=None,
        ),
    ]
    planned = plan_budget(rows, {})
    assert planned[0].status == "pending-budget"
    assert planned[0].tier == "unknown"


def test_parcial_aborted_no_publica_winrate_comparable():
    from ludex_agent.benchmark import BenchmarkResult

    aborted = BenchmarkResult(
        requested=2, completed=1, wins=1, losses=0, ties=0,
        provider="kimi", model="kimi-k2.6",
        failure="TransientProviderError: provider transport failed",
        failure_type="TransientProviderError",
        failure_cause_type="APITimeoutError",
    )
    assert not aborted.comparable
    assert aborted.win_rate is None
    assert aborted.interval is None
    # Un parcial conserva su clasificacion de fallo y NUNCA presenta
    # winrate: la matriz solo publica metricas comparables para corridas
    # completas.
    assert aborted.failure_type == "TransientProviderError"


def test_manifiesto_incluye_filas_de_fuera_de_scope_del_inventario():
    """Los modelos excluidos del inventario previo NO desaparecen del
    manifiesto: aparecen con su razon de capacidad."""
    fresh = {"google": ["veo-3.1-generate-preview"]}
    rows = build_manifest(
        fresh,
        previous_inventory={
            "models": {"google": [
                {"id": "veo-3.1-generate-preview", "in_scope": False,
                 "exclusion_reason": "generacion de video"},
            ]},
        },
        tier_prices={},
        routes=_routes(),
    )
    assert rows[0].status == "excluded:generacion de video"


def test_catalog_entry_preserva_deprecated_y_tier():
    entry = CatalogEntry(
        provider="open_code_zen", model="glm-5",
        protocol="chat_completions", endpoint=None, structured_output=None,
        tier="paid", price_input_usd=Decimal("1"), price_output_usd=Decimal("3.2"),
        price_source="zen-docs", deprecated=True,
    )
    assert entry.deprecated is True
    assert entry.tier == "paid"


# --- F2-10B R3 (MON-20): ejecutor fail-closed de R1 ----------------------


def _ready_row(provider, model, tier, protocol="chat_completions") -> ManifestRow:
    return ManifestRow(
        provider=provider, model=model, protocol=protocol, endpoint=None,
        structured_output=None, tier=tier, status="ready", battles=2,
        concurrency=1, persist=False, pin=(provider, model),
        estimated_cost_usd=Decimal("0"), estimated_smoke_usd=Decimal("0"),
    )


class _FakeSmokeProvider:
    def __init__(self, error=None, payload=None):
        self.error = error
        self.payload = payload or {
            "action": {"kind": "move", "id": "thunderbolt"},
            "target": None,
            "rationale": "brief rationale",
            "confidence": 0.9,
            "alternatives": [],
        }
        self.calls: list[str] = []

    async def complete(self, prompt, *, deadline, turn_id):
        self.calls.append(turn_id)
        if self.error is not None:
            raise self.error
        from ludex_agent.graph.provider import (
            CompletionEnvelope, CompletionUsage,
        )
        return CompletionEnvelope(
            payload=self.payload, provider="fake", model="fake-model",
            usage=CompletionUsage(input_tokens=1, output_tokens=1),
            latency_ms=1.0,
        )


def _ok_battles(completed=2, requested=2, wins=1, failure=None,
                failure_type=None, provider="fake", model="fake-model",
                http_status=None, error_code=None):
    from ludex_agent.benchmark import BenchmarkResult
    return (
        BenchmarkResult(
            requested=requested, completed=completed, wins=wins,
            losses=completed - wins, ties=0, provider=provider, model=model,
            failure=failure, failure_type=failure_type,
            http_status=http_status, provider_error_code=error_code,
        ),
        {
            "turns_total": 4, "calls_total": 6,
            "input_tokens": 100, "output_tokens": 20,
            "cached_input_tokens": 0, "reasoning_tokens": 0,
            "key_rotations": 1, "keys_quarantined": 0,
            "turns_transient_affected": 0, "turns_deadline_affected": 0,
            "turns_quota_affected": 0, "turns_model_invalid": 0,
            "turns_fallback": 0,
            "completion_latency_ms_count": 6,
            "completion_latency_ms_total": 60,
            "completion_latency_ms_p50": 10,
            "completion_latency_ms_p95": 12,
            "completion_latency_ms_max": 15,
            "decision_latency_ms_count": 4,
            "decision_latency_ms_total": 40,
            "decision_latency_ms_p50": 10,
            "decision_latency_ms_p95": 12,
            "decision_latency_ms_max": 15,
        },
    )


async def _run(rows, *, tier="free", refresh=None, previous=None,
               battle_failure=None, battle_failure_type=None,
               battles_completed=2, battles_requested=2, battles_wins=1,
               smoke_error=None, smoke_payload=None, build_provider=None,
               battle_http_status=None, battle_error_code=None):
    from ludex_agent.matrix import run_matrix_round
    from ludex_agent.graph.provider import ProviderError

    built: list[tuple[str, str]] = []
    battle_calls: list[int] = []

    def default_build_provider(provider, model):
        built.append((provider, model))
        return _FakeSmokeProvider(error=smoke_error, payload=smoke_payload)

    effective_build = build_provider or default_build_provider

    async def run_battles(provider, model, *, n, battle_timeout_seconds,
                          fmt, opponent):
        battle_calls.append(n)
        return _ok_battles(
            completed=battles_completed, requested=battles_requested,
            wins=battles_wins, failure=battle_failure,
            failure_type=battle_failure_type,
            http_status=battle_http_status, error_code=battle_error_code,
        )

    async def _refresh():
        result = refresh()
        if inspect.isawaitable(result):
            return await result
        return result

    results = await run_matrix_round(
        rows=rows, tier=tier, battle_timeout_seconds=1800,
        fmt="gen6randombattle", opponent="simple_heuristics",
        smoke_deadline_seconds=120,
        build_provider=effective_build, run_battles=run_battles,
        refresh_catalog=_refresh if refresh is not None else None,
        previous=previous,
    )
    return results, built, battle_calls


def test_r1_con_free_y_paid_ejecuta_unicamente_free():
    """Canario: un manifiesto con filas free+paid, --tier free, solo
    construye providers de las filas free. Si alguien quitara el filtro,
    el provider paid apareceria en `built` y el test se pondria rojo."""
    import asyncio

    rows = [
        _ready_row("open_code_zen", "mimo-v2.5-free", "free"),
        _ready_row("open_code_zen", "deepseek-v4-flash", "paid"),
        _ready_row("google", "gemma-4-26b-a4b-it", "free"),
    ]
    results, built, battle_calls = asyncio.run(_run(rows, tier="free"))
    assert sorted(built) == [
        ("google", "gemma-4-26b-a4b-it"),
        ("open_code_zen", "mimo-v2.5-free"),
    ]
    assert "deepseek-v4-flash" not in {m for _, m in built}
    assert all(r.status == "compatible" for r in results)
    assert battle_calls == [2, 2]


def test_tier_free_filtra_paid_y_el_runner_revalida_antes_del_provider():
    import asyncio
    from ludex_agent.matrix import run_matrix_round, select_rows_for_tier

    rows = [
        _ready_row("open_code_zen", "mimo-v2.5-free", "free"),
        _ready_row("open_code_zen", "deepseek-v4-flash", "paid"),
    ]
    assert [r.model for r in select_rows_for_tier(rows, "free")] == [
        "mimo-v2.5-free"
    ]

    # Si alguien quitara el filtro de seleccion, el check fail-closed del
    # runner aborta ANTES de construir el provider paid (mutacion).
    built: list[str] = []
    battle_calls: list[int] = []

    def build_provider(provider, model):
        built.append(model)
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        battle_calls.append(model)
        return _ok_battles()

    async def refresh():
        return {"open_code_zen": ["mimo-v2.5-free", "deepseek-v4-flash"]}

    # MUTACION: un filtro roto que devuelve todas las ready (free+paid)
    # debe ser atrapado por el check fail-closed ANTES de construir
    # cualquier provider.
    import ludex_agent.matrix as matrix_module

    def broken_filter(rows, tier):
        return [r for r in rows if r.status == "ready"]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(matrix_module, "select_rows_for_tier", broken_filter)
    try:
        with pytest.raises(ValueError, match="fuera de la fase"):
            asyncio.run(run_matrix_round(
                rows=rows, tier="free", battle_timeout_seconds=1800,
                fmt="gen6randombattle", opponent="simple_heuristics",
                smoke_deadline_seconds=120,
                build_provider=build_provider, run_battles=run_battles,
                refresh_catalog=None,
            ))
    finally:
        monkeypatch.undo()
    assert built == [] and battle_calls == []


def test_smoke_fallido_produce_0_batallas():
    import asyncio
    from ludex_agent.graph.provider import FatalProviderError

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, built, battle_calls = asyncio.run(_run(
        rows, smoke_error=FatalProviderError("provider permission or model unavailable"),
    ))
    assert results[0].status == "unsupported-protocol"
    assert results[0].smoke_ok is False
    assert results[0].battles_completed == 0
    assert battle_calls == []
    assert results[0].win_rate is None


def test_smoke_semantic_invalido_produce_0_batallas():
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, battle_calls = asyncio.run(_run(
        rows, smoke_payload={"action": {"kind": "move", "id": "thunderbolt"}},
    ))
    assert results[0].status == "invalid-semantic-response"
    assert results[0].battles_completed == 0
    assert battle_calls == []


def test_smoke_verde_produce_exactamente_2_batallas_y_winrate():
    import asyncio

    rows = [_ready_row("google", "gemma-4-26b-a4b-it", "free")]
    results, _, battle_calls = asyncio.run(_run(rows))
    assert results[0].status == "compatible"
    assert results[0].battles_requested == 2
    assert results[0].battles_completed == 2
    assert battle_calls == [2]
    assert results[0].win_rate == "0.5000"
    assert results[0].rotations == 1
    assert results[0].completion_latency_ms["p50"] == 10


def test_mezcla_efectiva_aborta_sin_winrate():
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, _ = asyncio.run(_run(
        rows, battle_failure="ProviderMixError: provider/model mezclado",
        battle_failure_type="ProviderMixError",
    ))
    assert results[0].status == "internal-defect"
    assert results[0].win_rate is None
    assert results[0].failure_type == "ProviderMixError"


def test_parcial_abortado_no_publica_winrate_comparable():
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, _ = asyncio.run(_run(
        rows, battles_completed=1, battles_requested=2,
        battle_failure="TransientProviderError: provider transport failed",
        battle_failure_type="TransientProviderError",
    ))
    assert results[0].status == "aborted"
    assert results[0].win_rate is None
    assert results[0].battles_completed == 1
    assert results[0].failure_type == "TransientProviderError"


def test_resume_no_repite_modelo_finalizado_ni_salta_sin_clasificar():
    import asyncio
    from ludex_agent.matrix import MatrixModelResult, run_matrix_round

    rows = [
        _ready_row("open_code_zen", "mimo-v2.5-free", "free"),
        _ready_row("open_code_zen", "deepseek-v4-flash-free", "free"),
    ]
    done = MatrixModelResult(
        provider="open_code_zen", model="mimo-v2.5-free", tier="free",
        protocol="chat_completions", status="compatible", smoke_ok=True,
        battles_requested=2, battles_completed=2,
        effective_provider="open_code_zen", effective_model="mimo-v2.5-free",
        win_rate="0.5000", completion_latency_ms=None,
        decision_latency_ms=None, tokens=None, retries=0, rotations=0,
        quarantined=0, failure_type=None, failure_cause_type=None,
    )
    built: list[str] = []

    def build_provider(provider, model):
        built.append(model)
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        return _ok_battles()

    results = asyncio.run(run_matrix_round(
        rows=rows, tier="free", battle_timeout_seconds=1800,
        fmt="gen6randombattle", opponent="simple_heuristics",
        smoke_deadline_seconds=120,
        build_provider=build_provider, run_battles=run_battles,
        refresh_catalog=None,
        previous={"open_code_zen/mimo-v2.5-free": done},
    ))
    statuses = {r.model: r.status for r in results}
    assert statuses["mimo-v2.5-free"] == "already-finalized"
    assert statuses["deepseek-v4-flash-free"] == "compatible"
    # el finalizado NO se repite; el sin clasificar SÍ se ejecuta
    assert built == ["deepseek-v4-flash-free"]


def test_modelo_fuera_del_catalogo_fresco_se_clasifica_no_se_ejecuta():
    import asyncio

    rows = [
        _ready_row("open_code_zen", "mimo-v2.5-free", "free"),
        _ready_row("open_code_zen", "deepseek-v4-flash-free", "free"),
    ]
    results, built, battle_calls = asyncio.run(_run(
        rows, refresh=lambda: {"open_code_zen": ["deepseek-v4-flash-free"]},
    ))
    by_model = {r.model: r for r in results}
    assert by_model["mimo-v2.5-free"].status == "removed-from-catalog"
    assert by_model["deepseek-v4-flash-free"].status == "compatible"
    assert built == [("open_code_zen", "deepseek-v4-flash-free")]
    assert battle_calls == [2]


# --- F2-10B R4 (MON-20): SECURITY HOLD — hardening offline -------------


def test_artefactos_de_matriz_no_llevan_secretos_ni_campos_de_env():
    """El artefacto por modelo tiene un schema cerrado: sin claves, sin
    variables de entorno, sin mensajes crudos. Si alguien agregara un campo
    que filtra credenciales, este test se pone rojo."""
    import json as _json

    from ludex_agent.matrix import MatrixModelResult

    result = MatrixModelResult(
        provider="open_code_zen", model="mimo-v2.5-free", tier="free",
        protocol="chat_completions", status="compatible", smoke_ok=True,
        battles_requested=2, battles_completed=2,
        effective_provider="open_code_zen", effective_model="mimo-v2.5-free",
        win_rate="0.5000", completion_latency_ms=None,
        decision_latency_ms=None, tokens=None, retries=0, rotations=0,
        quarantined=0, failure_type=None, failure_cause_type=None,
    )
    serialized = _json.dumps(result.to_dict())
    for forbidden in ("api_key", "api-key", "AIza", "sk-", "KIMI_",
                      "GEMINI_", "OPEN_CODE_ZEN_", "Bearer"):
        assert forbidden not in serialized, forbidden
    assert "sk-zen" not in serialized


_SECRET_PATTERN = re.compile(
    r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}"
)


def _scan_secret_patterns(root: Path) -> list[str]:
    """UNICA implementacion del scanner de patrones de credencial en
    territorio de tests (L-03). Recibe un root y devuelve SOLO paths
    relativos de los archivos JSON infractores; nunca los valores
    coincidentes (pueden ser material de credencial). Sin exclusiones de
    nombres: cubre todos los `**/*.json`, incluidos artefactos por modelo
    y state files de matrix-run."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.json")):
        # MUTACION L-03: reintroducir `if "matrix-run" in path.name:
        # continue` aca hace que el canario sintetico deje de detectar el
        # state file y se ponga rojo.
        if _SECRET_PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(root)))
    return offenders


def test_datos_de_evals_no_contienen_patrones_de_clave_real():
    """Canario de repositorio (L-03): TODOS los JSON de evals — incluidos
    artefactos por modelo y state files de matrix-run — jamás versionan
    patrones de clave. Usa la MISMA funcion de scan que el canario
    sintetico: no hay una implementacion paralela que pueda divergir."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "evals"
    offenders = _scan_secret_patterns(root)
    # solo nombres/rutas en el mensaje: el valor de la coincidencia no se
    # imprime (podria ser material de credencial)
    assert offenders == [], f"patrones de clave en datos de evals: {offenders}"


def test_canario_sintetico_detecta_exactamente_los_dos_archivos_contaminados(
    tmp_path,
):
    """Canario L-03: el fixture sintetico contiene un artefacto por modelo
    contaminado, un `matrix-run-state.json` contaminado y un JSON limpio
    (counterweight). El scan compartido detecta EXACTAMENTE los dos
    contaminados y no el limpio. Si alguien reintrodujera una exclusion de
    nombres `matrix-run` en la funcion compartida, el state file dejaria de
    detectarse y este canario se pondria rojo."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    (root / "r1-open_code_zen-mimo-v2.5-free-matrix.json").write_text(
        '{"status": "compatible", "note": "AIzaSy0000000000000000000000000000000x"}'
    )
    (root / "r1-matrix-run-state.json").write_text(
        '{"open_code_zen/mimo-v2.5-free": {"status": "compatible", '
        '"detail": "sk-fake000000000000000000000000000000"}'
        "}"
    )
    (root / "r1-google-gemma-4-26b-a4b-it-matrix.json").write_text(
        '{"status": "compatible", "battles": 2}'
    )

    offenders = _scan_secret_patterns(root)
    assert offenders == [
        "r1-matrix-run-state.json",
        "r1-open_code_zen-mimo-v2.5-free-matrix.json",
    ]


def test_el_scanner_devuelve_solo_paths_nunca_valores(tmp_path):
    """L-03: el scan comparte una unica funcion que devuelve SOLO
    paths/nombres. El valor coincidente (material de credencial) no aparece
    en el resultado ni en el mensaje de asercion."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    secret = "AIzaSy0000000000000000000000000000000x"
    (root / "r1-matrix-run-state.json").write_text(
        f'{{"model": "x", "note": "{secret}"}}'
    )

    offenders = _scan_secret_patterns(root)
    rendered = " ".join(offenders)
    assert offenders == ["r1-matrix-run-state.json"]
    assert secret not in rendered
    assert "AIza" not in rendered


@pytest.mark.asyncio
async def test_responses_backend_429_401_y_403_clasifican_por_senal(monkeypatch):
    """Fin a fin por la frontera HTTP del backend responses: el
    raise_for_status de httpx produce el HTTPStatusError y la clasificacion
    (429 rotacion, 401 cuarentena, 403 credential/model) corre con el
    cuerpo estructurado."""
    from ludex_agent.graph.provider import (
        DecisionMetrics,
        KeyRotatingProvider,
        ModelRoute,
        _ResponsesBackend,
    )

    class FakeResponse:
        def __init__(self, status: int, body: dict):
            self.status_code = status
            self._body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                request = httpx.Request("POST", "https://opencode.ai/zen/v1/responses")
                raise httpx.HTTPStatusError(
                    "boom", request=request,
                    response=httpx.Response(self.status_code, request=request, json=self._body),
                )

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, status, body):
            self._status, self._body = status, body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            return FakeResponse(self._status, self._body)

    def backend_for(status, body):
        return _ResponsesBackend(
            model="gpt-5.5", endpoint="https://opencode.ai/zen/v1/responses",
            timeout_seconds=60, route=ModelRoute(protocol="responses"),
        )

    async def classify(status, body):
        client = FakeClient(status, body)
        monkeypatch.setattr("ludex_agent.graph.provider.httpx.AsyncClient",
                            lambda *a, **k: client)
        metrics = DecisionMetrics()
        provider = KeyRotatingProvider(
            "open_code_zen", ("k-bad", "k-ok"), backend_for(status, body),
            metrics=metrics, transient_retries=0,
        )
        try:
            await provider.complete(
                "p", deadline=time.monotonic() + 5, turn_id="t"
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    # 429 -> rota y termina en ProviderPoolExhausted (2 claves, cooldown)
    assert await classify(429, {"error": {"code": 429}}) == "ProviderPoolExhausted"
    # 401 CON code estructurado allowlisted -> cuarentena, pool exhausted
    assert await classify(401, {"error": {"code": "invalid_api_key"}}) == "ProviderPoolExhausted"
    # 401 SIN senal estructurada (solo mensaje) -> FatalProviderError, sin
    # cuarentena ni rotacion (L-02/R1A: puede ser el provider upstream).
    assert await classify(401, {"error": {"message": "bad key"}}) == "FatalProviderError"
    # 403 credential-specific (google reason) -> cuarentena
    body_cred = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                         "reason": "API_KEY_INVALID"}]}}
    assert await classify(403, body_cred) == "ProviderPoolExhausted"
    # 403 model-wide -> FatalProviderError inmediato (sin quemar)
    body_wide = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                           "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                        "reason": "ACCESS_DENIED"}]}}
    assert await classify(403, body_wide) == "FatalProviderError"


def _paid_row(provider, model, cost) -> ManifestRow:
    return ManifestRow(
        provider=provider, model=model, protocol="chat_completions",
        endpoint=None, structured_output=None, tier="paid",
        status="ready", battles=2, concurrency=1, persist=False,
        pin=(provider, model),
        estimated_cost_usd=Decimal(str(cost)), estimated_smoke_usd=Decimal("0"),
    )


def test_plan_budget_ignorar_el_cap_pondria_rojo_el_canario():
    """L-02: con balance 20 / cap 10 / leave 1 la disponibilidad es
    min(10, 19) = 10. Una reserva de 15 NO puede entrar: si el codigo
    ignorara el cap (allowed = balance - leave = 19), el canario se pone
    rojo."""
    rows = [_paid_row("open_code_zen", "frontier", 15)]
    planned = plan_budget(rows, {
        "open_code_zen": BudgetSpec(
            balance_usd=Decimal("20"), cap_usd=Decimal("10"),
            leave_usd=Decimal("1"),
        ),
    })
    assert planned[0].status == "pending-budget"
    assert "no alcanza" in (planned[0].classification_note or "")


def test_plan_budget_ignorar_el_saldo_minimo_pondria_rojo_el_canario():
    """L-02: con balance 5 / cap 10 / leave 1 la disponibilidad es
    min(10, 4) = 4. Una reserva de 8 NO puede entrar (quemaria el saldo):
    si el codigo ignorara el saldo minimo (allowed = cap = 10), el canario
    se pone rojo."""
    rows = [_paid_row("kimi", "caro", 8)]
    planned = plan_budget(rows, {
        "kimi": BudgetSpec(
            balance_usd=Decimal("5"), cap_usd=Decimal("10"),
            leave_usd=Decimal("1"),
        ),
    })
    assert planned[0].status == "pending-budget"
    assert "no alcanza" in (planned[0].classification_note or "")


# --- LATWAN R1A REVIEW (MON-20): L-01 metricas reales, L-03 evidencia
# durable sanitizada ------------------------------------------------


def _keyed_provider(provider_name, model, error_factory, *, keys=("k",),
                    transient_retries=0):
    """Provider real con metrics y backend que siempre lanza el error
    construido por `error_factory` (una clave por 401 credential, 503, ...).
    Devuelve (provider, metrics, backend) para inspeccion del delta."""
    import httpx as _httpx
    from ludex_agent.graph.provider import (
        DecisionMetrics, KeyRotatingProvider,
    )

    request = _httpx.Request(
        "POST", f"https://opencode.ai/zen/v1/chat/completions"
    )
    metrics = DecisionMetrics()

    class Backend:
        def __init__(self):
            self.calls = 0

        async def complete(self, prompt, *, api_key, deadline):
            self.calls += 1
            raise error_factory(request)

    backend = Backend()
    provider = KeyRotatingProvider(
        provider_name, keys, backend, metrics=metrics,
        transient_retries=transient_retries,
    )
    return provider, metrics, backend


def test_smoke_fallido_reporta_quarantined_real_de_north():
    """L-01 (R1A): canario North. El artefacto de North decia
    quarantined=0 mientras el checkpoint afirmaba cuarentena +
    ProviderPoolExhausted. El delta real de DecisionMetrics del provider
    debe reportar 1: un 401 con senal estructurada pone la unica clave en
    cuarentena y el pool agotado se clasifica credential/model unavailable.
    Con el comportamiento rechazado (zeros fijos en `_smoke_failed`) este
    test se pone rojo."""
    import asyncio
    import httpx as _httpx
    from ludex_agent.graph.provider import KeyRotatingProvider

    def _401(request):
        return _httpx.HTTPStatusError(
            "unauthorized", request=request,
            response=_httpx.Response(401, request=request, json={
                "error": {"code": "invalid_api_key"},
            }),
        )

    provider, metrics, backend = _keyed_provider(
        "open_code_zen", "north-mini-code-free", _401, keys=("k",),
    )

    def build_provider(provider_name, model):
        return provider

    rows = [_ready_row("open_code_zen", "north-mini-code-free", "free")]
    results, _, battle_calls = asyncio.run(_run(
        rows, build_provider=build_provider,
    ))
    result = results[0]
    assert result.status == "credential/model unavailable"
    assert result.failure_type == "ProviderPoolExhausted"
    assert result.quarantined == 1
    assert result.rotations == 0
    assert result.retries == 0
    assert result.failure_stage == "smoke"
    assert backend.calls == 1
    assert battle_calls == []


def test_smoke_503_refleja_los_retries_reales():
    """L-01 (R1A): el artefacto de Ling decia retries=0 aunque el smoke
    reintento el 503. El delta real debe reflejar los retries ejecutados:
    con transient_retries=2 -> 3 intentos, 2 retries. Con el
    comportamiento rechazado (zeros fijos) este test se pone rojo."""
    import asyncio
    import httpx as _httpx

    def _503(request):
        return _httpx.HTTPStatusError(
            "boom", request=request,
            response=_httpx.Response(503, request=request),
        )

    provider, metrics, backend = _keyed_provider(
        "open_code_zen", "ling-3.0-tiny-free", _503, keys=("k",),
        transient_retries=2,
    )

    def build_provider(provider_name, model):
        return provider

    rows = [_ready_row("open_code_zen", "ling-3.0-tiny-free", "free")]
    results, _, battle_calls = asyncio.run(_run(
        rows, build_provider=build_provider,
    ))
    result = results[0]
    assert result.status == "externally-limited"
    assert result.failure_type == "TransientProviderError"
    assert result.retries == 2
    assert result.rotations == 0
    assert result.quarantined == 0
    assert backend.calls == 3
    assert battle_calls == []


def test_artefacto_fallido_persiste_solo_evidencia_sanitizada():
    """L-03 (R1A): failure_stage, http_status y provider_error_code entran
    al artefacto; el mensaje crudo, la URL, los headers, el body completo y
    los secretos del error jamas se persisten. El error de ejemplo trae una
    URL con query de clave y un mensaje con nombre de variable de entorno:
    nada de eso puede aparecer serializado."""
    import asyncio
    import json as _json
    import httpx as _httpx

    secret = "sk-zen-abcdefghijklmnopqrstuvwxyz0123456789"

    def _400(request):
        return _httpx.HTTPStatusError(
            "boom", request=request,
            response=_httpx.Response(400, request=request, json={
                "error": {
                    "message": (
                        "invalid request at "
                        "https://opencode.ai/zen/v1/chat/completions"
                        f"?api_key={secret} (OPEN_CODE_ZEN_API_KEY)"
                    ),
                    "type": "invalid_request_error",
                    "code": "invalid_request_error",
                },
            }),
        )

    provider, metrics, backend = _keyed_provider(
        "open_code_zen", "big-pickle", _400, keys=("k",),
    )

    def build_provider(provider_name, model):
        return provider

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    results, _, _ = asyncio.run(_run(rows, build_provider=build_provider))
    result = results[0]
    assert result.status == "unsupported-protocol"
    assert result.failure_stage == "smoke"
    assert result.http_status == 400
    assert result.provider_error_code == "invalid_request_error"
    serialized = _json.dumps(result.to_dict())
    for forbidden in (secret, "api_key=", "opencode.ai", "OPEN_CODE_ZEN_",
                      "invalid request at", "sk-zen"):
        assert forbidden not in serialized, forbidden


def test_fallo_de_batalla_etiqueta_failure_stage_battle():
    """L-03 (R1A): un fallo en la fase de batalla lleva failure_stage=battle
    y la evidencia sanitizada de http_status/provider_error_code via el
    BenchmarkResult cuando existen."""
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, _ = asyncio.run(_run(
        rows,
        battles_completed=1, battles_requested=2,
        battle_failure="TransientProviderError: provider server error",
        battle_failure_type="TransientProviderError",
        battle_http_status=503, battle_error_code="server_error",
    ))
    result = results[0]
    assert result.status == "aborted"
    assert result.failure_stage == "battle"
    assert result.http_status == 503
    assert result.provider_error_code == "server_error"
    assert result.win_rate is None


def test_smoke_exitoso_no_lleva_failure_stage():
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, _ = asyncio.run(_run(rows))
    result = results[0]
    assert result.status == "compatible"
    assert result.failure_stage is None
    assert result.http_status is None
    assert result.provider_error_code is None
