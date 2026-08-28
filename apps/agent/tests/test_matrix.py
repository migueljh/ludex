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
    tier_prices_from_pricing_table,
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
        from ludex_agent.graph.provider import DecisionMetrics

        self.error = error
        self.payload = payload or {
            "action": {"kind": "move", "id": "thunderbolt"},
            "target": None,
            "rationale": "brief rationale",
            "confidence": 0.9,
            "alternatives": [],
        }
        self.calls: list[str] = []
        self._metrics = DecisionMetrics()

    def metrics_snapshot(self) -> dict:
        # L-02 (post-R1B): como el provider real, expone sus metricas; el
        # delta del smoke llega al artefacto incluso cuando la batalla
        # falla despues.
        return self._metrics.snapshot()

    async def complete(self, prompt, *, deadline, turn_id):
        self.calls.append(turn_id)
        if self.error is not None:
            raise self.error
        from ludex_agent.graph.provider import (
            CompletionEnvelope, CompletionUsage,
        )
        self._metrics.usage(
            CompletionUsage(input_tokens=10, output_tokens=5)
        )
        self._metrics.completion_latency(1.0)
        return CompletionEnvelope(
            payload=self.payload, provider="fake", model="fake-model",
            usage=CompletionUsage(input_tokens=10, output_tokens=5),
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
               battle_http_status=None, battle_error_code=None,
               battle_raise=None, battle_wrapped=None):
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
        if battle_wrapped is not None:
            # `battle_wrapped` es (partial, exc): la frontera real del
            # benchmark lanza `BenchmarkFailure(partial) from exc`.
            partial, raw = battle_wrapped
            from ludex_agent.benchmark import BenchmarkFailure
            try:
                raise raw
            except BaseException as caught:
                raise BenchmarkFailure(partial) from caught
        if battle_raise is not None:
            raise battle_raise
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
    # T-08 (MON-20 R4): un FatalProviderError SIN status estructurado no
    # autoriza a afirmar un rechazo de protocolo: fail-closed a
    # internal-defect (antes era unsupported-protocol por default).
    assert results[0].status == "internal-defect"
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


def test_smoke_verde_produce_exactamente_2_batallas_sin_winrate():
    import asyncio

    rows = [_ready_row("google", "gemma-4-26b-a4b-it", "free")]
    results, _, battle_calls = asyncio.run(_run(rows))
    assert results[0].status == "compatible"
    assert results[0].battles_requested == 2
    assert results[0].battles_completed == 2
    assert battle_calls == [2]
    # I5 (MON-20 R2): N=2 no publica win_rate como evidencia de calidad
    assert results[0].win_rate is None
    assert results[0].comparable is False
    assert results[0].sample_size == 2
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


def test_resume_conserva_marca_de_stop_y_note_en_ya_finalizado():
    """T-02 (MON-20 R3): la rama `already-finalized` del resume conserva
    `compatibility_result` y `note` de la fila previa: un stop
    indeterminado no pierde su marca al reanudar."""
    import asyncio
    from ludex_agent.matrix import MatrixModelResult, run_matrix_round

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    prior_stop = MatrixModelResult(
        provider="open_code_zen", model="mimo-v2.5-free", tier="free",
        protocol="chat_completions", status="internal-defect", smoke_ok=True,
        battles_requested=2, battles_completed=0,
        effective_provider=None, effective_model=None,
        win_rate=None, completion_latency_ms=None,
        decision_latency_ms=None, tokens=None, retries=0, rotations=0,
        quarantined=0, failure_type="CancelledError",
        failure_cause_type=None, failure_stage="battle",
        comparable=False, sample_size=None,
        compatibility_result="indeterminate-current-run",
        note="fila interrumpida por CancelledError durante battle: "
             "artefacto de stop sanitizado",
    )
    built: list[str] = []

    def build_provider(provider, model):
        built.append(model)
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        raise AssertionError("el stop ya finalizado no se reejecuta")

    results = asyncio.run(run_matrix_round(
        rows=rows, tier="free", battle_timeout_seconds=1800,
        fmt="gen6randombattle", opponent="simple_heuristics",
        smoke_deadline_seconds=120,
        build_provider=build_provider, run_battles=run_battles,
        refresh_catalog=None,
        previous={"open_code_zen/mimo-v2.5-free": prior_stop},
    ))
    assert built == []
    final = results[0]
    assert final.status == "already-finalized"
    assert final.compatibility_result == "indeterminate-current-run"
    assert final.note == prior_stop.note
    assert final.failure_type == "CancelledError"


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


# --- LATWAN OFFLINE REVIEW (MON-20): L-05 y L-06 ------------------------


