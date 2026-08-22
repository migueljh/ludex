import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _disposable import verified_engine
from ludex_agent.db.repository import (
    _SAVE_BATTLE_SQL,
    BattleIdentityConflictError,
    BattleRepository,
)
from ludex_agent.db.session import session_factory
from ludex_agent.showdown.protocol import compute_opening_identity

# MON-11 (E/R2): TEST_DATABASE_URL, no DATABASE_URL -- este archivo nunca
# corre contra la base compartida. Cada test recibe una base descartable
# nueva via la fixture `test_database_url` (ver conftest.py / _disposable.py):
# antes, el unico cleanup era un DELETE al ARRANQUE de la fixture siguiente,
# que no protegia contra una corrida cancelada o que revienta a mitad de
# camino (asi quedo huerfana `battle-test-metadata` en la base compartida el
# 2026-08-08). El `repo` fixture pasa por `verified_engine` (no `make_engine`
# directo): confirma `current_database()` sobre la conexion REAL antes de
# que corra cualquier sentencia mutadora, no solo el string de la URL.
pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)",
)

TAG = "battle-test-repo-1"

# Aunque la base ahora es descartable y no compartida, `source="test"` se
# conserva: es el mismo dato que produciria una corrida real y varios tests
# lo verifican explicitamente (ver `SCOPE_TRAJECTORY` en dataset-audit).
SOURCE = "test"


# --- MON-10/F2-03: aperturas sinteticas realistas, con la asimetria REAL
# entre lados (D36) -----------------------------------------------------

def _opening(tag: str, *, own_hp: str = "309/309", rival_hp: str = "100/100",
            rival_species: str = "Lapras, L88, M") -> list[str]:
    """Bloque de apertura de una batalla singles. `own_hp` es el HP EXACTO
    que ve el dueno del lead; `rival_hp` es el PORCENTUAL que ve el rival.
    Ambos representan el 100% al arrancar: es la asimetria real del switch
    inicial que `compute_opening_identity` tiene que anular."""
    return [
        f">{tag}", "|init|battle", "|title|A vs. B", "|j|☆A", "", "|request|",
        "|t:|1785186819", "|gametype|singles",
        "|player|p1|A|101|", "|player|p2|B|102|",
        "|teamsize|p1|6", "|teamsize|p2|6",
        "|gen|6", "|tier|[Gen 6] Random Battle",
        "|rule|HP Percentage Mod: HP is shown in percentages",
        "|start",
        "|switch|p1a: Furret|Furret, L93, F|" + own_hp,
        f"|switch|p2a: Lapras|{rival_species}|" + rival_hp,
    ]


def _identity(tag: str, **kwargs: str) -> str:
    return compute_opening_identity(tag, _opening(tag, **kwargs))


# loop_scope="function" explicito: el default del proyecto
# (asyncio_default_fixture_loop_scope = "module" en pyproject.toml) es para la
# fixture de scope module de la Tarea 8. Sin este override, esta fixture
# (scope function) corre en el loop de modulo mientras el test corre en el de
# funcion, y asyncpg revienta con "attached to a different loop".
@pytest_asyncio.fixture(loop_scope="function")
async def repo(test_database_url):
    engine = await verified_engine(test_database_url)
    factory = session_factory(engine)
    async with factory() as s:
        # La base descartable trae el DDL de db/schema.sql pero no el seed
        # de `generations`: `save_trajectory` resuelve gen_number=6 contra
        # esa tabla, asi que todo test de este archivo (que solo usa gen 6)
        # la necesita presente.
        await s.execute(text(
            "INSERT INTO generations (gen_number, label) VALUES (6, 'XY/ORAS') "
            "ON CONFLICT (gen_number) DO NOTHING"
        ))
        await s.commit()
    yield BattleRepository(factory)
    await engine.dispose()


async def test_guarda_batalla_turno_trayectoria_y_paso(repo):
    bid = await repo.save_battle(
        battle_tag=TAG, identity_key=_identity(TAG), fmt="gen6randombattle",
        p1="A", p2="B", winner="A", source=SOURCE, played_by="bot",
    )
    await repo.save_turn(bid, "p1", 1, ["|turn|1", "|move|p1a: X|Y"])
    tid = await repo.save_trajectory(bid, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1,
        [{"kind": "move", "id": "y"}],
        {"kind": "move", "id": "y"}, "agent",
        action_path="llm_retry",
    )
    await repo.finalize(tid, result="win", reward=1)

    async with repo.factory() as s:
        row = (await s.execute(text(
            "SELECT reward, state_schema_version, decision_index, turn_number, "
            "action_source::text, action_path "
            "FROM trajectory_steps WHERE trajectory_id=:t"
        ), {"t": tid})).one()
        assert float(row[0]) == 1.0
        assert row[1] == 1
        assert row[2] == 0
        assert row[3] == 1
        assert row[4] == "agent"
        assert row[5] == "llm_retry"


