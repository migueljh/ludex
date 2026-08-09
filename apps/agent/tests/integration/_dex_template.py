"""Plantilla Postgres sanitizada para tests LIVE (`test_play.py`,
`test_graph_play.py`) -- MON-11 R3, rediseñada R4 (BLOCKER 2).

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

`abilities` y `type_chart` (tablas separadas en `db/schema.sql`, distintas
de la columna JSONB `pokemon.abilities` que SI se copia) NO se copian: no
hay ningun caller real en `apps/agent/src` que las consulte (verificado con
`grep -rl "FROM abilities|FROM type_chart" apps/agent/src/` -- vacio). Lo
mismo para `seed_runs`/`usage_stats` (metadata de `packages/seed`, ajena a
este codigo). Si algun caller real llega a necesitarlas, agregarlas a
`_DEX_TABLES` invalida el fingerprint automaticamente (ver abajo) y la
proxima corrida reconstruye.

Nunca `CREATE DATABASE ... TEMPLATE ludex`. Nunca `pg_dump`/copia completa
de la compartida (el cliente `pg_dump` instalado, 14.20, ademas rechaza
conectar a un servidor 16.x -- se usa `asyncpg` puro, sin ese problema de
version). Cada corrida de test clona la plantilla sanitizada con
`CREATE DATABASE ... TEMPLATE`, que en Postgres es una copia de archivos a
nivel de servidor, no una re-insercion fila por fila -- rapido incluso con
los ~130 000 renglones de `learnsets`.

## R4 (BLOCKER 2) -- por que "si existe, return" no alcanzaba

Latwan reprodujo `PARTIAL_TEMPLATE_ACCEPTED returned=True
generations_table=None`: `ensure_dex_template` (R3) sólo miraba si una base
llamada `ludex_dex_template` existía en `pg_database` -- ni el DDL, ni el
contenido, ni siquiera que las tablas existieran. Una base vacía (creada a
mano, o por una corrida anterior interrumpida ANTES de aplicar el schema)
se aceptaba igual, y cada clon posterior heredaba esa base rota en
silencio.

El rediseño reemplaza "existe → aceptar" por tres cambios:

1. **El nombre de la plantilla se deriva de un fingerprint** del contenido
   de `db/schema.sql` + un fingerprint del dex fuente, calculado con SQL en
   el servidor (nunca trae las filas al cliente sólo para hashear). Si el
   schema o el dex cambian, el fingerprint cambia, el nombre cambia, y el
   nombre viejo simplemente deja de buscarse -- nunca se reutiliza en
   silencio.
2. **Publicación atómica vía staging.** Se construye TODO (DDL + copia del
   dex) bajo un nombre de staging único (`ludex_dex_staging_<uuid>`, nunca
   reutilizado entre corridas) y sólo se "publica" -- `ALTER DATABASE ...
   RENAME TO <nombre-fingerprint>` -- si `_validate_template` aprueba. El
   nombre fingerprintado, si existe, SIEMPRE es una plantilla completa y
   validada: no hay ningún camino que lo cree sin pasar por la validación.
3. **Incluso si el nombre fingerprintado ya existe, se re-valida antes de
   aceptarlo** (no sólo se confía en que el nombre esté "ocupado" -- eso es
   exactamente lo que reprodujo Latwan). Si falla la validación, la función
   revienta ruidoso en vez de reconstruir automáticamente encima de algo
   que otra corrida concurrente podría estar usando ahora mismo.

## R5 (LINEAR_VERDICT L-01..L-03, L-05) -- por que conteo+PK tampoco alcanzaba

Latwan reprodujo esto TAMBIÉN: mutó `moves.pp` de 15 a 16 en un clon
descartable (mismo `id`, mismo conteo por tabla y por generación) y
`ensure_dex_template` seguía devolviendo `FINGERPRINT_UNCHANGED=True` /
`_validate_template(...)=(True, "ok")` contra la plantilla con el
contenido VIEJO. La huella R4 sólo agregaba PKs (`string_agg(id::text,
...)`) y la validación sólo comparaba conteos por generación + integridad
referencial -- ninguno de los dos mira el CONTENIDO de las columnas no
clave.

Tres cambios más:

4. **La huella cubre TODAS las columnas copiadas, no sólo la PK.** Cada
   fila se hashea completa vía `md5(ROW(col1, col2, ...)::text)` (Postgres
   normaliza la representación de texto de una fila, arrays y `jsonb`
   incluidos) y esos hashes se agregan ordenados por PK. Cambiar
   `moves.pp`, un campo `jsonb` (`pokemon.base_stats`/`abilities`) o
   `learnsets.learn_methods` cambia el hash de esa fila, cambia la huella
   de la tabla, cambia el fingerprint completo, cambia el nombre.
5. **La lista de columnas es una ÚNICA fuente de verdad.** `_DEX_TABLES`
   ya gobernaba qué columnas copia `_load_dex_table`; ahora TAMBIÉN
   gobierna qué columnas entran al `ROW(...)` de la huella
   (`_row_hash_expr`, construida a partir de `_DEX_TABLES[table]`). No hay
   una segunda lista de columnas que copiar y validar puedan divergir en
   silencio.
6. **Huella, copia y validación leen una única transacción `REPEATABLE
   READ` de sólo lectura** sobre la fuente (`source.transaction(...)`,
   abierta una vez en `ensure_dex_template` y usada para TODO lo que lee
   `source`). Antes eran sentencias `READ COMMITTED` independientes: nada
   garantizaba que la huella, la copia de filas, y la comparación de
   `_validate_template` vieran el mismo estado de la fuente si algo
   cambiara entre medio.

`_validate_template` ahora compara `_table_signature` (conteo + huella de
CONTENIDO completo) entre fuente y destino, no sólo conteos por generación
-- una plantilla con los mismos IDs y conteos pero datos distintos se
rechaza.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

import asyncpg

from _disposable import (
    SharedDatabaseGuardError,
    _FORBIDDEN_NAMES,
    _SCHEMA_PATH,
    _database_name,
    _with_database,
    assert_disposable,
)

_TEMPLATE_PREFIX = "ludex_dex_template_"
_STAGING_PREFIX = "ludex_dex_staging_"

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
`db/schema.sql` las crea, y este modulo jamas les inserta una fila. Tampoco
`abilities`/`type_chart`/`seed_runs`/`usage_stats` -- ver el docstring del
modulo, sin caller real que las necesite hoy."""

