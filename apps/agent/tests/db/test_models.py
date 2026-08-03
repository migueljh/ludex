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
            assert tablas_comparadas == 4, (
                f"se esperaban 4 tablas mapeadas, se compararon {tablas_comparadas}"
            )
            assert columnas_comparadas > 0, "no se comparo ninguna columna"
    finally:
        await engine.dispose()


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