def test_artefacto_rechaza_codigo_estructurado_inseguro():
    """L-05 (OFFLINE): un `error.code` estructurado con URL + query + clave
    falsa NO se persiste: provider_error_code queda None y la serializacion
    del artefacto no contiene la URL, `api_key=`, `sk-` ni el valor falso."""
    import asyncio
    import json as _json
    import httpx as _httpx

    fake = "https://provider.invalid/?api_key=sk-FAKE_SECRET-abc"

    def _400(request):
        return _httpx.HTTPStatusError(
            "boom", request=request,
            response=_httpx.Response(400, request=request, json={
                "error": {"message": "nope", "type": "invalid_request_error",
                          "code": fake},
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
    assert result.provider_error_code is None
    serialized = _json.dumps(result.to_dict())
    for forbidden in (fake, "provider.invalid", "api_key=", "sk-FAKE",
                      "sk-", "OPEN_CODE_ZEN_"):
        assert forbidden not in serialized, forbidden


def test_benchmark_result_serializado_no_filtra_el_valor_falso():
    """L-05 (OFFLINE): lo que llega a BenchmarkResult ya paso por
    `_structured_provider_error_code` (cli.py); serializar el resultado no
    puede contener URL, `api_key=`, `sk-` ni el valor falso."""
    import json as _json
    from dataclasses import asdict

    import httpx as _httpx

    from ludex_agent.benchmark import BenchmarkResult
    from ludex_agent.graph.provider import (
        _http_status_chain, _structured_provider_error_code,
    )

    request = _httpx.Request("POST", "https://provider.example/v1/x")
    leaky = _httpx.HTTPStatusError(
        "boom", request=request,
        response=_httpx.Response(400, request=request, json={
            "error": {"code": "https://provider.invalid/?api_key=sk-FAKE_SECRET-abc"},
        }),
    )
    code = _structured_provider_error_code(leaky)
    assert code is None
    result = BenchmarkResult(
        requested=2, completed=0, wins=0, losses=0, ties=0,
        provider="open_code_zen", model="x",
        failure="FatalProviderError: provider permission or model unavailable",
        failure_type="FatalProviderError",
        failure_cause_type="BadRequestError",
        http_status=_http_status_chain(leaky),
        provider_error_code=code,
    )
    serialized = _json.dumps(asdict(result))
    for forbidden in ("provider.invalid", "api_key=", "sk-FAKE", "sk-"):
        assert forbidden not in serialized, forbidden


def test_delta_de_metricas_baseline_fresco_reporta_metricas_reales():
    """L-06 (OFFLINE): con baseline fresco (sin muestras de latencia), el
    delta reporta las metricas reales posteriores: contadores/tokens/count/
    total por diferencia y max/p50/p95 copiados del snapshot posterior (la
    unica poblacion existente)."""
    from ludex_agent.matrix import _metrics_delta

    before = {
        "completion_latency_ms_count": 0,
        "completion_latency_ms_total": None,
        "completion_latency_ms_p50": None,
        "completion_latency_ms_p95": None,
        "completion_latency_ms_max": None,
        "decision_latency_ms_count": 0,
        "decision_latency_ms_total": None,
        "decision_latency_ms_p50": None,
        "decision_latency_ms_p95": None,
        "decision_latency_ms_max": None,
        "turns_transient_affected": 0, "key_rotations": 0,
        "keys_quarantined": 0, "transient_retries_executed": 0,
        "input_tokens": 0, "output_tokens": 0,
    }
    after = {
        "completion_latency_ms_count": 2,
        "completion_latency_ms_total": 300,
        "completion_latency_ms_p50": 150,
        "completion_latency_ms_p95": 195,
        "completion_latency_ms_max": 200,
        "decision_latency_ms_count": 0,
        "decision_latency_ms_total": None,
        "decision_latency_ms_p50": None,
        "decision_latency_ms_p95": None,
        "decision_latency_ms_max": None,
        "turns_transient_affected": 1, "key_rotations": 3,
        "keys_quarantined": 2, "transient_retries_executed": 2,
        "input_tokens": 100, "output_tokens": 20,
    }
    delta = _metrics_delta(before, after)
    assert delta is not None
    assert delta["completion_latency_ms_count"] == 2
    assert delta["completion_latency_ms_total"] == 300
    assert delta["completion_latency_ms_max"] == 200
    assert delta["completion_latency_ms_p50"] == 150
    assert delta["completion_latency_ms_p95"] == 195
    assert delta["key_rotations"] == 3
    assert delta["keys_quarantined"] == 2
    assert delta["transient_retries_executed"] == 2
    assert delta["input_tokens"] == 100
    assert delta["decision_latency_ms_count"] == 0


def test_delta_de_metricas_baseline_con_muestras_deja_gauges_no_comparables():
    """L-06 (OFFLINE): el ejemplo exacto de Latwan. Restar percentiles da
    max=100/p50=50/p95=95, matematicamente invalido: jamas se publica.
    Con baseline con muestras previas, los gauges max/p50/p95 quedan None
    (no comparables) y los contadores siguen calculandose por diferencia.
    Mutar a 'restar todos los ints' pone este canario rojo."""
    from ludex_agent.matrix import _metrics_delta

    before = {
        "completion_latency_ms_count": 1,
        "completion_latency_ms_total": 100,
        "completion_latency_ms_p50": 100,
        "completion_latency_ms_p95": 100,
        "completion_latency_ms_max": 100,
        "decision_latency_ms_count": 0,
        "decision_latency_ms_total": None,
        "decision_latency_ms_p50": None,
        "decision_latency_ms_p95": None,
        "decision_latency_ms_max": None,
        "key_rotations": 0, "keys_quarantined": 0,
        "transient_retries_executed": 0, "input_tokens": 0,
    }
    after = {
        "completion_latency_ms_count": 2,
        "completion_latency_ms_total": 300,
        "completion_latency_ms_p50": 150,
        "completion_latency_ms_p95": 195,
        "completion_latency_ms_max": 200,
        "decision_latency_ms_count": 0,
        "decision_latency_ms_total": None,
        "decision_latency_ms_p50": None,
        "decision_latency_ms_p95": None,
        "decision_latency_ms_max": None,
        "key_rotations": 0, "keys_quarantined": 0,
        "transient_retries_executed": 0, "input_tokens": 0,
    }
    delta = _metrics_delta(before, after)
    assert delta is not None
    # jamas los percentiles inventados del ejemplo de Latwan
    assert not (
        delta["completion_latency_ms_max"] == 100
        and delta["completion_latency_ms_p50"] == 50
        and delta["completion_latency_ms_p95"] == 95
    )
    assert delta["completion_latency_ms_max"] is None
    assert delta["completion_latency_ms_p50"] is None
    assert delta["completion_latency_ms_p95"] is None
    # contadores, count y total siguen calculandose por diferencia
    assert delta["completion_latency_ms_count"] == 1
    assert delta["completion_latency_ms_total"] == 200


# --- MON-20 post-R1B (LATWAN DESIGN VERDICT): L-02 fase post-smoke,
# L-03 infraestructura local, L-04 North y L-06 presupuesto R1C ----------


def _fatal_with_http(status: int) -> Exception:
    """FatalProviderError con causa HTTPStatusError controlada (sin texto
    libre: solo la cadena de status)."""
    import httpx as _httpx

    from ludex_agent.graph.provider import FatalProviderError

    request = _httpx.Request(
        "POST", "https://opencode.ai/zen/v1/chat/completions"
    )
    raw = _httpx.HTTPStatusError(
        "boom", request=request,
        response=_httpx.Response(status, request=request, json={
            "error": {"message": "nope"},
        }),
    )
    try:
        raise FatalProviderError("provider permission or model unavailable") from raw
    except FatalProviderError as exc:
        return exc


def test_fallo_de_batalla_por_connection_closed_conserva_fase_y_es_externo():
    """L-02/L-03 (post-R1B): smoke verde + `ConnectionClosedError` de
    Showdown durante la batalla -> `externally-limited`, stage=battle,
    smoke_ok=True, battles_requested=2 (objetivo), metricas del smoke
    conservadas, clase/causa sanitizadas. Con el comportamiento anterior
    (except externo que resetea) este test se pone rojo.

    L-02 (correccion LATWAN): la identidad efectiva del pin se preserva
    (el smoke ya la demando) y W/L/T quedan en 0 (no hubo batalla)."""
    import asyncio

    from websockets.exceptions import ConnectionClosedError

    rows = [_ready_row("open_code_zen", "deepseek-v4-flash-free", "free")]
    results, _, battle_calls = asyncio.run(_run(
        rows, battle_raise=ConnectionClosedError(None, None),
    ))
    result = results[0]
    assert result.status == "externally-limited"
    assert result.smoke_ok is True
    assert result.failure_stage == "battle"
    assert result.battles_requested == 2
    assert result.battles_completed == 0
    assert result.battles_wins == 0
    assert result.battles_losses == 0
    assert result.battles_ties == 0
    # identidad efectiva = pin, jamas None (el smoke ya la demostro)
    assert result.effective_provider == "open_code_zen"
    assert result.effective_model == "deepseek-v4-flash-free"
    assert result.failure_type == "ConnectionClosedError"
    assert result.win_rate is None
    # metricas del smoke conservadas (delta del provider)
    assert result.tokens is not None
    assert result.completion_latency_ms is not None
    assert battle_calls == [2]


def test_batalla_2_fallida_con_partial_tipado_conserva_progreso_identidad_y_wlt():
    """L-02 (correccion LATWAN, canario vinculante): smoke verde, batalla 1
    termina, batalla 2 lanza `ConnectionClosedError`. La frontera real
    (`_benchmark_command`) lanza `BenchmarkFailure` con resultado parcial
    TIPADO; la matriz conserva requested=2, completed=1, W/L/T reales,
    provider/model efectivos iguales al pin, stage=battle,
    externally-limited y winrate=None. Mutar `_battle_failed` a
    completed=0, borrar effective_provider/model o perder W/L/T pone este
    canario rojo."""
    import asyncio

    from websockets.exceptions import ConnectionClosedError

    from ludex_agent.benchmark import BenchmarkResult

    rows = [_ready_row("open_code_zen", "deepseek-v4-flash-free", "free")]
    partial = BenchmarkResult(
        requested=2, completed=1, wins=1, losses=0, ties=0,
        provider="open_code_zen", model="deepseek-v4-flash-free",
        failure="ConnectionClosedError: server disconnected",
        failure_type="ConnectionClosedError", failure_cause_type=None,
    )
    results, _, battle_calls = asyncio.run(_run(
        rows, battle_wrapped=(partial, ConnectionClosedError(None, None)),
    ))
    result = results[0]
    assert result.status == "externally-limited"
    assert result.smoke_ok is True
    assert result.failure_stage == "battle"
    assert result.battles_requested == 2
    assert result.battles_completed == 1
    assert result.battles_wins == 1
    assert result.battles_losses == 0
    assert result.battles_ties == 0
    # identidad efectiva = pin (preservada del resultado parcial tipado)
    assert result.effective_provider == "open_code_zen"
    assert result.effective_model == "deepseek-v4-flash-free"
    assert result.failure_type == "ConnectionClosedError"
    assert result.win_rate is None
    # metricas acumuladas del smoke conservadas (delta del provider)
    assert result.tokens is not None
    assert result.completion_latency_ms is not None
    assert battle_calls == [2]


def test_cleanup_fallido_marcado_internal_defect_en_la_matriz():
    """L-01 (correccion LATWAN): un resultado con
    `failure_type=InternalCleanupError` (cleanup fallido sin primaria) se
    clasifica `internal-defect` — nunca `aborted` ni `compatible` — y no
    publica winrate."""
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, _ = asyncio.run(_run(
        rows,
        battles_completed=2, battles_requested=2,
        battle_failure="internal defect: fallo el cierre de recursos del benchmark",
        battle_failure_type="InternalCleanupError",
    ))
    result = results[0]
    assert result.status == "internal-defect"
    assert result.failure_stage == "battle"
    assert result.battles_completed == 2
    assert result.win_rate is None


def test_fallo_de_batalla_por_showdown_no_disponible_conserva_fase_y_es_externo():
    """L-02/L-03 (post-R1B): smoke verde + indisponibilidad local de
    Showdown (`ShowdownUnavailableError` desde `_check_showdown_reachable`,
    `RuntimeError from OSError`) -> `externally-limited`, stage=battle,
    clase y causa preservadas (RuntimeError/OSError), sin mensajes.

    L-02 (correccion LATWAN, counterweight): el preflight falla ANTES de
    crear players: completed=0 (no hubo batallas) y la identidad efectiva
    ya demostrada por el smoke se preserva (provider/model = pin)."""
    import asyncio

    from ludex_agent.benchmark import ShowdownUnavailableError

    try:
        raise OSError("connection refused")
    except OSError as exc:
        try:
            raise ShowdownUnavailableError(
                "No se pudo conectar a Showdown en localhost:8100"
            ) from exc
        except ShowdownUnavailableError as wrapped:
            showdown_error = wrapped

    rows = [_ready_row("open_code_zen", "laguna-s-2.1-free", "free")]
    results, _, _ = asyncio.run(_run(rows, battle_raise=showdown_error))
    result = results[0]
    assert result.status == "externally-limited"
    assert result.smoke_ok is True
    assert result.failure_stage == "battle"
    assert result.battles_requested == 2
    assert result.battles_completed == 0
    # identidad efectiva del pin preservada (demostrada por el smoke)
    assert result.effective_provider == "open_code_zen"
    assert result.effective_model == "laguna-s-2.1-free"
    # ShowdownUnavailableError ES un RuntimeError; la causa OSError se
    # preserva como clase, nunca el mensaje.
    assert isinstance(result.failure_type, str)
    assert result.failure_type == "ShowdownUnavailableError"
    assert result.failure_cause_type == "OSError"
    assert result.win_rate is None


def test_fallo_interno_genuino_post_smoke_sigue_siendo_internal_defect_con_fase():
    """L-02 (post-R1B): una excepcion interna genuina despues del smoke
    verde SIGUE siendo `internal-defect`, pero conserva smoke_ok=True,
    stage=battle y battles_requested=2; no se convierte en
    externally-limited ni resetea la fase."""
    import asyncio

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    results, _, _ = asyncio.run(_run(
        rows, battle_raise=ValueError("bug interno del runner"),
    ))
    result = results[0]
    assert result.status == "internal-defect"
    assert result.smoke_ok is True
    assert result.failure_stage == "battle"
    assert result.battles_requested == 2
    assert result.failure_type == "ValueError"
    assert result.win_rate is None


def test_fatal_401_upstream_se_clasifica_credential_model_unavailable():
    """L-04 (post-R1B): `FatalProviderError` con HTTP 401 upstream/
    model-wide (sin senal key-specific) -> `credential/model unavailable`,
    con cero rotacion y cero cuarentena (el provider real ya no rota: solo
    se reclasifica la categoria del artefacto)."""
    import asyncio

    rows = [_ready_row("open_code_zen", "north-mini-code-free", "free")]
    results, _, _ = asyncio.run(_run(rows, smoke_error=_fatal_with_http(401)))
    result = results[0]
    assert result.status == "credential/model unavailable"
    assert result.smoke_ok is False
    assert result.http_status == 401
    assert result.rotations == 0
    assert result.quarantined == 0


def test_fatal_403_upstream_se_clasifica_credential_model_unavailable():
    import asyncio

    rows = [_ready_row("open_code_zen", "north-mini-code-free", "free")]
    results, _, _ = asyncio.run(_run(rows, smoke_error=_fatal_with_http(403)))
    assert results[0].status == "credential/model unavailable"
    assert results[0].http_status == 403


def test_fatal_400_structured_output_sigue_siendo_unsupported_protocol():
    """L-04 (post-R1B): HTTP 400 por structured output/response_format
    sigue siendo `unsupported-protocol`; la categoria queda reservada para
    el rechazo de protocolo, no para la credencial."""
    import asyncio

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    results, _, _ = asyncio.run(_run(rows, smoke_error=_fatal_with_http(400)))
    assert results[0].status == "unsupported-protocol"
    assert results[0].http_status == 400


def test_fatal_404_y_500_y_sin_status_de_smoke_son_internal_defect():
    """T-08 (MON-20 R4): la taxonomia del runner queda structured-only y
    fail-closed: FatalProviderError con HTTP 404, 500 o sin status en el
    smoke NO autoriza a afirmar `unsupported-protocol` (que queda reservado
    a HTTP 400). 404/500/None -> internal-defect."""
    import asyncio

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    for status in (404, 500):
        results, _, _ = asyncio.run(_run(
            rows, smoke_error=_fatal_with_http(status),
        ))
        assert results[0].status == "internal-defect", status
        assert results[0].http_status == status, status
    # sin status estructurado: fail-closed a internal-defect
    from ludex_agent.graph.provider import FatalProviderError

    results, _, _ = asyncio.run(_run(
        rows, smoke_error=FatalProviderError("boom sin status"),
    ))
    assert results[0].status == "internal-defect"
    assert results[0].http_status is None


def test_taxonomia_runner_y_coverage_son_la_misma_tabla():
    """T-08 (MON-20 R4) + T-11 (MON-20 R5): canario ABSOLUTO de las TRES
    rutas de clasificacion. La tabla structured-only de FatalProviderError
    es UNA y produce exactamente el mismo veredicto para
    400/401/403/404/500/None en: (1) el smoke del runner, (2) la excepcion
    DIRECTA durante batalla (run_battles levanta el ProviderError), y
    (3) la normalizacion historica del generador (runtime_status aborted y
    unsupported-protocol). Si cualquiera de las tres rutas vuelve a
    colapsar su clasificacion, este test se pone rojo."""
    import asyncio
    import sys as _sys
    from pathlib import Path as _Path

    from ludex_agent.graph.provider import FatalProviderError

    evals_dir = _Path(__file__).resolve().parents[1] / "evals"
    if str(evals_dir) not in _sys.path:
        _sys.path.insert(0, str(evals_dir))
    import build_matrix_coverage as bmc  # noqa: E402

    expected = {
        400: "unsupported-protocol",
        401: "credential/model unavailable",
        403: "credential/model unavailable",
        404: "internal-defect",
        500: "internal-defect",
        None: "internal-defect",
    }
    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    for status, verdict in expected.items():
        # ruta 1: smoke del runner
        smoke_error = (
            FatalProviderError("boom") if status is None
            else _fatal_with_http(status)
        )
        results, _, _ = asyncio.run(_run(
            rows, smoke_error=smoke_error,
        ))
        assert results[0].status == verdict, ("smoke", status, results[0].status)
        # ruta 2: excepcion DIRECTA durante batalla (smoke verde y la
        # batalla levanta el mismo ProviderError)
        battle_error = (
            FatalProviderError("boom") if status is None
            else _fatal_with_http(status)
        )
        results, _, _ = asyncio.run(_run(
            rows, battle_raise=battle_error,
        ))
        assert results[0].status == verdict, ("battle", status, results[0].status)
        assert results[0].failure_stage == "battle", status
        assert results[0].smoke_ok is True, status
        assert results[0].http_status == status, status
        # ruta 3: normalizacion historica del generador (aborted y
        # unsupported-protocol viejo)
        for runtime in ("aborted", "unsupported-protocol"):
            assert bmc.normalize_final_classification(
                runtime, "FatalProviderError", status
            ) == verdict, (runtime, status)


def test_canario_clase_x_status_x_ruta_no_vacuo():
    """T-13 (MON-20 R6): la taxonomia reconciliada (D43.2 ↔ D54 R1) es
    una tabla UNICA clase × status × causa, y TODAS las rutas alcanzables
    producen el mismo veredicto. Cubre clase (Fatal, ProviderMixError,
    CredentialRejected, ProviderPoolExhausted con/sin causa CredentialRejected,
    TransientProviderError, DecisionDeadlineExceeded, ProviderError generico)
    × rutas (smoke, batalla directa, normalizacion aborted y
    unsupported-protocol). Bypassear cualquiera de las rutas vuelve rojo."""
    import asyncio
    import sys as _sys
    from pathlib import Path as _Path

    from ludex_agent.graph.provider import (
        CredentialRejected,
        DecisionDeadlineExceeded,
        FatalProviderError,
        ProviderError,
        ProviderMixError,
        ProviderPoolExhausted,
        TransientProviderError,
    )

    evals_dir = _Path(__file__).resolve().parents[1] / "evals"
    if str(evals_dir) not in _sys.path:
        _sys.path.insert(0, str(evals_dir))
    import build_matrix_coverage as bmc  # noqa: E402

    def pool_from_credential_rejected() -> ProviderPoolExhausted:
        cause = CredentialRejected("pool quarantined por credencial")
        pool = ProviderPoolExhausted("pool exhausted por credencial")
        pool.__cause__ = cause
        return pool

    # (clase, http_status, failure_cause_type, veredicto esperado)
    cases = [
        ("FatalProviderError", 400, None, "unsupported-protocol"),
        ("FatalProviderError", 401, None, "credential/model unavailable"),
        ("FatalProviderError", 403, None, "credential/model unavailable"),
        ("FatalProviderError", 404, None, "internal-defect"),
        ("FatalProviderError", 500, None, "internal-defect"),
        ("FatalProviderError", None, None, "internal-defect"),
        ("ProviderMixError", None, None, "internal-defect"),
        ("CredentialRejected", None, None, "credential/model unavailable"),
        # T-13: pool agotado POR credencial (D43.2) no es limite externo
        ("ProviderPoolExhausted", None, "CredentialRejected",
         "credential/model unavailable"),
        # pool transitorio (cooldown/cuota) si es limite externo (D54 R1)
        ("ProviderPoolExhausted", None, None, "externally-limited"),
        ("TransientProviderError", None, None, "externally-limited"),
        ("DecisionDeadlineExceeded", None, None, "externally-limited"),
        ("ProviderError", None, None, "externally-limited"),
    ]
    rows = [_ready_row("open_code_zen", "big-pickle", "free")]

    def build_error(failure_type, http_status, failure_cause_type):
        if failure_type == "FatalProviderError":
            return (
                FatalProviderError("boom") if http_status is None
                else _fatal_with_http(http_status)
            )
        if failure_type == "ProviderMixError":
            return ProviderMixError("mezcla efectiva")
        if failure_type == "CredentialRejected":
            return CredentialRejected("credencial rechazada")
        if failure_type == "ProviderPoolExhausted":
            if failure_cause_type == "CredentialRejected":
                return pool_from_credential_rejected()
            return ProviderPoolExhausted("pool transitorio")
        if failure_type == "TransientProviderError":
            return TransientProviderError("transitorio")
        if failure_type == "DecisionDeadlineExceeded":
            return DecisionDeadlineExceeded("deadline")
        return ProviderError("generico")

    for failure_type, http_status, failure_cause_type, verdict in cases:
        error = build_error(failure_type, http_status, failure_cause_type)
        # ruta smoke
        results, _, _ = asyncio.run(_run(rows, smoke_error=error))
        assert results[0].status == verdict, (
            "smoke", failure_type, http_status, results[0].status,
        )
        # ruta batalla directa
        results, _, _ = asyncio.run(_run(rows, battle_raise=error))
        assert results[0].status == verdict, (
            "battle", failure_type, http_status, results[0].status,
        )
        # ruta normalizacion historica (aborted y unsupported-protocol)
        for runtime in ("aborted", "unsupported-protocol"):
            assert bmc.normalize_final_classification(
                runtime, failure_type, http_status, failure_cause_type
            ) == verdict, (
                runtime, failure_type, http_status, failure_cause_type,
            )


def test_pool_agotado_por_credencial_no_es_limite_externo():
    """T-13 (MON-20 R6): reconciliacion D43.2 ↔ D54 R1. Un
    ProviderPoolExhausted encadenado desde CredentialRejected (pool
    totalmente en cuarentena por 401/403 credential-specific) se clasifica
    `credential/model unavailable`, NO `externally-limited`. La causa se
    lee SOLO de la cadena estructurada (nunca del mensaje)."""
    import asyncio

    from ludex_agent.graph.provider import (
        CredentialRejected, ProviderPoolExhausted,
    )

    try:
        raise CredentialRejected("pool quarantined por credencial")
    except CredentialRejected as cause:
        pool = ProviderPoolExhausted("pool exhausted")
        pool.__cause__ = cause

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    results, _, _ = asyncio.run(_run(rows, smoke_error=pool))
    assert results[0].status == "credential/model unavailable"
    assert results[0].failure_type == "ProviderPoolExhausted"
    assert results[0].failure_cause_type == "CredentialRejected"
    results, _, _ = asyncio.run(_run(rows, battle_raise=pool))
    assert results[0].status == "credential/model unavailable"


def test_pool_transitorio_sin_causa_credencial_es_limite_externo():
    """T-13 (MON-20 R6): un ProviderPoolExhausted SIN causa
    CredentialRejected (cooldown/cuota/pool transitorio) se clasifica
    `externally-limited` (D54 R1), incluso en el smoke que antes lo
    mapeaba siempre a credential."""
    import asyncio

    from ludex_agent.graph.provider import ProviderPoolExhausted

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    results, _, _ = asyncio.run(_run(
        rows, smoke_error=ProviderPoolExhausted("pool transitorio"),
    ))
    assert results[0].status == "externally-limited"
    assert results[0].failure_cause_type is None


def test_build_provider_provider_selection_error_es_credential():
    """T-13 (MON-20 R6): sitio 1 del inventario (build_provider falla). Un
    ProviderSelectionError en la CONSTRUCCION del provider se clasifica
    `credential/model unavailable` (nunca limite externo ni defecto)."""
    import asyncio

    from ludex_agent.graph.provider import ProviderSelectionError

    def build_provider(provider_name, model_name):
        raise ProviderSelectionError("sin seleccion activa")

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    results, _, _ = asyncio.run(_run(rows, build_provider=build_provider))
    assert results[0].status == "credential/model unavailable"
    assert results[0].failure_type == "ProviderSelectionError"
    assert results[0].failure_stage == "smoke"


def test_excepcion_directa_de_batalla_usa_la_misma_taxonomia():
    """T-11 (MON-20 R5): la excepcion ProviderError que llega DIRECTA del
    run_battles (no via BenchmarkResult tipado) se clasifica con la misma
    taxonomia: FatalProviderError 400 -> unsupported-protocol; 401/403 ->
    credential; 404/500/None -> internal-defect; ProviderMixError ->
    internal-defect; transitorio -> externally-limited. Antes del arreglo
    TODO ProviderError salvo ProviderMixError caia a externally-limited."""
    import asyncio

    from ludex_agent.graph.provider import (
        FatalProviderError, ProviderMixError, TransientProviderError,
    )

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    cases = [
        (_fatal_with_http(400), "unsupported-protocol"),
        (_fatal_with_http(401), "credential/model unavailable"),
        (_fatal_with_http(403), "credential/model unavailable"),
        (_fatal_with_http(404), "internal-defect"),
        (_fatal_with_http(500), "internal-defect"),
        (FatalProviderError("boom sin status"), "internal-defect"),
        (ProviderMixError("mezcla efectiva"), "internal-defect"),
        (TransientProviderError("transitorio"), "externally-limited"),
    ]
    for error, verdict in cases:
        results, _, _ = asyncio.run(_run(rows, battle_raise=error))
        result = results[0]
        assert result.status == verdict, (type(error).__name__, result.status)
        assert result.failure_stage == "battle"
        assert result.smoke_ok is True
        assert result.failure_type == type(error).__name__


def test_manifiesto_unitario_r1c_dentro_del_cap_y_sin_cumulative_stale():
    """L-06 (post-R1B): el manifiesto unitario R1C declara smoke 0.00616 +
    2 batallas 0.4536 = 0.45976 <= 0.60 y NO conserva el cumulative stale
    0.66056 del plan original. plan_budget falla cerrado si la reserva
    excede el cap de ronda."""
    import json as _json
    from decimal import Decimal
    from pathlib import Path

    from ludex_agent.matrix import BudgetSpec, ManifestRow, plan_budget

    budget = {
        "open_code_zen": BudgetSpec(
            balance_usd=Decimal("1"), cap_usd=Decimal("0.60"),
            leave_usd=Decimal("0"),
        ),
    }

    def unit_row(cost: str, smoke: str) -> ManifestRow:
        return ManifestRow(
            provider="open_code_zen", model="deepseek-v4-flash",
            protocol="chat_completions", endpoint=None,
            structured_output="json_schema", tier="paid", status="ready",
            battles=2, concurrency=1, persist=False,
            pin=("open_code_zen", "deepseek-v4-flash"),
            estimated_cost_usd=Decimal(cost),
            estimated_smoke_usd=Decimal(smoke),
        )

    dentro = plan_budget([unit_row("0.4536", "0.00616")], budget)
    assert dentro[0].status == "ready"
    assert dentro[0].cumulative_cost_usd == Decimal("0.45976")

    sobre = plan_budget([unit_row("0.6", "0.01")], budget)
    assert sobre[0].status == "pending-budget"
    assert "no alcanza" in (sobre[0].classification_note or "")

    # Canario repo: el manifiesto r1b/r1c commiteado cumple el contrato
    manifest = _json.loads((
        Path(__file__).resolve().parents[1] / "evals" / "runs"
        / "r1c-matrix-manifest.json"
    ).read_text(encoding="utf-8"))
    row = manifest["rows"][0]
    assert row["model"] == "deepseek-v4-flash"
    assert row["estimated_smoke_usd"] == "0.00616"
    assert row["estimated_cost_usd"] == "0.4536"
    assert row["cumulative_cost_usd"] == "0.45976"
    total = Decimal(row["estimated_smoke_usd"]) + Decimal(row["estimated_cost_usd"])
    assert total <= Decimal("0.60")


# --- MON-20 DIAG-B: precedencia de tier/precio en build_manifest -----------


def test_diagb_tier_override_free_con_precios_cero_sin_tabla_es_free_costo_cero():
    """DIAG-B A1: sin hit en la tabla de precios, `tier_override: free` del
    inventario con precios 0/0 produce tier free y costo 0 — nunca convierte
    un free verificado en unknown."""
    rows = build_manifest(
        {"open_code_zen": ["mimo-v2.5-free"]},
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": "mimo-v2.5-free", "in_scope": True,
                 "tier_override": "free",
                 "prices": {
                     "input_per_million": "0", "output_per_million": "0",
                     "source_url": "https://models.dev/",
                 }},
            ]},
        },
        tier_prices={},
        routes=_routes(),
    )
    row = rows[0]
    assert row.status == "ready"
    assert row.tier == "free"
    assert row.estimated_cost_usd == Decimal("0")
    assert row.estimated_smoke_usd == Decimal("0")
    assert "models.dev" in (row.classification_note or "")