async def test_action_path_nullable_y_restringido(repo):
    tag = "battle-test-action-path"
    bid = await repo.save_battle(
        battle_tag=tag, identity_key=_identity(tag), fmt="gen6randombattle",
        p1="A", p2="B", winner=None, source=SOURCE, played_by="bot",
    )
    tid = await repo.save_trajectory(
        bid, gen_number=6, fmt="gen6randombattle", player_side="p1"
    )
    await repo.save_step(tid, 0, 1, {}, 1, [], None, "agent", action_path=None)

    async with repo.factory() as s:
        assert (await s.execute(text(
            "SELECT action_path FROM trajectory_steps "
            "WHERE trajectory_id=:t AND decision_index=0"
        ), {"t": tid})).scalar_one_or_none() is None
        with pytest.raises(DBAPIError, match="trajectory_steps_action_path_check") as exc:
            await s.execute(text(
                "UPDATE trajectory_steps SET action_path='random' "
                "WHERE trajectory_id=:t AND decision_index=0"
            ), {"t": tid})
        assert exc.value.orig.sqlstate == "23514"


async def test_dos_decisiones_del_mismo_turno_no_se_pisan(repo):
    """C2/D21: un cambio forzado tras un debilitamiento no avanza el turno.
    Antes, la clave (trajectory_id, turn_number) hacia que la segunda
    decision pisara a la primera; ahora decision_index las distingue."""
    bid = await repo.save_battle(
        battle_tag=TAG, identity_key=_identity(TAG), fmt="gen6randombattle",
        p1="A", p2="B", winner=None, source=SOURCE, played_by="bot",
    )
    # save_turn tambien, para no dejar un paso huerfano sin protocolo: el
    # invariante de dataset "cada paso tiene su protocolo" (test_play.py)
    # corre sobre TODA la tabla, sin filtrar por battle_tag ni source.
    await repo.save_turn(bid, "p1", 3, ["|turn|3"])
    tid = await repo.save_trajectory(bid, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(tid, 0, 3, {"paso": 0}, 1, [], {"kind": "move", "id": "tackle"}, "agent")
    await repo.save_step(tid, 1, 3, {"paso": 1}, 1, [], {"kind": "switch", "species": "y"}, "agent")

    async with repo.factory() as s:
        filas = (await s.execute(text(
            "SELECT decision_index, turn_number FROM trajectory_steps "
            "WHERE trajectory_id=:t ORDER BY decision_index"
        ), {"t": tid})).all()
        assert [tuple(f) for f in filas] == [(0, 3), (1, 3)]


async def test_reintentar_la_misma_batalla_devuelve_el_mismo_battle_id(repo):
    """Idempotencia (DESIGN VERDICT): reintentar la MISMA batalla -- misma
    identity_key, mismo source -- devuelve el mismo id y no crea una fila
    nueva, aunque el `battle_tag` reportado cambiara (no deberia, pero la
    identidad ya no depende de el)."""
    key = _identity(TAG)
    a = await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                               winner=None, source=SOURCE, played_by="bot")
    b = await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                               winner="A", source=SOURCE, played_by="bot")
    assert a == b, "la misma identity_key no debe crear dos filas"
    async with repo.factory() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM battles WHERE source = 'test' AND identity_key = :k"
        ), {"k": key})).scalar_one()
        assert n == 1


async def test_mismo_tag_y_metadata_con_aperturas_distintas_crea_dos_battles_sin_mezcla(repo):
    """El hallazgo central del checkpoint de diagnostico: dos batallas
    DISTINTAS que comparten battle_tag, p1, p2 y format (el escenario de un
    restart de Showdown) ya NO se fusionan, porque su apertura real produce
    identity_key distintas. Se verifica que no solo `battles` tiene dos
    filas: `battle_turns` y `trajectory_steps` de una tampoco pisan los de
    la otra (antes SI lo hacian, aunque `battles` pareciera correcto)."""
    key_a = _identity(TAG, own_hp="309/309", rival_hp="100/100")
    key_b = _identity(TAG, own_hp="248/248", rival_hp="100/100", rival_species="Dusknoir, L84, M")
    assert key_a != key_b

    bid_a = await repo.save_battle(battle_tag=TAG, identity_key=key_a, fmt="gen6randombattle",
                                   p1="A", p2="B", winner="A", source=SOURCE, played_by="bot")
    tid_a = await repo.save_trajectory(bid_a, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(tid_a, 0, 1, {"battle": "A"}, 1, [], {"kind": "move", "id": "x"}, "agent")

    bid_b = await repo.save_battle(battle_tag=TAG, identity_key=key_b, fmt="gen6randombattle",
                                   p1="A", p2="B", winner="B", source=SOURCE, played_by="bot")
    tid_b = await repo.save_trajectory(bid_b, gen_number=6, fmt="gen6randombattle", player_side="p1")
    await repo.save_step(tid_b, 0, 1, {"battle": "B"}, 1, [], {"kind": "move", "id": "y"}, "agent")

    assert bid_a != bid_b, "aperturas distintas bajo el mismo tag deben crear filas distintas"
    async with repo.factory() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM battles WHERE source = 'test' AND battle_tag = :t"
        ), {"t": TAG})).scalar_one()
        assert n == 2

        estado_a = (await s.execute(text(
            "SELECT state FROM trajectory_steps WHERE trajectory_id = :t"
        ), {"t": tid_a})).scalar_one()
        estado_b = (await s.execute(text(
            "SELECT state FROM trajectory_steps WHERE trajectory_id = :t"
        ), {"t": tid_b})).scalar_one()
        assert estado_a == {"battle": "A"}
        assert estado_b == {"battle": "B"}

        ganadores = dict((await s.execute(text(
            "SELECT id, winner FROM battles WHERE source = 'test' AND battle_tag = :t"
        ), {"t": TAG})).all())
        assert ganadores == {bid_a: "A", bid_b: "B"}


