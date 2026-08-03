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


# L-01 (LINEAR_VERDICT): la compatibilidad de metadata Y de winner se
# resuelve DENTRO del propio conflicto, en una unica sentencia. Postgres toma
# un lock exclusivo sobre la fila en conflicto en cuanto detecta la colision
# de `(source, identity_key)` -- antes de evaluar el WHERE -- y lo mantiene
# hasta el commit/rollback de esta transaccion. Un segundo escritor
# concurrente contra la MISMA identity_key queda bloqueado en este mismo
# INSERT hasta que el primero termine: no existe ninguna ventana entre "leer"
# y "escribir" donde dos llamadas puedan pasar una validacion separada antes
# de que cualquiera de las dos inserte. El WHERE es la unica fuente de verdad
# de "es compatible": si evalua falso, ni el UPDATE ni el INSERT ocurren y
# RETURNING no entrega fila para esa sentencia, sin ambiguedad posible con
# "no paso nada" (esta INSERT siempre intenta insertar o actualizar UNA fila).
_SAVE_BATTLE_SQL = text("""
    INSERT INTO battles (battle_tag, identity_key, format, p1, p2, winner,
                         played_by, source)
    VALUES (:tag, :key, :fmt, :p1, :p2, :w, CAST(:pb AS played_by_kind),
            CAST(:src AS battle_source))
    ON CONFLICT (source, identity_key) DO UPDATE
      SET winner = COALESCE(EXCLUDED.winner, battles.winner)
      WHERE battles.p1 = EXCLUDED.p1
        AND battles.p2 = EXCLUDED.p2
        AND battles.format = EXCLUDED.format
        AND battles.played_by = EXCLUDED.played_by
        AND (battles.winner IS NULL OR EXCLUDED.winner IS NULL
             OR battles.winner = EXCLUDED.winner)
    RETURNING id
""")


