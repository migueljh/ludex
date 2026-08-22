"""I5 (review de merge): `models.py` dice ser un espejo del DDL (D1), pero
nada lo comprobaba -- ni un import de `src/`, ni un test. Un espejo que
nadie ejecuta es una trampa: el proximo que lo lea le va a creer.

Este test compara `Base.metadata` contra `information_schema` de la base
VIVA, columna por columna: nombre, nullability y tipo real de Postgres
(`data_type`/`udt_name`, no solo el nombre que SQLAlchemy le puso). Si una
migracion futura desincroniza el modelo, esto se pone rojo en vez de quedar
como documentacion con sintaxis de Python.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.dialects import postgresql

from ludex_agent.config import load_settings
from ludex_agent.db import models
from ludex_agent.db.session import make_engine, session_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="necesita la base levantada"
)

_DIALECT = postgresql.dialect()

# DDL compilado (dialecto de Postgres) -> (data_type, udt_name) tal como los
# expone `information_schema.columns`. Los tipos ENUM (create_type=False)
# compilan directamente al nombre del tipo (D1: ya existen en la base) y se
# resuelven en la rama `else` de `_tipo_esperado`, no aca.
_TIPOS_CONOCIDOS = {
    "TEXT": ("text", "text"),
    "INTEGER": ("integer", "int4"),
    "NUMERIC": ("numeric", "numeric"),
    "JSONB": ("jsonb", "jsonb"),
    "TEXT[]": ("ARRAY", "_text"),
    "TIMESTAMP WITH TIME ZONE": ("timestamp with time zone", "timestamptz"),
    "TIMESTAMP WITHOUT TIME ZONE": ("timestamp without time zone", "timestamp"),
    # F2-08: `double precision` (confidence, decision_latency_ms) es la
    # sintaxis del DDL; `float8` es el udt_name real de Postgres.
    "DOUBLE PRECISION": ("double precision", "float8"),
    # F2-09: `boolean` (enabled/is_default) compila a BOOLEAN en SQLAlchemy;
    # el udt_name real de Postgres es `bool`.
    "BOOLEAN": ("boolean", "bool"),
}


def _tipo_esperado(column) -> tuple[str, str]:
    compilado = column.type.compile(dialect=_DIALECT).upper()
    if compilado in _TIPOS_CONOCIDOS:
        return _TIPOS_CONOCIDOS[compilado]
    # ENUM con create_type=False: compila al nombre del tipo tal cual se
    # declaro (`played_by_kind`, `battle_source`, `battle_result`,
    # `action_source`), que es exactamente `udt_name` en Postgres para un
    # tipo definido por el usuario.
    return ("USER-DEFINED", compilado.lower())


async def test_models_espeja_el_ddl_columna_por_columna():
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            divergencias: list[str] = []
            tablas_comparadas = 0
            columnas_comparadas = 0

            for tabla in models.Base.metadata.tables.values():
                filas = (await s.execute(text("""
                    SELECT column_name, is_nullable, data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :t
                """), {"t": tabla.name})).all()
                columnas_db = {r[0]: (r[1] == "YES", r[2], r[3]) for r in filas}

                assert columnas_db, (
                    f"la tabla '{tabla.name}' de models.py no existe en la "
                    "base (o no tiene columnas): el modelo referencia una "
                    "tabla que el DDL no tiene"
                )
                tablas_comparadas += 1

                nombres_modelo = {c.name for c in tabla.columns}
                nombres_db = set(columnas_db)
                if nombres_modelo != nombres_db:
                    solo_modelo = nombres_modelo - nombres_db
                    solo_db = nombres_db - nombres_modelo
                    divergencias.append(
                        f"{tabla.name}: columnas distintas (solo en modelo: "
                        f"{solo_modelo or '{}'}, solo en DB: {solo_db or '{}'})"
                    )
                    continue

                for columna in tabla.columns:
                    columnas_comparadas += 1
                    nullable_db, data_type_db, udt_name_db = columnas_db[columna.name]
                    data_type_esperado, udt_name_esperado = _tipo_esperado(columna)

                    if columna.nullable != nullable_db:
                        divergencias.append(
                            f"{tabla.name}.{columna.name}: nullable modelo="
                            f"{columna.nullable} vs db={nullable_db}"
                        )
                    if (data_type_db, udt_name_db) != (data_type_esperado, udt_name_esperado):
                        divergencias.append(
                            f"{tabla.name}.{columna.name}: tipo modelo="
                            f"{columna.type.compile(dialect=_DIALECT)!r} "
                            f"(esperaba data_type={data_type_esperado!r} "
                            f"udt_name={udt_name_esperado!r}) vs "
                            f"db data_type={data_type_db!r} udt_name={udt_name_db!r}"
                        )

            assert not divergencias, (
                "models.py diverge del DDL real:\n- " + "\n- ".join(divergencias)
            )
            # Canario: sin esto, una `Base.metadata` vacia (o un typo que
            # deje `tables` sin nada) pasaria en verde sin haber comparado
            # una sola columna.
            assert tablas_comparadas == 8, (
                f"se esperaban 8 tablas mapeadas (battles, battle_turns, "
                f"trajectories, trajectory_steps + providers, models, "
                f"settings de F2-09 + pending_decisions de D65), "
                f"se compararon {tablas_comparadas}"
            )
            assert columnas_comparadas > 0, "no se comparo ninguna columna"
    finally:
        await engine.dispose()


async def test_models_fk_y_unique_compuesto_espejan_el_ddl():
    """L-02 (MON-14 R2): el ORM de `models` refleja EXACTAMENTE el DDL ya
    aprobado de la migracion F2-09: FK a `providers.id` con `ON DELETE
    CASCADE` y UNIQUE compuesto `(provider_id, model_id)`. El espejo
    columna a columna no alcanzaba para esto: la FK podia perder el
    ondelete (la DB sigue borrando en cascada y el ORM afirmaria otra cosa)
    o desaparecer la unicidad compuesta, y el resto de los tests seguia en
    verde.

    Las mutaciones que eliminan `ondelete="CASCADE"` o el
    `UniqueConstraint` del modelo ponen este test en rojo.
    """
    table = models.Model.__table__

    fks = [fk for fk in table.foreign_keys if fk.parent.name == "provider_id"]
    assert len(fks) == 1, (
        "models.provider_id debe declarar exactamente una FK a providers.id"
    )
    assert fks[0].target_fullname == "providers.id", (
        f"la FK de models.provider_id apunta a {fks[0].target_fullname!r}, "
        "no a providers.id"
    )
    assert fks[0].ondelete == "CASCADE", (
        f"la FK de models.provider_id dice ondelete={fks[0].ondelete!r}: "
        "el DDL aprobado es ON DELETE CASCADE"
    )

    uniques = [
        c for c in table.constraints if isinstance(c, UniqueConstraint)
    ]
    assert len(uniques) == 1, (
        "models debe declarar exactamente un UniqueConstraint (el compuesto "
        "provider_id, model_id del DDL)"
    )
    columnas = sorted(c.name for c in uniques[0].columns)
    assert columnas == ["model_id", "provider_id"], (
        f"el UNIQUE del ORM cubre {columnas!r}, no ['model_id', "
        "'provider_id']"
    )


async def test_action_path_es_text_nullable_con_dominio_acotado():
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            column = (await s.execute(text("""
                SELECT is_nullable, data_type
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='trajectory_steps'
                  AND column_name='action_path'
            """))).one()
            definition = (await s.execute(text("""
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname='trajectory_steps_action_path_check'
            """))).scalar_one()

        assert column == ("YES", "text")
        assert all(value in definition for value in ("llm", "llm_retry", "fallback"))
    finally:
        await engine.dispose()


async def test_battles_constraints_e_indices_espejan_el_ddl():
    """D36: extiende la paridad esquema<->modelo a UNIQUE constraints e
    indices, no solo columnas/tipos. Sin esto, `battle_tag: Mapped[str] =
    mapped_column(Text, unique=True)` podia seguir diciendo `unique=True`
    despues de que la migracion de battle_identity se lo sacara, y
    `test_models_espeja_el_ddl_columna_por_columna` seguia en verde: nullability
    y tipo no dicen nada sobre unicidad.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT tc.constraint_name, kcu.column_name, kcu.ordinal_position
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                 AND kcu.table_schema = tc.table_schema
                WHERE tc.table_schema = 'public' AND tc.table_name = 'battles'
                  AND tc.constraint_type = 'UNIQUE'
                ORDER BY tc.constraint_name, kcu.ordinal_position
            """))).all()
            constraints_db: dict[str, list[str]] = {}
            for nombre, columna, _ in filas:
                constraints_db.setdefault(nombre, []).append(columna)

            indices_db = (await s.execute(text("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'battles'
            """))).all()
            indexdefs_db = {nombre: definicion for nombre, definicion in indices_db}

        table = models.Battle.__table__
        constraints_modelo = [
            c for c in table.constraints if isinstance(c, UniqueConstraint)
        ]
        assert constraints_modelo, "el modelo no declara ningun UniqueConstraint"
        for constraint in constraints_modelo:
            columnas_modelo = [c.name for c in constraint.columns]
            assert constraint.name in constraints_db, (
                f"falta en la DB el UNIQUE constraint {constraint.name!r} que "
                f"models.py declara sobre {columnas_modelo}"
            )
            assert constraints_db[constraint.name] == columnas_modelo, (
                f"{constraint.name}: columnas modelo={columnas_modelo} vs "
                f"db={constraints_db[constraint.name]}"
            )

        assert table.indexes, "el modelo no declara ningun Index"
        for index in table.indexes:
            assert index.name in indexdefs_db, (
                f"falta en la DB el indice {index.name!r} que models.py declara"
            )
            for column in index.columns:
                assert column.name in indexdefs_db[index.name], (
                    f"{index.name}: la DB no menciona la columna {column.name!r} "
                    f"({indexdefs_db[index.name]!r})"
                )

        # Canario negativo (D36): la vieja UNIQUE global sobre `battle_tag`
        # SOLA tiene que haber desaparecido -- es justo lo que esta migracion
        # vino a sacar. Si siguiera ahi, dos batallas con el mismo tag y
        # distinta identity_key no podrian coexistir y el arreglo no
        # arreglaria nada, aunque el resto de este test pasara en verde.
        assert "battles_battle_tag_key" not in constraints_db, (
            "sigue existiendo UNIQUE(battle_tag) global: la migracion de "
            "battle_identity no se aplico o no elimino esa constraint"
        )
    finally:
        await engine.dispose()


