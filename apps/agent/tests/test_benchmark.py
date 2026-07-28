import pytest

from ludex_agent.benchmark import (
    BenchmarkResult,
    run_benchmark,
    wilson_interval,
)


def test_wilson_reproduce_intervalo_medido_de_random():
    low, high = wilson_interval(143, 300)
    assert (round(low, 4), round(high, 4)) == (0.4208, 0.5331)


class FakeAgent:
    def __init__(self):
        self.n_won_battles = 0
        self.n_lost_battles = 0
        self.n_tied_battles = 0
        self.battles = {}

    async def battle_against(self, opponent, n_battles):
        self.n_won_battles = 3
        self.n_lost_battles = 1
        self.n_tied_battles = 1
        self.battles = {f"battle-{i}": object() for i in range(n_battles)}


@pytest.mark.asyncio
async def test_benchmark_no_persiste_por_default():
    persisted = []
    result = await run_benchmark(
        FakeAgent(), object(), n=5, persist_battle=persisted.append
    )

    assert isinstance(result, BenchmarkResult)
    assert result.completed == 5
    assert persisted == []


@pytest.mark.asyncio
async def test_benchmark_persistencia_es_opt_in():
    persisted = []

    async def persist(tag):
        persisted.append(tag)

    result = await run_benchmark(
        FakeAgent(), object(), n=5, persist=True, persist_battle=persist
    )

    assert result.completed == 5
    assert persisted == [f"battle-{i}" for i in range(5)]


def test_resultado_incompleto_no_publica_winrate_comparable():
    result = BenchmarkResult(
        requested=300, completed=140, wins=70, losses=70, ties=0,
        provider="google", model="gemini-test", failure="quota exhausted",
    )
    assert result.comparable is False
    assert result.win_rate is None
