import os
import re

import pytest
from sqlalchemy import text

from ludex_agent.config import load_settings
from ludex_agent.cli import play
from ludex_agent.db.repository import BattleRepository
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

            revisados = 0
            for turno, estado, side, battle_id in filas:
                # Se comparan LINEAS sueltas, no el protocolo concatenado. Pegar
                # todo y sacarle los separadores crea un blob donde una especie
                # puede "aparecer" a caballo entre dos tokens sin relacion, y una
                # fuga real pasaria como revelada.
                lineas = (await s.execute(text("""
                    SELECT unnest(protocol_lines) FROM battle_turns
                    WHERE battle_id = :b AND player_side = :ps AND turn_number <= :t
                """), {"b": battle_id, "ps": side, "t": turno})).scalars().all()
                normalizadas = [_normalizar(l) for l in lineas]

                for mon in estado["opponent"]["pokemon"]:
                    revisados += 1
                    especie = _normalizar(mon["species"])
                    visto = any(especie in linea for linea in normalizadas)
                    assert visto, (
                        f"FUGA: {mon['species']} aparece en el estado del turno "
                        f"{turno} pero el protocolo no lo revelo hasta ahi"
                    )

            # Canario: sin esto, un serializador que dejara `opponent.pokemon`
            # siempre vacio haria que el loop no itere nunca y el test pasara en
            # verde sin haber verificado una sola especie.
            assert revisados > 0, (
                "ningun paso tenia pokemon del rival: el test no verifico nada"
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


async def test_repersistir_la_misma_batalla_no_duplica(jugadas):
    """Idempotencia REAL: se vuelve a guardar una batalla ya guardada.

    La version anterior de este test solo contaba filas de una unica corrida,
    donde cada battle_tag es unico por construccion del loop: pasaba en verde
    aunque el ON CONFLICT estuviera roto o ausente. Para ejercer la garantia
    hay que reescribir la MISMA batalla y verificar que no aparece una fila
    nueva y que devuelve el mismo id.
    """
    engine = make_engine(load_settings().database_url)
    try:
        factory = session_factory(engine)
        repo = BattleRepository(factory)
        tag = jugadas[0]
        async with factory() as s:
            antes = (await s.execute(
                text("SELECT count(*) FROM battles"))).scalar_one()
            # `winner` TIENE que venir en el SELECT y volver tal cual: el
            # ON CONFLICT hace `SET winner = EXCLUDED.winner`, asi que mandar
            # None aca borraria el ganador real de una batalla ya jugada. Este
            # test verifica idempotencia; no debe destruir el dato que verifica.
            fila = (await s.execute(text(
                "SELECT id, format, p1, p2, winner FROM battles WHERE battle_tag = :t"),
                {"t": tag})).one()

        # Canario: sin esto la asercion de abajo se cumple sola cuando el
        # ganador ya viene en NULL, que es justo el estado que este test
        # provocaba antes. Una batalla de `jugadas` termino: tiene ganador.
        assert fila[4] is not None, "la batalla recien jugada debe tener ganador"

        de_nuevo = await repo.save_battle(
            battle_tag=tag, fmt=fila[1], p1=fila[2], p2=fila[3],
            winner=fila[4], source="local", played_by="bot",
        )

        async with factory() as s:
            ganador = (await s.execute(text(
                "SELECT winner FROM battles WHERE battle_tag = :t"),
                {"t": tag})).scalar_one()
        assert ganador == fila[4], "re-persistir no debe alterar el ganador"

        async with factory() as s:
            despues = (await s.execute(
                text("SELECT count(*) FROM battles"))).scalar_one()

        assert de_nuevo == fila[0], "el mismo battle_tag debe devolver el mismo id"
        assert despues == antes, "re-persistir no debe crear una fila nueva"
    finally:
        await engine.dispose()


async def test_cada_paso_de_estado_tiene_su_protocolo(jugadas):
    """La propiedad que hace REVERSIBLE al serializador.

    Si manana se descubre que el serializador tenia un defecto, el historico se
    re-deriva desde el protocolo crudo en vez de descartarse. Eso solo vale si
    CADA paso de estado tiene su protocolo, del mismo jugador y del mismo turno.
    Contar filas de cada lado por separado no alcanza: hay que verificar la
    correspondencia turno a turno.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            huerfanos = (await s.execute(text("""
                SELECT count(*) FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags)
                  AND NOT EXISTS (
                      SELECT 1 FROM battle_turns bt
                      WHERE bt.battle_id = t.battle_id
                        AND bt.player_side = t.player_side
                        AND bt.turn_number = ts.turn_number)
            """), {"tags": list(jugadas)})).scalar_one()
        assert huerfanos == 0, (
            f"{huerfanos} pasos de estado sin su protocolo crudo: el historico "
            "de esas batallas no se podria re-derivar"
        )
    finally:
        await engine.dispose()
