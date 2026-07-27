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


def _clave_de_busqueda(accion: str) -> str:
    """Reduce el id de Hidden Power a una clave que SI puede aparecer en el
    protocolo (fix-final, residual de C1, causa 2).

    poke-env distingue las 17 variantes de Hidden Power por tipo
    (`hiddenpowerground`, `hiddenpowergrass`, ...: son 17 ids de movimiento
    DISTINTOS, cada uno con su propio `.id`), pero Showdown narra la
    ejecucion siempre como `|move|...|Hidden Power|...`, sin el tipo — el
    tipo real es un dato oculto del set del pokemon (IVs), no algo que la
    narracion publica revele. Normalizado, `hiddenpowerground` nunca es
    substring de `hiddenpower`: comparar el id completo contra el protocolo
    no matchea NUNCA, para ninguna de las 17 variantes, aunque la accion se
    haya ejecutado. Verificado que esto es un defecto de esta comparacion y
    no un bug del agente (6 batallas frescas: 5 Hidden Power elegidas, 5
    ejecutadas segun el protocolo).

    Recorta especificamente el prefijo "hiddenpower" en vez de pelar
    cualquier sufijo de tipo en general: ya nos mordio una vez en el seed
    tratar estas 17 variantes como si compartieran un unico id base para
    otra cosa. Especifico a Hidden Power, no un pelado generico de sufijos
    que podria de nuevo matchear de mas contra otro movimiento.
    """
    if accion.startswith("hiddenpower"):
        return "hiddenpower"
    return accion


def _propio_no_actuo(lineas: list[str], side: str) -> bool:
    """Causas 1 y 1bis del residual de C1 (fix-final-report.md): el
    protocolo puede confirmar, sin narrar la accion elegida, que el juego
    impidio que se ejecutara. Evidencia corroborante contra el protocolo
    crudo (D17), no una excepcion a dedo.

    - `|cant|{side}a:`: el propio pokemon quiso actuar y el juego se lo
      impidio (dormido, paralizado, congelado). Es la causa mas frecuente
      del residual medido sobre 6 batallas frescas (316 filas: 296
      alineadas, 20 en el residual).
    - `|faint|{side}a:` que aparece ANTES que cualquier `|move|{side}a:` en
      el mismo bloque: el propio pokemon se debilito por un rival mas rapido
      antes de que le tocara actuar. Showdown no deja ningun otro rastro de
      esto (D20) — ni `|cant|`, ni `|-fail|`, nada — asi que la ausencia de
      un `|move|` propio ANTES del `|faint|` es la unica evidencia posible.
    """
    prefix_cant = f"|cant|{side}a:"
    prefix_move = f"|move|{side}a:"
    prefix_faint = f"|faint|{side}a:"
    if any(l.startswith(prefix_cant) for l in lineas):
        return True
    for l in lineas:
        if l.startswith(prefix_move):
            return False
        if l.startswith(prefix_faint):
            return True
    return False


@pytest.fixture(scope="module")
async def jugadas():
    # I6 (review final): esta fixture juega batallas REALES contra el server
    # local y las persiste en el MISMO Postgres que el dataset de
    # entrenamiento. `source="test"` las marca sinteticas (D19,
    # migracion 20260727000007) para que sean excluibles con
    # `source <> 'test'` en vez de mezclarse en silencio.
    return await play(2, "gen6randombattle", source="test")


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

    Invariante de TODO el dataset (review final, I1): antes filtraba por
    `battle_tag = ANY(:tags)`, es decir solo esta corrida. Corrido sin ese
    filtro habria encontrado I1 el dia que paso (un lote de protocolo perdido
    en silencio en una batalla vieja). `jugadas` sigue siendo un parametro
    para forzar que la fixture juegue algo antes de verificar.
    """
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT ts.turn_number, ts.state, t.player_side, t.battle_id
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                ORDER BY t.battle_id, ts.turn_number
            """))).all()
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
    """Invariante de TODO el dataset (review final): sin filtro de tags."""
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            distintas = (await s.execute(text(
                "SELECT DISTINCT state_schema_version FROM trajectory_steps"
            ))).scalars().all()
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
            winner=fila[4], source="test", played_by="bot",
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