def test_diagb_tier_override_unknown_con_precios_cero_sigue_unknown_y_pending():
    """DIAG-B A2: `tier_override: unknown` + precios 0/0 sin tabla PERMANECE
    unknown (nunca se convierte a free) y plan_budget lo deja pending-budget
    incluso con presupuesto disponible (no se puede probar costo cero)."""
    rows = build_manifest(
        {"google": ["gemini-2.5-flash"]},
        previous_inventory={
            "models": {"google": [
                {"id": "gemini-2.5-flash", "in_scope": True,
                 "tier_override": "unknown",
                 "prices": {
                     "input_per_million": "0", "output_per_million": "0",
                     "source_url": "https://models.dev/",
                 }},
            ]},
        },
        tier_prices={},
        routes=_routes(),
    )
    row = rows[0]
    assert row.tier == "unknown"
    assert row.estimated_cost_usd == Decimal("0")
    planned = plan_budget(rows, {
        "google": BudgetSpec(
            balance_usd=Decimal("1"), cap_usd=Decimal("1"),
            leave_usd=Decimal("0"),
        ),
    })
    assert planned[0].status == "pending-budget"
    assert planned[0].tier == "unknown"


def test_diagb_precios_no_cero_sin_override_infieren_paid():
    """DIAG-B A3: precios no cero del inventario sin hit de tabla y sin
    override infieren paid (no unknown)."""
    rows = build_manifest(
        {"open_code_zen": ["gpt-5.5"]},
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": "gpt-5.5", "in_scope": True,
                 "prices": {
                     "input_per_million": "5", "output_per_million": "30",
                     "source_url": "https://opencode.ai/docs/zen/",
                 }},
            ]},
        },
        tier_prices={},
        routes=_routes(),
    )
    row = rows[0]
    assert row.tier == "paid"
    assert row.estimated_cost_usd == Decimal("18.6")
    assert "opencode.ai/docs/zen" in (row.classification_note or "")


