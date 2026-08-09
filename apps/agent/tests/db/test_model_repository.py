"""F2-09 (MON-14): ModelRepository -- la DB gobierna la seleccion activa y
nunca persiste API keys (solo el NOMBRE de la env var)."""

import os

import pytest
from sqlalchemy import text

from _disposable import verified_engine
from ludex_agent.db.model_repository import (
    ModelRepository,
    ModelSelectionError,
)
from ludex_agent.db.session import session_factory

# MON-11 (E/R2): TEST_DATABASE_URL, no DATABASE_URL -- este archivo nunca
# corre contra la base compartida. La fixture `repo` hace DELETE FROM
# settings/models/providers sin ningun guardia propio; sobre la base
# compartida eso es destructivo de config real, no solo de filas
# source='test'. `verified_engine` (ver _disposable.py) confirma
# `current_database()` ANTES de que cualquiera de esos DELETE pueda correr.
pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="necesita TEST_DATABASE_URL (base descartable; nunca DATABASE_URL)",
)


pytest_asyncio = pytest.importorskip("pytest_asyncio")


@pytest_asyncio.fixture(loop_scope="function")
async def repo(test_database_url):
    engine = await verified_engine(test_database_url)
    factory = session_factory(engine)
    async with factory() as s:
        await s.execute(text("DELETE FROM settings"))
        await s.execute(text("DELETE FROM models"))
        await s.execute(text("DELETE FROM providers"))
        await s.execute(text(
            "INSERT INTO providers (name, base_url, api_key_env, enabled) "
            "VALUES ('google', NULL, 'GEMINI_API_KEY', true), "
            "('kimi', NULL, 'KIMI_API_KEY', true), "
            "('off-provider', NULL, 'OFF_KEY', false)"
        ))
        await s.execute(text(
            "INSERT INTO models (provider_id, model_id, label, is_default, enabled) "
            "SELECT p.id, 'gemini-2.5-flash', 'Gemini 2.5 Flash', true, true "
            "FROM providers p WHERE p.name = 'google'"
        ))
        await s.execute(text(
            "INSERT INTO models (provider_id, model_id, label, is_default, enabled) "
            "SELECT p.id, 'gemini-2.5-pro', 'Gemini 2.5 Pro', false, false "
            "FROM providers p WHERE p.name = 'google'"
        ))
        await s.execute(text(
            "INSERT INTO models (provider_id, model_id, label, is_default, enabled) "
            "SELECT p.id, 'kimi-k2.6', 'Kimi K2.6', false, true "
            "FROM providers p WHERE p.name = 'kimi'"
        ))
        await s.commit()
    yield ModelRepository(factory)
    await engine.dispose()


async def test_no_se_persiste_ninguna_api_key(repo, test_database_url):
    """BLOQUEANTE 3 (F2-09): la DB guarda el NOMBRE de la env var; el valor
    de una API key nunca aparece en ninguna columna."""
    engine = await verified_engine(test_database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text(
                "SELECT name, base_url, api_key_env FROM providers"
            ))).all()
            catalogo = "\n".join(repr(f) for f in filas)
    finally:
        await engine.dispose()

    assert ("google", None, "GEMINI_API_KEY") in filas
    # El catalogo solo contiene nombres de proveedores y NOMBRES de env vars;
    # ningun valor plausible de clave puede aparecer en ninguna columna.
    for secreto in ("AIzaSy", "sk-", "k="):
        assert secreto not in catalogo, (
            f"la DB no debe contener valores de claves: {catalogo!r}"
        )


async def test_active_selection_usa_el_default_sin_setting(repo):
    selection = await repo.active_selection()

    assert selection is not None
    assert selection.provider_name == "google"
    assert selection.model_id == "gemini-2.5-flash"


async def test_set_active_y_active_selection_la_devuelven(repo):
    await repo.set_active("kimi", "kimi-k2.6")

    selection = await repo.active_selection()

    assert selection.provider_name == "kimi"
    assert selection.model_id == "kimi-k2.6"


async def test_provider_row_devuelve_nombre_env_no_valor(repo):
    row = await repo.provider("google")

    assert row is not None
    assert row.name == "google"
    assert row.api_key_env == "GEMINI_API_KEY"
    assert row.enabled is True


async def test_provider_inexistente_devuelve_none(repo):
    assert await repo.provider("no-existe") is None


# --- L-01 (MON-14 R2): frontera unica de consulta/validacion de la
# seleccion activa ---------------------------------------------------------


async def test_validate_selection_acepta_seleccion_valida(repo):
    row = await repo.validate_selection("google", "gemini-2.5-flash")

    assert row.name == "google"
    assert row.api_key_env == "GEMINI_API_KEY"
    assert row.enabled is True


async def test_validate_selection_rechaza_provider_inexistente(repo):
    with pytest.raises(ModelSelectionError, match="no existe"):
        await repo.validate_selection("no-existe", "cualquier-modelo")


async def test_validate_selection_rechaza_provider_disabled(repo):
    with pytest.raises(ModelSelectionError, match="deshabilitado"):
        await repo.validate_selection("off-provider", "cualquier-modelo")


async def test_validate_selection_rechaza_modelo_inexistente(repo):
    with pytest.raises(ModelSelectionError, match="no existe"):
        await repo.validate_selection("google", "modelo-no-seedeado")


async def test_validate_selection_rechaza_modelo_disabled(repo):
    """La frontera no alcanza con que el provider este enabled: el model
    deshabilitado en la DB se rechaza igual (la decision no se atribuye a un
    modelo que la DB deshabilito)."""
    with pytest.raises(ModelSelectionError, match="deshabilitado"):
        await repo.validate_selection("google", "gemini-2.5-pro")


async def test_validate_selection_no_altera_settings(repo):
    """La frontera es de solo lectura: un rechazo deja el valor anterior de
    settings intacto."""
    await repo.set_active("google", "gemini-2.5-flash")

    with pytest.raises(ModelSelectionError):
        await repo.validate_selection("google", "gemini-2.5-pro")

    selection = await repo.active_selection()
    assert (selection.provider_name, selection.model_id) == (
        "google", "gemini-2.5-flash",
    )
