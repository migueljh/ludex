"""Escrituras. No conoce poke_env: recibe dicts ya serializados."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text


class BattleIdentityConflictError(RuntimeError):
    """D36: la misma `identity_key` (bajo el mismo `source`) ya existe con
    metadata incompatible.

    `identity_key` es un hash del bloque de apertura PUBLICO de la batalla
    (`ps-open-v1:sha256:...`, `compute_opening_identity`), no del
    `battle_tag`: el tag es la etiqueta de sala del servidor y se reusa tras
    un restart (D-3 del checkpoint de diagnostico), asi que ya NO puede ser
    la identidad persistida. Dos batallas distintas nunca deberian colisionar
    en la misma `identity_key` -- si lo hacen, o el fingerprint tiene una
    colision real, o alguien esta re-persistiendo con metadata equivocada.
    Mejor fallar ruidoso que fusionar callado.
    """


class BattleRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def save_battle(self, *, battle_tag: str, identity_key: str, fmt: str,
                          p1: str, p2: str, winner: str | None, source: str,
                          played_by: str) -> int:
        """Idempotente por `(source, identity_key)`: reejecutar el runner no
        duplica, y dos batallas distintas que comparten `battle_tag` (tag
        reusado tras un restart de Showdown) ya no se fusionan.

        D36: antes de upsertear, si la `identity_key` YA existe bajo el mismo
        `source` con otro p1/p2/format/played_by, es una colision real de la
        identidad (o un error de quien llama), no una re-persistencia
        legitima. El `winner` sigue una regla mas laxa a proposito: puede
        avanzar de NULL a un valor conocido (la batalla termino DESPUES de
        que se persistio parcialmente) o repetirse; dos ganadores conocidos
        DISTINTOS para la misma identidad tambien es una colision.
        """
        async with self.factory() as s:
            existente = (await s.execute(text(
                "SELECT p1, p2, format, played_by, winner FROM battles "
                "WHERE source = CAST(:src AS battle_source) AND identity_key = :key"
            ), {"src": source, "key": identity_key})).first()
            if existente is not None:
                if (existente[0] != p1 or existente[1] != p2
                        or existente[2] != fmt or existente[3] != played_by):
                    raise BattleIdentityConflictError(
                        f"identity_key {identity_key!r} (source={source!r}) ya existe "
                        f"con p1={existente[0]!r} p2={existente[1]!r} format={existente[2]!r} "
                        f"played_by={existente[3]!r}, distinto de la batalla actual "
                        f"(p1={p1!r} p2={p2!r} format={fmt!r} played_by={played_by!r})."
                    )
                if existente[4] is not None and winner is not None and existente[4] != winner:
                    raise BattleIdentityConflictError(
                        f"identity_key {identity_key!r} (source={source!r}) ya tiene "
                        f"winner={existente[4]!r}; la nueva persistencia trae "
                        f"winner={winner!r}: dos ganadores conocidos distintos para "
                        "la misma identidad."
                    )
            row = await s.execute(text("""
                INSERT INTO battles (battle_tag, identity_key, format, p1, p2, winner,
                                     played_by, source)
                VALUES (:tag, :key, :fmt, :p1, :p2, :w, CAST(:pb AS played_by_kind),
                        CAST(:src AS battle_source))
                ON CONFLICT (source, identity_key) DO UPDATE
                  SET winner = COALESCE(EXCLUDED.winner, battles.winner)
                RETURNING id
            """), {"tag": battle_tag, "key": identity_key, "fmt": fmt, "p1": p1, "p2": p2,
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