async def test_conflicto_de_metadata_con_misma_identidad_revienta(repo):
    """D36: si la MISMA identity_key ya existe con otro p1/p2/format/played_by,
    es una colision real (o un error de quien llama), no una re-persistencia
    legitima."""
    key = _identity(TAG)
    await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="gen6randombattle",
                           p1="A", p2="B", winner=None, source=SOURCE, played_by="bot")
    with pytest.raises(BattleIdentityConflictError):
        await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="gen6randombattle",
                               p1="OTRO", p2="B", winner=None, source=SOURCE, played_by="bot")


async def test_conflicto_de_winner_conocido_distinto_revienta(repo):
    key = _identity(TAG)
    await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                           winner="A", source=SOURCE, played_by="bot")
    with pytest.raises(BattleIdentityConflictError):
        await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                               winner="B", source=SOURCE, played_by="bot")


async def test_winner_avanza_de_null_a_conocido(repo):
    key = _identity(TAG)
    bid = await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                                 winner=None, source=SOURCE, played_by="bot")
    otra_vez = await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                                      winner="A", source=SOURCE, played_by="bot")
    assert otra_vez == bid
    async with repo.factory() as s:
        winner = (await s.execute(text(
            "SELECT winner FROM battles WHERE id = :b"
        ), {"b": bid})).scalar_one()
        assert winner == "A"


async def test_winner_repetido_no_revienta(repo):
    key = _identity(TAG)
    await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                           winner="A", source=SOURCE, played_by="bot")
    # No debe lanzar: el mismo ganador repetido es legitimo (re-persistencia).
    otra_vez = await repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                                      winner="A", source=SOURCE, played_by="bot")
    async with repo.factory() as s:
        winner = (await s.execute(text(
            "SELECT winner FROM battles WHERE id = :b"
        ), {"b": otra_vez})).scalar_one()
        assert winner == "A"


# --- L-01 (LINEAR_VERDICT CRITICAL): atomicidad bajo concurrencia real -----
#
# Latwan reprodujo la fusion forzando que dos llamadas pasaran un `SELECT` de
# precheck ANTES de que cualquiera insertara. Un `asyncio.gather` ingenuo NO
# alcanza para probarlo ni para probar el arreglo: medido contra Postgres por
# loopback, el round-trip es tan rapido que las dos corrutinas casi nunca
# interlevan de verdad -- ni con 12 escritoras concurrentes contra el codigo
# viejo con el precheck aparecio la fusion (0 fusiones observadas). Depender
# de esa suerte de scheduling es exactamente lo que Latwan pidio NO hacer.
#
# Las dos pruebas siguientes fuerzan la interleaving de verdad, a nivel de
# Postgres: la conexion 1 ejecuta `_SAVE_BATTLE_SQL` (la sentencia REAL de
# produccion, importada del modulo) y NO commitea; se demuestra POSITIVAMENTE
# que la conexion 2 queda bloqueada mientras tanto (no es una inferencia por
# ausencia de fusion, es un `wait_for` que efectivamente expira). Recien
# despues se commitea la conexion 1 y se observa que RETURNING de la
# conexion 2 viene vacio: la unica forma de que eso pase es que el WHERE del
# conflicto haya evaluado la incompatibilidad, ATOMICAMENTE, contra la fila
# que la conexion 1 acaba de confirmar.

async def _dos_conexiones_forzando_conflicto(database_url: str, datos1: dict, datos2: dict):
    """Devuelve (bloqueada_mientras_txn1_abierta, resultado2, filas_finales)."""
    engine = await verified_engine(database_url)
    try:
        async with engine.connect() as conn1, engine.connect() as conn2:
            await conn1.execute(_SAVE_BATTLE_SQL, datos1)
            tarea2 = asyncio.create_task(conn2.execute(_SAVE_BATTLE_SQL, datos2))

            bloqueada = False
            try:
                await asyncio.wait_for(asyncio.shield(tarea2), timeout=0.3)
            except asyncio.TimeoutError:
                bloqueada = True

            await conn1.commit()
            resultado2 = await tarea2
            filas_devueltas2 = resultado2.all()
            await conn2.commit()
        return bloqueada, filas_devueltas2
    finally:
        await engine.dispose()


