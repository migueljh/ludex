"""MON-11 R3: la plantilla sanitizada del dex y sus clones por corrida.

No juega batallas (eso es `test_play.py`/`test_graph_play.py`, mas lento y
contra el Showdown local); esto prueba SOLO el mecanismo de la plantilla:
que trae el dex, que las tablas mutables arrancan vacias, que clonar es
independiente, y que el guardia rechaza la base compartida ANTES de que
cualquier cosa pueda jugar o persistir contra ella.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from _dex_template import TEMPLATE_NAME, disposable_dex_clone, ensure_dex_template
from _disposable import SharedDatabaseGuardError, verified_engine

requires_databases = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (base descartable) y DATABASE_URL "
    "(unica fuente admitida, read-only, para el dex de referencia)",
)


@requires_databases
async def test_ensure_dex_template_trae_el_dex_y_deja_las_tablas_mutables_vacias():
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]
    name = await ensure_dex_template(base, shared)
    assert name == TEMPLATE_NAME

    from _disposable import _with_database

    conn = await asyncpg.connect(_with_database(base, name))
    try:
        assert await conn.fetchval("SELECT count(*) FROM generations") > 0
        assert await conn.fetchval("SELECT count(*) FROM pokemon") > 0
        assert await conn.fetchval("SELECT count(*) FROM moves") > 0
        assert await conn.fetchval("SELECT count(*) FROM items") > 0
        assert await conn.fetchval("SELECT count(*) FROM learnsets") > 0
        # Las tablas mutables/de config -- lo que MON-11 R3 exige excluir --
        # existen (el DDL las crea) pero arrancan en CERO filas.
        for table in ("battles", "battle_turns", "trajectories", "trajectory_steps",
                      "providers", "models", "settings"):
            count = await conn.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"{table} deberia arrancar vacia en la plantilla, tiene {count}"
    finally:
        await conn.close()


@requires_databases
async def test_disposable_dex_clone_es_independiente_y_se_dropea():
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]

    async with disposable_dex_clone(base, shared) as url:
        conn = await asyncpg.connect(url)
        try:
            assert await conn.fetchval("SELECT count(*) FROM pokemon") > 0
            await conn.execute(
                "INSERT INTO battles (battle_tag, format, p1, p2, played_by, source, identity_key) "
                "VALUES ('clone-test', 'gen6randombattle', 'A', 'B', 'bot', 'test', 'k1')",
            )
            n = await conn.fetchval("SELECT count(*) FROM battles")
            assert n == 1
        finally:
            await conn.close()
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]

    from _disposable import _with_database

    maintenance = await asyncpg.connect(_with_database(base, "postgres"))
    try:
        exists = await maintenance.fetchval(
            "SELECT count(*) > 0 FROM pg_database WHERE datname = $1", name,
        )
    finally:
        await maintenance.close()
    assert not exists, "el clon debe dropearse al salir del context manager"

    # La plantilla en si NUNCA recibe esa fila: cada clon es independiente.
    template_conn = await asyncpg.connect(_with_database(base, TEMPLATE_NAME))
    try:
        assert await template_conn.fetchval("SELECT count(*) FROM battles") == 0
    finally:
        await template_conn.close()


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="necesita DATABASE_URL para probar el rechazo (solo lectura: "
    "SELECT current_database())",
)
async def test_verified_engine_rechaza_la_compartida_antes_de_jugar_o_persistir():
    """El caso que R3 pide probar explicitamente: si `test_play.py` o
    `test_graph_play.py` apuntaran (por error, o por una reconexion futura)
    a `DATABASE_URL`, el guardia tiene que fallar ANTES de que se juegue
    una sola batalla o corra un solo INSERT/DELETE. `verified_engine` ya
    demostro esto para los tests DB unitarios (R2); esto confirma que el
    mismo guardia, sobre la MISMA base compartida real, sigue rechazando
    cuando el llamador es el codigo de tests LIVE."""
    from ludex_agent.config import load_settings

    shared_url = load_settings().database_url
    with pytest.raises(SharedDatabaseGuardError, match="ludex"):
        await verified_engine(shared_url)
