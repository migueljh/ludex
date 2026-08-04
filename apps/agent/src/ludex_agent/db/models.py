"""Modelos SQLAlchemy escritos a mano.

Por D1 el esquema vive en las migraciones SQL: estos modelos son un espejo, no
la fuente de verdad. Si divergen, manda la migracion.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
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
    """D36 (MON-10/F2-03): `battle_tag` dejo de ser la identidad -- es la
    etiqueta de sala del servidor de Showdown, y su contador se reusa tras un
    restart. `identity_key` (`ps-open-v1:sha256:...`, hash del bloque de
    apertura publico) es la identidad real; la unicidad es por
    `(source, identity_key)`, no global, para que un import y una grabacion
    local no se fusionen mientras `source` siga gobernando training.
    """

    __tablename__ = "battles"
    __table_args__ = (
        UniqueConstraint(
            "source", "identity_key", name="battles_source_identity_key_uniq"
        ),
        Index("battles_source_battle_tag_idx", "source", "battle_tag"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    battle_tag: Mapped[str] = mapped_column(Text)
    identity_key: Mapped[str] = mapped_column(Text)
    tournament_id: Mapped[int | None] = mapped_column(nullable=True)
    round_id: Mapped[int | None] = mapped_column(nullable=True)
    format: Mapped[str] = mapped_column(Text)
    p1: Mapped[str] = mapped_column(Text)
    p2: Mapped[str] = mapped_column(Text)
    winner: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_by: Mapped[str] = mapped_column(PlayedByKind)
    source: Mapped[str] = mapped_column(BattleSource)
    replay_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # I5 (review de merge): `Mapped[datetime]` sin tipo explicito compila a
    # TIMESTAMP WITHOUT TIME ZONE; el DDL dice `timestamptz` (`timestamp
    # with time zone`). `DateTime(timezone=True)` espeja el DDL exacto.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BattleTurn(Base):
    __tablename__ = "battle_turns"
    battle_id: Mapped[int] = mapped_column(ForeignKey("battles.id"), primary_key=True)
    player_side: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_number: Mapped[int] = mapped_column(primary_key=True)
    # DDL: `text[]`. `ARRAY(String)` mapea a `varchar[]`, un tipo distinto en
    # Postgres (minor de la review final): `ARRAY(Text)` espeja el DDL exacto.
    protocol_lines: Mapped[list[str]] = mapped_column(ARRAY(Text))
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
    # I5 (review de merge): mismo motivo que `Battle.created_at`.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TrajectoryStep(Base):
    """D21 (C2): la PK es `(trajectory_id, decision_index)`, no `turn_number`.

    Un cambio forzado tras un debilitamiento no avanza `battle.turn`, asi que
    `turn_number` NO puede ser parte de la clave: dos decisiones distintas
    pueden compartir turno. `decision_index` cuenta decisiones (una vez por
    llamada a `choose_move`), no turnos.
    """

    __tablename__ = "trajectory_steps"
    trajectory_id: Mapped[int] = mapped_column(
        ForeignKey("trajectories.id"), primary_key=True
    )
    decision_index: Mapped[int] = mapped_column(primary_key=True)
    turn_number: Mapped[int] = mapped_column()
    state: Mapped[dict] = mapped_column(JSONB)
    state_schema_version: Mapped[int] = mapped_column()
    legal_actions: Mapped[list] = mapped_column(JSONB)
    action_taken: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action_source: Mapped[str] = mapped_column(ActionSource)
    action_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # F2-08 (MON-13): metadata de la decision completa y de calidad ML.
    # NULL en filas historicas/random; los CHECKs de la migracion protegen
    # confidence [0,1], co-ocurrencia provider/model y de los cuatro tokens,
    # cached <= input y tipos JSONB de target/alternatives.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(
        Double, nullable=True
    )
    alternatives: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_latency_ms: Mapped[float | None] = mapped_column(
        Double, nullable=True
    )
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(nullable=True)
    reward: Mapped[float | None] = mapped_column(Numeric, nullable=True)
