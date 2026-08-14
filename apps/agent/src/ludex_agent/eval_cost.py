"""Cálculo reproducible de costo desde usage real y precios versionados."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping


DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "pricing-2026-08-14.json"
)
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class PriceEntry:
    provider: str
    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None
    checked_at: str
    source_url: str

    def __post_init__(self) -> None:
        prices = (
            self.input_per_million,
            self.output_per_million,
            self.cached_input_per_million,
        )
        if any(price is not None and price < 0 for price in prices):
            raise ValueError("prices cannot be negative")
        if not self.checked_at or not self.source_url:
            raise ValueError("price source and checked_at are required")


@dataclass(frozen=True)
class PricingTable:
    table_id: str
    currency: str
    entries: Mapping[tuple[str, str], PriceEntry]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICING_PATH) -> "PricingTable":
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        entries: dict[tuple[str, str], PriceEntry] = {}
        for raw in document["prices"]:
            key = (raw["provider"], raw["model"])
            if key in entries:
                raise ValueError(f"duplicate price: {key}")
            cached = raw.get("cached_input_per_million")
            entries[key] = PriceEntry(
                provider=key[0],
                model=key[1],
                input_per_million=Decimal(raw["input_per_million"]),
                output_per_million=Decimal(raw["output_per_million"]),
                cached_input_per_million=(
                    Decimal(cached) if cached is not None else None
                ),
                checked_at=raw["checked_at"],
                source_url=raw["source_url"],
            )
        table_id = document.get("pricing_table_id", "").strip()
        currency = document.get("currency", "").strip()
        if not table_id or not currency:
            raise ValueError("pricing table id and currency are required")
        return cls(table_id=table_id, currency=currency, entries=entries)

    def price(self, provider: str, model: str) -> PriceEntry:
        try:
            return self.entries[(provider, model)]
        except KeyError:
            raise KeyError(f"modelo sin precio: {provider}/{model}") from None


def calculate_cost(
    usage: Mapping[str, int | None],
    price: PriceEntry,
) -> Decimal | None:
    """Costo desde usage real y precios versionados.

    R3 (MON-15): `DecisionMetrics.snapshot()` mezcla contadores `int` con
    percentiles de latencia `int | None`. `calculate_cost` consume
    ÚNICAMENTE los campos de tokens (`input_tokens`, `cached_input_tokens`,
    `output_tokens`) y rechaza con `ValueError` si alguno es `None`: un
    conteo de tokens ausente no es calculable, y jamas se confunde con los
    percentiles nullable, que no se leen aca.
    """
    raw_input = usage.get("input_tokens")
    raw_cached = usage.get("cached_input_tokens")
    raw_output = usage.get("output_tokens")
    if None in (raw_input, raw_cached, raw_output):
        raise ValueError("token usage fields cannot be None")
    input_tokens = int(raw_input)
    cached_tokens = int(raw_cached)
    output_tokens = int(raw_output)
    if min(input_tokens, cached_tokens, output_tokens) < 0:
        raise ValueError("token usage cannot be negative")
    if cached_tokens > input_tokens:
        raise ValueError("cached input tokens cannot exceed input tokens")
    if cached_tokens and price.cached_input_per_million is None:
        return None
    uncached_tokens = input_tokens - cached_tokens
    cached_price = price.cached_input_per_million or Decimal(0)
    return (
        Decimal(uncached_tokens) * price.input_per_million
        + Decimal(cached_tokens) * cached_price
        + Decimal(output_tokens) * price.output_per_million
    ) / MILLION