_MUTABLE_TABLES = (
    "battles", "battle_turns", "trajectories", "trajectory_steps",
    "providers", "models", "settings",
)
"""Deben quedar en CERO filas en toda plantilla válida -- verificado por
`_validate_template`, no sólo asumido."""

_PK_EXPR = {
    "generations": "id::text",
    "pokemon": "id::text",
    "moves": "id::text",
    "items": "id::text",
    "learnsets": "(pokemon_id::text || ':' || move_id::text)",
}


def _row_hash_expr(table: str) -> str:
    """`ROW(...)::text` sobre EXACTAMENTE las columnas de `_DEX_TABLES`
    (R5, L-05: única fuente de verdad -- las mismas columnas que
    `_load_dex_table` copia). Postgres normaliza la representación de texto
    de una fila completa, `text[]` y `jsonb` incluidos: dos filas con el
    mismo contenido producen el mismo texto siempre; cualquier columna
    distinta (`moves.pp`, un `jsonb` como `pokemon.base_stats`,
    `learnsets.learn_methods`) cambia el hash."""
    columns = _DEX_TABLES[table]
    return f"md5(ROW({', '.join(columns)})::text)"


async def _table_signature(conn: asyncpg.Connection, table: str) -> str:
    """Conteo + hash de CONTENIDO completo (R5, L-01), calculado en el
    SERVIDOR -- nunca trae las filas al cliente sólo para fingerprint. Ya
    no es conteo+PK (R4): eso dejaba pasar una fila con el mismo `id` pero
    columnas no-clave distintas (reproducido por Latwan mutando
    `moves.pp`)."""
    pk_expr = _PK_EXPR[table]
    row_hash = _row_hash_expr(table)
    row = await conn.fetchrow(
        f"SELECT count(*) AS n, "
        f"md5(coalesce(string_agg({row_hash}, ',' ORDER BY {pk_expr}), '')) AS h "
        f"FROM {table}",
    )
    return f"{table}:{row['n']}:{row['h']}"