async def test_dos_conexiones_reales_se_serializan_por_metadata_incompatible(repo, test_database_url):
    key = _identity(TAG)
    datos1 = {"tag": TAG, "key": key, "fmt": "gen6randombattle",
             "p1": "A", "p2": "B", "w": None, "pb": "bot", "src": SOURCE}
    datos2 = {"tag": TAG, "key": key, "fmt": "gen6randombattle",
             "p1": "OTRO", "p2": "B", "w": None, "pb": "bot", "src": SOURCE}

    bloqueada, filas2 = await _dos_conexiones_forzando_conflicto(test_database_url, datos1, datos2)

    assert bloqueada, (
        "la conexion 2 tendria que haber quedado bloqueada por el lock de "
        "fila mientras la conexion 1 seguia con su transaccion abierta"
    )
    assert filas2 == [], (
        "con metadata incompatible, RETURNING de la conexion 2 tiene que "
        "venir vacio: su WHERE de conflicto evaluo falso"
    )
    async with repo.factory() as s:
        filas = (await s.execute(text(
            "SELECT p1 FROM battles WHERE source = 'test' AND identity_key = :k"
        ), {"k": key})).all()
    assert len(filas) == 1, "no puede haber quedado mas de una fila"
    assert filas[0][0] == "A", "conn1 comiteo primero: su metadata es la que queda, sin pisar"


async def test_dos_conexiones_reales_se_serializan_por_winner_incompatible(repo, test_database_url):
    key = _identity(TAG)
    datos1 = {"tag": TAG, "key": key, "fmt": "f",
             "p1": "A", "p2": "B", "w": "A", "pb": "bot", "src": SOURCE}
    datos2 = {"tag": TAG, "key": key, "fmt": "f",
             "p1": "A", "p2": "B", "w": "B", "pb": "bot", "src": SOURCE}

    bloqueada, filas2 = await _dos_conexiones_forzando_conflicto(test_database_url, datos1, datos2)

    assert bloqueada, (
        "la conexion 2 tendria que haber quedado bloqueada mientras la "
        "conexion 1 seguia con su transaccion abierta"
    )
    assert filas2 == [], (
        "con dos winners conocidos distintos, RETURNING de la conexion 2 "
        "tiene que venir vacio"
    )
    async with repo.factory() as s:
        winner = (await s.execute(text(
            "SELECT winner FROM battles WHERE source = 'test' AND identity_key = :k"
        ), {"k": key})).scalar_one()
        n = (await s.execute(text(
            "SELECT count(*) FROM battles WHERE source = 'test' AND identity_key = :k"
        ), {"k": key})).scalar_one()
    assert n == 1
    assert winner == "A", "el winner de conn1 (la que comiteo primero) no se pierde ni se pisa"


async def test_concurrencia_de_un_retry_compatible_devuelve_el_mismo_battle_id(repo):
    """Contrapeso positivo: la concurrencia no rompe el caso legitimo. Dos
    escrituras CONCURRENTES pero compatibles (mismo contenido exacto, el
    reintento real de un runner) tienen que resolver al mismo id, nunca
    lanzar, y dejar una sola fila."""
    key = _identity(TAG)
    resultados = await asyncio.gather(
        repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                         winner="A", source=SOURCE, played_by="bot"),
        repo.save_battle(battle_tag=TAG, identity_key=key, fmt="f", p1="A", p2="B",
                         winner="A", source=SOURCE, played_by="bot"),
    )
    assert resultados[0] == resultados[1], "dos escrituras compatibles concurrentes deben dar el mismo battle_id"
    async with repo.factory() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM battles WHERE source = 'test' AND identity_key = :k"
        ), {"k": key})).scalar_one()
    assert n == 1


async def test_dos_recorders_de_lados_opuestos_crean_una_battle_y_dos_trajectories(repo):
    """DESIGN VERDICT #9: dos sesiones/recorders independientes de la MISMA
    batalla -- uno por lado -- tienen que fusionar en una sola fila de
    `battles` y agregar una `trajectory` por lado, nunca duplicar la
    batalla. Reproduccion determinista: cada lado ve el HP exacto de su
    propio activo y el porcentual del rival (la asimetria real de
    Showdown), y `compute_opening_identity` los hace coincidir."""
    p1_stream = _opening(TAG, own_hp="309/309", rival_hp="100/100")
    p2_stream = _opening(TAG, own_hp="100/100", rival_hp="248/248")
    key_p1 = compute_opening_identity(TAG, p1_stream)
    key_p2 = compute_opening_identity(TAG, p2_stream)
    assert key_p1 == key_p2, "la paridad p1/p2 del fingerprint es lo que hace esto posible"

    bid_desde_p1 = await repo.save_battle(
        battle_tag=TAG, identity_key=key_p1, fmt="gen6randombattle",
        p1="A", p2="B", winner=None, source=SOURCE, played_by="bot",
    )
    tid_p1 = await repo.save_trajectory(bid_desde_p1, gen_number=6, fmt="gen6randombattle", player_side="p1")

    bid_desde_p2 = await repo.save_battle(
        battle_tag=TAG, identity_key=key_p2, fmt="gen6randombattle",
        p1="A", p2="B", winner="A", source=SOURCE, played_by="bot",
    )
    tid_p2 = await repo.save_trajectory(bid_desde_p2, gen_number=6, fmt="gen6randombattle", player_side="p2")

    assert bid_desde_p1 == bid_desde_p2, "los dos lados tienen que resolver a la MISMA batalla"
    assert tid_p1 != tid_p2, "cada lado tiene su propia trajectory"
    async with repo.factory() as s:
        n_battles = (await s.execute(text(
            "SELECT count(*) FROM battles WHERE source = 'test' AND identity_key = :k"
        ), {"k": key_p1})).scalar_one()
        n_trajectories = (await s.execute(text(
            "SELECT count(*) FROM trajectories WHERE battle_id = :b"
        ), {"b": bid_desde_p1})).scalar_one()
        assert n_battles == 1
        assert n_trajectories == 2