async def test_trajectory_steps_metadata_constraints_espejan_el_ddl():
    """F2-08: los CHECK de la migracion de metadata de decision existen, se
    llaman como el codigo espera (los tests de repository matchean por nombre)
    y su definicion expresa las reglas vinculantes del design verdict:
    confidence en [0,1], co-ocurrencia provider/model, co-ocurrencia de los
    cuatro tokens, cached <= input, latency >= 0 y tipos JSONB."""
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'trajectory_steps'::regclass
                  AND contype = 'c'
                ORDER BY conname
            """))).all()
        definiciones = dict(filas)
    finally:
        await engine.dispose()

    esperados: dict[str, list[str]] = {
        "trajectory_steps_confidence_check": [
            "confidence IS NULL", "confidence >= (0)::double precision",
            "confidence <= (1)::double precision",
        ],
        "trajectory_steps_provider_model_coherence_check": [
            "provider IS NULL", "model IS NULL",
        ],
        "trajectory_steps_usage_coherence_check": [
            "input_tokens IS NULL", "output_tokens IS NULL",
            "cached_input_tokens IS NULL", "reasoning_tokens IS NULL",
            "input_tokens IS NOT NULL", "reasoning_tokens IS NOT NULL",
        ],
        "trajectory_steps_input_tokens_nonnegative_check": [
            "input_tokens IS NULL", "input_tokens >= 0",
        ],
        "trajectory_steps_output_tokens_nonnegative_check": [
            "output_tokens IS NULL", "output_tokens >= 0",
        ],
        "trajectory_steps_cached_input_tokens_nonnegative_check": [
            "cached_input_tokens IS NULL", "cached_input_tokens >= 0",
        ],
        "trajectory_steps_reasoning_tokens_nonnegative_check": [
            "reasoning_tokens IS NULL", "reasoning_tokens >= 0",
        ],
        "trajectory_steps_cached_input_tokens_check": [
            "cached_input_tokens <= input_tokens",
        ],
        "trajectory_steps_latency_check": [
            "decision_latency_ms IS NULL", "decision_latency_ms >= (0)::double precision",
        ],
        "trajectory_steps_alternatives_type_check": [
            "jsonb_typeof(alternatives) = 'array'::text",
        ],
        "trajectory_steps_target_type_check": [
            "jsonb_typeof(target) = 'object'::text",
        ],
    }
    for nombre, fragmentos in esperados.items():
        assert nombre in definiciones, (
            f"falta el CHECK {nombre!r} en trajectory_steps: "
            f"la migracion F2-08 no se aplico o cambio el nombre"
        )
        for fragmento in fragmentos:
            assert fragmento in definiciones[nombre], (
                f"{nombre}: la definicion real no expresa la regla "
                f"{fragmento!r}: {definiciones[nombre]!r}"
            )

    # Canario del typo del borrador: el CHECK de `output_tokens` NO puede
    # referenciar `input_tokens` (el borrador del design verdict comprobaba
    # input_tokens en el constraint de output).
    assert "input_tokens" not in definiciones[
        "trajectory_steps_output_tokens_nonnegative_check"
    ], "el CHECK de output_tokens referencia input_tokens: typo del borrador"


# --- D65 (MON-31/Fase 3 S2): approval_outcome y pending_decisions ---------

async def test_approval_outcome_es_text_nullable_con_dominio_acotado():
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            column = (await s.execute(text("""
                SELECT is_nullable, data_type
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='trajectory_steps'
                  AND column_name='approval_outcome'
            """))).one()
            definicion = (await s.execute(text("""
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname='trajectory_steps_approval_outcome_check'
            """))).scalar_one()
        assert column == ("YES", "text")
        for valor in ("human_approved", "human_override", "timeout_auto"):
            assert valor in definicion
    finally:
        await engine.dispose()


async def test_human_override_metadata_null_check_referencia_las_once_columnas_d38():
    """El CHECK "global D38" (D65): existe y su definicion nombra las once
    columnas de metadata F2-08, no un subconjunto."""
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            definicion = (await s.execute(text("""
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname='trajectory_steps_human_override_metadata_null_check'
            """))).scalar_one()
    finally:
        await engine.dispose()

    assert "human_override" in definicion
    for columna in (
        "rationale", "target", "confidence", "alternatives", "provider",
        "model", "decision_latency_ms", "input_tokens", "output_tokens",
        "cached_input_tokens", "reasoning_tokens",
    ):
        assert f"{columna} IS NULL" in definicion, (
            f"el CHECK global D38 no menciona {columna!r}: {definicion!r}"
        )


async def test_pending_decision_pk_compuesta_espeja_el_ddl():
    """PK natural compuesta (battle_tag, decision_index, attempt_index),
    misma convencion que TrajectoryStep (D21/C2)."""
    table = models.PendingDecision.__table__
    pk_columnas = [c.name for c in table.primary_key.columns]
    assert pk_columnas == ["battle_tag", "decision_index", "attempt_index"]


async def test_pending_decisions_constraints_espejan_el_ddl():
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'pending_decisions'::regclass
                  AND contype = 'c'
                ORDER BY conname
            """))).all()
            indices = (await s.execute(text("""
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'pending_decisions'
            """))).all()
    finally:
        await engine.dispose()
    definiciones = dict(filas)
    nombres_indices = {r[0] for r in indices}

    esperados: dict[str, list[str]] = {
        "pending_decisions_status_check": [
            "awaiting", "human_approved", "human_override", "timeout_auto",
            "superseded", "aborted",
        ],
        "pending_decisions_resolved_by_check": ["operator", "timer", "system"],
        "pending_decisions_awaiting_has_no_resolution_check": [
            "status = 'awaiting'", "resolved_at IS NULL",
        ],
        "pending_decisions_resolution_action_check": [
            "human_approved", "human_override", "timeout_auto", "resolved_action IS NOT NULL",
        ],
        "pending_decisions_approval_wait_ms_check": [
            "approval_wait_ms IS NULL", "approval_wait_ms >=",
        ],
        "pending_decisions_action_type_check": ["jsonb_typeof(action) = 'object'"],
        "pending_decisions_resolved_action_type_check": [
            "resolved_action IS NULL", "jsonb_typeof(resolved_action) = 'object'",
        ],
        "pending_decisions_legal_actions_type_check": [
            "jsonb_typeof(legal_actions) = 'array'",
        ],
        "pending_decisions_model_envelope_type_check": [
            "jsonb_typeof(model_envelope) = 'object'",
        ],
        "pending_decisions_decision_index_nonnegative_check": ["decision_index >= 0"],
        "pending_decisions_attempt_index_nonnegative_check": ["attempt_index >= 0"],
    }
    for nombre, fragmentos in esperados.items():
        assert nombre in definiciones, f"falta el CHECK {nombre!r} en pending_decisions"
        for fragmento in fragmentos:
            assert fragmento in definiciones[nombre], (
                f"{nombre}: {definiciones[nombre]!r} no contiene {fragmento!r}"
            )
    assert "pending_decisions_status_idx" in nombres_indices
