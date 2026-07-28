from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ludex_agent.benchmark import BenchmarkResult
from ludex_agent.eval_cost import PricingTable
from ludex_agent.eval_report import (
    append_ledger_row,
    build_benchmark_record,
    write_run_json,
)
from ludex_agent.graph.provider import ModelRoute


def _metrics():
    return {
        "turns_total": 300,
        "calls_total": 300,
        "input_tokens": 20_000,
        "output_tokens": 2_500,
        "cached_input_tokens": 0,
        "reasoning_tokens": 900,
        "turns_model_invalid": 3,
        "turns_fallback": 1,
        "turns_deadline_affected": 0,
        "key_rotations": 0,
    }


def test_registro_completo_deriva_porcentajes_y_costo_por_batalla():
    result = BenchmarkResult(
        requested=15,
        completed=15,
        wins=5,
        losses=10,
        ties=0,
        provider="open_code_zen",
        model="minimax-m2.7",
    )
    table = PricingTable.load()

    record = build_benchmark_record(
        run_id="test-minimax",
        created_at=datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=table,
    )

    assert record.status == "complete"
    assert record.calls_per_battle == Decimal("20")
    assert record.invalid_recovered_pct.quantize(
        Decimal("0.0000000001")
    ) == Decimal("0.0066666667")
    assert record.fallback_pct.quantize(
        Decimal("0.0000000001")
    ) == Decimal("0.0033333333")
    assert record.total_cost == Decimal("0.009")
    assert record.cost_per_battle == Decimal("0.0006")
    assert record.projected_10k_cost == Decimal("6.0000")


def test_corrida_abortada_no_publica_winrate_ni_costo_por_batalla():
    result = BenchmarkResult(
        requested=15,
        completed=4,
        wins=2,
        losses=2,
        ties=0,
        provider="kimi",
        model="kimi-k2.6",
        failure="ProviderPoolExhausted: quota",
    )

    record = build_benchmark_record(
        run_id="test-aborted",
        created_at=datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(
            protocol="chat_completions",
            temperature=1.0,
            thinking="enabled",
            max_tokens=16_000,
        ),
        pricing=PricingTable.load(),
    )

    assert record.status == "aborted"
    assert record.win_rate is None
    assert record.wilson95 is None
    assert record.total_cost is not None
    assert record.cost_per_battle is None
    assert record.projected_10k_cost is None


def test_json_y_ledger_rechazan_sobrescritura_y_conservan_fuente(tmp_path):
    result = BenchmarkResult(
        requested=15,
        completed=15,
        wins=5,
        losses=10,
        ties=0,
        provider="open_code_zen",
        model="minimax-m2.7",
    )
    record = build_benchmark_record(
        run_id="test-minimax",
        created_at=datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )
    artifact = tmp_path / "runs" / "test-minimax.json"
    ledger = tmp_path / "BENCHMARKS.md"

    write_run_json(record, artifact)
    append_ledger_row(record, ledger, artifact)

    rendered = artifact.read_text()
    markdown = ledger.read_text()
    assert '"pricing_table_id": "2026-07-28-official"' in rendered
    assert "test-minimax" in markdown
    assert "test-minimax.json" in markdown
    with pytest.raises(FileExistsError):
        write_run_json(record, artifact)


def test_run_id_no_permite_rutas_ni_espacios():
    result = BenchmarkResult(
        requested=1,
        completed=1,
        wins=1,
        losses=0,
        ties=0,
        provider="open_code_zen",
        model="minimax-m2.7",
    )
    with pytest.raises(ValueError, match="run_id"):
        build_benchmark_record(
            run_id="../bad id",
            created_at=datetime.now(timezone.utc),
            result=result,
            metrics=_metrics(),
            opponent="simple_heuristics",
            fmt="gen6randombattle",
            route=ModelRoute(protocol="chat_completions"),
            pricing=PricingTable.load(),
        )