async def test_la_accion_de_la_fila_corresponde_a_su_propio_turno(jugadas):
    """C1: `action_taken` de la fila del turno N tiene que aparecer en el
    protocolo del turno N, no del N+1.

    Antes del arreglo, poke-env llama a `choose_move` en cuanto parsea el
    `|request|`, que Showdown manda ya resuelto y en su propio frame, antes de
    narrar el turno. La accion que se graba en la fila `turno=N` en realidad
    se ejecuta en el bloque de protocolo N+1. Ningun otro test lo detecta
    porque ninguno cruza `action_taken` contra el protocolo del MISMO turno.

    fix-final (residual medido sobre 6 batallas frescas, 316 filas: 296
    alineadas, 20 en el residual): de esas 20, 2 causas resultaron ser falsos
    positivos de ESTE chequeo, no del agente, y se excusan explicitamente
    (`_propio_no_actuo`, `_clave_de_busqueda`) con evidencia corroborante del
    protocolo (D17) — nunca a dedo, sin evidencia. Una tercera causa (fila
    corrida un turno, el mismo defecto de C1 pero residual: ver D20 y la
    entrada final de fix-final-report.md) NO se excusa: si sobrevive, este
    test tiene que seguir fallando por ella.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT ts.turn_number,
                       regexp_replace(
                           lower(coalesce(ts.action_taken->>'id', ts.action_taken->>'species')),
                           '[^a-z0-9]', '', 'g') AS accion,
                       t.player_side, t.battle_id
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags) AND ts.action_taken IS NOT NULL
            """), {"tags": list(jugadas)})).all()
            assert filas, "no hay pasos con accion para verificar"

            revisados = 0
            for turno, accion, side, battle_id in filas:
                lineas = (await s.execute(text("""
                    SELECT unnest(protocol_lines) FROM battle_turns
                    WHERE battle_id = :b AND player_side = :ps AND turn_number = :t
                """), {"b": battle_id, "ps": side, "t": turno})).scalars().all()
                candidatas = [
                    _normalizar(l) for l in lineas
                    if l.startswith(f"|move|{side}a:") or l.startswith(f"|switch|{side}a:")
                ]
                revisados += 1
                clave = _clave_de_busqueda(accion)
                visto = any(clave in linea for linea in candidatas)
                excusado = not visto and _propio_no_actuo(lineas, side)
                assert visto or excusado, (
                    f"la accion '{accion}' de la fila turno={turno} no aparece "
                    f"en el protocolo de ESE turno (bloque {turno}), lado {side}"
                )

            assert revisados > 0, (
                "ninguna fila tenia accion para verificar: el test no verifico nada"
            )
    finally:
        await engine.dispose()


async def test_los_switches_grabados_cierran_contra_el_protocolo(jugadas):
    """C2: la cantidad de decisiones de switch grabadas tiene que cerrar
    contra los `|switch|` propios del protocolo, descontando la entrada
    inicial del team preview (que es un `|switch|` en el protocolo pero no
    una decision de la politica).

    Antes del arreglo, `save_step` upsertea por `(trajectory_id, turn_number)`
    y un cambio forzado tras un debilitamiento no avanza el turno: la segunda
    decision pisa a la primera y el cambio forzado desaparece en silencio.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                battle_id, side = (await s.execute(text("""
                    SELECT t.battle_id, t.player_side FROM trajectories t
                    JOIN battles b ON b.id = t.battle_id WHERE b.battle_tag = :tag
                """), {"tag": tag})).one()

                switches_protocolo = (await s.execute(text("""
                    SELECT count(*) FROM battle_turns bt, LATERAL unnest(bt.protocol_lines) l
                    WHERE bt.battle_id = :b AND bt.player_side = :ps
                      AND l LIKE '|switch|' || :ps || 'a:%'
                """), {"b": battle_id, "ps": side})).scalar_one()

                switches_grabados = (await s.execute(text("""
                    SELECT count(*) FROM trajectory_steps ts
                    JOIN trajectories t ON t.id = ts.trajectory_id
                    WHERE t.battle_id = :b AND t.player_side = :ps
                      AND ts.action_taken ->> 'kind' = 'switch'
                """), {"b": battle_id, "ps": side})).scalar_one()

                assert switches_grabados == switches_protocolo - 1, (
                    f"{tag}: {switches_protocolo} switches en el protocolo, "
                    f"{switches_grabados} grabados como decision (se espera "
                    f"{switches_protocolo - 1}, descontando la entrada inicial "
                    "del team preview)"
                )
    finally:
        await engine.dispose()


async def test_cada_paso_de_estado_tiene_su_protocolo(jugadas):
    """La propiedad que hace REVERSIBLE al serializador.

    Si manana se descubre que el serializador tenia un defecto, el historico se
    re-deriva desde el protocolo crudo en vez de descartarse. Eso solo vale si
    CADA paso de estado tiene su protocolo, del mismo jugador y del mismo turno.
    Contar filas de cada lado por separado no alcanza: hay que verificar la
    correspondencia turno a turno.

    Invariante de TODO el dataset (review final, I1): antes filtraba por
    `battle_tag = ANY(:tags)` y solo habria detectado I1 si el lote perdido
    hubiera sido de esta misma corrida. Corrido sin el filtro es la unica
    manera de que este test hubiera atrapado I1 el dia que paso.
    """
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            huerfanos = (await s.execute(text("""
                SELECT count(*) FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                WHERE NOT EXISTS (
                      SELECT 1 FROM battle_turns bt
                      WHERE bt.battle_id = t.battle_id
                        AND bt.player_side = t.player_side
                        AND bt.turn_number = ts.turn_number)
            """))).scalar_one()
        assert huerfanos == 0, (
            f"{huerfanos} pasos de estado sin su protocolo crudo: el historico "
            "de esas batallas no se podria re-derivar"
        )
    finally:
        await engine.dispose()
