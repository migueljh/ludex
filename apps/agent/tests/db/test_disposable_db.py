"""MON-11 (E): higiene de tests DB -- nunca contra la base compartida.

`test_repository.py` dejo `battles.battle_tag='battle-test-metadata'`
huerfana en la base compartida el 2026-08-08: su unico cleanup era un
DELETE al ARRANQUE de la corrida siguiente, asi que una corrida cancelada a
mitad de camino no limpiaba nada. Este modulo prueba el reemplazo: una base
Postgres descartable, creada y dropeada por corrida, con un guardia que
falla ruidosamente si algo intenta apuntar a la base compartida en vez de a
una descartable -- nunca por inferencia, siempre por nombre explicito."""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

from sqlalchemy import text

from _disposable import (
    SharedDatabaseGuardError,
    assert_disposable,
    disposable_database,
    verified_engine,
)

requires_test_database = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)",
)

requires_shared_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="necesita DATABASE_URL para probar el rechazo (unica interaccion: "
    "SELECT current_database(), de solo lectura)",
)


def _to_asyncpg(url: str) -> str:
    return url.replace("postgres://", "postgresql+asyncpg://", 1).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


# --- assert_disposable: el guardia es logica pura, sin DB (rapido, siempre corre) ---

def test_guardia_acepta_un_nombre_con_el_prefijo_descartable():
    assert_disposable("postgres://u:p@localhost:15432/ludex_test_abc123")


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u:p@localhost:15432/ludex",
        "postgres://u:p@localhost:15432/postgres",
        "postgres://u:p@localhost:15432/template1",
        "postgres://u:p@localhost:15432/otra_cosa",
        "postgres://u:p@localhost:15432/",
    ],
)
def test_guardia_rechaza_toda_base_que_no_sea_explicitamente_descartable(url):
    """Canario (MON-11): si `TEST_DATABASE_URL` terminara apuntando a la
    base compartida (`ludex`) -- o a cualquier nombre que no sea uno que
    ESTE MODULO generó -- el guardia tiene que fallar ruidoso, nunca
    degradar en silencio a "no se puede verificar, sigo igual"."""
    with pytest.raises(SharedDatabaseGuardError):
        assert_disposable(url)


# --- disposable_database: ciclo de vida real contra el Postgres ya levantado ---

@requires_test_database
async def test_disposable_database_crea_una_base_que_pasa_el_guardia():
    base = os.environ["TEST_DATABASE_URL"]
    async with disposable_database(base) as url:
        assert_disposable(url)
        conn = await asyncpg.connect(url)
        try:
            total_tablas = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        finally:
            await conn.close()
        assert total_tablas > 0, "db/schema.sql no se aplico"


@requires_test_database
async def test_disposable_database_se_dropea_al_salir_en_verde():
    base = os.environ["TEST_DATABASE_URL"]
    async with disposable_database(base) as url:
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    assert not await _existe_la_base(base, name), (
        "la base descartable sigue viva despues de un `async with` exitoso"
    )


@requires_test_database
async def test_disposable_database_se_dropea_incluso_si_el_bloque_revienta():
    base = os.environ["TEST_DATABASE_URL"]
    name = None
    with pytest.raises(RuntimeError, match="boom"):
        async with disposable_database(base) as url:
            name = url.rsplit("/", 1)[-1].split("?", 1)[0]
            raise RuntimeError("boom")
    assert name is not None
    assert not await _existe_la_base(base, name), (
        "una excepcion dentro del `async with` no puede dejar la base huerfana"
    )