async def _dex_fingerprint(source: asyncpg.Connection) -> str:
    parts = [await _table_signature(source, table) for table in _DEX_TABLES]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _schema_fingerprint() -> str:
    return hashlib.sha256(_SCHEMA_PATH.read_bytes()).hexdigest()


async def _template_name(source: asyncpg.Connection) -> str:
    schema_fp = _schema_fingerprint()
    dex_fp = await _dex_fingerprint(source)
    combined = hashlib.sha256(f"{schema_fp}:{dex_fp}".encode()).hexdigest()
    # Nombre de base Postgres: identificador acotado, el hash completo sobra.
    return f"{_TEMPLATE_PREFIX}{combined[:24]}"


def _assert_not_reserved(url: str) -> None:
    name = _database_name(url)
    if name in _FORBIDDEN_NAMES:
        raise SharedDatabaseGuardError(f"'{name}' no puede usarse -- nombre reservado.")


async def _load_dex_table(source: asyncpg.Connection, dest: asyncpg.Connection, table: str) -> int:
    columns = _DEX_TABLES[table]
    rows = await source.fetch(f"SELECT {', '.join(columns)} FROM {table}")
    if not rows:
        return 0
    await dest.copy_records_to_table(
        table, records=[tuple(row) for row in rows], columns=columns,
    )
    return len(rows)


async def _generation_counts(conn: asyncpg.Connection, table: str) -> dict[int, int]:
    if table == "generations":
        rows = await conn.fetch("SELECT gen_number, count(*) AS n FROM generations GROUP BY gen_number")
    elif table == "learnsets":
        rows = await conn.fetch(
            "SELECT g.gen_number, count(*) AS n FROM learnsets l "
            "JOIN pokemon p ON p.id = l.pokemon_id "
            "JOIN generations g ON g.id = p.gen_id "
            "GROUP BY g.gen_number",
        )
    else:
        rows = await conn.fetch(
            f"SELECT g.gen_number, count(*) AS n FROM {table} x "
            f"JOIN generations g ON g.id = x.gen_id GROUP BY g.gen_number",
        )
    return {row["gen_number"]: row["n"] for row in rows}