async def test_identity_key_es_unica_y_no_nula_en_todo_el_corpus(repo):
    """Canario dinamico (DESIGN VERDICT #10): sobre TODA la tabla `battles`,
    no solo sobre las filas de esta corrida (AGENTS.md: `WHERE battle_tag =
    ANY(:tags)` ya escondio un defecto una vez). No fija el total mutable de
    filas -- eso cambia con cada corrida real -- pero exige `rows_checked >
    0` para que un dataset vacio no haga pasar el canario sin haber
    verificado nada.

    MON-11 (E): "toda la tabla" ahora es la de la base descartable de este
    test, no la compartida -- se inserta una fila propia para que
    `rows_checked > 0` siga siendo una verificacion real y no un fixture
    vacio que pasa por vacuidad."""
    await repo.save_battle(
        battle_tag=TAG, identity_key=_identity(TAG), fmt="gen6randombattle",
        p1="A", p2="B", winner=None, source=SOURCE, played_by="bot",
    )
    async with repo.factory() as s:
        total = (await s.execute(text("SELECT count(*) FROM battles"))).scalar_one()
        nulas = (await s.execute(text(
            "SELECT count(*) FROM battles WHERE identity_key IS NULL"
        ))).scalar_one()
        duplicadas = (await s.execute(text("""
            SELECT count(*) FROM (
                SELECT source, identity_key FROM battles
                GROUP BY source, identity_key HAVING count(*) > 1
            ) d
        """))).scalar_one()
    assert total > 0, "rows_checked debe ser > 0: el canario no verifico nada"
    assert nulas == 0, f"{nulas} filas con identity_key NULL en todo el corpus"
    assert duplicadas == 0, f"{duplicadas} pares (source, identity_key) duplicados"


# --- F2-08 (MON-13): metadata de decision en trajectory_steps ------------

_METADATA_COMPLETA = dict(
    rationale="corto y user-facing, nunca chain-of-thought",
    target={"kind": "active_opponent", "id": "eevee"},
    confidence=0.9,
    alternatives=[{"kind": "move", "id": "x"}],
    provider="open_code_zen",
    model="minimax-m2.7",
    decision_latency_ms=123.4,
    input_tokens=10,
    output_tokens=5,
    cached_input_tokens=2,
    reasoning_tokens=1,
)


async def _trajectory_test(repo) -> int:
    bid = await repo.save_battle(
        battle_tag="battle-test-metadata", identity_key=_identity("battle-test-metadata"),
        fmt="gen6randombattle", p1="A", p2="B", winner=None, source=SOURCE,
        played_by="bot",
    )
    # El seed de `generations` (gen 6) ya lo aplica la fixture `repo`.
    return await repo.save_trajectory(
        bid, gen_number=6, fmt="gen6randombattle", player_side="p1"
    )


async def test_save_step_persiste_metadata_completa(repo):
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1,
        [{"kind": "move", "id": "x"}], {"kind": "move", "id": "x"}, "agent",
        action_path="llm", **_METADATA_COMPLETA,
    )

    async with repo.factory() as s:
        fila = (await s.execute(text("""
            SELECT rationale, target, confidence, alternatives, provider, model,
                   decision_latency_ms, input_tokens, output_tokens,
                   cached_input_tokens, reasoning_tokens
            FROM trajectory_steps WHERE trajectory_id=:t AND decision_index=0
        """), {"t": tid})).one()
    assert fila[0] == _METADATA_COMPLETA["rationale"]
    assert fila[1] == _METADATA_COMPLETA["target"]
    assert float(fila[2]) == _METADATA_COMPLETA["confidence"]
    assert fila[3] == _METADATA_COMPLETA["alternatives"]
    assert fila[4] == _METADATA_COMPLETA["provider"]
    assert fila[5] == _METADATA_COMPLETA["model"]
    assert float(fila[6]) == _METADATA_COMPLETA["decision_latency_ms"]
    assert fila[7:11] == (
        10, 5, 2, 1,
    )


