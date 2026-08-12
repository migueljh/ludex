"""Runner reusable y estadística del benchmark."""

from __future__ import annotations

import asyncio
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


class BenchmarkDeadlineExceeded(Exception):
    """El deadline propio de una batalla del benchmark venció."""


class ShowdownUnavailableError(RuntimeError):
    """L-03 (post-R1B): el preflight local de Showdown fallo (indisponibilidad
    de INFRAESTRUCTURA, no del modelo). Se levanta `from` el OSError real del
    probe de conexion, que queda como causa preservada en vivo. La matriz lo
    clasifica `externally-limited` con stage=battle: nunca una
    incompatibilidad del modelo ni internal-defect."""


def failure_classification(
    exc: BaseException,
) -> tuple[str, str | None]:
    """Nombres de clase del error clasificado y de su causa original.

    R3 (MON-15): los artefactos persisten SOLO nombres de clases —
    `failure_type` (clase del error clasificado) y `failure_cause_type`
    (clase de la causa original, via `__cause__`) — nunca mensajes crudos,
    URLs, módulos, tracebacks ni secretos. Un error sin causa deja
    `failure_cause_type=None`; jamas se inventa una.
    """
    cause = exc.__cause__
    return (
        type(exc).__name__,
        type(cause).__name__ if cause is not None else None,
    )


class BenchmarkFailure(Exception):
    """Fallo del benchmark con resultado parcial TIPADO (L-02, correccion
    LATWAN).

    `_benchmark_command` envuelve toda excepcion no clasificada de
    `run_benchmark` (p.ej. `ConnectionClosedError` de Showdown en la batalla
    2) en `BenchmarkFailure`, transportando un `BenchmarkResult` parcial con
    el progreso REAL (`requested`/`completed`/W/L/T desde los contadores del
    agente), la identidad efectiva (provider/model pinneados) y la evidencia
    sanitizada (`failure_type`/`failure_cause_type`/`http_status`/
    `provider_error_code`).

    No es un atributo ad hoc sobre una excepcion generica: el resultado
    parcial es un campo tipado (`result`) de una clase propia de la frontera.
    La excepcion original queda como `__cause__` — la primaria se preserva y
    `failure_classification`/`_http_status_chain`/`_structured_provider_error_code`
    siguen la cadena.
    """

    def __init__(self, result: BenchmarkResult) -> None:
        super().__init__(result.failure or "benchmark failure")
        self.result = result


class InternalCleanupError(RuntimeError):
    """Marcador clasificado (nunca se lanza): fallo del cierre de recursos
    del benchmark (drain / PSClient de ambos players / CalcClient / context
    repository / engine) SIN excepcion primaria en vuelo (L-01, correccion
    LATWAN).

    Su nombre de clase (sanitizado) se persiste como `failure_type` del
    resultado para que la matriz lo clasifique `internal-defect`: una
    corrida con cleanup fallido jamas puede quedar `compatible`. La clase de
    la causa real del primer paso fallido se preserva como
    `failure_cause_type`.
    """


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
    # R3 (MON-15): evidencia durable y sanitizada del fallo. `failure` es el
    # mensaje publico sanitizado; `failure_type` y `failure_cause_type` son
    # SOLO nombres de clase (ver `failure_classification`).
    failure_type: str | None = None
    failure_cause_type: str | None = None
    # L-03 (R1A): evidencia durable y sanitizada ampliada. `http_status` es
    # el status HTTP cuando existe y `provider_error_code` sale SOLO de
    # campos estructurados permitidos (ver `_structured_provider_error_code`
    # en graph/provider.py); nunca mensajes, URLs, headers ni secretos.
    http_status: int | None = None
    provider_error_code: str | None = None

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
    timeout: float | None = None,
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

    async def play_one() -> None:
        wait_for_failure = getattr(
            agent, "wait_for_background_failure", None
        )
        if wait_for_failure is None:
            await agent.battle_against(opponent, n_battles=1)
            return
        battle_task = asyncio.create_task(
            agent.battle_against(opponent, n_battles=1)
        )
        failure_task = asyncio.create_task(wait_for_failure())
        try:
            await asyncio.wait(
                {battle_task, failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failure_task.done():
                failure = failure_task.result()
                battle_task.cancel()
                await asyncio.gather(
                    battle_task, return_exceptions=True
                )
                raise failure
            failure_task.cancel()
            await asyncio.gather(
                failure_task, return_exceptions=True
            )
            await battle_task
        finally:
            if not battle_task.done():
                battle_task.cancel()
            if not failure_task.done():
                failure_task.cancel()
            await asyncio.gather(
                battle_task, failure_task, return_exceptions=True
            )

    try:
        for _ in range(n):
            known = set(agent.battles)
            if timeout is not None:
                timeout_ctx = None
                try:
                    async with asyncio.timeout(timeout) as timeout_ctx:
                        await play_one()
                except TimeoutError as exc:
                    if (
                        timeout_ctx is not None
                        and timeout_ctx.expired()
                    ):
                        raise BenchmarkDeadlineExceeded(
                            f"benchmark deadline exceeded after {timeout}s"
                        ) from exc
                    raise
            else:
                await play_one()
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
