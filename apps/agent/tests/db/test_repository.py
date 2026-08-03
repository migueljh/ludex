import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ludex_agent.config import load_settings
from ludex_agent.db.repository import BattleIdentityConflictError, BattleRepository
from ludex_agent.db.session import make_engine, session_factory
from ludex_agent.showdown.protocol import compute_opening_identity

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)

TAG = "battle-test-repo-1"

# I6 (review final): estas filas viven en las mismas tablas que el dataset de
# entrenamiento. `source="test"` (migracion 20260727000007) las marca como
# sinteticas, y el DELETE de la fixture se acota ADEMAS por `source = 'test'`
# para que ni siquiera una coincidencia de prefijo pueda tocar una fila real.
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
async def repo():
    engine = make_engine(load_settings().database_url)
    factory = session_factory(engine)
    async with factory() as s:
        await s.execute(text(
            "DELETE FROM battles WHERE battle_tag LIKE 'battle-test-%' AND source = 'test'"
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


async def test_identity_key_es_unica_y_no_nula_en_todo_el_corpus():
    """Canario dinamico (DESIGN VERDICT #10): sobre TODA la tabla `battles`,
    no solo sobre las filas de esta corrida (AGENTS.md: `WHERE battle_tag =
    ANY(:tags)` ya escondio un defecto una vez). No fija el total mutable de
    filas -- eso cambia con cada corrida real -- pero exige `rows_checked >
    0` para que un dataset vacio no haga pasar el canario sin haber
    verificado nada."""
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
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
    finally:
        await engine.dispose()
