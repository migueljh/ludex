"""Modelos SQLAlchemy escritos a mano.

Por D1 el esquema vive en las migraciones SQL: estos modelos son un espejo, no
la fuente de verdad. Si divergen, manda la migracion.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Los tipos ENUM ya existen en la base: `create_type=False` evita que
# SQLAlchemy intente recrearlos. Tiparlos como Text haria que un insert por ORM
# mande un string plano a una columna enum y falle en asyncpg.
PlayedByKind = PGEnum(name="played_by_kind", create_type=False)
BattleSource = PGEnum(name="battle_source", create_type=False)
BattleResult = PGEnum(name="battle_result", create_type=False)
ActionSource = PGEnum(name="action_source", create_type=False)


class Battle(Base):
    __tablename__ = "battles"
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_tag: Mapped[str] = mapped_column(Text, unique=True)
    tournament_id: Mapped[int | None] = mapped_column(nullable=True)
    round_id: Mapped[int | None] = mapped_column(nullable=True)
    format: Mapped[str] = mapped_column(Text)
    p1: Mapped[str] = mapped_column(Text)
    p2: Mapped[str] = mapped_column(Text)
    winner: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_by: Mapped[str] = mapped_column(PlayedByKind)
    source: Mapped[str] = mapped_column(BattleSource)
    replay_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column()


class BattleTurn(Base):
    __tablename__ = "battle_turns"
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"), primary_key=True)
    player_side: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_number: Mapped[int] = mapped_column(primary_key=True)
    protocol_lines: Mapped[list[str]] = mapped_column(ARRAY(String))
    agent_reasoning: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Trajectory(Base):
    __tablename__ = "trajectories"
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"))
    gen_id: Mapped[int] = mapped_column()
    format: Mapped[str] = mapped_column(Text)
    player_side: Mapped[str] = mapped_column(Text)
    final_result: Mapped[str | None] = mapped_column(BattleResult, nullable=True)
    elo_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column()


class TrajectoryStep(Base):
    __tablename__ = "trajectory_steps"
    trajectory_id: Mapped[int] = mapped_column(
        ForeignKey("trajectories.id"), primary_key=True
    )
    turn_number: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[dict] = mapped_column(JSONB)
    state_schema_version: Mapped[int] = mapped_column()
    legal_actions: Mapped[list] = mapped_column(JSONB)
    action_taken: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action_source: Mapped[str] = mapped_column(ActionSource)
    reward: Mapped[float | None] = mapped_column(Numeric, nullable=True)