async def _validate_template(template_url: str, source: asyncpg.Connection) -> tuple[bool, str]:
    """Devuelve `(valido, motivo)`. Nunca se confía en que el nombre exista
    -- esto es lo que reemplaza el `if exists: return` que Latwan
    reprodujo. Corre SIEMPRE, tanto sobre una plantilla recién construida
    en staging como sobre una ya publicada que se está por reutilizar."""
    try:
        conn = await asyncpg.connect(template_url)
    except Exception as exc:
        return False, f"no se pudo conectar: {exc}"
    try:
        expected_tables = set(_DEX_TABLES) | set(_MUTABLE_TABLES)
        existing = {
            row["table_name"]
            for row in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
                list(expected_tables),
            )
        }
        missing = expected_tables - existing
        if missing:
            return False, f"faltan tablas: {sorted(missing)}"

        for table in _MUTABLE_TABLES:
            n = await conn.fetchval(f"SELECT count(*) FROM {table}")
            if n != 0:
                return False, f"{table} tiene {n} filas, debería estar vacía"

        for table in _DEX_TABLES:
            # Capa 1, barata: conteos por generación -- falla rápido con un
            # diagnóstico claro ante un desfase grueso (filas de más/menos).
            source_counts = await _generation_counts(source, table)
            dest_counts = await _generation_counts(conn, table)
            if source_counts != dest_counts:
                return False, (
                    f"{table}: conteos por generación no coinciden "
                    f"(fuente={source_counts}, destino={dest_counts})"
                )

        for table in _DEX_TABLES:
            # Capa 2 (R5, L-01): firma de CONTENIDO completo -- misma
            # función que la huella del nombre (`_table_signature`), así
            # que "el destino valida" significa literalmente "el destino
            # tiene el mismo contenido, columna por columna, que la
            # fuente" -- no sólo los mismos conteos. Esto es lo que
            # atrapa el caso que Latwan reprodujo: mismo `id`, mismo
            # conteo, `moves.pp` distinto.
            source_sig = await _table_signature(source, table)
            dest_sig = await _table_signature(conn, table)
            if source_sig != dest_sig:
                return False, (
                    f"{table}: firma de contenido no coincide "
                    f"(fuente={source_sig}, destino={dest_sig})"
                )

        orphan_checks = {
            "learnsets.pokemon_id -> pokemon.id": (
                "SELECT count(*) FROM learnsets l "
                "WHERE NOT EXISTS (SELECT 1 FROM pokemon p WHERE p.id = l.pokemon_id)"
            ),
            "learnsets.move_id -> moves.id": (
                "SELECT count(*) FROM learnsets l "
                "WHERE NOT EXISTS (SELECT 1 FROM moves m WHERE m.id = l.move_id)"
            ),
            "pokemon.gen_id -> generations.id": (
                "SELECT count(*) FROM pokemon p "
                "WHERE NOT EXISTS (SELECT 1 FROM generations g WHERE g.id = p.gen_id)"
            ),
            "moves.gen_id -> generations.id": (
                "SELECT count(*) FROM moves m "
                "WHERE NOT EXISTS (SELECT 1 FROM generations g WHERE g.id = m.gen_id)"
            ),
            "items.gen_id -> generations.id": (
                "SELECT count(*) FROM items i "
                "WHERE NOT EXISTS (SELECT 1 FROM generations g WHERE g.id = i.gen_id)"
            ),
        }
        for label, query in orphan_checks.items():
            orphans = await conn.fetchval(query)
            if orphans != 0:
                return False, f"integridad referencial rota: {label} ({orphans} huérfanas)"

        return True, "ok"
    finally:
        await conn.close()


