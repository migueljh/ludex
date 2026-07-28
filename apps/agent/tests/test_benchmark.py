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
        for _ in range(n_battles):
            index = len(self.battles)
            if index < 3:
                self.n_won_battles += 1
            elif index == 3:
                self.n_lost_battles += 1
            else:
                self.n_tied_battles += 1
            self.battles[f"battle-{index}"] = object()


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


@pytest.mark.asyncio
async def test_benchmark_reporta_progreso_despues_de_cada_batalla():
    progress = []

    async def report(result):
        progress.append((
            result.completed, result.wins, result.losses, result.ties
        ))

    result = await run_benchmark(
        FakeAgent(), object(), n=5, on_progress=report
    )

    assert result.completed == 5
    assert progress == [
        (1, 1, 0, 0),
        (2, 2, 0, 0),
        (3, 3, 0, 0),
        (4, 3, 1, 0),
        (5, 3, 1, 1),
    ]


@pytest.mark.asyncio
async def test_interrupcion_conserva_el_ultimo_progreso_completo():
    class InterruptingAgent(FakeAgent):
        async def battle_against(self, opponent, n_battles):
            if len(self.battles) == 1:
                raise KeyboardInterrupt
            await super().battle_against(opponent, n_battles)

    progress = []

    with pytest.raises(KeyboardInterrupt):
        await run_benchmark(
            InterruptingAgent(),
            object(),
            n=5,
            on_progress=lambda result: progress.append(result.completed),
        )

    assert progress == [1, 1]


def test_resultado_incompleto_no_publica_winrate_comparable():
    result = BenchmarkResult(
        requested=300, completed=140, wins=70, losses=70, ties=0,
        provider="google", model="gemini-test", failure="quota exhausted",
    )
    assert result.comparable is False
    assert result.win_rate is None
