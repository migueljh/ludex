import pg from "pg";
import type { Pool } from "pg";
import type {
  BattleRecord,
  BattleTurnRecord,
  Dataset,
  StepState,
  TrajectoryRecord,
  TrajectoryStepRecord,
} from "./types.js";

export const READ_QUERIES = {
  battles: `SELECT id, battle_tag, format, p1, p2, winner
            FROM battles
            ORDER BY id`,
  turns: `SELECT battle_id, player_side, turn_number, protocol_lines
          FROM battle_turns
          ORDER BY battle_id, player_side, turn_number`,
  trajectories: `SELECT t.id, t.battle_id, g.gen_number, t.format,
                         t.player_side, t.final_result
                  FROM trajectories t
                  JOIN generations g ON g.id = t.gen_id
                  ORDER BY t.id`,
  steps: `SELECT trajectory_id, turn_number, decision_index, state,
                 state_schema_version, legal_actions, action_taken,
                 action_source, reward
          FROM trajectory_steps
          ORDER BY trajectory_id, decision_index`,
} as const;

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
  final_result: "win" | "loss" | null;
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
  reward: string | null;
}

function filterByGen(dataset: Dataset, gen: number): Dataset {
  const trajectories = dataset.trajectories.filter(
    (trajectory) => trajectory.gen === gen,
  );
  const trajectoryIds = new Set(trajectories.map((trajectory) => trajectory.id));
  const battleIds = new Set(trajectories.map((trajectory) => trajectory.battleId));
  return {
    battles: dataset.battles.filter((battle) => battleIds.has(battle.id)),
    turns: dataset.turns.filter((turn) => battleIds.has(turn.battleId)),
    trajectories,
    steps: dataset.steps.filter((step) => trajectoryIds.has(step.trajectoryId)),
  };
}

export async function loadDataset(pool: Pool, gen?: number): Promise<Dataset> {
  if (gen !== undefined && (!Number.isInteger(gen) || gen < 1)) {
    throw new Error(`gen debe ser un entero positivo; recibido: ${gen}`);
  }
  const [battleResult, turnResult, trajectoryResult, stepResult] = await Promise.all([
    pool.query<BattleRow>(READ_QUERIES.battles),
    pool.query<TurnRow>(READ_QUERIES.turns),
    pool.query<TrajectoryRow>(READ_QUERIES.trajectories),
    pool.query<StepRow>(READ_QUERIES.steps),
  ]);

  const battles: BattleRecord[] = battleResult.rows.map((row) => ({
    id: row.id,
    battleTag: row.battle_tag,
    format: row.format,
    p1: row.p1,
    p2: row.p2,
    winner: row.winner,
  }));
  const turns: BattleTurnRecord[] = turnResult.rows.map((row) => ({
    battleId: row.battle_id,
    playerSide: row.player_side,
    turnNumber: row.turn_number,
    protocolLines: row.protocol_lines,
  }));
  const trajectories: TrajectoryRecord[] = trajectoryResult.rows.map((row) => ({
    id: row.id,
    battleId: row.battle_id,
    gen: row.gen_number,
    format: row.format,
    playerSide: row.player_side,
    finalResult: row.final_result,
  }));
  const steps: TrajectoryStepRecord[] = stepResult.rows.map((row) => ({
    trajectoryId: row.trajectory_id,
    turnNumber: row.turn_number,
    decisionIndex: row.decision_index,
    state: row.state,
    stateSchemaVersion: row.state_schema_version,
    legalActions: row.legal_actions,
    actionTaken: row.action_taken,
    actionSource: row.action_source,
    reward: row.reward === null ? null : Number(row.reward),
  }));

  const dataset = { battles, turns, trajectories, steps };
  return gen === undefined ? dataset : filterByGen(dataset, gen);
}
