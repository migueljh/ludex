from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ludex_agent.benchmark import BenchmarkResult
from ludex_agent.eval_cost import PricingTable
from ludex_agent.eval_report import (
    append_ledger_row,
    build_benchmark_record,
    write_run_json,
    write_run_snapshot,
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
        "turns_transient_affected": 7,
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


def test_corrida_parcial_conserva_costo_observado_por_batalla():
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
        status="running",
    )

    assert record.status == "running"
    assert record.win_rate is None
    assert record.wilson95 is None
    assert record.total_cost is not None
    assert record.calls_per_battle == Decimal("75")
    assert record.cost_per_battle == Decimal("0.00725")
    assert record.projected_10k_cost == Decimal("72.50000")


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


def test_ledger_inserta_la_corrida_en_la_tabla_antes_de_notas(tmp_path):
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
        run_id="test-con-notas",
        created_at=datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )
    artifact = tmp_path / "runs" / "test-con-notas.json"
    ledger = tmp_path / "BENCHMARKS.md"
    ledger.write_text(
        "# Benchmarks de modelos\n\n"
        "| Run | Transitorios |\n"
        "|---|---:|\n"
        "| anterior | 0 |\n\n"
        "## Controles parciales\n\n"
        "- evidencia histórica\n",
        encoding="utf-8",
    )

    append_ledger_row(record, ledger, artifact)

    markdown = ledger.read_text(encoding="utf-8")
    assert markdown.index("test-con-notas") < markdown.index("## Controles parciales")
    assert "| 7 |" in markdown


def test_snapshot_parcial_se_reemplaza_atomicamente(tmp_path):
    artifact = tmp_path / "runs" / "partial.json"
    base = dict(
        run_id="partial",
        created_at=datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
        status="running",
    )
    first = build_benchmark_record(
        result=BenchmarkResult(
            requested=15, completed=1, wins=1, losses=0, ties=0,
            provider="open_code_zen", model="minimax-m2.7",
        ),
        **base,
    )
    second = build_benchmark_record(
        result=BenchmarkResult(
            requested=15, completed=2, wins=1, losses=1, ties=0,
            provider="open_code_zen", model="minimax-m2.7",
        ),
        **base,
    )

    write_run_snapshot(first, artifact)
    write_run_snapshot(second, artifact)

    rendered = artifact.read_text()
    assert '"completed": 2' in rendered
    assert '"wins": 1' in rendered
    assert not artifact.with_suffix(".json.tmp").exists()


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


def _metrics_con_latencia():
    metrics = _metrics()
    metrics.update({
        "completion_latency_ms_count": 3,
        "completion_latency_ms_total": 600,
        "completion_latency_ms_p50": 200,
        "completion_latency_ms_p95": 220,
        "completion_latency_ms_max": 250,
        "decision_latency_ms_count": 3,
        "decision_latency_ms_total": 900,
        "decision_latency_ms_p50": 300,
        "decision_latency_ms_p95": 310,
        "decision_latency_ms_max": 320,
    })
    return metrics


def _aborted_result(failure="QuotaExceeded: provider quota exhausted"):
    return BenchmarkResult(
        requested=15, completed=3, wins=1, losses=2, ties=0,
        provider="kimi", model="kimi-k2.6", failure=failure,
    )