def test_diagb_sin_tabla_ni_precios_conserva_el_tier_override():
    """DIAG-B A4: sin hit ni precios, un `tier_override` explicito se
    conserva (free verificado sin precio sigue free/costo 0) y un modelo sin
    override ni datos queda unknown — jamas al reves."""
    rows = build_manifest(
        {"open_code_zen": ["gpt-5.5", "deepseek-v4-flash"]},
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": "gpt-5.5", "in_scope": True,
                 "tier_override": "free"},
                {"id": "deepseek-v4-flash", "in_scope": True},
            ]},
        },
        tier_prices={},
        routes=_routes(),
    )
    by_model = {(r.provider, r.model): r for r in rows}
    assert by_model[("open_code_zen", "gpt-5.5")].tier == "free"
    assert by_model[("open_code_zen", "gpt-5.5")].estimated_cost_usd == Decimal("0")
    assert by_model[("open_code_zen", "deepseek-v4-flash")].tier == "unknown"


def test_diagb_hit_de_tabla_manda_en_precios_y_override_solo_sobre_tier():
    """DIAG-B A5: con hit de tabla, precios y fuente de la tabla mandan; el
    `tier_override` explicito manda SOLO sobre el tier, incluso si vale
    `unknown`."""
    rows = build_manifest(
        {"open_code_zen": ["deepseek-v4-flash"]},
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": "deepseek-v4-flash", "in_scope": True,
                 "tier_override": "unknown"},
            ]},
        },
        tier_prices={
            ("open_code_zen", "deepseek-v4-flash"): (
                "paid", "0.14", "0.28", "zen-docs",
            ),
        },
        routes=_routes(),
    )
    row = rows[0]
    assert row.tier == "unknown"
    assert row.estimated_cost_usd == Decimal("0.4536")
    assert "zen-docs" in (row.classification_note or "")


