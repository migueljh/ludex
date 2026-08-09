"""Base Postgres descartable para tests DB reales (MON-11, restriccion E / R2).

`test_repository.py` corria contra `load_settings().database_url` -- la
MISMA base compartida que sirve al dataset de entrenamiento -- y su unico
cleanup era un `DELETE ... WHERE battle_tag LIKE 'battle-test-%'` al
ARRANQUE de la fixture siguiente. Una corrida CANCELADA COOPERATIVAMENTE
(`asyncio.CancelledError` recibida en el punto del `await`, p.ej. Ctrl-C
sobre un loop de asyncio corriendo, o un timeout de pytest) o que revienta
por una excepcion antes de terminar no disparaba ese DELETE: dejo huerfana
`battles.battle_tag='battle-test-metadata'` en la base compartida el
2026-08-08. Este modulo reemplaza ese patron: cada corrida obtiene una base
Postgres nueva, con el DDL de `db/schema.sql` aplicado, y la dropea en un
`finally` alrededor del `yield`.

Alcance exacto de esa garantia (R2): el `finally` de Python corre ante
salida normal, excepcion, y cancelacion COOPERATIVA -- el flujo normal de
`asyncio`, donde `CancelledError` se lanza DENTRO del generador y el
interprete sigue vivo para ejecutar el `finally`. NO cubre un `SIGKILL` ni
una caida del proceso (segfault, OOM-kill, `kill -9`): ahi el interprete
nunca vuelve a correr, ningun `finally` de Python se ejecuta, y la base
descartable queda huerfana igual que antes. Este modulo no tiene mitigacion
para ese caso -- lo unico que existe hoy es el prefijo `ludex_test_`, que
hace que esas bases sean identificables y baratas de barrer manualmente
(`DROP DATABASE` por nombre, nunca contra `ludex`).

`TEST_DATABASE_URL` es una variable separada de `DATABASE_URL` a proposito:
nunca hay fallback silencioso de una a la otra. Apunta al MISMO servidor
Postgres ya levantado (el docker-compose de Ludex sigue arriba; este modulo
nunca lo toca) pero el nombre de base de esa URL es indiferente -- se
reemplaza siempre por uno generado con el prefijo `ludex_test_`.

R2 agrega `verified_engine`: `assert_disposable` sola solo parsea el STRING
de la URL -- no prueba que la conexion REAL que usa el repository aterrice
ahi (un typo en como se arma la URL, una variable de entorno que gana por
precedencia, etc. pueden hacer que el string diga una cosa y la conexion
real apunte a otra). `verified_engine` abre la conexion de verdad, corre
`SELECT current_database()` como PRIMERA sentencia, y revienta ANTES de
devolver el engine si el nombre no empieza con `ludex_test_` -- ninguna
sentencia mutadora (el `DELETE`/`INSERT` de una fixture) puede ejecutarse
sobre ese engine sin pasar primero por este chequeo, porque es literalmente
lo primero que se le pide a la conexion.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ludex_agent.db.session import make_engine

_SCHEMA_PATH = Path(__file__).resolve().parents[4] / "db" / "schema.sql"
_DISPOSABLE_PREFIX = "ludex_test_"
_FORBIDDEN_NAMES = frozenset({"", "ludex", "postgres", "template0", "template1"})


class SharedDatabaseGuardError(RuntimeError):
    """Algo intento apuntar a una base que no es explicitamente descartable.

    Nunca se infiere por ausencia de evidencia de que sea la compartida: el
    unico criterio que pasa es un nombre que EMPIECE con
    `ludex_test_` -- lo unico que `disposable_database` genera."""


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _with_database(url: str, name: str) -> str:
    # asyncpg no entiende `sslmode` como parametro de query (lo rechaza, ver
    # `config._to_asyncpg`): se descarta igual que alli.
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))


def _check_disposable_name(name: str) -> None:
    if name in _FORBIDDEN_NAMES or not name.startswith(_DISPOSABLE_PREFIX):
        raise SharedDatabaseGuardError(
            f"'{name or '(vacio)'}' no es una base descartable: se esperaba un "
            f"nombre que empiece con '{_DISPOSABLE_PREFIX}'. Los tests DB "
            "(MON-11) nunca corren contra la base compartida."
        )


def assert_disposable(url: str) -> None:
    """Falla ruidoso si el NOMBRE en `url` no es inequivocamente descartable.

    Chequeo de string, sin conectar -- ver `verified_engine` para el
    chequeo real contra `current_database()`."""
    _check_disposable_name(_database_name(url))


async def verified_engine(url: str) -> AsyncEngine:
    """Crea el engine de `url` y verifica `current_database()` ANTES de
    devolverlo. Falla cerrado (`SharedDatabaseGuardError`) y dispone el
    engine si el nombre no empieza con `ludex_test_` -- ninguna sentencia
    mutadora que dependa del engine devuelto puede correr sin pasar por acá
    primero, porque este chequeo es la primera consulta que se ejecuta."""
    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        _check_disposable_name(name)
    except BaseException:
        await engine.dispose()
        raise
    return engine


@asynccontextmanager
async def disposable_database(base_url: str) -> AsyncIterator[str]:
    """Crea una base Postgres nueva sobre el servidor de `base_url`, le
    aplica `db/schema.sql`, entrega su URL y dropea esa base en un
    `finally` alrededor del `yield` -- ver el docstring del modulo para el
    alcance exacto de esa garantia (no cubre SIGKILL/caida del proceso).

    `base_url` es la conexion de mantenimiento (host, puerto, credenciales);
    el nombre de base que trae se ignora y se reemplaza por uno generado.
    """
    name = f"{_DISPOSABLE_PREFIX}{uuid.uuid4().hex[:16]}"
    disposable_url = _with_database(base_url, name)
    assert_disposable(disposable_url)
    maintenance_url = _with_database(base_url, "postgres")

    maintenance = await asyncpg.connect(maintenance_url)
    try:
        await maintenance.execute(f'CREATE DATABASE "{name}"')
    finally:
        await maintenance.close()

    try:
        conn = await asyncpg.connect(disposable_url)
        try:
            await conn.execute(_SCHEMA_PATH.read_text())
        finally:
            await conn.close()
        yield disposable_url
    finally:
        maintenance = await asyncpg.connect(maintenance_url)
        try:
            await maintenance.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await maintenance.close()
