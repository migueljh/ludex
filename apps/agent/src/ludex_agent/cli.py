"""Runner: juega N batallas en el server local y las persiste."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer
from poke_env import AccountConfiguration
from poke_env.player import (
    MaxBasePowerPlayer,
    RandomPlayer,
    SimpleHeuristicsPlayer,
)

from .benchmark import BenchmarkDeadlineExceeded, BenchmarkResult, run_benchmark
from .config import PROVIDER_CATALOG, load_settings
from .db.context_repository import PostgresContextRepository
from .db.model_repository import ModelRepository, ModelSelectionError
from .db.repository import BattleRepository
from .db.session import make_engine, session_factory
from .showdown.client import LudexPlayer, local_server_configuration
from .showdown.protocol import compute_opening_identity
from .graph.calc import CalcClient
from .graph.decision import DecisionResponse
from .graph.provider import (
    AnthropicDecisionProvider,
    DecisionMetrics,
    GeminiDecisionProvider,
    ModelRoute,
    OpenAICompatibleDecisionProvider,
    PinnedResolver,
    ProviderChain,
    ProviderError,
    load_model_routes,
    model_route,
    provider_keys,
)
from .graph.workflow import build_decision_graph
from .eval_cost import DEFAULT_PRICING_PATH, PricingTable
from .eval_report import (
    BenchmarkRecord,
    append_ledger_row,
    build_benchmark_record,
    write_run_snapshot,
)
from .state.schema import STATE_SCHEMA_VERSION

logger = logging.getLogger(__name__)


class IncompleteTrajectoryError(RuntimeError):
    """Una trayectoria no puede escribirse completa y queda fuera del dataset."""

app = typer.Typer(pretty_exceptions_show_locals=False)
AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_LEDGER_PATH = REPO_ROOT / "docs" / "BENCHMARKS.md"
DEFAULT_RUNS_PATH = AGENT_ROOT / "evals" / "runs"

# Una batalla de gen6randombattle ronda los 60-120 turnos y tarda segundos.
# Este techo es holgado a proposito: solo existe para que un server colgado
# no deje el proceso esperando indefinidamente. Es POR BATALLA (minor de la
# review final: antes era `* n`, presupuestando el lote entero y haciendose
# cada vez mas probable de saltar cuanto mas grande el lote).
BATTLE_TIMEOUT_SECONDS = 180


def _progress_summary(record: BenchmarkRecord) -> str:
    metrics = record.metrics
    cost = (
        f"{record.pricing_currency} {record.total_cost}"
        if record.total_cost is not None
        else "unknown"
    )
    return (
        f"progress={record.completed}/{record.requested} "
        f"w-l-t={record.wins}-{record.losses}-{record.ties} "
        f"calls={metrics.get('calls_total', 0)} "
        f"tokens={metrics.get('input_tokens', 0)}/"
        f"{metrics.get('output_tokens', 0)} cost={cost}"
    )


async def _check_showdown_reachable(ws_url: str) -> None:
    """Falla en segundos, no en minutos, si el server local no esta arriba.

    Minor triageado a Important en la review final: sin este chequeo, con el
    server apagado, los tests de integracion (y este runner) tardaban 360s en
    errar via el timeout del batching en vez de fallar rapido con un mensaje
    util. `asyncio.open_connection` al host:puerto del websocket es barato y
    cubre el caso comun (contenedor no levantado).
    """
    parts = urlsplit(ws_url.replace("ws://", "http://").replace("wss://", "https://"))
    host = parts.hostname or "localhost"
    port = parts.port or 80
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"No se pudo conectar a Showdown en {host}:{port} ({exc}). "
            "Levantar el server local con: "
            "docker compose --profile local up -d showdown"
        ) from exc


async def _battle_against_or_failure(agent: Any, rival: Any) -> None:
    """Propaga fallos del listener al loop que gobierna la batalla.

    poke-env procesa frames en una task/loop de fondo; esperar solamente
    `battle_against` puede dejar al runner colgado hasta su timeout aunque el
    handler ya haya fallado. El future compartido de Ludex está shielded, por
    lo que cancelar este vigilante no cancela la señal para la batalla
    siguiente.
    """
    wait_for_failure = getattr(agent, "wait_for_background_failure", None)
    if wait_for_failure is None:
        await agent.battle_against(rival, n_battles=1)
        return
    battle_task = asyncio.create_task(agent.battle_against(rival, n_battles=1))
    failure_task = asyncio.create_task(wait_for_failure())
    try:
        await asyncio.wait(
            {battle_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if failure_task.done():
            raise failure_task.result()
        await battle_task
    finally:
        for task in (battle_task, failure_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(battle_task, failure_task, return_exceptions=True)


async def play(n: int, fmt: str, *, source: str = "local") -> list[str]:
    """Juega `n` batallas y las persiste.

    `source` (I6, review final): 'local' es el valor real para corridas que
    alimentan el dataset de entrenamiento. Los tests de integracion (que
    juegan batallas REALES contra el mismo Postgres compartido, no un
    fixture) deben pasar `source="test"` para que sus filas queden marcadas
    y excluibles con `source <> 'test'`, en vez de mezclarse en silencio con
    el dataset real. Contrato documentado en D19 y en la migracion
    `20260727000007_battle_source_test.sql`.
    """
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    await _check_showdown_reachable(settings.showdown_ws_url)
    suffix = str(abs(hash((n, fmt))) % 10_000)

    agent = LudexPlayer(
        account_configuration=AccountConfiguration(f"{settings.bot_username}{suffix}", None),
        server_configuration=server, battle_format=fmt, log_level=40,
    )
    rival = RandomPlayer(
        account_configuration=AccountConfiguration(f"Rival{suffix}", None),
        server_configuration=server, battle_format=fmt, log_level=40,
    )

    engine = make_engine(settings.database_url)
    repo = BattleRepository(session_factory(engine))
    tags: list[str] = []
    try:
        for _ in range(n):
            antes = set(agent.battles)
            # I5 + minors 1/2 (review final): timeout POR BATALLA, no por
            # lote, y la batalla se persiste apenas termina, no despues de
            # jugar las n. Un cuelgue en la batalla 5 de 5 ya no tira a la
            # basura las 4 anteriores, que quedaron commiteadas.
            battle_timeout = asyncio.timeout(BATTLE_TIMEOUT_SECONDS)
            try:
                async with battle_timeout:
                    await _battle_against_or_failure(agent, rival)
            except TimeoutError:
                if not battle_timeout.expired():
                    raise
                break
            nuevas = [t for t in agent.battles if t not in antes]
            for tag in nuevas:
                await _persist_one(agent, repo, tag, fmt, source)
                tags.append(tag)
    finally:
        await engine.dispose()
    return tags


def _battle_outcome(battle: Any) -> tuple[str | None, str, float]:
    """Deriva `(winner, result, reward)` de una batalla YA terminada.

    I6 (review de merge): `battle.won` de poke-env es un `Optional[bool]`
    que vale `None` en DOS situaciones bien distintas: "todavia no termino" Y
    "empate" (`tied()` nunca toca `_won`, solo `won_by()` lo hace, y este
    metodo solo se llama con un nombre de ganador). El codigo anterior hacia
    `battle.player_username if battle.won else battle.opponent_username`,
    que colapsa esas dos situaciones en la rama `else`: un empate quedaba
    grabado con el RIVAL como `winner` (un hecho falso en `battles`), con
    `final_result='loss'` (dejando `'tie'` del enum inalcanzable) y con
    `reward=-1` para una batalla que no se perdio.

    Se llama solo cuando `battle.finished` es verdadero, asi que `None` en
    este punto SOLO puede significar empate. `reward=0` para el empate: ni
    castiga ni premia, coherente con `+1`/`-1` de las otras dos ramas.
    """
    if battle.won is True:
        return battle.player_username, "win", 1.0
    if battle.won is False:
        return battle.opponent_username, "loss", -1.0
    return None, "tie", 0.0


async def _persist_one(
    agent: LudexPlayer, repo: BattleRepository, tag: str, fmt: str, source: str
) -> None:
    battle = agent.battles[tag]
    side = battle.player_role
    # I3 (review final): `battles.p1`/`p2` deben derivarse del ROL, no
    # asumir que el agente siempre es p1. Hoy es invisible porque el agente
    # siempre desafia y queda de p1, pero eso deja de ser cierto el dia que
    # acepte un desafio o juegue ladder.
    if side == "p1":
        p1, p2 = battle.player_username, battle.opponent_username
    else:
        p1, p2 = battle.opponent_username, battle.player_username

    winner: str | None = None
    result: str | None = None
    reward: float | None = None
    if battle.finished:
        winner, result, reward = _battle_outcome(battle)

    # MON-10/F2-03 (D36): la identidad persistida sale del bloque de
    # apertura PUBLICO (turno 0), no del battle_tag -- su contador se reusa
    # tras un restart de Showdown. `compute_opening_identity` falla cerrado
    # (`OpeningIdentityError`) si esa apertura no cierra; se propaga tal
    # cual, antes de escribir nada, en vez de persistir con una identidad
    # que no representa de verdad a esta partida.
    recorder = agent.recorders[tag]
    identity_key = compute_opening_identity(tag, recorder.lines_for_turn(0))

    battle_id = await repo.save_battle(
        battle_tag=tag, identity_key=identity_key, fmt=fmt, p1=p1, p2=p2,
        winner=winner, source=source, played_by="bot",
    )
    for turn in recorder.turns():
        await repo.save_turn(battle_id, side, turn, recorder.lines_for_turn(turn))

    # C1/D22: la materializacion del estado es sincronica, dentro del manejo
    # de mensajes; esto corrige la etiqueta de turno contra el protocolo antes
    # de validar el conjunto completo de slots. El preflight ocurre antes de
    # crear la trayectoria: una batalla incompleta conserva protocolo crudo,
    # pero no deja una fila vacia dentro del dataset de politica.
    await agent.wait_for_pending_steps(tag)
    blocker = agent.trajectory_blocker(tag)
    if blocker is not None:
        decision_index, phase = blocker
        agent.lost_step_count += 1
        raise IncompleteTrajectoryError(
            f"{tag}: trajectory_step incompleto decision_index={decision_index} "
            f"phase={phase} (lost_step_count={agent.lost_step_count})"
        )
    traj = await repo.save_trajectory(
        battle_id, gen_number=battle.gen, fmt=fmt, player_side=side
    )
    # D21/F2-02: decision_index cuenta decisiones canonicas resueltas. Los
    # retries rechazados reemplazan el mismo slot y no producen filas.
    for decision_index, step in enumerate(agent.steps[tag]):
        assert step is not None and step["state"] is not None
        await repo.save_step(
            traj, decision_index, step["turn"], step["state"], STATE_SCHEMA_VERSION,
            step["state"]["legal_actions"], step["action_taken"], "agent",
            action_path=step.get("action_path"),
            # F2-08 (D38): la metadata de la decision canónica viaja del step
            # a la fila. La ruta random no setea estas claves: `get` devuelve
            # None y la fila queda NULL, coherente con la historia.
            rationale=step.get("rationale"),
            target=step.get("target"),
            confidence=step.get("confidence"),
            alternatives=step.get("alternatives"),
            provider=step.get("provider"),
            model=step.get("model"),
            decision_latency_ms=step.get("decision_latency_ms"),
            input_tokens=step.get("input_tokens"),
            output_tokens=step.get("output_tokens"),
            cached_input_tokens=step.get("cached_input_tokens"),
            reasoning_tokens=step.get("reasoning_tokens"),
        )
    if battle.finished:
        await repo.finalize(traj, result=result, reward=reward)


@app.command()
def run(n: int = 5, fmt: str | None = None) -> None:
    # Sin default literal aca: el formato por defecto vive en config.py (Global
    # Constraints: "ninguna generacion se hardcodea en src/"). `None` deja que
    # `load_settings().showdown_battle_format` decida.
    tags = asyncio.run(play(n, fmt or load_settings().showdown_battle_format))
    typer.echo(f"{len(tags)} batallas persistidas")


def _benchmark_provider(
    name: str, model: str, timeout: float, metrics: DecisionMetrics
) -> ProviderChain:
    providers = {
        "google": ("GEMINI_API_KEY", "GEMINI_API_KEYS", None),
        "kimi": ("KIMI_API_KEY", None, "KIMI_BASE_URL"),
        "open_code_zen": (
            "OPEN_CODE_ZEN_API_KEY", None, "OPEN_CODE_ZEN_BASE_URL",
        ),
        "anthropic": ("ANTHROPIC_API_KEY", None, None),
        "openai": ("OPENAI_API_KEY", None, None),
    }
    if name not in providers:
        raise RuntimeError(f"proveedor desconocido: {name}")
    primary_env, pool_env, base_env = providers[name]
    aliases = (
        (("GOOGLE_API_KEY", "GOOGLE_API_KEYS"),)
        if name == "google" else ()
    )
    keys = provider_keys(
        os.environ, primary_env, pool_env, aliases=aliases
    )
    route = (
        model_route(load_model_routes(), name, model)
        if name in {"kimi", "open_code_zen"}
        else None
    )
    if name == "google":
        selected = GeminiDecisionProvider(
            keys, model=model, response_schema=DecisionResponse,
            timeout_seconds=timeout, metrics=metrics,
        )
    elif name == "anthropic":
        selected = AnthropicDecisionProvider(
            keys, model=model, response_schema=DecisionResponse,
            timeout_seconds=timeout, metrics=metrics,
        )
    elif route is not None and route.protocol == "messages":
        selected = AnthropicDecisionProvider(
            keys, name=name, model=model,
            base_url=os.environ.get(base_env) if base_env else None,
            response_schema=DecisionResponse, timeout_seconds=timeout,
            metrics=metrics, route=route,
        )
    elif route is not None and route.protocol == "responses":
        raise RuntimeError(
            f"{name}/{model}: protocolo responses todavía no implementado"
        )
    else:
        selected = OpenAICompatibleDecisionProvider(
            name, keys, model=model, base_url=os.environ.get(base_env) if base_env else None,
            response_schema=DecisionResponse, timeout_seconds=timeout,
            metrics=metrics, route=route,
        )
    # Regla de medición: aunque exista LUDEX_PROVIDER_CHAIN, el benchmark
    # queda fijado al proveedor/modelo elegidos y nunca cruza de proveedor.
    return ProviderChain(
        [selected], allow_cross_provider=False, metrics=metrics
    )


@app.command("provider-smoke")
def provider_smoke_command(
    provider: str = typer.Option(...),
    model: str = typer.Option(...),
) -> None:
    """Una completion estructurada, sin Showdown ni persistencia."""
    settings = load_settings()
    metrics = DecisionMetrics()
    selected = _benchmark_provider(
        provider, model, settings.llm_request_timeout_seconds, metrics
    )
    prompt = (
        "Elegí exactamente una acción legal y respondé con action, un "
        "rationale breve, confidence en [0,1] y alternatives (puede ser []). "
        "legal_actions="
        '[{"kind":"move","id":"tackle"},{"kind":"switch","species":"pikachu"}]'
    )
    try:
        envelope = asyncio.run(selected.complete(
            prompt,
            deadline=time.monotonic() + settings.llm_request_timeout_seconds,
            turn_id=f"provider-smoke:{provider}:{model}",
        ))
    except ProviderError as exc:
        typer.echo(f"ABORTED: {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from None
    try:
        parsed = DecisionResponse.model_validate(envelope.payload)
    except (ValueError, TypeError):
        typer.echo("ABORTED: invalid model response")
        raise typer.Exit(code=1) from None
    typer.echo(f"provider={provider} model={model} action={parsed.action}")
    typer.echo(f"decision_metrics={metrics.snapshot()}")


async def _sync_open_code_zen_models(
    *, base_url: str, api_key: str,
    client: Any | None = None,
) -> list[str]:
    """Resuelve el catalogo de modelos de OpenCode Zen desde su endpoint
    /models durante CONFIGURACION (nunca en runtime de batalla). El catalogo
    no se hardcodea: se lee del endpoint (shape OpenAI-compatible,
    {"data": [{"id": ...}]}). Un shape invalido falla ruidoso.

    Excepcion de cliente documentada (D39): el sync usa httpx directo porque
    es configuracion, no una decision; el runtime sigue con los clientes
    especializados por proveedor.
    """
    import httpx

    if client is None:
        client = httpx.AsyncClient()
        owns_client = True
    else:
        owns_client = False
    try:
        response = await client.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        response.raise_for_status()
        try:
            document = response.json()
        except ValueError as exc:
            raise RuntimeError("OpenCode /models no devolvio JSON") from exc
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, list):
            raise RuntimeError(
                "OpenCode /models sin forma {'data': [...]}: "
                f"shape inesperado: {type(document).__name__}"
            )
        ids = [
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
        if not ids:
            raise RuntimeError("OpenCode /models no devolvio ningun modelo con id")
        return ids
    finally:
        if owns_client:
            await client.aclose()


@app.command("provider-init")
def provider_init_command(
    sync_open_code_zen: bool = typer.Option(
        True, help="Sincronizar el catalogo de modelos de OpenCode Zen desde /models"
    ),
) -> None:
    """Bootstrap de configuracion: puebla `providers` desde el catalogo de
    config (nombres de env vars, NUNCA valores) y, para open_code_zen,
    resuelve el catalogo de modelos desde su endpoint /models. Si LUDEX_MODEL
    esta definido, lo deja como default y activo."""
    import asyncio as _asyncio

    settings = load_settings()
    engine = make_engine(settings.database_url)
    repo = ModelRepository(session_factory(engine))

    async def run() -> None:
        provider_ids: dict[str, int] = {}
        for name, (primary_env, _, base_env) in PROVIDER_CATALOG.items():
            base_url = os.environ.get(base_env) if base_env else None
            provider_ids[name] = await repo.upsert_provider(
                name=name, base_url=base_url, api_key_env=primary_env,
            )
            typer.echo(f"provider={name} api_key_env={primary_env}")
        if sync_open_code_zen:
            key = os.environ.get("OPEN_CODE_ZEN_API_KEY", "").strip()
            base_url = os.environ.get("OPEN_CODE_ZEN_BASE_URL", "").strip()
            if key and base_url:
                ids = await _sync_open_code_zen_models(base_url=base_url, api_key=key)
                for model_id in ids:
                    await repo.upsert_model(
                        provider_id=provider_ids["open_code_zen"],
                        model_id=model_id, label=model_id,
                    )
                typer.echo(f"open_code_zen: {len(ids)} modelos sincronizados desde /models")
            else:
                typer.echo(
                    "open_code_zen: sin OPEN_CODE_ZEN_API_KEY/OPEN_CODE_ZEN_BASE_URL, "
                    "catalogo no sincronizado"
                )
        if settings.llm_model:
            provider_id = provider_ids[settings.llm_provider]
            await repo.upsert_model(
                provider_id=provider_id, model_id=settings.llm_model,
                label=settings.llm_model, is_default=True,
            )
            await repo.set_active(settings.llm_provider, settings.llm_model)
            typer.echo(
                f"activo={settings.llm_provider}/{settings.llm_model} "
                "(default desde LUDEX_MODEL)"
            )

    _asyncio.run(run())
    typer.echo("provider-init ok")


@app.command("model-set")
def model_set_command(
    provider: str = typer.Option(...),
    model: str = typer.Option(...),
) -> None:
    """Fija la seleccion activa de modelo en la DB (surte efecto en la
    siguiente decision del grafo; sin secretos)."""
    import asyncio as _asyncio

    settings = load_settings()
    engine = make_engine(settings.database_url)
    repo = ModelRepository(session_factory(engine))

    async def run() -> None:
        # L-01 (R2): la frontera unica de validacion. Un provider o model
        # inexistente o deshabilitado rechaza el comando ANTES de escribir
        # nada en settings.
        try:
            await repo.validate_selection(provider, model)
        except ModelSelectionError as exc:
            raise typer.BadParameter(str(exc))
        await repo.set_active(provider, model)
        typer.echo(f"activo={provider}/{model}")

    _asyncio.run(run())


async def _benchmark_command(
    *, n: int, opponent: str, concurrency: int, persist: bool,
    provider_name: str, model: str, fmt: str,
    on_progress: Callable[
        [BenchmarkResult, Mapping[str, int]], Awaitable[None] | None
    ] | None = None,
) -> tuple[BenchmarkResult, dict[str, int]]:
    settings = load_settings()
    await _check_showdown_reachable(settings.showdown_ws_url)
    server = local_server_configuration(settings.showdown_ws_url)
    metrics = DecisionMetrics()
    selected = _benchmark_provider(
        provider_name, model, settings.llm_request_timeout_seconds, metrics
    )
    calculator = CalcClient(
        os.environ.get("CALC_BASE_URL", "http://127.0.0.1:8200"),
        timeout_seconds=settings.llm_request_timeout_seconds,
    )
    context_repository = PostgresContextRepository(settings.database_url)
    # F2-09 (D28): el benchmark fija provider/model al inicio y el resolver
    # pinneado audita cada envelope: cualquier mezcla efectiva dentro de la
    # corrida aborta con ProviderMixError.
    pinned = PinnedResolver(
        selected, provider_name, model, enforce_pin=True,
    )
    graph = build_decision_graph(
        calculator, pinned, metrics, context_repository
    )
    suffix = str(time.time_ns())[-8:]
    common = {
        "server_configuration": server,
        "battle_format": fmt,
        "log_level": 40,
        "max_concurrent_battles": concurrency,
    }
    agent = LudexPlayer(
        account_configuration=AccountConfiguration(f"Bench{suffix}", None),
        decision_graph=graph,
        decision_budget_seconds=settings.decision_budget_seconds,
        **common,
    )
    opponent_types = {
        "random": RandomPlayer,
        "max_base_power": MaxBasePowerPlayer,
        "simple_heuristics": SimpleHeuristicsPlayer,
    }
    if opponent not in opponent_types:
        raise RuntimeError(f"oponente desconocido: {opponent}")
    rival = opponent_types[opponent](
        account_configuration=AccountConfiguration(f"Opp{suffix}", None),
        **common,
    )
    engine = make_engine(settings.database_url)
    factory = session_factory(engine)
    repo = BattleRepository(factory) if persist else None

    async def persist_tag(tag: str) -> None:
        assert repo is not None
        await _persist_one(agent, repo, tag, fmt, "local")

    async def report_progress(result: BenchmarkResult) -> None:
        if on_progress is not None:
            pending = on_progress(result, metrics.snapshot())
            if pending is not None:
                await pending

    try:
        try:
            result = await run_benchmark(
                agent, rival, n=n, persist=persist,
                persist_battle=persist_tag if persist else None,
                provider=provider_name, model=model,
                on_progress=report_progress,
                timeout=BATTLE_TIMEOUT_SECONDS,
            )
        except (ProviderError, BenchmarkDeadlineExceeded) as exc:
            completed = (
                agent.n_won_battles + agent.n_lost_battles
                + agent.n_tied_battles
            )
            result = BenchmarkResult(
                requested=n, completed=completed,
                wins=agent.n_won_battles, losses=agent.n_lost_battles,
                ties=agent.n_tied_battles, provider=provider_name,
                model=model, failure=f"{type(exc).__name__}: {exc}",
            )
    finally:
        await calculator.aclose()
        await context_repository.aclose()
        await engine.dispose()
    return result, metrics.snapshot()


@app.command("benchmark")
def benchmark_command(
    n: int = 300,
    opponent: str = "random",
    concurrency: int = 1,
    persist: bool = False,
    provider: str | None = None,
    model: str | None = None,
    fmt: str | None = None,
    run_id: str | None = None,
    pricing: Path = DEFAULT_PRICING_PATH,
    ledger: Path = DEFAULT_LEDGER_PATH,
    record: bool = True,
) -> None:
    settings = load_settings()
    provider_name = provider or settings.llm_provider
    model_name = model or settings.llm_model
    if not model_name:
        raise typer.BadParameter("falta --model o LUDEX_MODEL")
    selected_route = (
        model_route(load_model_routes(), provider_name, model_name)
        if provider_name in {"kimi", "open_code_zen"}
        else None
    )
    effective_run_id = run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
        + f"-{provider_name}-{model_name}".replace("_", "-").replace(".", "-")
    )
    artifact = DEFAULT_RUNS_PATH / f"{effective_run_id}.json"
    if record and artifact.exists():
        raise typer.BadParameter(
            f"ya existe el artefacto de corrida: {artifact}"
        )
    pricing_table = PricingTable.load(pricing)
    created_at = datetime.now(timezone.utc)
    effective_route = selected_route or ModelRoute(protocol=provider_name)

    async def report_progress(
        progress: BenchmarkResult, metrics: Mapping[str, int]
    ) -> None:
        partial = build_benchmark_record(
            run_id=effective_run_id,
            created_at=created_at,
            result=progress,
            metrics=metrics,
            opponent=opponent,
            fmt=fmt or settings.showdown_battle_format,
            route=effective_route,
            pricing=pricing_table,
            status="running",
        )
        if record:
            write_run_snapshot(partial, artifact)
        typer.echo(_progress_summary(partial))

    result, metrics = asyncio.run(_benchmark_command(
        n=n, opponent=opponent, concurrency=concurrency, persist=persist,
        provider_name=provider_name, model=model_name,
        fmt=fmt or settings.showdown_battle_format,
        on_progress=report_progress,
    ))
    typer.echo(
        f"completed={result.completed}/{result.requested} "
        f"provider={provider_name} model={model_name}"
    )
    if result.comparable:
        low, high = result.interval or (0, 0)
        typer.echo(
            f"wins={result.wins} losses={result.losses} ties={result.ties} "
            f"winrate={result.win_rate:.4%} wilson95=[{low:.4%}, {high:.4%}]"
        )
    else:
        typer.echo(f"ABORTED: {result.failure}")
    typer.echo(f"decision_metrics={metrics}")
    benchmark_record = build_benchmark_record(
        run_id=effective_run_id,
        created_at=created_at,
        result=result,
        metrics=metrics,
        opponent=opponent,
        fmt=fmt or settings.showdown_battle_format,
        route=effective_route,
        pricing=pricing_table,
    )
    typer.echo(
        f"usage_calls={metrics.get('calls_total', 0)} "
        f"input_tokens={metrics.get('input_tokens', 0)} "
        f"output_tokens={metrics.get('output_tokens', 0)} "
        f"total_cost={benchmark_record.total_cost}"
    )
    if record:
        write_run_snapshot(benchmark_record, artifact)
        append_ledger_row(benchmark_record, ledger, artifact)
        typer.echo(f"benchmark_record={artifact}")
    if result.failure:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