@requires_test_database
async def test_disposable_database_se_dropea_incluso_si_la_tarea_se_cancela():
    """El caso real de MON-11: una corrida cancelada COOPERATIVAMENTE (Ctrl-C
    sobre un loop de asyncio vivo, timeout de pytest-asyncio) tiene que
    limpiar igual. `asyncio.CancelledError` se lanza DENTRO del generador en
    el punto del `yield`, con el interprete todavia corriendo; el `finally`
    que envuelve ese `yield` en `disposable_database` es lo unico que lo
    garantiza -- sin el, exactamente lo que paso con
    `battle-test-metadata`.

    Alcance (R2): esto prueba cancelacion cooperativa, NO un `kill -9` del
    proceso. Un `SIGKILL` mata al interprete antes de que corra CUALQUIER
    `finally` de Python -- no hay forma de que codigo de aplicacion se
    entere. Ver el docstring de `_disposable.py` para el detalle."""
    base = os.environ["TEST_DATABASE_URL"]
    name_holder: dict[str, str] = {}
    entro = asyncio.Event()

    async def cuerpo():
        async with disposable_database(base) as url:
            name_holder["name"] = url.rsplit("/", 1)[-1].split("?", 1)[0]
            entro.set()
            await asyncio.sleep(3600)

    tarea = asyncio.create_task(cuerpo())
    await asyncio.wait_for(entro.wait(), timeout=10)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert not await _existe_la_base(base, name_holder["name"]), (
        "la cancelacion de la tarea no puede dejar la base descartable huerfana"
    )


# --- verified_engine: el chequeo corre sobre la conexion REAL, no aislado ---

@requires_test_database
async def test_verified_engine_confirma_current_database_de_la_base_generada():
    """No basta con que `assert_disposable` acepte el STRING de la URL
    (MON-11 R2): esto prueba el engine de SQLAlchemy que un repository real
    usaria -- la misma clase de objeto que `test_repository.py` y
    `test_model_repository.py` le pasan a `session_factory` -- y confirma
    que `SELECT current_database()`, corrido sobre ESA conexion, devuelve un
    nombre descartable."""
    base = os.environ["TEST_DATABASE_URL"]
    async with disposable_database(base) as url:
        engine = await verified_engine(_to_asyncpg(url))
        try:
            async with engine.connect() as conn:
                name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert name.startswith("ludex_test_")
        finally:
            await engine.dispose()


@requires_shared_database
async def test_verified_engine_rechaza_si_la_conexion_real_aterriza_en_la_compartida():
    """El caso que R2 pide cubrir explicitamente: si alguien reconecta
    `test_repository.py` o `test_model_repository.py` a `DATABASE_URL` (la
    base compartida), `verified_engine` tiene que fallar ANTES de devolver
    un engine utilizable -- ninguna fixture que dependa de el llega a
    ejecutar su DELETE/INSERT. La unica interaccion con la base compartida
    en este test es un `SELECT current_database()`: de solo lectura, nunca
    una escritura."""
    from ludex_agent.config import load_settings

    shared_url = load_settings().database_url  # apunta a 'ludex', la compartida
    with pytest.raises(SharedDatabaseGuardError, match="ludex"):
        await verified_engine(shared_url)


def test_verified_engine_es_lo_que_usan_las_fixtures_repo_reales():
    """Canario de composicion: si alguna fixture `repo` (test_repository.py
    o test_model_repository.py) dejara de pasar por `verified_engine` --
    por ejemplo, si alguien la reescribe para volver a `make_engine` directo
    -- este test lo detecta por inspeccion de codigo fuente, sin necesidad
    de una base real. Complementa (no reemplaza) las pruebas de arriba, que
    prueban el comportamiento; esta prueba la composicion real de los dos
    archivos que la R2 nombra explicitamente."""
    import inspect

    import test_model_repository
    import test_repository

    for modulo in (test_repository, test_model_repository):
        fuente = inspect.getsource(modulo.repo)
        assert "verified_engine" in fuente, (
            f"{modulo.__name__}.repo ya no pasa por verified_engine() -- "
            "volveria a poder correr un DELETE sin chequear current_database()"
        )


async def _existe_la_base(base_url: str, name: str) -> bool:
    from _disposable import _with_database

    maintenance = await asyncpg.connect(_with_database(base_url, "postgres"))
    try:
        return await maintenance.fetchval(
            "SELECT count(*) > 0 FROM pg_database WHERE datname = $1", name
        )
    finally:
        await maintenance.close()
