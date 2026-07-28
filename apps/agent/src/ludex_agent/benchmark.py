"""Runner reusable y estadística del benchmark."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


def wilson_interval(
    wins: int, n: int, confidence: float = 0.95
) -> tuple[float, float]:
    if n <= 0:
        raise ValueError("n must be positive")
    if confidence != 0.95:
        raise ValueError("only the measured 95% confidence level is supported")
    z = 1.959963984540054
    proportion = wins / n
    denominator = 1 + z * z / n
    center = (proportion + z * z / (2 * n)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / n + z * z / (4 * n * n)
        )
        / denominator
    )
    return center - margin, center + margin


@dataclass(frozen=True)
class BenchmarkResult:
    requested: int
    completed: int
    wins: int
    losses: int
    ties: int
    provider: str | None = None
    model: str | None = None
    failure: str | None = None

    @property
    def comparable(self) -> bool:
        return self.failure is None and self.completed == self.requested

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.completed if self.comparable and self.completed else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return (
            wilson_interval(self.wins, self.completed)
            if self.comparable and self.completed else None
        )


async def run_benchmark(
    agent: Any,
    opponent: Any,
    *,
    n: int,
    persist: bool = False,
    persist_battle: Callable[[str], Awaitable[None] | None] | None = None,
    provider: str | None = None,
    model: str | None = None,
    on_progress: Callable[
        [BenchmarkResult], Awaitable[None] | None
    ] | None = None,
) -> BenchmarkResult:
    if persist and persist_battle is None:
        raise ValueError("persist=True requires persist_battle")

    def current_result() -> BenchmarkResult:
        return BenchmarkResult(
            requested=n,
            completed=(
                agent.n_won_battles
                + agent.n_lost_battles
                + agent.n_tied_battles
            ),
            wins=agent.n_won_battles,
            losses=agent.n_lost_battles,
            ties=agent.n_tied_battles,
            provider=provider,
            model=model,
        )

    async def report_progress() -> None:
        if on_progress is not None:
            pending = on_progress(current_result())
            if inspect.isawaitable(pending):
                await pending

    try:
        for _ in range(n):
            known = set(agent.battles)
            await agent.battle_against(opponent, n_battles=1)
            if persist:
                for tag in agent.battles:
                    if tag not in known:
                        pending = persist_battle(tag)
                        if inspect.isawaitable(pending):
                            await pending
            await report_progress()
    except BaseException:
        await report_progress()
        raise
    return current_result()