async def test_dos_decisiones_mismo_turno_con_metadata_distinta_no_se_pisan(repo):
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 3, {"paso": 0}, 1, [], {"kind": "move", "id": "tackle"}, "agent",
        action_path="llm", confidence=0.9, alternatives=[], provider="google",
        model="gemini-2.5-flash", rationale="primera",
        input_tokens=1, output_tokens=1, cached_input_tokens=0, reasoning_tokens=0,
    )
    await repo.save_step(
        tid, 1, 3, {"paso": 1}, 1, [], {"kind": "switch", "species": "y"}, "agent",
        action_path="llm_retry", confidence=0.4, alternatives=[], provider="kimi",
        model="kimi-k2.6", rationale="segunda",
        input_tokens=2, output_tokens=2, cached_input_tokens=0, reasoning_tokens=0,
    )

    async with repo.factory() as s:
        filas = (await s.execute(text("""
            SELECT decision_index, turn_number, confidence, provider, model, rationale
            FROM trajectory_steps WHERE trajectory_id=:t ORDER BY decision_index
        """), {"t": tid})).all()
    assert [(f[0], f[1]) for f in filas] == [(0, 3), (1, 3)]
    assert float(filas[0][2]) == 0.9 and float(filas[1][2]) == 0.4
    assert filas[0][3:6] == ("google", "gemini-2.5-flash", "primera")
    assert filas[1][3:6] == ("kimi", "kimi-k2.6", "segunda")


async def test_repersistir_con_metadata_completa_es_idempotente(repo):
    """Idempotencia (F2-08): re-persistir la MISMA decision con la metadata
    completa produce la misma fila y los mismos valores: una sola fila, sin
    duplicados y sin cambiar la metadata."""
    tid = await _trajectory_test(repo)
    for _ in range(2):
        await repo.save_step(
            tid, 0, 1, {"schema_version": 1}, 1, [], {"kind": "move", "id": "x"}, "agent",
            action_path="llm", **_METADATA_COMPLETA,
        )

    async with repo.factory() as s:
        filas = (await s.execute(text(
            "SELECT provider, model, confidence, rationale FROM trajectory_steps "
            "WHERE trajectory_id=:t"
        ), {"t": tid})).all()
        count = (await s.execute(text(
            "SELECT count(*) FROM trajectory_steps WHERE trajectory_id=:t"
        ), {"t": tid})).scalar_one()
    assert count == 1
    assert filas == [(
        "open_code_zen", "minimax-m2.7", 0.9,
        "corto y user-facing, nunca chain-of-thought",
    )]


async def test_repersistir_sin_metadata_nulifica_el_grupo_completo(repo):
    """F2-08 (correccion del tech lead): la metadata se reemplaza COMO GRUPO
    con EXCLUDED puro. Re-persistir la misma decision sin metadata deja las
    11 columnas en NULL -- nunca se conserva un subconjunto anterior (COALESCE
    podia actualizar action_taken pero retener rationale/provider/model de una
    accion anterior). El canario: `action_taken` SI fue actualizado por la
    re-persistencia."""
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1, [], {"kind": "move", "id": "x"}, "agent",
        action_path="llm", **_METADATA_COMPLETA,
    )
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1, [], {"kind": "switch", "species": "y"}, "agent",
        action_path="llm",
    )

    async with repo.factory() as s:
        fila = (await s.execute(text("""
            SELECT action_taken, rationale, target, confidence, alternatives,
                   provider, model, decision_latency_ms, input_tokens,
                   output_tokens, cached_input_tokens, reasoning_tokens
            FROM trajectory_steps WHERE trajectory_id=:t AND decision_index=0
        """), {"t": tid})).one()
    assert fila[0] == {"kind": "switch", "species": "y"}, (
        "canario: la re-persistencia tiene que haber actualizado action_taken"
    )
    assert fila[1:] == (None,) * 11, (
        "la re-persistencia sin metadata tiene que nulificar el grupo completo"
    )


async def test_historia_y_ruta_random_quedan_null(repo):
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 1, {}, 1, [], None, "agent",
    )

    async with repo.factory() as s:
        fila = (await s.execute(text("""
            SELECT rationale, target, confidence, alternatives, provider, model,
                   decision_latency_ms, input_tokens, output_tokens,
                   cached_input_tokens, reasoning_tokens
            FROM trajectory_steps WHERE trajectory_id=:t AND decision_index=0
        """), {"t": tid})).one()
    assert fila == (None,) * 11


