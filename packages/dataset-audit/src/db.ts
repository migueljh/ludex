/** Carga del dataset. Estrictamente read-only y con costo de queries CONSTANTE.
 *
 * Son SEIS `SELECT` parametrizados, siempre los mismos, corran 1 paso o
 * 33 000: el scope y la generación se aplican DENTRO del SQL, no filtrando en
 * memoria lo que ya se trajo. `test/db.test.ts` tiene el canario que falla si
 * alguien agrega una query por paso.
 */

import pg from "pg";
import type { Pool } from "pg";
import type {
  BattleRecord,
  BattleTurnRecord,
  Dataset,
  DexMove,
  DexPokemon,
  FinalResult,
  Scope,
  StepState,
  TrajectoryRecord,
  TrajectoryStepRecord,
} from "./types.js";

/** Predicado de scope, idéntico en las cuatro queries del dataset.
 * `$1` = scope, `$2` = generación (NULL = todas). */
const SCOPE_BATTLE = "($1::text = 'all' OR b.source <> 'test')";
const SCOPE_TRAJECTORY =
  "($1::text = 'all' OR (b.source <> 'test' AND t.final_result IS NOT NULL))";
const GEN_BATTLE = `($2::int IS NULL OR EXISTS (
      SELECT 1 FROM trajectories tg
      JOIN generations gg ON gg.id = tg.gen_id
      WHERE tg.battle_id = b.id AND gg.gen_number = $2::int))`;

export const READ_QUERIES = {
  battles: `SELECT b.id, b.battle_tag, b.format, b.p1, b.p2, b.winner,
                   b.source::text AS source, b.played_by::text AS played_by
            FROM battles b
            WHERE ${SCOPE_BATTLE} AND ${GEN_BATTLE}
            ORDER BY b.id`,
  turns: `SELECT bt.battle_id, bt.player_side, bt.turn_number, bt.protocol_lines
          FROM battle_turns bt
          JOIN battles b ON b.id = bt.battle_id
          WHERE ${SCOPE_BATTLE} AND ${GEN_BATTLE}
          ORDER BY bt.battle_id, bt.player_side, bt.turn_number`,
  trajectories: `SELECT t.id, t.battle_id, g.gen_number, t.format,
                        t.player_side, t.final_result::text AS final_result
                 FROM trajectories t
                 JOIN generations g ON g.id = t.gen_id
                 JOIN battles b ON b.id = t.battle_id
                 WHERE ${SCOPE_TRAJECTORY}
                   AND ($2::int IS NULL OR g.gen_number = $2::int)
                 ORDER BY t.id`,
  steps: `SELECT s.trajectory_id, s.turn_number, s.decision_index, s.state,
                 s.state_schema_version, s.legal_actions, s.action_taken,
                 s.action_source::text AS action_source, s.action_path, s.reward
          FROM trajectory_steps s
          JOIN trajectories t ON t.id = s.trajectory_id
          JOIN generations g ON g.id = t.gen_id
          JOIN battles b ON b.id = t.battle_id
          WHERE ${SCOPE_TRAJECTORY}
            AND ($2::int IS NULL OR g.gen_number = $2::int)
          ORDER BY s.trajectory_id, s.decision_index`,
  // El dex local es la otra fuente de verdad admitida (nunca internet, nunca
  // runtime). Se trae entero por generación: son ~1700 filas y su costo no
  // depende de trajectory_steps.
  // El dex no depende del scope, así que toma la generación en `$1`: pasarle
  // un `$1` que nunca usa hace que PostgreSQL rechace la sentencia con
  // "could not determine data type of parameter $1".
  dexPokemon: `SELECT g.gen_number, p.showdown_id, p.base_species, p.forme,
                      p.types, p.abilities
               FROM pokemon p
               JOIN generations g ON g.id = p.gen_id
               WHERE ($1::int IS NULL OR g.gen_number = $1::int)
               ORDER BY g.gen_number, p.showdown_id`,
  dexMoves: `SELECT g.gen_number, m.showdown_id, m.pp
             FROM moves m
             JOIN generations g ON g.id = m.gen_id
             WHERE ($1::int IS NULL OR g.gen_number = $1::int)
             ORDER BY g.gen_number, m.showdown_id`,
} as const;

/** Cantidad de queries que `loadDataset` emite, siempre. Pineado a propósito:
 * el canario de `test/db.test.ts` compara contra este número exacto. */
export const EXPECTED_QUERY_COUNT = Object.keys(READ_QUERIES).length;

