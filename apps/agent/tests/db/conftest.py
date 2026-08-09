"""Fixture compartida de base descartable para los tests de `db/` (MON-11, E).

`TEST_DATABASE_URL` es explicita y separada de `DATABASE_URL`: sin ella, los
tests que la piden se saltean -- nunca caen de vuelta a la base compartida.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from _disposable import disposable_database


def _to_asyncpg(url: str) -> str:
    # Mismo shim que `ludex_agent.config._to_asyncpg`: SQLAlchemy async
    # necesita el driver explicito en el esquema.
    return url.replace("postgres://", "postgresql+asyncpg://", 1).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


@pytest_asyncio.fixture(loop_scope="function")
async def test_database_url():
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip(
            "necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)"
        )
    async with disposable_database(base) as url:
        yield _to_asyncpg(url)
