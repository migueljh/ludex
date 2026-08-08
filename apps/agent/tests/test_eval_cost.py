from decimal import Decimal

import pytest

from ludex_agent.eval_cost import (
    PriceEntry,
    PricingTable,
    calculate_cost,
)


def test_costo_se_calcula_por_clase_de_token_no_por_llamada():
    usage = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 250_000,
        "output_tokens": 100_000,
    }
    price = PriceEntry(
        provider="open_code_zen",
        model="minimax-m2.7",
        input_per_million=Decimal("0.30"),
        output_per_million=Decimal("1.20"),
        cached_input_per_million=Decimal("0.06"),
        checked_at="2026-07-28",
        source_url="https://opencode.ai/docs/zen/",
    )

    assert calculate_cost(usage, price) == Decimal("0.360")


def test_costo_desconocido_no_se_convierte_en_cero():
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 10,
    }
    price = PriceEntry(
        provider="example",
        model="missing-cache-price",
        input_per_million=Decimal("1"),
        output_per_million=Decimal("2"),
        cached_input_per_million=None,
        checked_at="2026-07-28",
        source_url="https://example.test/pricing",
    )

    assert calculate_cost(usage, price) is None


def test_tabla_versionada_carga_modelos_y_fuentes_oficiales():
    table = PricingTable.load()

    minimax = table.price("open_code_zen", "minimax-m2.7")
    assert table.table_id == "2026-07-28-official"
    assert minimax.input_per_million == Decimal("0.30")
    assert minimax.output_per_million == Decimal("1.20")
    assert minimax.cached_input_per_million == Decimal("0.06")
    assert minimax.checked_at == "2026-07-28"
    assert minimax.source_url == "https://opencode.ai/docs/zen/"

    deepseek_flash = table.price("open_code_zen", "deepseek-v4-flash")
    assert deepseek_flash.input_per_million == Decimal("0.14")
    assert deepseek_flash.output_per_million == Decimal("0.28")
    assert deepseek_flash.cached_input_per_million == Decimal("0.028")
    assert deepseek_flash.checked_at == "2026-07-28"
    assert deepseek_flash.source_url == "https://opencode.ai/docs/zen/"


def test_modelo_sin_precio_deja_hueco_honesto():
    table = PricingTable.load()

    with pytest.raises(KeyError, match="sin precio"):
        table.price("open_code_zen", "modelo-inventado")


def test_calculo_rechaza_none_en_campos_de_tokens():
    """R3: `calculate_cost` consume solo campos de tokens y rechaza None en
    ellos; los percentiles de latencia nullable no se leen aca."""
    price = PriceEntry(
        provider="open_code_zen",
        model="minimax-m2.7",
        input_per_million=Decimal("0.30"),
        output_per_million=Decimal("1.20"),
        cached_input_per_million=Decimal("0.06"),
        checked_at="2026-07-28",
        source_url="https://opencode.ai/docs/zen/",
    )
    usage_con_none_en_tokens = {
        "input_tokens": None,
        "cached_input_tokens": 0,
        "output_tokens": 10,
    }
    with pytest.raises(ValueError, match="token usage fields cannot be None"):
        calculate_cost(usage_con_none_en_tokens, price)


def test_calculo_ignora_percentiles_nullable_y_costo_se_mantiene():
    """R3: un snapshot de `DecisionMetrics` con percentiles de latencia en
    None (sin muestras) no debe romper el costo: los tokens son int."""
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 0,
        "output_tokens": 10,
        "completion_latency_ms_p50": None,
        "completion_latency_ms_p95": None,
        "decision_latency_ms_p50": None,
        "decision_latency_ms_max": None,
    }
    price = PriceEntry(
        provider="open_code_zen",
        model="minimax-m2.7",
        input_per_million=Decimal("0.30"),
        output_per_million=Decimal("1.20"),
        cached_input_per_million=Decimal("0.06"),
        checked_at="2026-07-28",
        source_url="https://opencode.ai/docs/zen/",
    )

    assert calculate_cost(usage, price) == Decimal("0.000042")