def test_diagb_cinco_rutas_oficiales_construyen_manifiesto_esperado():
    """DIAG-B C: los cinco modelos verificados en zen docs (2026-08-14)
    construyen el manifiesto con `load_model_routes()` + la tabla real
    (default 08-14): ninguno queda `missing-route`, los protocolos son los
    literales aprobados y tiers/costos derivados a mano."""
    from ludex_agent.eval_cost import PricingTable
    from ludex_agent.graph.provider import load_model_routes

    routes = load_model_routes()
    pricing = PricingTable.load()
    assert pricing.table_id == "2026-08-14-zen-moonshot-modelsdev"
    five = [
        "gemini-3.7-flash", "grok-4.6", "muse-spark-1.2",
        "hy3-free", "nemotron-3.5-lightning-free",
    ]
    rows = build_manifest(
        {"open_code_zen": five},
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": model, "in_scope": True} for model in five
            ]},
        },
        tier_prices=tier_prices_from_pricing_table(pricing),
        routes=routes,
    )
    by_model = {r.model: r for r in rows}
    assert len(rows) == 5
    for model in five:
        assert by_model[model].status == "ready", model

    assert by_model["gemini-3.7-flash"].protocol == "google"
    assert by_model["gemini-3.7-flash"].structured_output == "json_schema"
    assert by_model["gemini-3.7-flash"].tier == "paid"
    assert by_model["gemini-3.7-flash"].estimated_cost_usd == Decimal("5.40")
    assert by_model["gemini-3.7-flash"].estimated_smoke_usd == Decimal("0.075")

    assert by_model["grok-4.6"].protocol == "responses"
    assert by_model["grok-4.6"].structured_output == "text_json"
    assert by_model["grok-4.6"].tier == "paid"
    assert by_model["grok-4.6"].estimated_cost_usd == Decimal("6.72")
    assert by_model["grok-4.6"].estimated_smoke_usd == Decimal("0.092")

    assert by_model["muse-spark-1.2"].protocol == "responses"
    assert by_model["muse-spark-1.2"].structured_output == "text_json"
    assert by_model["muse-spark-1.2"].tier == "paid"
    assert by_model["muse-spark-1.2"].estimated_cost_usd == Decimal("4.26")
    assert by_model["muse-spark-1.2"].estimated_smoke_usd == Decimal("0.0585")

    assert by_model["hy3-free"].protocol == "chat_completions"
    assert by_model["hy3-free"].structured_output == "text_json"
    assert by_model["hy3-free"].tier == "free"
    assert by_model["hy3-free"].estimated_cost_usd == Decimal("0")

    assert by_model["nemotron-3.5-lightning-free"].protocol == "chat_completions"
    assert by_model["nemotron-3.5-lightning-free"].structured_output == "text_json"
    assert by_model["nemotron-3.5-lightning-free"].tier == "free"
    assert by_model["nemotron-3.5-lightning-free"].estimated_cost_usd == Decimal("0")


# --- MON-20 R2 (Changes Requested): M1, I3, I4, I5, I2 -------------------


def test_get_type_hints_de_run_matrix_round_funciona():
    """M1: `matrix.py` importa Callable/Awaitable; sin ellos,
    `typing.get_type_hints(run_matrix_round)` reventaba con NameError y
    cualquier introspeccion futura (pydantic, typer, TypeAdapter) se rompe."""
    import typing

    from ludex_agent.matrix import run_matrix_round

    hints = typing.get_type_hints(run_matrix_round)
    assert "build_provider" in hints
    assert "run_battles" in hints
    assert "on_result" in hints


