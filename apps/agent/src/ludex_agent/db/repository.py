"""Escrituras. No conoce poke_env: recibe dicts ya serializados."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text


class BattleTagCollisionError(RuntimeError):
    """I2: el mismo battle_tag ya existe con un p1/p2/format distinto.

    `battle_tag` (`battle-<formato>-<N>`) viene del contador global del
    servidor de Showdown, que vive en `logs/lastbattle.txt` DENTRO del
    contenedor (sin volumen: no sobrevive a un rebuild). Si el contador
    reinicia, un `battle_tag` viejo puede reusarse para una batalla
    completamente distinta. Con upsert-only (D13, sin deletes) esto fusionaria
    en silencio dos batallas distintas en una fila. Mejor fallar ruidoso que
    fusionar callado.
    """


class BattleRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def save_battle(self, *, battle_tag: str, fmt: str, p1: str, p2: str,
                          winner: str | None, source: str, played_by: str) -> int:
        """Idempotente por battle_tag: reejecutar el runner no duplica.

        I2: antes de upsertear, si el tag YA existe con otro p1/p2/format,
        es una colision del contador de Showdown (dos batallas distintas
        fusionandose), no una re-persistencia legitima. Se aborta ruidoso en
        vez de pisar en silencio.
        """
        async with self.factory() as s:
            existente = (await s.execute(text(
                "SELECT p1, p2, format FROM battles WHERE battle_tag = :tag"
            ), {"tag": battle_tag})).first()
            if existente is not None and (
                existente[0] != p1 or existente[1] != p2 or existente[2] != fmt
            ):
                raise BattleTagCollisionError(
                    f"battle_tag {battle_tag!r} ya existe con p1={existente[0]!r} "
                    f"p2={existente[1]!r} format={existente[2]!r}, distinto de la "
                    f"batalla actual (p1={p1!r} p2={p2!r} format={fmt!r}). "
                    "Probablemente el contador de Showdown reinicio (rebuild o "
                    "--force-recreate sin volumen); persistir pisaria una batalla "
                    "distinta bajo el mismo tag."
                )
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
                ON CONFLICT (battle_id, player_side) DO UPDATE
                  SET format = EXCLUDED.format,
                      -- minor de la review final: antes solo se refrescaba
                      -- `format`; re-persistir con otra generacion dejaba
                      -- `gen_id` viejo con el `format` nuevo.
                      gen_id = EXCLUDED.gen_id
                RETURNING id
            """), {"b": battle_id, "gen": gen_number, "fmt": fmt, "ps": player_side})
            await s.commit()
            return row.scalar_one()

    async def save_step(self, trajectory_id: int, decision_index: int, turn: int,
                        state: dict, version: int, legal: list, action: dict | None,
                        source: str, action_path: str | None = None) -> None:
        """D21 (C2): la clave es `(trajectory_id, decision_index)`.

        `decision_index` cuenta decisiones, no turnos, asi que un cambio
        forzado tras un debilitamiento (mismo `turn`, decision_index distinto)
        ya no se pisa. `turn_number` se actualiza como columna comun.
        """
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_taken, action_source,
                   action_path)
                VALUES (:tj, :di, :t, CAST(:st AS jsonb), :v, CAST(:la AS jsonb),
                        CAST(:at AS jsonb), CAST(:src AS action_source), :path)
                -- turn_number, state_schema_version y action_source TAMBIEN se
                -- actualizan: si un paso se reescribe tras un bump de version,
                -- dejar la version vieja con el estado nuevo hace que la fila
                -- mienta sobre su propio formato. `reward` queda
                -- deliberadamente afuera, para no pisar el que ya escribio
                -- finalize().
                ON CONFLICT (trajectory_id, decision_index) DO UPDATE
                  SET turn_number = EXCLUDED.turn_number,
                      state = EXCLUDED.state,
                      state_schema_version = EXCLUDED.state_schema_version,
                      legal_actions = EXCLUDED.legal_actions,
                      action_taken = EXCLUDED.action_taken,
                      action_source = EXCLUDED.action_source,
                      action_path = EXCLUDED.action_path
            """), {"tj": trajectory_id, "di": decision_index, "t": turn,
                   "st": json.dumps(state), "v": version, "la": json.dumps(legal),
                   "at": json.dumps(action) if action is not None else None,
                   "src": source, "path": action_path})
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
