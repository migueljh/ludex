import asyncio
import concurrent.futures

import pytest

from ludex_agent.benchmark import (
    BenchmarkResult,
    run_benchmark,
    wilson_interval,
)
from ludex_agent.graph.provider import TransientProviderError


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


@pytest.mark.asyncio
async def test_fallo_de_tarea_de_mensajes_aborta_batalla_colgada():
    class FailingAgent(FakeAgent):
        cancelled = False

        async def battle_against(self, opponent, n_battles):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

        async def wait_for_background_failure(self):
            await asyncio.sleep(0)
            return TransientProviderError("provider transport failed")

    agent = FailingAgent()

    with pytest.raises(TransientProviderError, match="transport"):
        await run_benchmark(agent, object(), n=1)

    assert agent.cancelled is True


def test_resultado_incompleto_no_publica_winrate_comparable():
    result = BenchmarkResult(
        requested=300, completed=140, wins=70, losses=70, ties=0,
        provider="google", model="gemini-test", failure="quota exhausted",
    )
    assert result.comparable is False
    assert result.win_rate is None


class _ObservableAgent:
    """Agente de control para auditar lifetime de tareas hijas.

    No usa `sleep` como oráculo: los eventos son deterministas y el test
    espera explícitamente a que las coroutines internas arranquen.
    """

    def __init__(self):
        self.n_won_battles = 0
        self.n_lost_battles = 0
        self.n_tied_battles = 0
        self.battles = {}

        self.battle_started = asyncio.Event()
        self.battle_done = asyncio.Event()
        self.battle_cancelled = False
        self.battle_result = None

        self.failure_started = asyncio.Event()
        self.failure_done = asyncio.Event()
        self.failure_cancelled = False
        self.failure_result = None

    async def battle_against(self, opponent, n_battles):
        self.battle_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.battle_cancelled = True
            raise
        finally:
            self.battle_done.set()

    async def wait_for_background_failure(self):
        self.failure_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.failure_cancelled = True
            raise
        finally:
            self.failure_done.set()

    def _finish_battle(self):
        index = len(self.battles)
        self.battles[f"battle-{index}"] = object()
        if index < 3:
            self.n_won_battles += 1
        elif index == 3:
            self.n_lost_battles += 1
        else:
            self.n_tied_battles += 1


class _CompletingAgent(_ObservableAgent):
    async def battle_against(self, opponent, n_battles):
        self.battle_started.set()
        self._finish_battle()
        self.battle_done.set()

    async def wait_for_background_failure(self):
        self.failure_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.failure_cancelled = True
            raise
        finally:
            self.failure_done.set()


class _BackgroundFailureAgent(_ObservableAgent):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    async def wait_for_background_failure(self):
        self.failure_started.set()
        try:
            raise self._exc
        finally:
            self.failure_done.set()


@pytest.mark.asyncio
async def test_cancelacion_externa_limpia_hijos():
    agent = _ObservableAgent()
    runner = asyncio.create_task(run_benchmark(agent, object(), n=1))
    await asyncio.wait_for(
        asyncio.gather(agent.battle_started.wait(), agent.failure_started.wait()),
        timeout=1,
    )
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert agent.battle_cancelled is True, "battle_task no fue cancelada"
    assert agent.failure_cancelled is True, "failure_task no fue cancelada"
    assert agent.battle_done.is_set()
    assert agent.failure_done.is_set()


@pytest.mark.asyncio
async def test_deadline_propio_cancela_hijos():
    agent = _ObservableAgent()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await run_benchmark(agent, object(), n=1)
    await asyncio.wait_for(agent.battle_done.wait(), timeout=1)
    await asyncio.wait_for(agent.failure_done.wait(), timeout=1)
    assert agent.battle_cancelled is True
    assert agent.failure_cancelled is True


@pytest.mark.asyncio
async def test_fallo_background_conserva_identidad():
    exc = TransientProviderError("provider transport failed")
    agent = _BackgroundFailureAgent(exc)
    with pytest.raises(TransientProviderError, match="transport"):
        await run_benchmark(agent, object(), n=1)
    assert agent.battle_done.is_set()
    assert agent.battle_cancelled is True


@pytest.mark.asyncio
async def test_timeouterror_background_no_se_confunde_con_deadline():
    """Un TimeoutError del canal background se propia como fallo background,
    no como timeout propio del benchmark."""
    exc = asyncio.TimeoutError("projection timed out")
    agent = _BackgroundFailureAgent(exc)
    with pytest.raises(asyncio.TimeoutError, match="projection"):
        await run_benchmark(agent, object(), n=1, timeout=3600)
    assert agent.battle_done.is_set()
    assert agent.battle_cancelled is True


@pytest.mark.asyncio
async def test_exito_normal_deja_cero_hijos_pendientes():
    agent = _CompletingAgent()
    result = await run_benchmark(agent, object(), n=3)
    assert result.completed == 3
    assert agent.battle_done.is_set()
    assert agent.failure_done.is_set()
    assert agent.failure_cancelled is True
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert pending == [], f"tareas hijas vivas: {pending}"


class _ShieldedFailureAgent(_CompletingAgent):
    """Imita el `wait_for_background_failure` real que usa `asyncio.shield`."""

    def __init__(self):
        super().__init__()
        self._failure_future = concurrent.futures.Future()

    async def wait_for_background_failure(self):
        self.failure_started.set()
        try:
            return await asyncio.shield(
                asyncio.wrap_future(self._failure_future)
            )
        finally:
            self.failure_done.set()


@pytest.mark.asyncio
async def test_exito_con_shield_no_deja_hijos_pendientes():
    agent = _ShieldedFailureAgent()
    result = await run_benchmark(agent, object(), n=1)
    assert result.completed == 1
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert pending == [], f"tareas hijas vivas tras shield: {pending}"


@pytest.mark.asyncio
async def test_deadline_interno_cancela_hijos():
    agent = _ObservableAgent()
    with pytest.raises(TimeoutError):
        await run_benchmark(agent, object(), n=1, timeout=0.01)
    await asyncio.wait_for(agent.battle_done.wait(), timeout=1)
    await asyncio.wait_for(agent.failure_done.wait(), timeout=1)
    assert agent.battle_cancelled is True
    assert agent.failure_cancelled is True