def test_artefactos_incluyen_contexto_de_corrida_auditable():
    """I4: el artefacto atomico es auditable sin contexto externo: persiste
    `battle_timeout_seconds`, identidad de ronda, `generated_at` y
    referencia+hash del manifiesto en TODAS las filas (compatible, fallida,
    stop y ya-finalizada)."""
    import asyncio
    import hashlib

    from ludex_agent.matrix import MatrixModelResult, run_matrix_round

    rows = [
        _ready_row("open_code_zen", "mimo-v2.5-free", "free"),
        _ready_row("google", "gemma-4-26b-a4b-it", "free"),
    ]
    done = MatrixModelResult(
        provider="google", model="gemma-4-26b-a4b-it", tier="free",
        protocol="google", status="compatible", smoke_ok=True,
        battles_requested=2, battles_completed=2,
        effective_provider="google", effective_model="gemma-4-26b-a4b-it",
        win_rate=None, completion_latency_ms=None,
        decision_latency_ms=None, tokens=None, retries=0, rotations=0,
        quarantined=0, failure_type=None, failure_cause_type=None,
        comparable=False, sample_size=2,
    )
    built: list[str] = []

    def build_provider(provider, model):
        built.append(model)
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        return _ok_battles()

    manifest_hash = hashlib.sha256(b"manifest-json").hexdigest()
    results = asyncio.run(run_matrix_round(
        rows=rows, tier="free", battle_timeout_seconds=1234.0,
        fmt="gen6randombattle", opponent="simple_heuristics",
        smoke_deadline_seconds=120,
        build_provider=build_provider, run_battles=run_battles,
        refresh_catalog=None,
        previous={"google/gemma-4-26b-a4b-it": done},
        round_name="r2-ctx", manifest_ref="20260814t183716z-matrix-manifest.json",
        manifest_sha256=manifest_hash,
    ))
    for result in results:
        assert result.battle_timeout_seconds == 1234.0, result.model
        assert result.round == "r2-ctx", result.model
        assert result.generated_at, result.model
        assert result.manifest == "20260814t183716z-matrix-manifest.json"
        assert result.manifest_sha256 == manifest_hash
    # la fila ya-finalizada tambien lleva el contexto de la corrida nueva
    final = next(r for r in results if r.status == "already-finalized")
    assert final.round == "r2-ctx"
    assert final.manifest_sha256 == manifest_hash


def test_compatible_no_publica_winrate_ni_n2_como_calidad():
    """I5: una corrida de 2 batallas (N=2) prueba compatibilidad funcional,
    nunca calidad: `win_rate` queda null y se conservan W/L/T +
    `comparable=false` + `sample_size`."""
    import asyncio

    rows = [_ready_row("google", "gemma-4-26b-a4b-it", "free")]
    results, _, _ = asyncio.run(_run(rows))
    result = results[0]
    assert result.status == "compatible"
    assert result.battles_completed == 2
    assert result.win_rate is None
    assert result.comparable is False
    assert result.sample_size == 2
    # W/L/T conservados como datos, no como winrate
    assert result.battles_wins == 1
    assert result.battles_losses == 1
    assert result.battles_ties == 0


def test_operador_prohibido_construye_fila_no_ejecutable_en_manifiesto():
    """I3: la politica declarativa versionada marca gpt-5.6-luna como
    `operator-prohibited` en manifiestos NUEVOS: battles=0, sin costo
    estimado, con la accion en la nota. No es un condicional por nombre en
    src: sale del archivo de politica."""
    rows = build_manifest(
        {"open_code_zen": ["gpt-5.6-luna", "mimo-v2.5-free"]},
        previous_inventory={
            "models": {"open_code_zen": [
                {"id": "gpt-5.6-luna", "in_scope": True},
                {"id": "mimo-v2.5-free", "in_scope": True},
            ]},
        },
    )
    by_model = {r.model: r for r in rows}
    luna = by_model["gpt-5.6-luna"]
    assert luna.status == "operator-prohibited"
    assert luna.battles == 0
    assert "operator-prohibited-never-retry" in (luna.classification_note or "")
    # el resto de las filas no se contamina
    assert by_model["mimo-v2.5-free"].status == "ready"


def test_matrix_run_rechaza_fila_prohibida_con_cero_llamadas():
    """I3: aunque un manifiesto (p.ej. el versionado) traiga gpt-5.6-luna
    como `ready`, matrix-run la rechaza ANTES del primer request: cero
    providers construidos, cero batallas, cero refrescos de catalogo."""
    import asyncio
    import pytest

    from ludex_agent.matrix import run_matrix_round

    rows = [
        _ready_row("open_code_zen", "gpt-5.6-luna", "paid"),
        _ready_row("open_code_zen", "mimo-v2.5-free", "free"),
    ]
    built: list[str] = []
    refreshes: list[str] = []

    def build_provider(provider, model):
        built.append(model)
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        raise AssertionError("no deberia llamarse")

    async def refresh():
        refreshes.append("refresh")
        return {}

    with pytest.raises(ValueError, match="operator-prohibited"):
        asyncio.run(run_matrix_round(
            rows=rows, tier="paid", battle_timeout_seconds=1800,
            fmt="gen6randombattle", opponent="simple_heuristics",
            smoke_deadline_seconds=120,
            build_provider=build_provider, run_battles=run_battles,
            refresh_catalog=refresh,
        ))
    assert built == []
    assert refreshes == []


