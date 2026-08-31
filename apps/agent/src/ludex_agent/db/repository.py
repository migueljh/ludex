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
                         played_by, source, replay_url)
    VALUES (:tag, :key, :fmt, :p1, :p2, :w, CAST(:pb AS played_by_kind),
            CAST(:src AS battle_source), :replay_url)
    ON CONFLICT (source, identity_key) DO UPDATE
      SET winner = COALESCE(EXCLUDED.winner, battles.winner),
          -- MON-40/Fase 3 S9: gancho estrecho, no columna de identidad. La
          -- primera persistencia puede correr ANTES de que Showdown emita
          -- el link (el `|raw|` de replay llega recien al cierre real de la
          -- sala); COALESCE deja que una re-persistencia posterior de la
          -- MISMA batalla complete el dato sin pisar uno ya conocido con
          -- NULL.
          replay_url = COALESCE(EXCLUDED.replay_url, battles.replay_url)
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
                          played_by: str, replay_url: str | None = None) -> int:
        """Idempotente por `(source, identity_key)`: reejecutar el runner no
        duplica, y dos batallas distintas que comparten `battle_tag` (tag
        reusado tras un restart de Showdown) ya no se fusionan.

        D36 + L-01: la compatibilidad se resuelve ATOMICAMENTE dentro de
        `_SAVE_BATTLE_SQL` (ver su comentario), no con un `SELECT` previo
        separado del `INSERT`. El `winner` avanza NULL->conocido o repite el
        mismo; dos ganadores conocidos DISTINTOS, o cualquier diferencia de
        p1/p2/format/played_by, hacen que el WHERE del conflicto de falso y
        `RETURNING` no entregue fila para esta sentencia.

        `replay_url` (MON-40/Fase 3 S9) NO participa en la compatibilidad de
        identidad: es un gancho estrecho, opcional por naturaleza (`None`
        cuando Showdown todavia no emitio el link), y default `None` para no
        romper a ningun caller existente.
        """
        async with self.factory() as s:
            row = await s.execute(_SAVE_BATTLE_SQL, {
                "tag": battle_tag, "key": identity_key, "fmt": fmt, "p1": p1, "p2": p2,
                "w": winner, "pb": played_by, "src": source, "replay_url": replay_url,
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
                              player_side: str, elo_bucket: str | None = None) -> int:
        """MON-40/Fase 3 S9: `elo_bucket` es el rating PUBLICO del rival
        (`battle.opponent_rating`), nunca el propio -- el gancho es para
        identidad del rival (Fase 6), no para el desempeno del agente.
        Solo llega poblado cuando Showdown mando el `|raw|` de rating (tipico
        de ladder; ausente en challenge, que por eso persiste NULL sin
        ninguna rama especial aca: la ausencia del dato ES la senal). Nunca
        se agrupa en rangos ("bucket" es el nombre de la columna heredado del
        diseno, no una categoria inventada por esta implementacion) -- D17:
        omitir es valido, inventar no.
        """
        async with self.factory() as s:
            row = await s.execute(text("""
                INSERT INTO trajectories (battle_id, gen_id, format, player_side, elo_bucket)
                SELECT :b, g.id, :fmt, :ps, :elo FROM generations g WHERE g.gen_number = :gen
                ON CONFLICT (battle_id, player_side) DO UPDATE
                  SET format = EXCLUDED.format,
                      -- minor de la review final: antes solo se refrescaba
                      -- `format`; re-persistir con otra generacion dejaba
                      -- `gen_id` viejo con el `format` nuevo.
                      gen_id = EXCLUDED.gen_id,
                      -- mismo criterio que replay_url: gancho opcional, no
                      -- pisar un valor ya conocido con NULL de una
                      -- re-persistencia que no lo trae.
                      elo_bucket = COALESCE(EXCLUDED.elo_bucket, trajectories.elo_bucket)
                RETURNING id
            """), {"b": battle_id, "gen": gen_number, "fmt": fmt, "ps": player_side,
                   "elo": elo_bucket})
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
                        reasoning_tokens: int | None = None,
                        approval_outcome: str | None = None) -> None:
        """D21 (C2): la clave es `(trajectory_id, decision_index)`.

        `decision_index` cuenta decisiones, no turnos, asi que un cambio
        forzado tras un debilitamiento (mismo `turn`, decision_index distinto)
        ya no se pisa. `turn_number` se actualiza como columna comun.

        F2-08 (MON-13): la metadata de la decision (rationale/confidence/
        alternatives/target/provider/model/latencia/usage) llega por parametro
        y en el DO UPDATE se reemplaza con EXCLUDED PURO, como grupo: la
        re-persistencia de la misma decision es idempotente (misma metadata ->
        mismos valores) y una re-persistencia explicita sin metadata deja las
        11 columnas en NULL. Nunca se conserva un subconjunto anterior:
        COALESCE podia actualizar `action_taken` pero retener
        rationale/provider/model de una accion anterior, creando una fila
        incoherente (correccion del TECH LEAD PARTIAL CHECKPOINT VERDICT).
        El diseño sigue siendo de una sola fila por decision canonica; el
        intento rechazado por Showdown no llega nunca a esta tabla (D34).

        D65 (MON-31/Fase 3 S2): `approval_outcome` es un tercer eje
        ortogonal, nunca colapsado con `action_source`/`action_path`. Un
        `human_override` se persiste con `approval_outcome="human_override"`
        y el caller SIN pasar ninguno de los kwargs de metadata D38 (todos
        quedan en su default `None`): el mismo EXCLUDED puro de arriba deja
        el grupo completo en NULL, y el CHECK
        `trajectory_steps_human_override_metadata_null_check` lo rechaza si
        alguno viniera poblado.
        """
        async with self.factory() as s:
            await s.execute(text("""
                INSERT INTO trajectory_steps
                  (trajectory_id, decision_index, turn_number, state,
                   state_schema_version, legal_actions, action_taken, action_source,
                   action_path, rationale, target, confidence, alternatives,
                   provider, model, decision_latency_ms, input_tokens,
                   output_tokens, cached_input_tokens, reasoning_tokens,
                   approval_outcome)
                VALUES (:tj, :di, :t, CAST(:st AS jsonb), :v, CAST(:la AS jsonb),
                        CAST(:at AS jsonb), CAST(:src AS action_source), :path,
                        :rationale, CAST(:target AS jsonb), :confidence,
                        CAST(:alt AS jsonb), :provider, :model, :latency_ms,
                        :in_tok, :out_tok, :cached_tok, :reasoning_tok,
                        :approval_outcome)
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
                      action_path = EXCLUDED.action_path,
                      rationale = EXCLUDED.rationale,
                      target = EXCLUDED.target,
                      confidence = EXCLUDED.confidence,
                      alternatives = EXCLUDED.alternatives,
                      provider = EXCLUDED.provider,
                      model = EXCLUDED.model,
                      decision_latency_ms = EXCLUDED.decision_latency_ms,
                      input_tokens = EXCLUDED.input_tokens,
                      output_tokens = EXCLUDED.output_tokens,
                      cached_input_tokens = EXCLUDED.cached_input_tokens,
                      reasoning_tokens = EXCLUDED.reasoning_tokens,
                      approval_outcome = EXCLUDED.approval_outcome
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
                   "reasoning_tok": reasoning_tokens,
                   "approval_outcome": approval_outcome})
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
