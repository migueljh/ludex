"""Lecturas historicas para la API de control (spec Fase 3 S3.3, D65 MON-33
Task 4).

`ApiReadRepository` es exclusivamente de lectura sobre `battles`/
`trajectories`/`trajectory_steps` (ya existentes desde Fase 2). Nunca importa
ni usa `PendingDecisionRepository` (Task 3): esa clase es el unico escritor
de `pending_decisions` y vive exclusivamente dentro de `POKE_LOOP` (D65
S3.3). El estado *vivo* de una decision (awaiting) sale del
`ApprovalRegistry` en memoria (Task 4), nunca de una consulta a esta clase.

Mismo patron que `PostgresContextRepository` (`context_repository.py`): el
engine se crea perezosamente en el primer uso, con `NullPool`, bindeado al
loop de asyncio vivo en ese momento. D65 S3.3 prohibe compartir un
`AsyncEngine` entre loops; a diferencia de `PostgresContextRepository`, este
repositorio ademas GUARDA el loop de creacion y compara en cada uso: un uso
posterior desde otro loop levanta `CrossLoopRepositoryError` (tipado) en vez
de dejar que se propague el error generico de asyncio/asyncpg ("Future
attached to a different loop").
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


class CrossLoopRepositoryError(RuntimeError):
    """`ApiReadRepository` se creo en un loop de asyncio y se lo uso desde
    otro. Nunca comparte el `AsyncEngine`/pool entre loops (D65 S3.3): el
    loop de FastAPI y `POKE_LOOP` son procesos logicos distintos."""

    def __init__(self, *, created_loop_id: int, used_loop_id: int) -> None:
        super().__init__(
            "ApiReadRepository se creo en el loop de asyncio "
            f"{created_loop_id} y se lo uso desde el loop {used_loop_id}: un "
            "AsyncEngine no puede cruzar loops (D65 S3.3, spec Fase 3 3.3)."
        )
        self.created_loop_id = created_loop_id
        self.used_loop_id = used_loop_id


@dataclass(frozen=True)
class BattleSummary:
    battle_tag: str
    format: str
    p1: str
    p2: str
    winner: str | None
    played_by: str
    source: str


class ApiReadRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._engine = None
        self._factory = None
        self._loop_id: int | None = None

    def _ensure_factory(self):
        loop_id = id(asyncio.get_running_loop())
        if self._factory is None:
            self._loop_id = loop_id
            self._engine = create_async_engine(
                self._database_url, poolclass=NullPool,
            )
            self._factory = async_sessionmaker(
                self._engine, expire_on_commit=False,
            )
        elif loop_id != self._loop_id:
            raise CrossLoopRepositoryError(
                created_loop_id=self._loop_id, used_loop_id=loop_id,
            )
        return self._factory

    async def get_battle_by_tag(self, battle_tag: str) -> BattleSummary | None:
        factory = self._ensure_factory()
        async with factory() as session:
            row = (await session.execute(text("""
                SELECT battle_tag, format, p1, p2, winner, played_by, source
                FROM battles WHERE battle_tag = :tag
                ORDER BY id DESC LIMIT 1
            """), {"tag": battle_tag})).first()
        if row is None:
            return None
        return BattleSummary(*row)

    async def list_recent_battles(self, limit: int = 50) -> list[BattleSummary]:
        factory = self._ensure_factory()
        async with factory() as session:
            rows = (await session.execute(text("""
                SELECT battle_tag, format, p1, p2, winner, played_by, source
                FROM battles ORDER BY id DESC LIMIT :limit
            """), {"limit": limit})).all()
        return [BattleSummary(*row) for row in rows]

    async def aclose(self) -> None:
        engine = self._engine
        self._engine = None
        self._factory = None
        self._loop_id = None
        if engine is not None:
            await engine.dispose()


def battle_summary_as_dict(summary: BattleSummary) -> dict[str, object]:
    return asdict(summary)