class BattleRepository:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def save_battle(self, *, battle_tag: str, identity_key: str, fmt: str,
                          p1: str, p2: str, winner: str | None, source: str,
                          played_by: str) -> int:
        """Idempotente por `(source, identity_key)`: reejecutar el runner no
        duplica, y dos batallas distintas que comparten `battle_tag` (tag
        reusado tras un restart de Showdown) ya no se fusionan.

        D36 + L-01: la compatibilidad se resuelve ATOMICAMENTE dentro de
        `_SAVE_BATTLE_SQL` (ver su comentario), no con un `SELECT` previo
        separado del `INSERT`. El `winner` avanza NULL->conocido o repite el
        mismo; dos ganadores conocidos DISTINTOS, o cualquier diferencia de
        p1/p2/format/played_by, hacen que el WHERE del conflicto de falso y
        `RETURNING` no entregue fila para esta sentencia.
        """
        async with self.factory() as s:
            row = await s.execute(_SAVE_BATTLE_SQL, {
                "tag": battle_tag, "key": identity_key, "fmt": fmt, "p1": p1, "p2": p2,
                "w": winner, "pb": played_by, "src": source,
            })
            result = row.first()
            if result is not None:
                await s.commit()
                return result[0]

            # RETURNING vacio bajo este ON CONFLICT solo significa una cosa:
            # ya existia una fila con esta (source, identity_key) y su WHERE
            # de compatibilidad dio falso. La decision ya se tomo, atomica,
            # arriba; esta SELECT es solo para armar un mensaje util, leyendo
            # la MISMA fila que seguimos bloqueando en esta transaccion.
            existente = (await s.execute(text(
                "SELECT p1, p2, format, played_by, winner FROM battles "
                "WHERE source = CAST(:src AS battle_source) AND identity_key = :key"
            ), {"src": source, "key": identity_key})).first()
            assert existente is not None, (
                "RETURNING vacio sin fila existente bajo ON CONFLICT "
                "(source, identity_key): no deberia poder pasar (D13, upsert-only)"
            )
            if (existente[0] != p1 or existente[1] != p2
                    or existente[2] != fmt or existente[3] != played_by):
                raise BattleIdentityConflictError(
                    f"identity_key {identity_key!r} (source={source!r}) ya existe "
                    f"con p1={existente[0]!r} p2={existente[1]!r} format={existente[2]!r} "
                    f"played_by={existente[3]!r}, distinto de la batalla actual "
                    f"(p1={p1!r} p2={p2!r} format={fmt!r} played_by={played_by!r})."
                )
            raise BattleIdentityConflictError(
                f"identity_key {identity_key!r} (source={source!r}) ya tiene "
                f"winner={existente[4]!r}; la nueva persistencia trae "
                f"winner={winner!r}: dos ganadores conocidos distintos para "
                "la misma identidad."
            )

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
                        source: str, action_path: str | None = None,
                        *, rationale: str | None = None,
                        target: dict | None = None,
                        confidence: float | None = None,
                        alternatives: list | None = None,
                        provider: str | None = None,
                        model: str | None = None,
                        decision_latency_ms: float | None = None,
                        input_tokens: int | None = None,
                        output_tokens: int | None = None,
                        cached_input_tokens: int | None = None,
                        reasoning_tokens: int | None = None) -> None:
        """D21 (C2): la clave es `(trajectory_id, decision_index)`.

        `decision_index` cuenta decisiones, no turnos, asi que un cambio
        forzado tras un debilitamiento (mismo `turn`, decision_index distinto)
        ya no se pisa. `turn_number` se actualiza como columna comun.

        F2-08 (MON-13): la metadata de la decision (rationale/confidence/
        alternatives/target/provider/model/latencia/usage) llega por parametro
        y se actualiza con COALESCE en el DO UPDATE: re-persistir la MISMA
        decision sin metadata (runner no cableado, D34) no puede borrar la
        metadata de la accion ya resuelta. El diseño sigue siendo de una sola
        fila por decision canonica; el intento rechazado por Showdown no
        llega nunca a esta tabla (D34).
        """
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_taken, action_source,
                   action_path, rationale, target, confidence, alternatives,
                   provider, model, decision_latency_ms, input_tokens,
                   output_tokens, cached_input_tokens, reasoning_tokens)
                VALUES (:tj, :di, :t, CAST(:st AS jsonb), :v, CAST(:la AS jsonb),
                        CAST(:at AS jsonb), CAST(:src AS action_source), :path,
                        :rationale, CAST(:target AS jsonb), :confidence,
                        CAST(:alt AS jsonb), :provider, :model, :latency_ms,
                        :in_tok, :out_tok, :cached_tok, :reasoning_tok)
                -- turn_number, state_schema_version y action_source TAMBIEN se
                -- actualizan: si un paso se reescribe tras un bump de version,
                -- dejar la version vieja con el estado nuevo hace que la fila
                -- mienta sobre su propio formato. `reward` queda
                -- deliberadamente afuera, para no pisar el que ya escribio
                -- finalize(). La metadata nueva usa COALESCE: NULL nunca pisa
                -- un valor ya persistido.
                ON CONFLICT (trajectory_id, decision_index) DO UPDATE
                  SET turn_number = EXCLUDED.turn_number,
                      state = EXCLUDED.state,
                      state_schema_version = EXCLUDED.state_schema_version,
                      legal_actions = EXCLUDED.legal_actions,
                      action_taken = EXCLUDED.action_taken,
                      action_source = EXCLUDED.action_source,
                      action_path = EXCLUDED.action_path,
                      rationale = COALESCE(EXCLUDED.rationale, trajectory_steps.rationale),
                      target = COALESCE(EXCLUDED.target, trajectory_steps.target),
                      confidence = COALESCE(EXCLUDED.confidence, trajectory_steps.confidence),
                      alternatives = COALESCE(EXCLUDED.alternatives, trajectory_steps.alternatives),
                      provider = COALESCE(EXCLUDED.provider, trajectory_steps.provider),
                      model = COALESCE(EXCLUDED.model, trajectory_steps.model),
                      decision_latency_ms = COALESCE(EXCLUDED.decision_latency_ms, trajectory_steps.decision_latency_ms),
                      input_tokens = COALESCE(EXCLUDED.input_tokens, trajectory_steps.input_tokens),
                      output_tokens = COALESCE(EXCLUDED.output_tokens, trajectory_steps.output_tokens),
                      cached_input_tokens = COALESCE(EXCLUDED.cached_input_tokens, trajectory_steps.cached_input_tokens),
                      reasoning_tokens = COALESCE(EXCLUDED.reasoning_tokens, trajectory_steps.reasoning_tokens)
            """), {"tj": trajectory_id, "di": decision_index, "t": turn,
                   "st": json.dumps(state), "v": version, "la": json.dumps(legal),
                   "at": json.dumps(action) if action is not None else None,
                   "src": source, "path": action_path,
                   "rationale": rationale,
                   "target": json.dumps(target) if target is not None else None,
                   "confidence": confidence,
                   "alt": json.dumps(alternatives) if alternatives is not None else None,
                   "provider": provider, "model": model,
                   "latency_ms": decision_latency_ms,
                   "in_tok": input_tokens, "out_tok": output_tokens,
                   "cached_tok": cached_input_tokens,
                   "reasoning_tok": reasoning_tokens})
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
