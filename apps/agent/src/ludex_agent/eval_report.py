"""Artefactos durables de benchmarks: JSON detallado y ledger Markdown."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .benchmark import BenchmarkResult
from .eval_cost import PricingTable, calculate_cost
from .graph.provider import ModelRoute


RUN_ID_PATTERN = re.compile(r"[a-z0-9-]+")


@dataclass(frozen=True)
class BenchmarkRecord:
    run_id: str
    created_at: str
    status: str
    provider: str
    model: str
    opponent: str
    format: str
    route: ModelRoute
    requested: int
    completed: int
    wins: int
    losses: int
    ties: int
    failure: str | None
    win_rate: Decimal | None
    wilson95: tuple[Decimal, Decimal] | None
    metrics: Mapping[str, int]
    calls_per_battle: Decimal | None
    invalid_recovered_pct: Decimal
    fallback_pct: Decimal
    total_cost: Decimal | None
    cost_per_battle: Decimal | None
    projected_10k_cost: Decimal | None
    pricing_table_id: str
    pricing_currency: str
    pricing_source_url: str | None

    def to_json_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        return convert(asdict(self))


def _ratio(numerator: int, denominator: int) -> Decimal:
    return (
        Decimal(numerator) / Decimal(denominator)
        if denominator
        else Decimal(0)
    )


def build_benchmark_record(
    *,
    run_id: str,
    created_at: datetime,
    result: BenchmarkResult,
    metrics: Mapping[str, int],
    opponent: str,
    fmt: str,
    route: ModelRoute,
    pricing: PricingTable,
) -> BenchmarkRecord:
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run_id must match [a-z0-9-]+")
    provider = result.provider or ""
    model = result.model or ""
    try:
        price = pricing.price(provider, model)
    except KeyError:
        price = None
    total_cost = calculate_cost(metrics, price) if price is not None else None
    complete = result.comparable
    turns_total = int(metrics.get("turns_total", 0))
    invalid = int(metrics.get("turns_model_invalid", 0))
    fallback = int(metrics.get("turns_fallback", 0))
    recovered = max(0, invalid - fallback)
    calls = int(metrics.get("calls_total", 0))
    win_rate = (
        Decimal(result.wins) / Decimal(result.completed)
        if complete and result.completed
        else None
    )
    interval = result.interval if complete else None
    wilson = (
        (Decimal(str(interval[0])), Decimal(str(interval[1])))
        if interval is not None
        else None
    )
    cost_per_battle = (
        total_cost / Decimal(result.completed)
        if complete and result.completed and total_cost is not None
        else None
    )
    return BenchmarkRecord(
        run_id=run_id,
        created_at=created_at.isoformat(),
        status="complete" if complete else "aborted",
        provider=provider,
        model=model,
        opponent=opponent,
        format=fmt,
        route=route,
        requested=result.requested,
        completed=result.completed,
        wins=result.wins,
        losses=result.losses,
        ties=result.ties,
        failure=result.failure,
        win_rate=win_rate,
        wilson95=wilson,
        metrics=dict(metrics),
        calls_per_battle=(
            Decimal(calls) / Decimal(result.completed)
            if complete and result.completed
            else None
        ),
        invalid_recovered_pct=_ratio(recovered, turns_total),
        fallback_pct=_ratio(fallback, turns_total),
        total_cost=total_cost,
        cost_per_battle=cost_per_battle,
        projected_10k_cost=(
            cost_per_battle * Decimal(10_000)
            if cost_per_battle is not None
            else None
        ),
        pricing_table_id=pricing.table_id,
        pricing_currency=pricing.currency,
        pricing_source_url=price.source_url if price is not None else None,
    )


def write_run_json(record: BenchmarkRecord, path: str | Path) -> None:
    target = Path(path)
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record.to_json_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _display(value: Decimal | None, *, percent: bool = False) -> str:
    if value is None:
        return ""
    rendered = value * 100 if percent else value
    return f"{rendered:.4f}" + ("%" if percent else "")


LEDGER_HEADER = """# Benchmarks de modelos

Registro acumulativo. El costo se calcula con usage real; una celda vacía
significa desconocido o no comparable, nunca cero implícito.

| Fecha | Run | Proveedor/modelo | Batallas | W-L-T | Winrate | Wilson 95% | Llamadas/batalla | Tokens in/out | Costo total | Costo/batalla | 10.000 batallas | Ilegales retry/fallback | Deadlines | Rotaciones | Precios |
|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---:|---:|---|
"""


def append_ledger_row(
    record: BenchmarkRecord,
    ledger_path: str | Path,
    artifact_path: str | Path,
) -> None:
    ledger = Path(ledger_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    existing = ledger.read_text(encoding="utf-8") if ledger.exists() else LEDGER_HEADER
    artifact = Path(artifact_path)
    link = os.path.relpath(artifact, ledger.parent)
    interval = (
        f"{_display(record.wilson95[0], percent=True)}–"
        f"{_display(record.wilson95[1], percent=True)}"
        if record.wilson95 is not None
        else ""
    )
    metrics = record.metrics
    row = (
        f"| {record.created_at[:10]} | [{record.run_id}]({link}) | "
        f"{record.provider}/{record.model} | "
        f"{record.completed}/{record.requested} | "
        f"{record.wins}-{record.losses}-{record.ties} | "
        f"{_display(record.win_rate, percent=True)} | {interval} | "
        f"{_display(record.calls_per_battle)} | "
        f"{metrics.get('input_tokens', 0)}/{metrics.get('output_tokens', 0)} | "
        f"{_display(record.total_cost)} | "
        f"{_display(record.cost_per_battle)} | "
        f"{_display(record.projected_10k_cost)} | "
        f"{_display(record.invalid_recovered_pct, percent=True)}/"
        f"{_display(record.fallback_pct, percent=True)} | "
        f"{metrics.get('turns_deadline_affected', 0)} | "
        f"{metrics.get('key_rotations', 0)} | "
        f"{record.pricing_table_id} |\n"
    )
    temporary = ledger.with_suffix(ledger.suffix + ".tmp")
    temporary.write_text(existing.rstrip() + "\n" + row, encoding="utf-8")
    temporary.replace(ledger)
