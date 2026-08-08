"""Base Postgres descartable para tests DB reales (MON-11, restriccion E).

`test_repository.py` corria contra `load_settings().database_url` -- la
MISMA base compartida que sirve al dataset de entrenamiento -- y su unico
cleanup era un `DELETE ... WHERE battle_tag LIKE 'battle-test-%'` al
ARRANQUE de la fixture siguiente. Una corrida cancelada o que revienta antes
de terminar no dispara ese DELETE: dejo huerfana
`battles.battle_tag='battle-test-metadata'` en la base compartida el
2026-08-08. Este modulo reemplaza ese patron: cada corrida obtiene una base
Postgres nueva, con el DDL de `db/schema.sql` aplicado, y la dropea siempre
al salir -- exito, excepcion o cancelacion -- porque el cleanup vive en un
`finally` alrededor del `yield`, no en el arranque de la proxima corrida.

`TEST_DATABASE_URL` es una variable separada de `DATABASE_URL` a proposito:
nunca hay fallback silencioso de una a la otra. Apunta al MISMO servidor
Postgres ya levantado (el docker-compose de Ludex sigue arriba; este modulo
nunca lo toca) pero el nombre de base de esa URL es indiferente -- se
reemplaza siempre por uno generado con el prefijo `ludex_test_`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg

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


def assert_disposable(url: str) -> None:
    """Falla ruidoso si `url` no es inequivocamente una base descartable."""
    name = _database_name(url)
    if name in _FORBIDDEN_NAMES or not name.startswith(_DISPOSABLE_PREFIX):
        raise SharedDatabaseGuardError(
            f"'{name or '(vacio)'}' no es una base descartable: se esperaba un "
            f"nombre que empiece con '{_DISPOSABLE_PREFIX}'. Los tests DB "
            "(MON-11) nunca corren contra la base compartida."
        )


@asynccontextmanager
async def disposable_database(base_url: str) -> AsyncIterator[str]:
    """Crea una base Postgres nueva sobre el servidor de `base_url`, le
    aplica `db/schema.sql`, entrega su URL y SIEMPRE la dropea al salir.

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
