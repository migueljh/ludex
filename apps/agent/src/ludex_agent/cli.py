"""Runner: juega N batallas en el server local y las persiste."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
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

from .benchmark import (
    BenchmarkDeadlineExceeded,
    BenchmarkFailure,
    BenchmarkResult,
    InternalCleanupError,
    ShowdownUnavailableError,
    failure_classification,
    run_benchmark,
)
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
    PinnedResolver,
    ProviderChain,
    ProviderError,
    ProviderSelectionError,
    build_route_provider,
    load_model_routes,
    model_route,
    provider_keys,
    _http_status_chain,
    _structured_provider_error_code,
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


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """Escritura atomica: primero el .tmp, luego rename. Un fallo a mitad
    de escritura JAMAS reemplaza el ultimo artefacto valido."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


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
        # L-03 (post-R1B): clase propia para la indisponibilidad LOCAL de
        # Showdown (RuntimeError from OSError): la matriz la clasifica
        # `externally-limited`, nunca internal-defect ni incompatibilidad.
        raise ShowdownUnavailableError(
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
            battle_timeout = asyncio.timeout(settings.battle_timeout_seconds)
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
    if not keys:
        raise ProviderSelectionError(
            f"NOT RUN: credential unavailable for {name}/{model}"
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
    else:
        selected = build_route_provider(
            name, model, base_url=os.environ.get(base_env) if base_env else None,
            keys=keys, metrics=metrics, timeout_seconds=timeout, route=route,
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
    prompt = (
        "Elegí exactamente una acción legal y respondé con action, un "
        "rationale breve, confidence en [0,1] y alternatives (puede ser []). "
        "legal_actions="
        '[{"kind":"move","id":"tackle"},{"kind":"switch","species":"pikachu"}]'
    )
    try:
        selected = _benchmark_provider(
            provider, model, settings.llm_request_timeout_seconds, metrics
        )
        envelope = asyncio.run(selected.complete(
            prompt,
            deadline=time.monotonic() + settings.llm_request_timeout_seconds,
            turn_id=f"provider-smoke:{provider}:{model}",
        ))
    except ProviderSelectionError as exc:
        typer.echo(f"{exc}")
        raise typer.Exit(code=2) from None
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


async def _close_player_sockets(agent: Any, rival: Any) -> list[BaseException]:
    """L-01 (post-R1B): cierra los PSClient de AMBOS players via la API real
    de poke-env (`ps_client.stop_listening()`, async y cross-loop: agenda el
    cierre en el POKE_LOOP propio de poke-env y espera; `Player` no ofrece
    close()). Sin esto, cada benchmark abortado fuga 2 websockets + sus
    listener tasks en el POKE_LOOP global, acumulandose entre modelos de
    una misma ronda de la matriz.

    L-01 (correccion LATWAN): devuelve los errores de cierre de cada player
    (jamas los traga): la frontera estructurada decide — con primaria en
    vuelo se preserva; sin primaria, un cierre fallido impide reportar
    `compatible`. `CancelledError` NO se traga como fallo ordinario: se
    propaga.
    """
    errors: list[BaseException] = []
    for player in (agent, rival):
        ps_client = getattr(player, "ps_client", None)
        stop = getattr(ps_client, "stop_listening", None)
        if stop is None:
            continue
        try:
            await stop()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.warning(
                "fallo al cerrar PSClient de %r: %s",
                getattr(player, "username", type(player).__name__),
                type(exc).__name__,
            )
            errors.append(exc)
    return errors


async def _structured_cleanup(
    agent: Any,
    rival: Any,
    calculator: Any,
    context_repository: Any,
    engine: Any,
) -> list[BaseException]:
    """L-01 (correccion LATWAN): frontera estructurada del cleanup del
    benchmark.

    Intenta SIEMPRE y EN ORDEN: drain de decisiones en vuelo (D46) →
    PSClient del agent → PSClient del rival → CalcClient → context
    repository → engine. Cada tramo se intenta aunque los anteriores fallen
    y devuelve los errores de todos los pasos (jamas se tragan): sin
    excepcion primaria, un fallo de cleanup impide reportar `compatible`.

    `CancelledError` se propaga: un cierre interrumpido por cancelacion no
    se convierte en fallo ordinario de cleanup. Recursos que no llegaron a
    crearse (None) se saltan.
    """
    errors: list[BaseException] = []

    async def attempt(label: str, step: Any) -> None:
        if step is None:
            return
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.warning("cleanup %s fallo: %s", label, type(exc).__name__)
            errors.append(exc)

    # D46/MON-23: la decision en vuelo de la ultima batalla corre como task
    # hermana de `run_benchmark`/`battle_against` en `ps_client.loop`;
    # cancelar/timeoutear ese wrapper NO la toca. Hay que drenarla antes de
    # cerrar `CalcClient`/el context repository o una decision huerfana
    # puede usarlos ya cerrados.
    await attempt(
        "drain", agent.drain_inflight_decisions if agent is not None else None
    )
    errors.extend(await _close_player_sockets(agent, rival))
    await attempt("calc", calculator.aclose if calculator is not None else None)
    await attempt(
        "context",
        context_repository.aclose
        if context_repository is not None else None,
    )
    await attempt("engine", engine.dispose if engine is not None else None)
    return errors


def _partial_benchmark_result(
    exc: BaseException,
    agent: Any,
    *,
    requested: int,
    provider: str,
    model: str,
) -> BenchmarkResult:
    """L-02 (correccion LATWAN): resultado parcial TIPADO de un fallo del
    benchmark en la frontera de `_benchmark_command`.

    Preserva el progreso REAL (requested/completed/W/L/T desde los
    contadores del agente — jamas ceros inventados), la identidad efectiva
    (provider/model pinneados, los mismos que el smoke ya verifico) y la
    evidencia sanitizada de la excepcion (`failure_type`/
    `failure_cause_type` via `failure_classification`; `http_status`/
    `provider_error_code` solo de campos estructurados). Sin agente (fallo
    antes de crearlo, p.ej. el preflight de Showdown) el progreso es 0, que
    es la verdad: no hubo batallas.
    """
    if agent is None:
        completed = wins = losses = ties = 0
    else:
        completed = (
            agent.n_won_battles + agent.n_lost_battles + agent.n_tied_battles
        )
        wins = agent.n_won_battles
        losses = agent.n_lost_battles
        ties = agent.n_tied_battles
    failure_type, failure_cause_type = failure_classification(exc)
    return BenchmarkResult(
        requested=requested, completed=completed, wins=wins, losses=losses,
        ties=ties, provider=provider, model=model,
        failure=f"{type(exc).__name__}: {exc}",
        failure_type=failure_type, failure_cause_type=failure_cause_type,
        http_status=_http_status_chain(exc),
        provider_error_code=_structured_provider_error_code(exc),
    )


def _cleanup_failure_result(
    agent: Any,
    errors: Sequence[BaseException],
    *,
    requested: int,
    provider: str,
    model: str,
) -> BenchmarkResult:
    """L-01 (correccion LATWAN): sin excepcion primaria, un fallo del
    cleanup NO puede dejar la corrida `compatible`: se emite un
    `BenchmarkResult` con `failure_type=InternalCleanupError` (marcador
    clasificado que la matriz traduce a `internal-defect`), mensaje
    sanitizado fijo (jamas mensajes crudos de los pasos) y la clase de la
    causa real del primer paso fallido como `failure_cause_type`. El
    progreso real y la identidad efectiva se preservan.
    """
    completed = (
        agent.n_won_battles + agent.n_lost_battles + agent.n_tied_battles
        if agent is not None else 0
    )
    wins = agent.n_won_battles if agent is not None else 0
    losses = agent.n_lost_battles if agent is not None else 0
    ties = agent.n_tied_battles if agent is not None else 0
    first = errors[0]
    return BenchmarkResult(
        requested=requested, completed=completed, wins=wins, losses=losses,
        ties=ties, provider=provider, model=model,
        failure="internal defect: fallo el cierre de recursos del benchmark",
        failure_type=InternalCleanupError.__name__,
        failure_cause_type=type(first).__name__,
        http_status=_http_status_chain(first),
        provider_error_code=_structured_provider_error_code(first),
    )


async def _benchmark_command(
    *, n: int, opponent: str, concurrency: int, persist: bool,
    provider_name: str, model: str, fmt: str,
    battle_timeout_seconds: float,
    on_progress: Callable[
        [BenchmarkResult, Mapping[str, int | None]], Awaitable[None] | None
    ] | None = None,
) -> tuple[BenchmarkResult, dict[str, int | None]]:
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    metrics = DecisionMetrics()
    # L-05 (post-R1B): la seleccion/validacion local de credenciales ocurre
    # ANTES del chequeo de Showdown: sin credenciales el comando emite
    # not-run (ProviderSelectionError, exit 2) sin depender de que el server
    # local este arriba. El chequeo de Showdown sigue vigente cuando las
    # credenciales existen (no se debilita).
    selected = _benchmark_provider(
        provider_name, model, settings.llm_request_timeout_seconds, metrics
    )
    opponent_types = {
        "random": RandomPlayer,
        "max_base_power": MaxBasePowerPlayer,
        "simple_heuristics": SimpleHeuristicsPlayer,
    }
    if opponent not in opponent_types:
        raise RuntimeError(f"oponente desconocido: {opponent}")
    agent: Any | None = None
    rival: Any | None = None
    calculator: Any | None = None
    context_repository: Any | None = None
    engine: Any | None = None
    result: BenchmarkResult | None = None
    primary: BaseException | None = None
    try:
        await _check_showdown_reachable(settings.showdown_ws_url)
        calculator = CalcClient(
            os.environ.get("CALC_BASE_URL", "http://127.0.0.1:8200"),
            timeout_seconds=settings.llm_request_timeout_seconds,
        )
        context_repository = PostgresContextRepository(settings.database_url)
        # F2-09 (D28): el benchmark fija provider/model al inicio y el
        # resolver pinneado audita cada envelope: cualquier mezcla
        # efectiva dentro de la corrida aborta con ProviderMixError.
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
            result = await run_benchmark(
                agent, rival, n=n, persist=persist,
                persist_battle=persist_tag if persist else None,
                provider=provider_name, model=model,
                on_progress=report_progress,
                timeout=battle_timeout_seconds,
            )
        except (ProviderError, BenchmarkDeadlineExceeded) as exc:
            completed = (
                agent.n_won_battles + agent.n_lost_battles
                + agent.n_tied_battles
            )
            # R3 (MON-15): evidencia durable y sanitizada del fallo. El
            # error clasificado (`TransientProviderError`, etc.) conserva
            # su `__cause__` original (p.ej. `APITimeoutError`) porque
            # `KeyRotatingProvider` lo re-lanza con `raise error from raw`.
            # Persistimos SOLO los nombres de clase, nunca el mensaje crudo.
            failure_type, failure_cause_type = failure_classification(exc)
            # L-03 (R1A): http_status y provider_error_code (solo campo
            # estructurado permitido) cuando existen; jamas texto libre.
            result = BenchmarkResult(
                requested=n, completed=completed,
                wins=agent.n_won_battles, losses=agent.n_lost_battles,
                ties=agent.n_tied_battles, provider=provider_name,
                model=model, failure=f"{type(exc).__name__}: {exc}",
                failure_type=failure_type,
                failure_cause_type=failure_cause_type,
                http_status=_http_status_chain(exc),
                provider_error_code=_structured_provider_error_code(exc),
            )
    except BaseException as exc:
        primary = exc
    # L-01 (correccion LATWAN): frontera estructurada del cleanup. Se
    # intentan SIEMPRE y en orden drain → ambos players → calc → contexto →
    # engine, aunque cualquiera falle; los errores de los pasos se recogen,
    # nunca se tragan.
    try:
        cleanup_errors = await _structured_cleanup(
            agent, rival, calculator, context_repository, engine,
        )
    except asyncio.CancelledError:
        raise
    if primary is not None:
        # La primaria se preserva SIEMPRE: cancelacion, KeyboardInterrupt y
        # SystemExit se re-lanzan tal cual (jamas se convierten en un fallo
        # de cleanup); el resto viaja como BenchmarkFailure con resultado
        # parcial tipado y su causa original intacta en `__cause__`.
        if isinstance(primary, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise primary
        partial = _partial_benchmark_result(
            primary, agent, requested=n,
            provider=provider_name, model=model,
        )
        raise BenchmarkFailure(partial) from primary
    if cleanup_errors and (result is None or result.failure is None):
        result = _cleanup_failure_result(
            agent, cleanup_errors, requested=n,
            provider=provider_name, model=model,
        )
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
    battle_timeout: float | None = typer.Option(
        None, "--battle-timeout",
        help="Deadline por batalla en segundos (default: LUDEX_BATTLE_TIMEOUT_SECONDS o 180). "
        "La matriz usa 1800. Positivo; independiente del deadline de cada decision.",
    ),
) -> None:
    settings = load_settings()
    if battle_timeout is not None and battle_timeout <= 0:
        raise typer.BadParameter("--battle-timeout debe ser un numero positivo")
    effective_battle_timeout = (
        battle_timeout or settings.battle_timeout_seconds
    )
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
        progress: BenchmarkResult, metrics: Mapping[str, int | None]
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
            battle_timeout_seconds=effective_battle_timeout,
        )
        if record:
            write_run_snapshot(partial, artifact)
        typer.echo(_progress_summary(partial))

    try:
        result, metrics = asyncio.run(_benchmark_command(
            n=n, opponent=opponent, concurrency=concurrency, persist=persist,
            provider_name=provider_name, model=model_name,
            fmt=fmt or settings.showdown_battle_format,
            battle_timeout_seconds=effective_battle_timeout,
            on_progress=report_progress,
        ))
    except ProviderSelectionError as exc:
        # F2-10: sin credenciales no se corre ni se publica un winrate
        # comparable. Se emite un artefacto de corrida explícito con status
        # "not-run" para que quede trazable que el punto de control fue
        # alcanzado aunque la corrida real no se ejecutó.
        typer.echo(f"{exc}")
        # R3 (MON-15): not-run conserva la clase del error de seleccion
        # (`ProviderSelectionError`) sin inventar una causa: este error se
        # lanza directo, sin `raise ... from`, asi que `__cause__` es None.
        failure_type, failure_cause_type = failure_classification(exc)
        not_run_result = BenchmarkResult(
            requested=n, completed=0, wins=0, losses=0, ties=0,
            provider=provider_name, model=model_name,
            failure=str(exc),
            failure_type=failure_type,
            failure_cause_type=failure_cause_type,
        )
        not_run_metrics: dict[str, int | None] = {
            "turns_total": 0, "calls_total": 0, "input_tokens": 0,
            "output_tokens": 0, "cached_input_tokens": 0,
            "reasoning_tokens": 0, "key_rotations": 0,
            "keys_quarantined": 0, "transient_retries_executed": 0,
            "provider_switches": 0, "turns_quota_affected": 0,
            "turns_transient_affected": 0, "turns_deadline_affected": 0,
            "turns_model_invalid": 0, "turns_fallback": 0,
            # L-01 (R2): sin muestras no hay latencia comparable; los
            # percentiles de una corrida no ejecutada son None, no 0.
            "completion_latency_ms_count": 0,
            "completion_latency_ms_total": None,
            "completion_latency_ms_p50": None,
            "completion_latency_ms_p95": None,
            "completion_latency_ms_max": None,
            "decision_latency_ms_count": 0,
            "decision_latency_ms_total": None,
            "decision_latency_ms_p50": None,
            "decision_latency_ms_p95": None,
            "decision_latency_ms_max": None,
        }
        not_run_record = build_benchmark_record(
            run_id=effective_run_id,
            created_at=created_at,
            result=not_run_result,
            metrics=not_run_metrics,
            opponent=opponent,
            fmt=fmt or settings.showdown_battle_format,
            route=effective_route,
            pricing=pricing_table,
            status="not-run",
            battle_timeout_seconds=effective_battle_timeout,
        )
        if record:
            write_run_snapshot(not_run_record, artifact)
            append_ledger_row(not_run_record, ledger, artifact)
            typer.echo(f"not_run_record={artifact}")
        raise typer.Exit(code=2) from None
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
        battle_timeout_seconds=effective_battle_timeout,
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


@app.command("matrix-plan")
def matrix_plan_command(
    inventory: Path = typer.Option(
        None, "--inventory",
        help="Inventario baseline commiteado (default: ultimo "
        "2026*provider-matrix-inventory*.json en evals/runs)",
    ),
    manifest: Path = typer.Option(
        None, "--manifest", help="Archivo de manifiesto de salida",
    ),
    budget: Path | None = typer.Option(
        None, "--budget",
        help="JSON de presupuesto: {provider: {balance_usd, cap_usd, leave_usd}}",
    ),
    overrides: Path | None = typer.Option(
        None, "--overrides",
        help="Inventario enriquecido (tier_override/prices/deprecated) que "
        "matiza el baseline: lo que la tabla de precios no cubre o el tier "
        "no puede probarse (p.ej. Gemini free-tier no verificado).",
    ),
    no_refresh: bool = typer.Option(False, "--no-refresh"),
) -> None:
    """Construye el manifiesto reproducible de la matriz SIN consumir cuota.

    Refresca /models (metadata) de google/kimi/open_code_zen, calcula
    altas/bajas contra el inventario commiteado, y escribe una fila por
    provider/model con protocolo/ruta, tier, costo estimado y clasificacion
    (ready / excluded / missing-route / pending-budget). No ejecuta smokes
    ni batallas: es el plan que Latwan aprueba antes de gastar."
    """
    from .matrix import (
        BudgetSpec,
        ManifestRow,
        MatrixModelResult,
        build_manifest,
        delta_catalog,
        manifest_to_dict,
        plan_budget,
        refresh_models,
        run_matrix_round,
        tier_prices_from_pricing_table,
    )
    from .eval_cost import PricingTable

    settings = load_settings()
    pricing_table = PricingTable.load(
        Path(os.environ.get(
            "LUDEX_PRICING_TABLE", DEFAULT_PRICING_PATH
        ))
    )
    tier_prices = tier_prices_from_pricing_table(pricing_table)
    import asyncio as _asyncio

    previous = {}
    inventory_path = inventory
    if inventory_path is None:
        candidates = sorted(
            Path(DEFAULT_RUNS_PATH).glob("*provider-matrix-inventory*.json")
        )
        if not candidates:
            raise typer.BadParameter(
                "no hay inventario baseline en evals/runs; pasalo con --inventory"
            )
        inventory_path = candidates[-1]
    previous = json.loads(inventory_path.read_text(encoding="utf-8"))
    if overrides is not None:
        enrichment = json.loads(overrides.read_text(encoding="utf-8"))
        prev_models = previous.setdefault("models", {})
        for provider, entries in enrichment.get("models", {}).items():
            by_id = {
                entry.get("id"): entry for entry in prev_models.get(provider, [])
            }
            for entry in entries:
                model_id = entry.get("id")
                if model_id is None:
                    continue
                target = by_id.get(model_id)
                if target is None:
                    target = {"id": model_id}
                    by_id[model_id] = target
                target.update(entry)
            prev_models[provider] = list(by_id.values())

    budgets: dict[str, BudgetSpec] = {}
    if budget is not None:
        raw = json.loads(budget.read_text(encoding="utf-8"))
        budgets = {
            name: BudgetSpec(
                balance_usd=Decimal(str(spec["balance_usd"])),
                cap_usd=Decimal(str(spec["cap_usd"])),
                leave_usd=Decimal(str(spec.get("leave_usd", 0))),
            )
            for name, spec in raw.items()
        }

    async def run() -> None:
        fresh: dict[str, list[str]] = {}
        if not no_refresh:
            for provider, (key_env, base_env) in (
                ("google", ("GEMINI_API_KEY", None)),
                ("kimi", ("KIMI_API_KEY", "KIMI_BASE_URL")),
                ("open_code_zen", (
                    "OPEN_CODE_ZEN_API_KEY", "OPEN_CODE_ZEN_BASE_URL",
                )),
            ):
                key = os.environ.get(key_env, "").strip()
                base_url = (
                    os.environ.get(base_env, "").strip()
                    if base_env else None
                )
                if not key:
                    typer.echo(f"matrix-plan: {provider} sin clave, catalogo no refrescado")
                    continue
                models = await refresh_models(
                    provider, base_url=base_url, api_key=key,
                    environ=os.environ,
                )
                fresh[provider] = models
                typer.echo(f"matrix-plan: {provider} /models = {len(models)}")
        else:
            for provider in ("google", "kimi", "open_code_zen"):
                prev = previous.get("models", {}).get(provider, [])
                fresh[provider] = [entry["id"] for entry in prev]

        delta = delta_catalog(
            {
                provider: [entry["id"] for entry in previous.get("models", {}).get(provider, [])]
                for provider in ("google", "kimi", "open_code_zen")
            },
            fresh,
        )
        for provider, changes in delta.items():
            typer.echo(
                f"matrix-plan: delta {provider}: "
                f"+{len(changes['added'])} -{len(changes['removed'])}"
            )
        rows = build_manifest(
            fresh,
            previous_inventory=previous,
            tier_prices=tier_prices,
        )
        rows = plan_budget(rows, budgets)
        target = manifest or (
            Path(DEFAULT_RUNS_PATH)
            / f"{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')}-matrix-manifest.json"
        )
        document = manifest_to_dict(rows)
        document["delta"] = delta
        document["baseline_inventory"] = str(inventory_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        typer.echo(f"matrix-plan: {target} rows={len(rows)} {by_status}")

    _asyncio.run(run())


@app.command("matrix-run")
def matrix_run_command(
    manifest: Path = typer.Option(..., "--manifest", help="Manifiesto aprobado"),
    tier: str = typer.Option(..., "--tier", help="Fase: free | paid"),
    round_name: str = typer.Option("r1", "--round", help="Nombre de la ronda"),
    battle_timeout: float = typer.Option(
        1800.0, "--battle-timeout", help="Deadline por batalla (matriz: 1800)",
    ),
    smoke_timeout: float = typer.Option(
        None, "--smoke-timeout",
        help="Deadline del smoke en segundos (default: request timeout + margen)",
    ),
    resume: bool = typer.Option(False, "--resume"),
    zen_auto_reload_confirmed: bool = typer.Option(
        False, "--zen-auto-reload-confirmed",
        help="Confirmacion explicita de que el auto-reload de OpenCode Zen "
        "esta DESACTIVADO. Obligatorio si la fase toca open_code_zen.",
    ),
) -> None:
    """Ejecuta una fase de la matriz de forma fail-closed.

    - `--tier` es obligatorio: solo se ejecutan filas `ready` de ESE tier;
    - refresca /models antes de la ronda (metadata, sin cuota);
    - 1 smoke por modelo; si pasa, EXACTAMENTE 2 batallas pinneadas
      (enforce_pin=True, sin chains, concurrency=1, persist=false,
      opponent=simple_heuristics, formato configurado);
    - artefacto atomico por modelo + estado de reanudacion;
    - un parcial/abortado nunca publica winrate comparable.
    """
    from .matrix import (
        ManifestRow, MatrixModelResult, refresh_models, run_matrix_round
    )

    settings = load_settings()
    if battle_timeout <= 0:
        raise typer.BadParameter("--battle-timeout debe ser positivo")
    if tier not in {"free", "paid"}:
        raise typer.BadParameter("--tier debe ser free o paid")

    document = json.loads(manifest.read_text(encoding="utf-8"))
    rows = _manifest_rows_from_document(document)
    if any(row.tier == "free" and row.provider == "open_code_zen"
           for row in rows if row.status == "ready"):
        if not zen_auto_reload_confirmed:
            raise typer.BadParameter(
                "fase que toca open_code_zen requiere "
                "--zen-auto-reload-confirmed (auto-reload desactivado)"
            )

    run_dir = Path(DEFAULT_RUNS_PATH)
    state_path = run_dir / f"{round_name}-matrix-run-state.json"
    previous: dict[str, MatrixModelResult] = {}
    if resume and state_path.exists():
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        for key, value in raw.items():
            previous[key] = MatrixModelResult(**value)

    import asyncio as _asyncio

    async def refresh_catalog() -> dict[str, list[str]]:
        fresh: dict[str, list[str]] = {}
        for provider, (key_env, base_env) in (
            ("google", ("GEMINI_API_KEY", None)),
            ("kimi", ("KIMI_API_KEY", "KIMI_BASE_URL")),
            ("open_code_zen", (
                "OPEN_CODE_ZEN_API_KEY", "OPEN_CODE_ZEN_BASE_URL",
            )),
        ):
            key = os.environ.get(key_env, "").strip()
            base_url = (
                os.environ.get(base_env, "").strip() if base_env else None
            )
            if not key:
                continue
            fresh[provider] = await refresh_models(
                provider, base_url=base_url, api_key=key,
                environ=os.environ,
            )
        return fresh

    def on_result(result: MatrixModelResult) -> None:
        key = f"{result.provider}/{result.model}"
        previous[key] = result
        state: dict[str, Any] = {
            k: v.to_dict() for k, v in previous.items()
        }
        _atomic_write_json(state_path, state)
        if result.status == "already-finalized":
            return
        _atomic_write_json(
            run_dir / f"{round_name}-{result.provider}-{result.model}-matrix.json",
            result.to_dict(),
        )
        typer.echo(
            f"matrix-run: {key} -> {result.status} "
            f"battles={result.battles_completed}/{result.battles_requested}"
        )

    async def run_battles(provider_name, model_name, *, n, battle_timeout_seconds,
                          fmt, opponent):
        return await _benchmark_command(
            n=n, opponent=opponent, concurrency=1, persist=False,
            provider_name=provider_name, model=model_name,
            fmt=fmt, battle_timeout_seconds=battle_timeout_seconds,
        )

    def build_provider(provider_name: str, model_name: str):
        from .graph.provider import load_model_routes, model_route

        metrics = DecisionMetrics()
        if provider_name in {"kimi", "open_code_zen"}:
            model_route(load_model_routes(), provider_name, model_name)
        return _benchmark_provider(
            provider_name, model_name,
            settings.llm_request_timeout_seconds, metrics,
        )

    effective_smoke_timeout = smoke_timeout or (
        max(settings.llm_request_timeout_seconds, 60.0) + 30.0
    )

    _asyncio.run(run_matrix_round(
        rows=rows,
        tier=tier,
        battle_timeout_seconds=battle_timeout,
        fmt=settings.showdown_battle_format,
        opponent="simple_heuristics",
        smoke_deadline_seconds=effective_smoke_timeout,
        build_provider=build_provider,
        run_battles=run_battles,
        refresh_catalog=refresh_catalog,
        previous=previous,
        on_result=on_result,
    ))
    summary = _matrix_run_summary(previous)
    typer.echo(f"matrix-run: {round_name} terminado: {summary}")


def _manifest_rows_from_document(document: dict[str, Any]) -> list[ManifestRow]:
    from .matrix import ManifestRow
    rows: list[ManifestRow] = []
    for raw in document.get("rows", []):
        pin = raw.get("pin")
        rows.append(ManifestRow(
            provider=raw["provider"], model=raw["model"],
            protocol=raw.get("protocol"),
            endpoint=raw.get("endpoint"),
            structured_output=raw.get("structured_output"),
            tier=raw.get("tier", "unknown"),
            status=raw.get("status", "pending"),
            battles=int(raw.get("battles", 0)),
            concurrency=int(raw.get("concurrency", 1)),
            persist=bool(raw.get("persist", False)),
            pin=(pin[0], pin[1]) if isinstance(pin, list) and len(pin) == 2
            else (raw["provider"], raw["model"]),
            estimated_cost_usd=(
                Decimal(str(raw["estimated_cost_usd"]))
                if raw.get("estimated_cost_usd") is not None else None
            ),
            estimated_smoke_usd=(
                Decimal(str(raw["estimated_smoke_usd"]))
                if raw.get("estimated_smoke_usd") is not None else None
            ),
            cumulative_cost_usd=(
                Decimal(str(raw["cumulative_cost_usd"]))
                if raw.get("cumulative_cost_usd") is not None else None
            ),
            classification_note=raw.get("classification_note"),
        ))
    return rows


def _matrix_run_summary(previous: Mapping[str, Any]) -> dict[str, int]:
    from collections import Counter
    counter: Counter[str] = Counter()
    for result in previous.values():
        counter[result.status] += 1
    return dict(counter)


if __name__ == "__main__":
    app()