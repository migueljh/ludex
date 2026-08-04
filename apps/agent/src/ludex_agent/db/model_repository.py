"""Seleccion activa de provider/model (F2-09/MON-14).

La DB gobierna que modelo se usa en cada decision; el env es solo bootstrap.
Este repositorio NUNCA guarda ni expone valores de API keys: `providers`
solo conserva el NOMBRE de la variable de entorno (`api_key_env`); la clave
se lee del entorno en runtime por ese nombre.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


@dataclass(frozen=True)
class ProviderModel:
    provider_name: str
    model_id: str


@dataclass(frozen=True)
class ProviderRow:
    name: str
    base_url: str | None
    api_key_env: str
    enabled: bool


_ACTIVE_MODEL_KEY = "active_model"


class ModelRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def active_selection(self) -> ProviderModel | None:
        """La seleccion activa de `settings`; si no existe, el modelo default
        habilitado del primer proveedor habilitado. `None` si no hay nada."""
        async with self.factory() as s:
            row = (await s.execute(text("""
                SELECT value->>'provider', value->>'model'
                FROM settings WHERE key = :k
            """), {"k": _ACTIVE_MODEL_KEY})).first()
            if row is not None and row[0] and row[1]:
                return ProviderModel(row[0], row[1])
            default = (await s.execute(text("""
                SELECT p.name, m.model_id
                FROM models m
                JOIN providers p ON p.id = m.provider_id
                WHERE m.is_default AND m.enabled AND p.enabled
                ORDER BY m.id
                LIMIT 1
            """))).first()
            if default is None:
                return None
            return ProviderModel(default[0], default[1])

    async def provider(self, name: str) -> ProviderRow | None:
        async with self.factory() as s:
            row = (await s.execute(text("""
                SELECT name, base_url, api_key_env, enabled
                FROM providers WHERE name = :n
            """), {"n": name})).first()
        if row is None:
            return None
        return ProviderRow(row[0], row[1], row[2], row[3])

    async def set_active(self, provider_name: str, model_id: str) -> None:
        """Fija la seleccion activa (sin secretos: solo nombres)."""
        import json

        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO settings (key, value)
                VALUES (:k, CAST(:v AS jsonb))
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """), {
                "k": _ACTIVE_MODEL_KEY,
                "v": json.dumps({"provider": provider_name, "model": model_id}),
            })
            await s.commit()

    async def upsert_provider(self, *, name: str, base_url: str | None,
                              api_key_env: str, enabled: bool = True) -> int:
        """Bootstrap de configuracion: el NOMBRE de la env var, nunca el
        valor de la clave."""
        async with self.factory() as s:
            row = await s.execute(text("""
                INSERT INTO providers (name, base_url, api_key_env, enabled)
                VALUES (:n, :b, :e, :en)
                ON CONFLICT (name) DO UPDATE
                  SET base_url = EXCLUDED.base_url,
                      api_key_env = EXCLUDED.api_key_env,
                      enabled = EXCLUDED.enabled
                RETURNING id
            """), {"n": name, "b": base_url, "e": api_key_env, "en": enabled})
            await s.commit()
            return row.scalar_one()

    async def upsert_model(self, *, provider_id: int, model_id: str, label: str,
                           is_default: bool = False, enabled: bool = True) -> None:
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO models (provider_id, model_id, label, is_default, enabled)
                VALUES (:p, :m, :l, :d, :en)
                ON CONFLICT (provider_id, model_id) DO UPDATE
                  SET label = EXCLUDED.label,
                      is_default = EXCLUDED.is_default,
                      enabled = EXCLUDED.enabled
            """), {
                "p": provider_id, "m": model_id, "l": label,
                "d": is_default, "en": enabled,
            })
            await s.commit()

    async def list_models(self, provider_name: str | None = None) -> list[ProviderModel]:
        async with self.factory() as s:
            params: dict[str, Any] = {}
            where = ""
            if provider_name is not None:
                where = "WHERE p.name = :pn"
                params["pn"] = provider_name
            rows = (await s.execute(text(f"""
                SELECT p.name, m.model_id
                FROM models m JOIN providers p ON p.id = m.provider_id
                {where}
                ORDER BY p.name, m.model_id
            """), params)).all()
        return [ProviderModel(r[0], r[1]) for r in rows]
