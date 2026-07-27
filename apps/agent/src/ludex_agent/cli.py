"""Runner: juega N batallas en el server local y las persiste."""

from __future__ import annotations

import asyncio

import typer
from poke_env import AccountConfiguration
from poke_env.player import RandomPlayer

from .config import load_settings
from .db.repository import BattleRepository
from .db.session import make_engine, session_factory
from .showdown.client import LudexPlayer, local_server_configuration
from .state.schema import STATE_SCHEMA_VERSION

app = typer.Typer()

# Una batalla de gen6randombattle ronda los 60-120 turnos y tarda segundos.
# Este techo es holgado a proposito: solo existe para que un server colgado
# no deje el proceso esperando indefinidamente.
BATTLE_TIMEOUT_SECONDS = 180


async def play(n: int, fmt: str) -> list[str]:
    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(abs(hash((n, fmt))) % 10_000)

    agent = LudexPlayer(
        account_configuration=AccountConfiguration(f"{settings.bot_username}{suffix}", None),
        server_configuration=server, battle_format=fmt, log_level=40,
    )
    rival = RandomPlayer(
        account_configuration=AccountConfiguration(f"Rival{suffix}", None),
        server_configuration=server, battle_format=fmt, log_level=40,
    )
    # Sin timeout, una batalla que no termina —server colgado, conexion
    # cortada— deja el proceso esperando para siempre y no persiste nada.
    async with asyncio.timeout(BATTLE_TIMEOUT_SECONDS * n):
        await agent.battle_against(rival, n_battles=n)

    engine = make_engine(settings.database_url)
    repo = BattleRepository(session_factory(engine))
    tags: list[str] = []
    try:
        for tag, battle in agent.battles.items():
            tags.append(tag)
            side = battle.player_role
            battle_id = await repo.save_battle(
                battle_tag=tag, fmt=fmt,
                p1=battle.player_username, p2=battle.opponent_username,
                winner=(battle.player_username if battle.won else battle.opponent_username)
                if battle.finished else None,
                source="local", played_by="bot",
            )
            recorder = agent.recorders[tag]
            for turn in recorder.turns():
                await repo.save_turn(battle_id, side, turn, recorder.lines_for_turn(turn))

            traj = await repo.save_trajectory(
                battle_id, gen_number=battle.gen, fmt=fmt, player_side=side
            )
            for step in agent.steps[tag]:
                await repo.save_step(
                    traj, step["turn"], step["state"], STATE_SCHEMA_VERSION,
                    step["state"]["legal_actions"], step["action_taken"], "agent",
                )
            if battle.finished:
                await repo.finalize(
                    traj, result="win" if battle.won else "loss",
                    reward=1 if battle.won else -1,
                )
    finally:
        await engine.dispose()
    return tags


@app.command()
def run(n: int = 5, fmt: str | None = None) -> None:
    # Sin default literal aca: el formato por defecto vive en config.py (Global
    # Constraints: "ninguna generacion se hardcodea en src/"). `None` deja que
    # `load_settings().showdown_battle_format` decida.
    tags = asyncio.run(play(n, fmt or load_settings().showdown_battle_format))
    typer.echo(f"{len(tags)} batallas persistidas")


if __name__ == "__main__":
    app()
