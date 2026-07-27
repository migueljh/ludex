"""Escrituras. No conoce poke_env: recibe dicts ya serializados."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text


class BattleRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def save_battle(self, *, battle_tag: str, fmt: str, p1: str, p2: str,
                          winner: str | None, source: str, played_by: str) -> int:
        """Idempotente por battle_tag: reejecutar el runner no duplica."""
        async with self.factory() as s:
            row = await s.execute(text("""
                INSERT INTO battles (battle_tag, format, p1, p2, winner, played_by, source)
                VALUES (:tag, :fmt, :p1, :p2, :w, CAST(:pb AS played_by_kind),
                        CAST(:src AS battle_source))
                ON CONFLICT (battle_tag) DO UPDATE SET winner = EXCLUDED.winner
                RETURNING id
            """), {"tag": battle_tag, "fmt": fmt, "p1": p1, "p2": p2,
                   "w": winner, "pb": played_by, "src": source})
            await s.commit()
            return row.scalar_one()

    async def save_turn(self, battle_id: int, player_side: str, turn: int,
                        lines: list[str]) -> None:
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO battle_turns (battle_id, player_side, turn_number, protocol_lines)
                VALUES (:b, :ps, :t, :lines)
                ON CONFLICT (battle_id, player_side, turn_number)
                DO UPDATE SET protocol_lines = EXCLUDED.protocol_lines
            """), {"b": battle_id, "ps": player_side, "t": turn, "lines": lines})
            await s.commit()

    async def save_trajectory(self, battle_id: int, *, gen_number: int, fmt: str,
                              player_side: str) -> int:
        async with self.factory() as s:
            row = await s.execute(text("""
                INSERT INTO trajectories (battle_id, gen_id, format, player_side)
                SELECT :b, g.id, :fmt, :ps FROM generations g WHERE g.gen_number = :gen
                ON CONFLICT (battle_id, player_side) DO UPDATE SET format = EXCLUDED.format
                RETURNING id
            """), {"b": battle_id, "gen": gen_number, "fmt": fmt, "ps": player_side})
            await s.commit()
            return row.scalar_one()

    async def save_step(self, trajectory_id: int, turn: int, state: dict,
                        version: int, legal: list, action: dict | None,
                        source: str) -> None:
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, turn_number, state, state_schema_version,
                   legal_actions, action_taken, action_source)
                VALUES (:tj, :t, CAST(:st AS jsonb), :v, CAST(:la AS jsonb),
                        CAST(:at AS jsonb), CAST(:src AS action_source))
                -- state_schema_version y action_source TAMBIEN se actualizan:
                -- si un paso se reescribe tras un bump de version, dejar la
                -- version vieja con el estado nuevo hace que la fila mienta
                -- sobre su propio formato. `reward` queda deliberadamente
                -- afuera, para no pisar el que ya escribio finalize().
                ON CONFLICT (trajectory_id, turn_number) DO UPDATE
                  SET state = EXCLUDED.state,
                      state_schema_version = EXCLUDED.state_schema_version,
                      legal_actions = EXCLUDED.legal_actions,
                      action_taken = EXCLUDED.action_taken,
                      action_source = EXCLUDED.action_source
            """), {"tj": trajectory_id, "t": turn, "st": json.dumps(state), "v": version,
                   "la": json.dumps(legal),
                   "at": json.dumps(action) if action is not None else None,
                   "src": source})
            await s.commit()

    async def finalize(self, trajectory_id: int, *, result: str,
                       reward: float) -> None:
        """Propaga el reward a TODOS los pasos: sin esto no se puede entrenar."""
        async with self.factory() as s:
            await s.execute(text(
                "UPDATE trajectories SET final_result = CAST(:r AS battle_result) WHERE id = :t"
            ), {"r": result, "t": trajectory_id})
            await s.execute(text(
                "UPDATE trajectory_steps SET reward = :rw WHERE trajectory_id = :t"
            ), {"rw": reward, "t": trajectory_id})
            await s.commit()