async def test_fallback_con_usage_persiste_sin_violar_co_ocurrencia(repo):
    """Prueba focal de persistencia del flujo F2-08: un fallback con llamadas
    LLM facturables tiene provider/model/confidence NULL pero tokens y
    latencia no-NULL -- la co-ocurrencia del CHECK es entre provider/model y
    entre los cuatro tokens, NO entre provider y tokens (design verdict)."""
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 1, {}, 1, [], {"kind": "move", "id": "tackle"}, "agent",
        action_path="fallback",
        rationale="deterministic fallback after two invalid model responses",
        target=None,
        confidence=None,
        alternatives=[],
        provider=None,
        model=None,
        decision_latency_ms=250.0,
        input_tokens=2,
        output_tokens=2,
        cached_input_tokens=0,
        reasoning_tokens=0,
    )

    async with repo.factory() as s:
        fila = (await s.execute(text("""
            SELECT rationale, target, confidence, alternatives, provider, model,
                   decision_latency_ms, input_tokens, output_tokens,
                   cached_input_tokens, reasoning_tokens
            FROM trajectory_steps WHERE trajectory_id=:t AND decision_index=0
        """), {"t": tid})).one()
    assert fila[0] == "deterministic fallback after two invalid model responses"
    assert fila[1] is None          # target
    assert fila[2] is None          # confidence
    assert fila[3] == []            # alternatives
    assert fila[4] is None          # provider
    assert fila[5] is None          # model
    assert float(fila[6]) == 250.0  # decision_latency_ms
    assert fila[7:11] == (2, 2, 0, 0)


async def test_confidence_fuera_de_rango_viola_check(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(DBAPIError, match="trajectory_steps_confidence_check") as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, confidence)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 1.5)
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_provider_sin_model_viola_co_ocurrencia(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_provider_model_coherence_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, provider)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 'google')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_tokens_parciales_violan_co_ocurrencia_de_usage(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_usage_coherence_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source,
                   input_tokens, output_tokens, cached_input_tokens)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 10, 5, 2)
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_cached_mayor_que_input_viola_check(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_cached_input_tokens_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source,
                   input_tokens, output_tokens, cached_input_tokens, reasoning_tokens)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 5, 5, 6, 0)
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_output_tokens_negativo_viola_check(repo):
    """El borrador del design verdict tenia el CHECK de `output_tokens`
    apuntando a `input_tokens`: con input_tokens=5 valido, un output=-1
    pasaba. Este test exige el CHECK corregido."""
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_output_tokens_nonnegative_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source,
                   input_tokens, output_tokens, cached_input_tokens, reasoning_tokens)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 5, -1, 0, 0)
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_latencia_negativa_viola_check(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_latency_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source,
                   decision_latency_ms)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', -1)
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_alternatives_no_array_viola_check(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_alternatives_type_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, alternatives)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', '{"a": 1}')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_target_no_object_viola_check(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_target_type_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, target)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', '["slot"]')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


# --- D65 (MON-31/Fase 3 S2): approval_outcome y grupo NULL de human_override

async def test_save_step_persiste_approval_outcome_human_approved_con_metadata_completa(repo):
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1,
        [{"kind": "move", "id": "x"}], {"kind": "move", "id": "x"}, "agent",
        action_path="llm", approval_outcome="human_approved", **_METADATA_COMPLETA,
    )
    async with repo.factory() as s:
        fila = (await s.execute(text(
            "SELECT approval_outcome, action_source::text, rationale, provider "
            "FROM trajectory_steps WHERE trajectory_id=:t AND decision_index=0"
        ), {"t": tid})).one()
    assert fila[0] == "human_approved"
    assert fila[1] == "agent"
    assert fila[2] == _METADATA_COMPLETA["rationale"]
    assert fila[3] == _METADATA_COMPLETA["provider"]


async def test_save_step_persiste_approval_outcome_timeout_auto_con_metadata_completa(repo):
    tid = await _trajectory_test(repo)
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1,
        [{"kind": "move", "id": "x"}], {"kind": "move", "id": "x"}, "agent",
        action_path="llm", approval_outcome="timeout_auto", **_METADATA_COMPLETA,
    )
    async with repo.factory() as s:
        outcome = (await s.execute(text(
            "SELECT approval_outcome FROM trajectory_steps "
            "WHERE trajectory_id=:t AND decision_index=0"
        ), {"t": tid})).scalar_one()
    assert outcome == "timeout_auto"


async def test_save_step_human_override_sin_metadata_persiste_grupo_null(repo):
    """D65: la fila `human_override` se persiste con `action_source='human'`
    y SIN pasar ningun kwarg de metadata D38 -- el mismo EXCLUDED puro deja
    las 11 columnas en NULL como grupo."""
    tid = await _trajectory_test(repo)
    accion_humana = {"kind": "switch", "species": "charizard"}
    await repo.save_step(
        tid, 0, 1, {"schema_version": 1}, 1, [{"kind": "move", "id": "x"}],
        accion_humana, "human", action_path=None, approval_outcome="human_override",
    )
    async with repo.factory() as s:
        fila = (await s.execute(text("""
            SELECT action_source::text, approval_outcome, action_taken, rationale,
                   target, confidence, alternatives, provider, model,
                   decision_latency_ms, input_tokens, output_tokens,
                   cached_input_tokens, reasoning_tokens
            FROM trajectory_steps WHERE trajectory_id=:t AND decision_index=0
        """), {"t": tid})).one()
    assert fila[0] == "human"
    assert fila[1] == "human_override"
    assert fila[2] == accion_humana
    assert fila[3:] == (None,) * 11, (
        "human_override tiene que dejar las 11 columnas D38 en NULL como grupo"
    )


