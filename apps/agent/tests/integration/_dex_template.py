"""Plantilla Postgres sanitizada para tests LIVE (`test_play.py`,
`test_graph_play.py`) -- MON-11 R3.

Estos tests juegan batallas REALES contra el Showdown local y persisten via
`PostgresContextRepository`/`BattleRepository`, que necesitan el dex
completo (`generations`, `pokemon`, `moves`, `items`, `learnsets`) para
resolver tipos/abilities/movesets durante la decision. La base compartida
tiene ese dex, pero copiarla ENTERA (o usarla como `TEMPLATE` de Postgres)
traeria tambien `battles`/`trajectories`/`trajectory_steps` reales y
`providers`/`models`/`settings` -- exactamente lo que MON-11 viene
excluyendo. Este modulo arma una plantilla PROPIA, con el DDL de
`db/schema.sql` (tablas mutables vacias por construccion) y SOLO las
tablas de referencia del dex, cargadas mediante una consulta a la base
compartida forzada a `default_transaction_read_only=on`.

Nunca `CREATE DATABASE ... TEMPLATE ludex`. Nunca `pg_dump`/copia completa
de la compartida (el cliente `pg_dump` instalado, 14.20, ademas rechaza
conectar a un servidor 16.x -- se usa `asyncpg` puro, sin ese problema de
version). Cada corrida de test clona la plantilla sanitizada con
`CREATE DATABASE ... TEMPLATE`, que en Postgres es una copia de archivos a
nivel de servidor, no una re-insercion fila por fila -- rapido incluso con
los ~130 000 renglones de `learnsets`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

import asyncpg

from _disposable import SharedDatabaseGuardError, _SCHEMA_PATH, _with_database, assert_disposable

TEMPLATE_NAME = "ludex_dex_template"

_DEX_TABLES: dict[str, list[str]] = {
    "generations": ["id", "gen_number", "label"],
    "pokemon": [
        "id", "gen_id", "showdown_id", "dex_num", "name", "base_species",
        "forme", "is_default", "types", "base_stats", "abilities",
        "weight_kg", "evolves_from", "tier", "base_species_name",
    ],
    "moves": [
        "id", "gen_id", "showdown_id", "name", "type", "category", "power",
        "accuracy", "pp", "priority", "target", "flags", "description",
        "power_kind",
    ],
    "items": ["id", "gen_id", "showdown_id", "name", "description", "properties"],
    "learnsets": ["pokemon_id", "move_id", "learn_methods"],
}
"""Unicas tablas que este modulo copia desde la compartida. Nunca
`battles`/`battle_turns`/`trajectories`/`trajectory_steps`/`providers`/
`models`/`settings` -- esas quedan vacias en la plantilla por construccion:
`db/schema.sql` las crea, y este modulo jamas les inserta una fila."""


async def _load_dex_table(source: asyncpg.Connection, dest: asyncpg.Connection, table: str) -> int:
    columns = _DEX_TABLES[table]
    rows = await source.fetch(f"SELECT {', '.join(columns)} FROM {table}")
    if not rows:
        return 0
    await dest.copy_records_to_table(
        table, records=[tuple(row) for row in rows], columns=columns,
    )
    return len(rows)


async def ensure_dex_template(base_url: str, shared_url: str) -> str:
    """Garantiza que `ludex_dex_template` existe, con el DDL completo y SOLO
    las tablas de `_DEX_TABLES` cargadas desde `shared_url` (forzada
    read-only). Idempotente: si ya existe, no la reconstruye -- la
    plantilla es de solo lectura para los clones, nunca cambia entre
    corridas de la misma sesion de Postgres."""
    template_url = _with_database(base_url, TEMPLATE_NAME)
    assert_disposable_template(template_url)
    maintenance_url = _with_database(base_url, "postgres")

    maintenance = await asyncpg.connect(maintenance_url)
    try:
        exists = await maintenance.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEMPLATE_NAME,
        )
        if exists:
            return TEMPLATE_NAME
        await maintenance.execute(f'CREATE DATABASE "{TEMPLATE_NAME}"')
    finally:
        await maintenance.close()

    schema_conn = await asyncpg.connect(template_url)
    try:
        await schema_conn.execute(_SCHEMA_PATH.read_text())
    finally:
        await schema_conn.close()

    # Conexion FORZADA read-only contra la compartida: es la unica fuente
    # admitida para el dex, y esta funcion NUNCA le ejecuta nada mas que
    # los SELECT de `_load_dex_table`.
    source = await asyncpg.connect(
        shared_url, server_settings={"default_transaction_read_only": "on"},
    )
    dest = await asyncpg.connect(template_url)
    try:
        for table in _DEX_TABLES:
            await _load_dex_table(source, dest, table)
    finally:
        await source.close()
        await dest.close()

    return TEMPLATE_NAME


def assert_disposable_template(url: str) -> None:
    """La plantilla NO usa el prefijo `ludex_test_` (no es descartable por
    corrida, es persistente entre corridas) pero tampoco puede ser `ludex`
    ni ningun nombre reservado -- reusa la misma lista negra que
    `assert_disposable`, sin exigir el prefijo de un clon efimero."""
    from _disposable import _FORBIDDEN_NAMES, _database_name

    name = _database_name(url)
    if name in _FORBIDDEN_NAMES:
        raise SharedDatabaseGuardError(
            f"'{name}' no puede ser la plantilla del dex -- nombre reservado.",
        )


@asynccontextmanager
async def disposable_dex_clone(base_url: str, shared_url: str) -> AsyncIterator[str]:
    """Clona `ludex_dex_template` (construyéndola primero si hace falta) en
    una base nueva `ludex_test_<uuid>`, la entrega, y la dropea en un
    `finally` alrededor del `yield` -- mismo alcance que `disposable_database`
    en `_disposable.py`: cubre salida normal, excepción, y cancelación
    cooperativa; nunca un `SIGKILL`."""
    await ensure_dex_template(base_url, shared_url)

    name = f"ludex_test_{uuid4().hex[:16]}"
    clone_url = _with_database(base_url, name)
    assert_disposable(clone_url)
    maintenance_url = _with_database(base_url, "postgres")

    maintenance = await asyncpg.connect(maintenance_url)
    try:
        await maintenance.execute(
            f'CREATE DATABASE "{name}" WITH TEMPLATE "{TEMPLATE_NAME}"',
        )
    finally:
        await maintenance.close()

    try:
        yield clone_url
    finally:
        maintenance = await asyncpg.connect(maintenance_url)
        try:
            await maintenance.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await maintenance.close()
