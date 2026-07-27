"""Modelos SQLAlchemy escritos a mano.

Por D1 el esquema vive en las migraciones SQL: estos modelos son un espejo, no
la fuente de verdad. Si divergen, manda la migracion.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Battle(Base):
    __tablename__ = "battles"
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_tag: Mapped[str] = mapped_column(Text, unique=True)
    format: Mapped[str] = mapped_column(Text)
    p1: Mapped[str] = mapped_column(Text)
    p2: Mapped[str] = mapped_column(Text)
    winner: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_by: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column()


class BattleTurn(Base):
    __tablename__ = "battle_turns"
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"), primary_key=True)
    player_side: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_number: Mapped[int] = mapped_column(primary_key=True)
    protocol_lines: Mapped[list[str]] = mapped_column(ARRAY(String))


class Trajectory(Base):
    __tablename__ = "trajectories"
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"))
    gen_id: Mapped[int] = mapped_column()
    format: Mapped[str] = mapped_column(Text)
    player_side: Mapped[str] = mapped_column(Text)
    final_result: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    action_source: Mapped[str] = mapped_column(Text)
    reward: Mapped[float | None] = mapped_column(Numeric, nullable=True)
