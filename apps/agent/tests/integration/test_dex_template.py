"""MON-11 R3/R4: la plantilla sanitizada del dex y sus clones por corrida.

No juega batallas (eso es `test_play.py`/`test_graph_play.py`, mas lento y
contra el Showdown local); esto prueba SOLO el mecanismo de la plantilla:
que trae el dex con conteos exactos, que las tablas mutables arrancan
vacias, que clonar es independiente, que el guardia rechaza la base
compartida ANTES de que cualquier cosa pueda jugar o persistir contra
ella, y (R4, BLOCKER 2) que una plantilla parcial/vieja/rota nunca se
acepta por la sola existencia de su nombre.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

import _dex_template
from _dex_template import (
    _DEX_TABLES,
    _MUTABLE_TABLES,
    _generation_counts,
    _template_name,
    _validate_template,
    disposable_dex_clone,
    ensure_dex_template,
)
from _disposable import SharedDatabaseGuardError, _with_database, verified_engine

requires_databases = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (base descartable) y DATABASE_URL "
    "(unica fuente admitida, read-only, para el dex de referencia)",
)


async def _connect_source():
    return await asyncpg.connect(
        os.environ["DATABASE_URL"],
        server_settings={"default_transaction_read_only": "on"},
    )


async def _maintenance(base: str) -> asyncpg.Connection:
    return await asyncpg.connect(_with_database(base, "postgres"))


@requires_databases
async def test_ensure_dex_template_trae_conteos_exactos_por_generacion_y_tablas_mutables_vacias():
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]
    name = await ensure_dex_template(base, shared)

    source = await _connect_source()
    dest = await asyncpg.connect(_with_database(base, name))
    try:
        for table in _DEX_TABLES:
            source_counts = await _generation_counts(source, table)
            dest_counts = await _generation_counts(dest, table)
            assert dest_counts == source_counts, f"{table}: {dest_counts} != {source_counts}"
            assert sum(source_counts.values()) > 0, f"{table}: la fuente no tiene filas para comparar"
        for table in _MUTABLE_TABLES:
            count = await dest.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"{table} debería arrancar vacía en la plantilla, tiene {count}"
    finally:
        await source.close()
        await dest.close()


@requires_databases
async def test_disposable_dex_clone_es_independiente_y_se_dropea():
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]

    async with disposable_dex_clone(base, shared) as url:
        template_name = await ensure_dex_template(base, shared)
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
        clone_name = url.rsplit("/", 1)[-1].split("?", 1)[0]

    maintenance = await _maintenance(base)
    try:
        exists = await maintenance.fetchval(
            "SELECT count(*) > 0 FROM pg_database WHERE datname = $1", clone_name,
        )
    finally:
        await maintenance.close()
    assert not exists, "el clon debe dropearse al salir del context manager"

    # La plantilla en si NUNCA recibe esa fila: cada clon es independiente.
    template_conn = await asyncpg.connect(_with_database(base, template_name))
    try:
        assert await template_conn.fetchval("SELECT count(*) FROM battles") == 0
    finally:
        await template_conn.close()


@requires_databases
async def test_db_preexistente_parcial_bajo_el_nombre_fingerprintado_no_es_aceptada(monkeypatch):
    """R4 BLOCKER 2 -- reproducción de Latwan: `PARTIAL_TEMPLATE_ACCEPTED
    returned=True generations_table=None`. Una base VACÍA (sin `db/schema.sql`
    aplicado, sin `generations`) creada bajo el nombre que
    `ensure_dex_template` buscaría tiene que ser rechazada -- nunca
    aceptada por la sola existencia del nombre. Se fuerza un nombre único y
    falso (en vez del fingerprint real) para que este test sea determinista
    sin importar si la plantilla real ya está publicada por otro test de
    este mismo archivo."""
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]
    fake_name = f"ludex_dex_template_{uuid.uuid4().hex[:24]}"

    async def fake_template_name(source):
        return fake_name

    monkeypatch.setattr(_dex_template, "_template_name", fake_template_name)

    maintenance = await _maintenance(base)
    try:
        await maintenance.execute(f'CREATE DATABASE "{fake_name}"')  # SIN aplicar schema.sql
    finally:
        await maintenance.close()

    try:
        with pytest.raises(RuntimeError, match="no pasa la validación"):
            await ensure_dex_template(base, shared)
    finally:
        maintenance = await _maintenance(base)
        try:
            await maintenance.execute(f'DROP DATABASE IF EXISTS "{fake_name}" WITH (FORCE)')
        finally:
            await maintenance.close()


@requires_databases
async def test_fingerprint_distinto_no_reutiliza_silenciosamente_el_nombre_anterior(monkeypatch):
    """Si el schema cambia, el nombre de la plantilla cambia -- nunca se
    reutiliza en silencio una plantilla construida para un schema/dex
    viejo."""
    source = await _connect_source()
    try:
        name_real = await _template_name(source)

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            fake_schema = Path(tmp) / "schema.sql"
            fake_schema.write_text("-- schema distinto, solo para el fingerprint\n")
            monkeypatch.setattr(_dex_template, "_SCHEMA_PATH", fake_schema)
            name_fake = await _template_name(source)

        assert name_fake != name_real
    finally:
        await source.close()


@requires_databases
async def test_fallo_a_mitad_de_construccion_no_publica_y_limpia_el_staging(monkeypatch):
    """R4 BLOCKER 2: una excepción durante la copia del dex no puede dejar
    ni una plantilla parcial publicada bajo el nombre fingerprintado, ni un
    staging huérfano."""
    base = os.environ["TEST_DATABASE_URL"]
    shared = os.environ["DATABASE_URL"]

    fake_name = f"ludex_dex_template_{uuid.uuid4().hex[:24]}"

    async def fake_template_name(source):
        return fake_name

    monkeypatch.setattr(_dex_template, "_template_name", fake_template_name)

    real_loader = _dex_template._load_dex_table
    calls = {"n": 0}

    async def broken_loader(source, dest, table):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("fallo inyectado a mitad de la copia del dex")
        return await real_loader(source, dest, table)

    monkeypatch.setattr(_dex_template, "_load_dex_table", broken_loader)

    with pytest.raises(RuntimeError, match="fallo inyectado"):
        await ensure_dex_template(base, shared)

    maintenance = await _maintenance(base)
    try:
        published = await maintenance.fetchval(
            "SELECT count(*) FROM pg_database WHERE datname = $1", fake_name,
        )
        staging_orphans = await maintenance.fetchval(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'ludex_dex_staging_%'",
        )
    finally:
        await maintenance.close()
    assert published == 0, "no se puede publicar una plantilla parcial bajo el nombre fingerprintado"
    assert staging_orphans == 0, "el staging de esta corrida debe limpiarse tras el fallo"


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
