"""Runner: juega N batallas en el server local y las persiste."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import typer
from poke_env import AccountConfiguration
from poke_env.player import RandomPlayer

from .config import load_settings
from .db.repository import BattleRepository
from .db.session import make_engine, session_factory
from .showdown.client import LudexPlayer, local_server_configuration
from .state.schema import STATE_SCHEMA_VERSION

logger = logging.getLogger(__name__)

app = typer.Typer()

# Una batalla de gen6randombattle ronda los 60-120 turnos y tarda segundos.
# Este techo es holgado a proposito: solo existe para que un server colgado
# no deje el proceso esperando indefinidamente. Es POR BATALLA (minor de la
# review final: antes era `* n`, presupuestando el lote entero y haciendose
# cada vez mas probable de saltar cuanto mas grande el lote).
BATTLE_TIMEOUT_SECONDS = 180


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
            try:
                async with asyncio.timeout(BATTLE_TIMEOUT_SECONDS):
                    await agent.battle_against(rival, n_battles=1)
            except TimeoutError:
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

    battle_id = await repo.save_battle(
        battle_tag=tag, fmt=fmt, p1=p1, p2=p2, winner=winner,
        source=source, played_by="bot",
    )
    recorder = agent.recorders[tag]
    for turn in recorder.turns():
        await repo.save_turn(battle_id, side, turn, recorder.lines_for_turn(turn))

    traj = await repo.save_trajectory(
        battle_id, gen_number=battle.gen, fmt=fmt, player_side=side
    )
    # C1 (ver LudexPlayer._finalize_pending_steps, D22): la materializacion
    # del estado ahora es sincronica, dentro del manejo de mensajes; esto
    # solo corrige la etiqueta de turno contra el protocolo antes de leer
    # agent.steps[tag].
    await agent.wait_for_pending_steps(tag)
    # D21 (C2): decision_index es el indice de la lista, que ya numera una
    # vez por decision (una vez por llamada a choose_move), no por turno.
    for decision_index, step in enumerate(agent.steps[tag]):
        # I3 (review de merge): este camino era el mismo modo de falla de C2
        # recreado -- perder una decision del dataset sin que nadie se
        # entere. Hoy no ocurre nunca (0 huecos de decision_index medidos
        # sobre toda la base), pero si el dia de mañana se graban miles de
        # batallas desatendidas, un hueco tiene que dejar rastro: un warning
        # con el detalle y un contador consultable (`agent.lost_step_count`),
        # no un `continue` mudo. `step["state"] is None` cubre ademas el paso
        # defensivo de `wait_for_pending_steps` (paso reservado que nunca se
        # llego a materializar): sin este chequeo aca tambien, ese paso no
        # es `None` pero igual revienta con un TypeError al leer
        # `step["state"]["legal_actions"]`, cambiando una perdida silenciosa
        # por un crash a mitad de la persistencia de la batalla.
        if step is None or step.get("state") is None:
            agent.lost_step_count += 1
            logger.warning(
                "paso %d de %s se descarta sin persistir (%s): la decision "
                "se pierde del dataset (lost_step_count=%d)",
                decision_index, tag,
                "step es None" if step is None else "step['state'] es None",
                agent.lost_step_count,
            )
            continue
        await repo.save_step(
            traj, decision_index, step["turn"], step["state"], STATE_SCHEMA_VERSION,
            step["state"]["legal_actions"], step["action_taken"], "agent",
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


if __name__ == "__main__":
    app()