def test_cancelacion_en_vuelo_durante_batalla_emite_stop_y_relanza():
    """I2: interrumpir una fila en vuelo (batalla) emite SINCRONICAMENTE por
    on_result un artefacto de stop sanitizado y RE-LANZA la misma excepcion
    (nunca se traga): la fila ya no se pierde entera."""
    import asyncio
    import json

    from ludex_agent.matrix import run_matrix_round

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    on_result_rows: list[dict] = []
    artifacts: list[str] = []

    def build_provider(provider, model):
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        await asyncio.Event().wait()  # cuelga hasta la cancelacion

    def on_result(result):
        on_result_rows.append(result.to_dict())
        artifacts.append(json.dumps(result.to_dict()))

    async def _scenario():
        task = asyncio.create_task(run_matrix_round(
            rows=rows, tier="free", battle_timeout_seconds=1800,
            fmt="gen6randombattle", opponent="simple_heuristics",
            smoke_deadline_seconds=120,
            build_provider=build_provider, run_battles=run_battles,
            refresh_catalog=None, on_result=on_result,
            round_name="r-cancel", manifest_ref="m.json",
            manifest_sha256="abc",
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(_scenario())
    # la excepcion no se trago: on_result corrio UNA vez, sincronicamente,
    # ANTES del re-lanzamiento, y dejo artefacto durable
    assert len(on_result_rows) == 1
    stop = on_result_rows[0]
    assert stop["provider"] == "open_code_zen"
    assert stop["model"] == "mimo-v2.5-free"
    assert stop["status"] == "internal-defect"
    assert stop["failure_type"] == "CancelledError"
    assert stop["failure_stage"] == "battle"
    assert stop["smoke_ok"] is True
    assert stop["compatibility_result"] == "indeterminate-current-run"
    assert stop["comparable"] is False
    assert stop["win_rate"] is None
    assert stop["battles_requested"] == 2
    assert stop["round"] == "r-cancel"
    assert len(artifacts) == 1


def test_cancelacion_durante_smoke_emite_stop_con_stage_smoke():
    """I2: la interrupcion durante el smoke (antes de la batalla) deja un
    stop con la etapa REAL (smoke), smoke_ok=false y 0 batallas pedidas."""
    import asyncio

    from ludex_agent.matrix import run_matrix_round

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    on_result_rows: list[dict] = []

    class _HangingProvider:
        def metrics_snapshot(self):
            return None

        async def complete(self, prompt, *, deadline, turn_id):
            await asyncio.Event().wait()

    def build_provider(provider, model):
        return _HangingProvider()

    async def run_battles(provider, model, **kwargs):
        raise AssertionError("no deberia llamarse")

    def on_result(result):
        on_result_rows.append(result.to_dict())

    async def _scenario():
        task = asyncio.create_task(run_matrix_round(
            rows=rows, tier="free", battle_timeout_seconds=1800,
            fmt="gen6randombattle", opponent="simple_heuristics",
            smoke_deadline_seconds=120,
            build_provider=build_provider, run_battles=run_battles,
            refresh_catalog=None, on_result=on_result,
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())
    stop = on_result_rows[0]
    assert stop["status"] == "internal-defect"
    assert stop["failure_stage"] == "smoke"
    assert stop["smoke_ok"] is False
    assert stop["battles_requested"] == 0
    assert stop["failure_type"] == "CancelledError"


def test_keyboard_interrupt_emite_stop_y_relanza_la_misma_excepcion():
    """I2: KeyboardInterrupt a mitad de batalla emite el stop y se relanza
    tal cual (no se convierte en fallo ordinario)."""
    import asyncio

    from ludex_agent.matrix import run_matrix_round

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    on_result_rows: list[dict] = []

    def build_provider(provider, model):
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        raise KeyboardInterrupt()

    def on_result(result):
        on_result_rows.append(result.to_dict())

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run_matrix_round(
            rows=rows, tier="free", battle_timeout_seconds=1800,
            fmt="gen6randombattle", opponent="simple_heuristics",
            smoke_deadline_seconds=120,
            build_provider=build_provider, run_battles=run_battles,
            refresh_catalog=None, on_result=on_result,
        ))
    stop = on_result_rows[0]
    assert stop["status"] == "internal-defect"
    assert stop["failure_type"] == "KeyboardInterrupt"
    assert stop["failure_stage"] == "battle"
    assert stop["compatibility_result"] == "indeterminate-current-run"


def test_system_exit_emite_stop_y_relanza_la_misma_excepcion():
    """I2: SystemExit a mitad de fila emite el stop y se relanza tal cual."""
    import asyncio

    from ludex_agent.matrix import run_matrix_round

    rows = [_ready_row("open_code_zen", "mimo-v2.5-free", "free")]
    on_result_rows: list[dict] = []

    def build_provider(provider, model):
        return _FakeSmokeProvider()

    async def run_battles(provider, model, **kwargs):
        raise SystemExit(3)

    def on_result(result):
        on_result_rows.append(result.to_dict())

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(run_matrix_round(
            rows=rows, tier="free", battle_timeout_seconds=1800,
            fmt="gen6randombattle", opponent="simple_heuristics",
            smoke_deadline_seconds=120,
            build_provider=build_provider, run_battles=run_battles,
            refresh_catalog=None, on_result=on_result,
        ))
    assert excinfo.value.code == 3
    assert on_result_rows[0]["failure_type"] == "SystemExit"


# --- MON-20 R7: los TRES INVARIANTES ENFORCED (punto 1 del brief) -----------


def _taxonomy_cases():
    """Tabla completa clase -> (http_status, failure_cause_type, veredicto)
    con la que el runner y la cobertura DEBEN coincidir. Incluye la cadena
    profunda que demuestra F1: la decision se limita a la causa DIRECTA (lo
    unico que el artefacto persiste)."""
    from ludex_agent.graph.provider import (
        CredentialRejected, DecisionDeadlineExceeded, FatalProviderError,
        ProviderError, ProviderMixError, ProviderPoolExhausted,
        TransientProviderError,
    )

    def _pool_deep_chain() -> ProviderPoolExhausted:
        # F1: CredentialRejected a profundidad 2 (causa directa = transitoria)
        cred = CredentialRejected("credencial profunda")
        trans = TransientProviderError("transitoria intermedia")
        trans.__cause__ = cred
        pool = ProviderPoolExhausted("pool profundo")
        pool.__cause__ = trans
        return pool

    def _pool_direct_credential() -> ProviderPoolExhausted:
        cause = CredentialRejected("pool quarantined")
        pool = ProviderPoolExhausted("pool exhausted")
        pool.__cause__ = cause
        return pool

    def _fatal(status):
        return (
            FatalProviderError("boom") if status is None
            else _fatal_with_http(status)
        )

    from ludex_agent.graph.provider import QuotaExceeded as _QuotaExceeded

    return [
        # (nombre, excepcion, veredicto esperado)
        ("Fatal 400", _fatal(400), "unsupported-protocol"),
        ("Fatal 401", _fatal(401), "credential/model unavailable"),
        ("Fatal 403", _fatal(403), "credential/model unavailable"),
        ("Fatal 404", _fatal(404), "internal-defect"),
        ("Fatal 500", _fatal(500), "internal-defect"),
        ("Fatal None", _fatal(None), "internal-defect"),
        ("ProviderMixError", ProviderMixError("mix"), "internal-defect"),
        ("CredentialRejected", CredentialRejected("cred"), "credential/model unavailable"),
        ("Pool causa directa Credential", _pool_direct_credential(),
         "credential/model unavailable"),
        ("Pool sin causa", ProviderPoolExhausted("pool"),
         "externally-limited"),
        ("Pool cadena profunda Credential", _pool_deep_chain(),
         "externally-limited"),
        ("TransientProviderError", TransientProviderError("trans"),
         "externally-limited"),
        ("DecisionDeadlineExceeded", DecisionDeadlineExceeded("deadline"),
         "externally-limited"),
        ("ProviderError generico", ProviderError("gen"),
         "externally-limited"),
        ("QuotaExceeded", _QuotaExceeded("quota agotada"), "externally-limited"),
    ]


def test_round_trip_runner_serializacion_normalize():
    """INVARIANTE 1a (MON-20 R7): para CADA clase de la tabla, el veredicto
    que el runner decide se reproduce EXACTAMENTE desde el artefacto
    serializado: clasificar con el runner real, serializar el
    MatrixModelResult tal como se persiste, re-derivar desde ese JSON con
    normalize_final_classification y exigir igualdad, por TODAS las rutas
    alcanzables (smoke y batalla directa; build_provider para selection).
    Con F1 este test esta ROJO: el runner camina la cadena de causas y
    decide credential, pero el artefacto persiste solo la causa directa y
    la cobertura re-deriva externally-limited."""
    import asyncio
    import sys as _sys
    from pathlib import Path as _Path

    evals_dir = _Path(__file__).resolve().parents[1] / "evals"
    if str(evals_dir) not in _sys.path:
        _sys.path.insert(0, str(evals_dir))
    import build_matrix_coverage as bmc  # noqa: E402

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    for name, error, expected in _taxonomy_cases():
        if name == "QuotaExceeded":
            # KeyRotatingProvider captura QuotaExceeded y no la propaga,
            # pero la clasificacion por la tabla SI es alcanzable en la
            # normalizacion historica y en la fuente unica.
            assert bmc.normalize_final_classification(
                "aborted", "QuotaExceeded", None, None
            ) == expected, name
            assert bmc.provider_failure_class("QuotaExceeded", None, None) \
                == expected
            continue
        # ruta smoke
        smoke_results, _, _ = asyncio.run(_run(rows, smoke_error=error))
        smoke = smoke_results[0]
        assert smoke.status == expected, (name, "smoke", smoke.status)
        _assert_round_trip(smoke, expected, name, "smoke", bmc)
        # ruta batalla directa
        battle_results, _, _ = asyncio.run(_run(rows, battle_raise=error))
        battle = battle_results[0]
        assert battle.status == expected, (name, "battle", battle.status)
        _assert_round_trip(battle, expected, name, "battle", bmc)


def _assert_round_trip(result, expected, name, route, bmc):
    """El JSON serializado (lo que se persiste) re-deriva el MISMO veredicto
    que el runner decidio. Con F1 esto falla para la cadena profunda."""
    serialized = result.to_dict()
    rederived = bmc.normalize_final_classification(
        serialized["status"],
        serialized["failure_type"],
        serialized["http_status"],
        serialized["failure_cause_type"],
    )
    assert rederived == result.status, (
        name, route, "runner:", result.status,
        "failure_cause_type persistido:", serialized["failure_cause_type"],
        "rederivado:", rederived,
    )
    assert result.status == expected


def test_introspeccion_subclases_provider_error_entran_en_tabla():
    """INVARIANTE 1b (MON-20 R8, F-A): la membresia de la tabla se DERIVA
    de la tabla misma, no se declara. R8 borro EXPLICIT_CLASSES (frozenset
    a mano desacoplado de las ramas): el invariante le exige a
    `explicit_failure_class` una rama EXPLICITA para cada clase del
    universo, via el sentinel del fail-closed: el probe
    `explicit_failure_class(nombre, None, None)` no debe devolver None.
    Universo: la jerarquia de ProviderError por introspeccion (9 clases) +
    las dos clases de benchmark.py que la tabla clasifica y que NO heredan
    de ProviderError (InternalCleanupError, BenchmarkDeadlineExceeded),
    importadas como objetos de clase, nunca como strings a mano.
    LIMITACION (escrita): una clase nueva agregada FUERA de la jerarquia
    de ProviderError y fuera de esas dos clases de benchmark.py no entra
    al universo de probes de este invariante y el fail-closed la absorbe.
    Mutaciones medidas que DETECTA (R8): subclase nueva sin rama;
    sacar InternalCleanupError de la rama; sacar BenchmarkDeadlineExceeded
    de la rama (las tres dejan 1b ROJO)."""
    from ludex_agent.benchmark import (
        BenchmarkDeadlineExceeded,
        InternalCleanupError,
    )
    from ludex_agent.graph.provider import ProviderError
    from ludex_agent.provider_taxonomy import (
        explicit_failure_class,
        provider_failure_class,
    )

    seen: set[str] = set()
    frontier = [ProviderError]
    while frontier:
        current = frontier.pop()
        if current.__name__ in seen:
            continue
        seen.add(current.__name__)
        frontier.extend(current.__subclasses__())
    assert "ProviderError" in seen
    assert len(seen) == 9, seen  # base + 8 subclases
    universe = sorted(
        seen
        | {InternalCleanupError.__name__, BenchmarkDeadlineExceeded.__name__}
    )
    # INVARIANTE 1b: ninguna clase del universo cae al fail-closed de la
    # tabla (None = sin rama explicita = clase olvidada).
    missing = [
        name for name in universe
        if explicit_failure_class(name, None, None) is None
    ]
    assert not missing, (
        f"clases sin rama explicita en la tabla (fail-closed): {missing}"
    )
    # QuotaExceeded es externo por definicion (limite de cuota)
    assert provider_failure_class("QuotaExceeded", None, None) == \
        "externally-limited"


def test_literales_taxonomia_solo_en_sitios_allowlist():
    """INVARIANTE 1c (MON-20 R7/R8): los literales de la taxonomia que se
    PRODUCEN fuera de la fuente unica (provider_taxonomy.py) estan
    declarados en una allowlist por (archivo, funcion, literal) con
    justificacion escrita. R8 extendio el scan de matrix.py a
    build_matrix_coverage.py (sitios: _FAITHFUL y el set de re-derivacion
    de normalize_final_classification).
    LIMITACIONES (escritas, no cubiertas a proposito):
    - NO detecta literales construidos dinamicamente (concatenacion,
      f-strings, constantes armadas en runtime);
    - NO detecta literales partidos en varias lineas (continuaciones de
      string, `"exter" "nally-limited"`); solo mira una linea a la vez;
    - solo detecta contextos de PRODUCCION en una linea: asignacion
      `status =`, `return `, `_fail("`, `_smoke_failed(x, "` y miembros
      de set/frozenset (precedidos por `{` o `,` en la misma linea, o
      solos en su linea); comparaciones, comentarios y docstrings no
      matchean;
    - solo escanea los dos archivos listados aca; un archivo nuevo con un
      sitio nuevo no se escanea hasta agregarlo explicitamente.
    Mutaciones medidas que DETECTA (R8): literal nuevo producido en
    matrix.py en sitio no allowlisted; literal nuevo producido en
    build_matrix_coverage.py (ambas dejan 1c ROJO)."""
    import re
    from pathlib import Path as _Path

    _root = _Path(__file__).resolve().parents[1]

    def _function_at(text: str, lineno: int) -> str:
        current = "<module>"
        for i, line in enumerate(text.splitlines(), start=1):
            if i > lineno:
                break
            m = re.match(r"^(?:async )?def (\w+)", line)
            if m:
                current = m.group(1)
            elif line[:1] not in ("", " ", "\t", ")", "]", "}", ",") \
                    and line.strip():
                # codigo de nivel modulo (constantes, docstrings, imports):
                # el alcance de funcion termino. Las continuaciones de
                # firma en col 0 (`)`/`]`/`}`/`,`) no cortan el alcance.
                current = "<module>"
        return current

    def _sites(text: str) -> set[tuple[str, str, int]]:
        literals = (
            "internal-defect", "externally-limited", "unsupported-protocol",
            "credential/model unavailable",
        )
        sites = set()
        for i, line in enumerate(text.splitlines(), start=1):
            for literal in literals:
                # solo sitios que PRODUCEN el literal (asignacion/return/
                # _fail/_smoke_failed o miembro de set/frozenset), no
                # comparaciones ni docstrings. Un miembro solitario en su
                # linea (primer miembro de un set multilinea) tambien
                # cuenta: una linea cuyo unico contenido es el literal.
                if re.search(
                    rf'(?:status\s*=\s*|return\s+|_fail\("|'
                    rf'_smoke_failed\(\w+,\s*"|[,{{]\s*)'
                    rf'"{re.escape(literal)}"', line
                ) or re.search(
                    rf'^\s*"{re.escape(literal)}",?\s*$', line
                ):
                    sites.add((_function_at(text, i), literal, i))
        return sites

    files = {
        "matrix.py": _root / "src" / "ludex_agent" / "matrix.py",
        "build_matrix_coverage.py": _root / "evals" / "build_matrix_coverage.py",
    }
    allowlists = {
        "matrix.py": {
            # except Exception del runner: fail-closed del runner, no
            # fallo de provider.
            ("run_matrix_round", "internal-defect"),
            # I2: stop por interrupcion, compatibilidad indeterminada.
            ("_terminal_stop_result", "internal-defect"),
            # conteo parcial de benchmark sin failure: no es veredicto de
            # provider (la alternativa fail-closed acusaria a la casa un
            # parcial reportado sin error de proveedor).
            ("_run_one", "externally-limited"),
            # L-03: taxonomia de infraestructura LOCAL de Showdown,
            # deliberadamente fuera de la fuente unica (D60/R8 F-C).
            ("_battle_infrastructure_status", "externally-limited"),
            ("_battle_infrastructure_status", "internal-defect"),
            # FINAL_STATUSES (modulo): vocabulario de validacion del
            # artefacto persistido, no una derivacion de veredicto.
            ("<module>", "internal-defect"),
            ("<module>", "externally-limited"),
            ("<module>", "unsupported-protocol"),
            ("<module>", "credential/model unavailable"),
        },
        "build_matrix_coverage.py": {
            # _FAITHFUL (modulo): passthrough de clases terminales no
            # re-derivables.
            ("<module>", "internal-defect"),
            # normalize_final_classification: set de clases re-derivables
            # (T-08/F6), no una derivacion paralela.
            ("normalize_final_classification", "unsupported-protocol"),
            ("normalize_final_classification", "credential/model unavailable"),
        },
    }
    offenders: set[tuple[str, str, str, int]] = set()
    for name, path in files.items():
        text = path.read_text(encoding="utf-8")
        for func, literal, lineno in _sites(text):
            if (func, literal) not in allowlists[name]:
                offenders.add((name, func, literal, lineno))
    assert not offenders, (
        f"literales de taxonomia fuera de la allowlist: {offenders}"
    )


def test_ruteo_sitio1_build_provider_por_la_fuente_unica():
    """F3 (MON-20 R7): canario de RUTEO del sitio 1 (build_provider). Usa una
    clase cuyo veredicto DIFIERE del literal viejo
    (credential/model unavailable): TransientProviderError -> externally-
    limited. Si el sitio vuelve al literal fijo, este test se pone rojo.
    """
    import asyncio

    from ludex_agent.graph.provider import TransientProviderError

    def build_provider(provider_name, model_name):
        raise TransientProviderError("transitorio en construccion")

    rows = [_ready_row("open_code_zen", "big-pickle", "free")]
    results, _, _ = asyncio.run(_run(rows, build_provider=build_provider))
    assert results[0].status == "externally-limited"
    assert results[0].failure_type == "TransientProviderError"
    assert results[0].failure_stage == "smoke"


def test_matrix_run_ejecuta_sus_batallas_via_benchmark_command(tmp_path, monkeypatch):
    """MON-35 (requisito 2): las batallas de `matrix-run` se ejecutan a
    traves de `cli._benchmark_command`, el UNICO punto que inyecta la
    politica que nunca gatea: la garantia offline del benchmark se
    extiende a la matriz por delegacion, no por una politica paralela."""
    import asyncio
    import json as _json

    from ludex_agent import cli as cli_module
    from ludex_agent.benchmark import BenchmarkResult
    from ludex_agent.cli import app
    from typer.testing import CliRunner

    manifest = tmp_path / "manifest.json"
    manifest.write_text(_json.dumps({"rows": [{
        "provider": "open_code_zen", "model": "mimo-v2.5-free",
        "protocol": "chat_completions", "endpoint": None,
        "structured_output": "json_schema", "tier": "free",
        "status": "ready", "battles": 2, "concurrency": 1,
        "persist": False, "pin": ["open_code_zen", "mimo-v2.5-free"],
        "estimated_cost_usd": "0", "estimated_smoke_usd": "0",
        "classification_note": "",
    }]}))
    calls: list[dict] = []

    async def fake_benchmark_command(**kwargs):
        calls.append(dict(kwargs))
        return (
            BenchmarkResult(
                requested=1, completed=1, wins=0, losses=1, ties=0,
            ),
            {},
        )

    async def fake_run_matrix_round(**kwargs):
        await kwargs["run_battles"](
            "open_code_zen", "mimo-v2.5-free", n=1,
            battle_timeout_seconds=60, fmt="gen6randombattle",
            opponent="simple_heuristics",
        )
        return []

    monkeypatch.setattr(cli_module, "_benchmark_command", fake_benchmark_command)
    monkeypatch.setattr(
        "ludex_agent.matrix.run_matrix_round", fake_run_matrix_round
    )
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://x:x@localhost:15432/x"
    )
    monkeypatch.setenv("OPEN_CODE_ZEN_API_KEY", "fake-key")
    monkeypatch.setenv("OPEN_CODE_ZEN_BASE_URL", "https://opencode.ai/zen/v1")
    for secret_env in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY",
                       "GOOGLE_API_KEYS", "KIMI_API_KEY", "KIMI_BASE_URL"):
        monkeypatch.delenv(secret_env, raising=False)

    result = CliRunner().invoke(
        app,
        ["matrix-run", "--manifest", str(manifest), "--tier", "free",
         "--round", "test-delegation", "--zen-auto-reload-confirmed"],
    )
    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1, (
        f"las batallas de matrix-run deben pasar por _benchmark_command: {calls}"
    )
    assert calls[0]["provider_name"] == "open_code_zen"
    assert calls[0]["model"] == "mimo-v2.5-free"
    assert calls[0]["battle_timeout_seconds"] == 60