async def test_approval_outcome_fuera_del_dominio_viola_check(repo):
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_approval_outcome_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, approval_outcome)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 'inventado')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_human_override_con_metadata_parcial_viola_check_global(repo):
    """El CHECK "global D38" (D65): un `human_override` con SIQUIERA UNA de
    las 11 columnas poblada revienta -- nunca queda parcialmente poblado.
    `rationale` solo, con el resto NULL, ya alcanza para violarlo."""
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_human_override_metadata_null_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source,
                   approval_outcome, rationale)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'human', 'human_override',
                        'no deberia poder persistir')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_human_override_con_action_source_agent_viola_check_de_matriz(repo):
    """T-02 (TASOS REVIEW PACKET R2): sin el CHECK de matriz, un
    `human_override` etiquetado `action_source='agent'` (autoria falsa) se
    insertaba sin que la migracion lo rechazara -- el CHECK global D38 solo
    mira la metadata, nunca `action_source`."""
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_approval_outcome_action_source_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, approval_outcome)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'agent', 'human_override')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_action_source_human_con_approval_outcome_null_viola_check_de_matriz(repo):
    """La otra mitad de la iff: `action_source='human'` solo se escribe via
    override (`repository.save_step`); una fila 'human' sin
    `approval_outcome='human_override'` (aca NULL) es igual de invalida."""
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_approval_outcome_action_source_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'human')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_human_approved_con_action_source_human_viola_check_de_matriz(repo):
    """`human_approved`/`timeout_auto` solo emparejan con `action_source='agent'`
    -- el humano no "aprueba" su propia accion, aprueba la del modelo."""
    tid = await _trajectory_test(repo)
    with pytest.raises(
        DBAPIError, match="trajectory_steps_approval_outcome_action_source_check"
    ) as exc:
        async with repo.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_source, approval_outcome)
                VALUES (:t, 0, 1, '{}', 1, '[]', 'human', 'human_approved')
            """), {"t": tid})
    assert exc.value.orig.sqlstate == "23514"


async def test_action_source_agent_con_approval_outcome_null_sigue_siendo_valido(repo):
    """NULL sigue siendo historico y valido (T-02): la inmensa mayoria de las
    filas 'agent' nunca pasan por el gate HITL y no deben empezar a exigir
    approval_outcome poblado -- el CHECK de matriz no puede romper esto."""
    tid = await _trajectory_test(repo)
    async with repo.factory() as s:
        await s.execute(text("""
            INSERT INTO trajectory_steps
              (trajectory_id, decision_index, turn_number, state,
               state_schema_version, legal_actions, action_source)
            VALUES (:t, 0, 1, '{}', 1, '[]', 'agent')
        """), {"t": tid})
        await s.commit()
        outcome = (await s.execute(text(
            "SELECT approval_outcome FROM trajectory_steps "
            "WHERE trajectory_id=:t AND decision_index=0"
        ), {"t": tid})).scalar_one()
    assert outcome is None


async def test_ningun_human_override_tiene_metadata_d38_parcial_en_todo_el_corpus(repo):
    """Canario global (AGENTS.md): corre sobre TODA `trajectory_steps` de la
    base descartable, no solo sobre las filas de este test -- exactamente el
    patron de `test_identity_key_es_unica_y_no_nula_en_todo_el_corpus`.
    `filas_override > 0` prueba que el escaneo ejercio algo real, no que
    paso por vacuidad."""
    tid = await _trajectory_test(repo)
    # Una fila human_override legitima (grupo NULL) y una agent con
    # metadata completa: si el escaneo global filtrara de mas, cualquiera
    # de las dos podria esconder una violacion real.
    await repo.save_step(
        tid, 0, 1, {}, 1, [], {"kind": "switch", "species": "charizard"}, "human",
        approval_outcome="human_override",
    )
    await repo.save_step(
        tid, 1, 1, {}, 1, [], {"kind": "move", "id": "x"}, "agent",
        approval_outcome="human_approved", **_METADATA_COMPLETA,
    )

    async with repo.factory() as s:
        filas_override = (await s.execute(text(
            "SELECT count(*) FROM trajectory_steps WHERE approval_outcome = 'human_override'"
        ))).scalar_one()
        violaciones = (await s.execute(text("""
            SELECT count(*) FROM trajectory_steps
            WHERE approval_outcome = 'human_override'
              AND (rationale IS NOT NULL OR target IS NOT NULL OR confidence IS NOT NULL
                   OR alternatives IS NOT NULL OR provider IS NOT NULL OR model IS NOT NULL
                   OR decision_latency_ms IS NOT NULL OR input_tokens IS NOT NULL
                   OR output_tokens IS NOT NULL OR cached_input_tokens IS NOT NULL
                   OR reasoning_tokens IS NOT NULL)
        """))).scalar_one()
    assert filas_override > 0, "rows_checked debe ser > 0: el canario no verifico nada"
    assert violaciones == 0
