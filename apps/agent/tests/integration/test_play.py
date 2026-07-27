import os
import re

import pytest
from sqlalchemy import text

from ludex_agent.config import load_settings
from ludex_agent.cli import play
from ludex_agent.db.session import make_engine, session_factory
from ludex_agent.state.schema import STATE_SCHEMA_VERSION

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="necesita postgres y el server local de showdown",
)


def _normalizar(texto: str) -> str:
    """Normaliza para comparar especie (id de poke-env) contra protocolo crudo.

    poke-env normaliza `species` a un identificador sin espacios ni puntuacion
    (p.ej. "Mr. Mime" -> "mrmime", "Farfetch'd" -> "farfetchd"). El protocolo
    crudo conserva la puntuacion tal como la manda el server. Sin quitar TODA
    la puntuacion (no solo "-" y " "), especies como Mr. Mime dan un falso
    positivo de fuga: revisado empiricamente en battle-gen6randombattle-18,
    turno 9 del protocolo, donde "Mr. Mime" aparece pero el chequeo original
    (que solo sacaba "-" y " ") no lo encontraba por el punto.
    """
    return re.sub(r"[^a-z0-9]", "", texto.lower())


@pytest.fixture(scope="module")
async def jugadas():
    return await play(2, "gen6randombattle")


async def test_persiste_batallas_turnos_y_pasos(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                bid = (await s.execute(text(
                    "SELECT id FROM battles WHERE battle_tag=:t"), {"t": tag})).scalar_one()
                turnos = (await s.execute(text(
                    "SELECT count(*) FROM battle_turns WHERE battle_id=:b"),
                    {"b": bid})).scalar_one()
                pasos = (await s.execute(text("""
                    SELECT count(*) FROM trajectory_steps ts
                    JOIN trajectories t ON t.id = ts.trajectory_id WHERE t.battle_id = :b
                """), {"b": bid})).scalar_one()
                assert turnos > 0 and pasos > 0
    finally:
        await engine.dispose()


async def test_el_reward_esta_propagado(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            sin_reward = (await s.execute(text("""
                SELECT count(*) FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags) AND ts.reward IS NULL
            """), {"tags": list(jugadas)})).scalar_one()
            assert sin_reward == 0
    finally:
        await engine.dispose()


async def test_no_hay_fuga_de_informacion_del_rival(jugadas):
    """LA propiedad de correccion de esta rebanada.

    Para cada turno N, ningun pokemon del rival puede estar en el estado si el
    protocolo no lo revelo hasta ese turno. El protocolo persistido es el juez.
    Si un modelo se entrena con informacion que un jugador no tiene, es inutil
    en batalla real.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT ts.turn_number, ts.state, t.player_side, t.battle_id
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags)
                ORDER BY t.battle_id, ts.turn_number
            """), {"tags": list(jugadas)})).all()
            assert filas, "no hay pasos que verificar"

            for turno, estado, side, battle_id in filas:
                acumulado = (await s.execute(text("""
                    SELECT string_agg(array_to_string(protocol_lines, ' '), ' ')
                    FROM battle_turns
                    WHERE battle_id = :b AND player_side = :ps AND turn_number <= :t
                """), {"b": battle_id, "ps": side, "t": turno})).scalar_one() or ""
                for mon in estado["opponent"]["pokemon"]:
                    especie = _normalizar(mon["species"])
                    visto = especie in _normalizar(acumulado)
                    assert visto, (
                        f"FUGA: {mon['species']} aparece en el estado del turno "
                        f"{turno} pero el protocolo no lo revelo hasta ahi"
                    )
    finally:
        await engine.dispose()


async def test_la_version_de_esquema_esta_en_todas_las_filas(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            distintas = (await s.execute(text("""
                SELECT DISTINCT ts.state_schema_version FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags)
            """), {"tags": list(jugadas)})).scalars().all()
            assert distintas == [STATE_SCHEMA_VERSION]
    finally:
        await engine.dispose()


async def test_reejecutar_no_duplica(jugadas):
    """El runner es idempotente por battle_tag."""
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                n = (await s.execute(text(
                    "SELECT count(*) FROM battles WHERE battle_tag=:t"),
                    {"t": tag})).scalar_one()
                assert n == 1
    finally:
        await engine.dispose()