async def ensure_dex_template(base_url: str, shared_url: str) -> str:
    """Garantiza una plantilla del dex válida y devuelve su nombre.

    El nombre se deriva de un fingerprint del schema + el dex fuente: si
    cambia cualquiera de los dos, el nombre cambia y una plantilla vieja
    nunca se reutiliza en silencio. Si el nombre fingerprintado ya existe,
    se RE-VALIDA antes de aceptarlo -- nunca se confía en la mera
    existencia. Si no existe, se construye en un staging de nombre único,
    se valida, y sólo entonces se publica con un `RENAME` atómico.

    R5 (L-02): TODO lo que lee `source` -- la huella, la copia de filas, y
    la comparación de `_validate_template` -- corre dentro de UNA sola
    transacción `REPEATABLE READ` de sólo lectura, abierta acá y sostenida
    hasta el final. Antes eran sentencias `READ COMMITTED` sueltas: nada
    impedía que la huella viera un estado de la fuente distinto del que
    después leía la copia o la validación.
    """
    source = await asyncpg.connect(
        shared_url, server_settings={"default_transaction_read_only": "on"},
    )
    try:
        async with source.transaction(isolation="repeatable_read", readonly=True):
            template_name = await _template_name(source)
            template_url = _with_database(base_url, template_name)
            _assert_not_reserved(template_url)
            maintenance_url = _with_database(base_url, "postgres")

            maintenance = await asyncpg.connect(maintenance_url)
            try:
                exists = await maintenance.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", template_name,
                )
            finally:
                await maintenance.close()

            if exists:
                valid, reason = await _validate_template(template_url, source)
                if valid:
                    return template_name
                raise RuntimeError(
                    f"'{template_name}' existe pero no pasa la validación ({reason}). "
                    "No se reconstruye automáticamente encima de un nombre que otra "
                    "corrida concurrente podría estar usando ahora mismo -- requiere "
                    "intervención manual (confirmar que está huérfana y DROP DATABASE)."
                )

            staging_name = f"{_STAGING_PREFIX}{uuid4().hex[:16]}"
            staging_url = _with_database(base_url, staging_name)
            _assert_not_reserved(staging_url)

            maintenance = await asyncpg.connect(maintenance_url)
            try:
                await maintenance.execute(f'CREATE DATABASE "{staging_name}"')
            finally:
                await maintenance.close()

            try:
                schema_conn = await asyncpg.connect(staging_url)
                try:
                    await schema_conn.execute(_SCHEMA_PATH.read_text())
                finally:
                    await schema_conn.close()

                dest = await asyncpg.connect(staging_url)
                try:
                    for table in _DEX_TABLES:
                        await _load_dex_table(source, dest, table)
                finally:
                    await dest.close()

                valid, reason = await _validate_template(staging_url, source)
                if not valid:
                    raise RuntimeError(
                        f"la plantilla recién construida en '{staging_name}' no pasó "
                        f"la validación: {reason}",
                    )

                maintenance = await asyncpg.connect(maintenance_url)
                try:
                    try:
                        await maintenance.execute(
                            f'ALTER DATABASE "{staging_name}" RENAME TO "{template_name}"',
                        )
                    except asyncpg.exceptions.DuplicateDatabaseError:
                        # Carrera: otro proceso publicó el MISMO fingerprint
                        # primero. No es un fallo -- el trabajo ya está hecho;
                        # sólo hace falta limpiar nuestro propio staging (nunca
                        # el que ganó la carrera).
                        await maintenance.execute(f'DROP DATABASE IF EXISTS "{staging_name}"')
                finally:
                    await maintenance.close()
            except BaseException:
                # Cualquier falla (excepción o cancelación cooperativa) ANTES de
                # publicar: se borra ÚNICAMENTE el staging de nombre exacto y
                # único de ESTA corrida. Nunca `ludex`, nunca el nombre
                # fingerprintado, nunca otro staging.
                maintenance = await asyncpg.connect(maintenance_url)
                try:
                    await maintenance.execute(f'DROP DATABASE IF EXISTS "{staging_name}" WITH (FORCE)')
                finally:
                    await maintenance.close()
                raise

            return template_name
    finally:
        await source.close()


@asynccontextmanager
async def disposable_dex_clone(base_url: str, shared_url: str) -> AsyncIterator[str]:
    """Clona la plantilla del dex válida (construyéndola primero si hace
    falta) en una base nueva `ludex_test_<uuid>`, la entrega, y la dropea
    en un `finally` alrededor del `yield` -- mismo alcance que
    `disposable_database` en `_disposable.py`: cubre salida normal,
    excepción, y cancelación cooperativa; nunca un `SIGKILL`."""
    template_name = await ensure_dex_template(base_url, shared_url)

    name = f"ludex_test_{uuid4().hex[:16]}"
    clone_url = _with_database(base_url, name)
    assert_disposable(clone_url)
    maintenance_url = _with_database(base_url, "postgres")

    maintenance = await asyncpg.connect(maintenance_url)
    try:
        await maintenance.execute(
            f'CREATE DATABASE "{name}" WITH TEMPLATE "{template_name}"',
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