export function createReadOnlyPool(): Pool {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("Falta DATABASE_URL. Copiar .env.example a .env.");
  }
  return new pg.Pool({
    connectionString,
    // Defensa en profundidad: PostgreSQL rechaza escrituras aunque una
    // consulta futura se agregue por error.
    options: "-c default_transaction_read_only=on",
  });
}

interface BattleRow {
  id: number;
  battle_tag: string;
  format: string;
  p1: string;
  p2: string;
  winner: string | null;
  source: string;
  played_by: string;
}

interface TurnRow {
  battle_id: number;
  player_side: string;
  turn_number: number;
  protocol_lines: string[];
}

interface TrajectoryRow {
  id: number;
  battle_id: number;
  gen_number: number;
  format: string;
  player_side: string;
  final_result: string | null;
}

interface StepRow {
  trajectory_id: number;
  turn_number: number;
  decision_index: number;
  state: StepState;
  state_schema_version: number;
  legal_actions: unknown[];
  action_taken: Record<string, unknown> | null;
  action_source: string;
  action_path: string | null;
  reward: string | null;
}

interface DexPokemonRow {
  gen_number: number;
  showdown_id: string;
  base_species: string | null;
  forme: string | null;
  types: string[] | null;
  abilities: Record<string, string> | null;
}

interface DexMoveRow {
  gen_number: number;
  showdown_id: string;
  pp: number | null;
}

/** Lo mínimo que `loadDataset` necesita de un pool. Existe para que el canario
 * de conteo de queries pueda pasar un doble sin levantar Postgres. */
export interface Queryable {
  query(text: string, values?: unknown[]): Promise<{ rows: unknown[] }>;
}

export interface LoadOptions {
  scope?: Scope;
  gen?: number;
}

export async function loadDataset(
  pool: Queryable,
  options: LoadOptions = {},
): Promise<Dataset> {
  const scope: Scope = options.scope ?? "all";
  const gen = options.gen;
  if (gen !== undefined && (!Number.isInteger(gen) || gen < 1)) {
    throw new Error(`gen debe ser un entero positivo; recibido: ${gen}`);
  }
  const params = [scope, gen ?? null];
  const dexParams = [gen ?? null];

  const [battleResult, turnResult, trajectoryResult, stepResult, dexResult, moveResult] =
    await Promise.all([
      pool.query(READ_QUERIES.battles, params),
      pool.query(READ_QUERIES.turns, params),
      pool.query(READ_QUERIES.trajectories, params),
      pool.query(READ_QUERIES.steps, params),
      pool.query(READ_QUERIES.dexPokemon, dexParams),
      pool.query(READ_QUERIES.dexMoves, dexParams),
    ]);

  const battles: BattleRecord[] = (battleResult.rows as BattleRow[]).map((row) => ({
    id: row.id,
    battleTag: row.battle_tag,
    format: row.format,
    p1: row.p1,
    p2: row.p2,
    winner: row.winner,
    source: row.source as BattleRecord["source"],
    playedBy: row.played_by,
  }));
  const turns: BattleTurnRecord[] = (turnResult.rows as TurnRow[]).map((row) => ({
    battleId: row.battle_id,
    playerSide: row.player_side,
    turnNumber: row.turn_number,
    protocolLines: row.protocol_lines,
  }));
  const trajectories: TrajectoryRecord[] = (trajectoryResult.rows as TrajectoryRow[]).map((row) => ({
    id: row.id,
    battleId: row.battle_id,
    gen: row.gen_number,
    format: row.format,
    playerSide: row.player_side,
    finalResult: row.final_result as FinalResult | null,
  }));
  const steps: TrajectoryStepRecord[] = (stepResult.rows as StepRow[]).map((row) => ({
    trajectoryId: row.trajectory_id,
    turnNumber: row.turn_number,
    decisionIndex: row.decision_index,
    state: row.state,
    stateSchemaVersion: row.state_schema_version,
    legalActions: row.legal_actions,
    actionTaken: row.action_taken,
    actionSource: row.action_source,
    actionPath: row.action_path,
    reward: row.reward === null ? null : Number(row.reward),
  }));
  const dexPokemon: DexPokemon[] = (dexResult.rows as DexPokemonRow[]).map((row) => ({
    gen: row.gen_number,
    showdownId: row.showdown_id,
    baseSpecies: row.base_species ?? row.showdown_id,
    forme: row.forme,
    types: row.types ?? [],
    abilities: Object.values(row.abilities ?? {}),
  }));
  const dexMoves: DexMove[] = (moveResult.rows as DexMoveRow[]).map((row) => ({
    gen: row.gen_number,
    showdownId: row.showdown_id,
    pp: row.pp,
  }));

  return { battles, turns, trajectories, steps, dexPokemon, dexMoves };
}
