"""Pruebas del runner/matriz dinamica (F2-10B/MON-20)."""

from __future__ import annotations

from decimal import Decimal

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
    # gpt-5.5 (5/30) y gemini-3.6-flash (1.5/7.5) son caros: con cap 10 y
    # leave 1, el hard-stop debe dejar alguno en pending-budget o todos
    # ready pero con acumulado <= 9.
    total = sum(
        (r.cumulative_cost_usd or Decimal("0"))
        for r in ready if r.cumulative_cost_usd is not None
    )
    assert total <= Decimal("9") or any(
        r.status == "pending-budget" for r in planned
    )


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