def test_record_con_muestras_expone_ambas_poblaciones_y_ledger_distinguido(tmp_path):
    """L-01 (R2): con muestras, el record y el ledger distinguen por nombre
    completion vs decision, y cada poblacion lleva sus propios valores."""
    record = build_benchmark_record(
        run_id="test-latency-both",
        created_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        result=BenchmarkResult(
            requested=3, completed=3, wins=1, losses=2, ties=0,
            provider="kimi", model="kimi-k2.6",
        ),
        metrics=_metrics_con_latencia(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )
    artifact = tmp_path / "runs" / "test-latency-both.json"
    ledger = tmp_path / "BENCHMARKS.md"
    write_run_snapshot(record, artifact)
    append_ledger_row(record, ledger, artifact)

    rendered = artifact.read_text()
    assert '"completion_latency_ms_total": 600' in rendered
    assert '"completion_latency_ms_p50": 200' in rendered
    assert '"decision_latency_ms_total": 900' in rendered
    assert '"decision_latency_ms_p50": 300' in rendered
    markdown = ledger.read_text()
    assert "Completion p50/p95/max (ms)" in markdown
    assert "Decision p50/p95/max (ms)" in markdown
    assert "200/220/250" in markdown
    assert "300/310/320" in markdown


def test_corrida_abortada_con_progreso_no_publica_latencia_comparable_ni_winrate(tmp_path):
    """L-01/L-02 (R2): un run abortado con progreso real (3/15) puede
    versionarse como abortado; el artefacto JSON conserva los valores reales
    como evidencia, pero el LEDGER no publica latencia ni winrate de runs
    incompletos: celdas blancas, nunca 0/0/0 comparable."""
    metrics = _metrics_con_latencia()
    metrics["completion_latency_ms_count"] = 0
    for key in ("total", "p50", "p95", "max"):
        metrics[f"completion_latency_ms_{key}"] = None
    record = build_benchmark_record(
        run_id="test-aborted-latency",
        created_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        result=_aborted_result(),
        metrics=metrics,
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )
    artifact = tmp_path / "runs" / "test-aborted-latency.json"
    ledger = tmp_path / "BENCHMARKS.md"
    write_run_snapshot(record, artifact)
    append_ledger_row(record, ledger, artifact)

    rendered = artifact.read_text()
    assert record.status == "aborted"
    assert record.win_rate is None
    assert record.wilson95 is None
    assert '"completion_latency_ms_total": null' in rendered
    assert '"completion_latency_ms_p50": null' in rendered
    assert '"completion_latency_ms_max": null' in rendered
    assert '"decision_latency_ms_p50": 300' in rendered
    markdown = ledger.read_text()
    assert "0/0/0" not in markdown
    # El ledger no publica latencia de runs incompletos: las dos celdas de
    # latencia quedan vacias (tres separadores consecutivos).
    assert "|  |  | 2026-07-28-official" in markdown
    assert "300/310/320" not in markdown


# --- R3 (MON-15): cadena sintetica del error original hasta el JSON --------

import json as _json

from ludex_agent.benchmark import (
    BenchmarkDeadlineExceeded,
    failure_classification,
)
from ludex_agent.graph.provider import TransientProviderError


def _relanzado(raw: BaseException) -> TransientProviderError:
    """Mismo camino que `KeyRotatingProvider.complete`: clasifica y re-lanza
    con `raise error from raw` para conservar `__cause__`."""
    try:
        raise TransientProviderError("provider transport failed") from raw
    except TransientProviderError as exc:
        return exc


def test_chain_sintetico_kimi_llega_sanitizado_al_json(tmp_path):
    """R3: raw APITimeoutError -> TransientProviderError(__cause__) ->
    resultado de _benchmark_command -> BenchmarkRecord -> JSON. Solo
    nombres de clase; el mensaje crudo jamas aparece en el artefacto."""
    raw = TimeoutError(
        "Request timed out. (url: https://api.kimi.com/v1/chat/completions?api_key=AIzaSyFake-000000000000)"
    )
    classified = _relanzado(raw)
    failure_type, failure_cause_type = failure_classification(classified)
    result = BenchmarkResult(
        requested=1, completed=0, wins=0, losses=0, ties=0,
        provider="kimi", model="kimi-k2.6",
        failure=f"{failure_type}: {classified}",
        failure_type=failure_type,
        failure_cause_type=failure_cause_type,
    )

    record = build_benchmark_record(
        run_id="test-kimi-chain",
        created_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )
    artifact = tmp_path / "runs" / "test-kimi-chain.json"
    write_run_json(record, artifact)

    assert record.failure_type == "TransientProviderError"
    assert record.failure_cause_type == "TimeoutError"
    rendered = artifact.read_text()
    assert '"failure_type": "TransientProviderError"' in rendered
    assert '"failure_cause_type": "TimeoutError"' in rendered
    # El mensaje crudo, la URL y el secreto jamas se persisten.
    assert "Request timed out" not in rendered
    assert "api.kimi.com" not in rendered
    assert "AIzaSyFake" not in rendered


def test_chain_sintetico_deadline_sin_causa_queda_null():
    """R3: `BenchmarkDeadlineExceeded` sin `__cause__` deja
    `failure_cause_type=None` en el JSON; no se inventa causa."""
    try:
        raise BenchmarkDeadlineExceeded("benchmark deadline exceeded after 180s")
    except BenchmarkDeadlineExceeded as exc:
        failure_type, failure_cause_type = failure_classification(exc)
        failure_message = str(exc)
    result = BenchmarkResult(
        requested=1, completed=0, wins=0, losses=0, ties=0,
        provider="google", model="gemini-2.5-flash",
        failure=f"{failure_type}: {failure_message}",
        failure_type=failure_type,
        failure_cause_type=failure_cause_type,
    )

    record = build_benchmark_record(
        run_id="test-deadline-chain",
        created_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )

    assert record.failure_type == "BenchmarkDeadlineExceeded"
    assert record.failure_cause_type is None
    rendered = _json.dumps(record.to_json_dict())
    assert '"failure_cause_type": null' in rendered
    assert "benchmark deadline exceeded" in rendered


def test_not_run_conserva_provider_selection_error_sin_causa():
    """R3: el path not-run de la CLI conserva `ProviderSelectionError` y no
    inventa causa (el error se lanza directo, sin `raise ... from`)."""
    from ludex_agent.graph.provider import ProviderSelectionError

    try:
        raise ProviderSelectionError("NOT RUN: credential unavailable for kimi/kimi-k2.6")
    except ProviderSelectionError as exc:
        failure_type, failure_cause_type = failure_classification(exc)
        failure_message = str(exc)
    result = BenchmarkResult(
        requested=1, completed=0, wins=0, losses=0, ties=0,
        provider="kimi", model="kimi-k2.6",
        failure=failure_message,
        failure_type=failure_type,
        failure_cause_type=failure_cause_type,
    )

    record = build_benchmark_record(
        run_id="test-not-run-chain",
        created_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        result=result,
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
        status="not-run",
    )

    assert record.failure_type == "ProviderSelectionError"
    assert record.failure_cause_type is None
    assert record.status == "not-run"


def test_battle_timeout_se_persiste_en_el_artefacto():
    """F2-10B (MON-20): el deadline por batalla configurado se persiste en
    el artefacto. Si el codigo persistiera otro valor (p.ej. un 180 fijo),
    este test falla."""
    record = build_benchmark_record(
        run_id="matriz-1",
        created_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        result=BenchmarkResult(
            requested=2, completed=0, wins=0, losses=0, ties=0,
            provider="kimi", model="kimi-k2.6",
        ),
        metrics=_metrics(),
        opponent="simple_heuristics",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
        status="aborted",
        battle_timeout_seconds=1800.0,
    )
    assert record.battle_timeout_seconds == 1800.0
    assert record.to_json_dict()["battle_timeout_seconds"] == 1800.0


def test_battle_timeout_default_si_no_se_pasa():
    record = build_benchmark_record(
        run_id="default-timeout",
        created_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        result=BenchmarkResult(
            requested=1, completed=1, wins=1, losses=0, ties=0,
            provider="open_code_zen", model="mimo-v2.5-free",
        ),
        metrics=_metrics(),
        opponent="random",
        fmt="gen6randombattle",
        route=ModelRoute(protocol="chat_completions"),
        pricing=PricingTable.load(),
    )
    assert record.battle_timeout_seconds == 180.0
