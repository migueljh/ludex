export interface BattleRecord {
  id: number;
  battleTag: string;
  format: string;
  p1: string;
  p2: string;
  winner: string | null;
}

export interface BattleTurnRecord {
  battleId: number;
  playerSide: string;
  turnNumber: number;
  protocolLines: string[];
}

export interface TrajectoryRecord {
  id: number;
  battleId: number;
  gen: number;
  format: string;
  playerSide: string;
  finalResult: "win" | "loss" | null;
}

export interface OpponentPokemonState {
  species?: unknown;
  [key: string]: unknown;
}

export interface StepState {
  turn?: unknown;
  schema_version?: unknown;
  opponent?: {
    pokemon?: OpponentPokemonState[];
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface TrajectoryStepRecord {
  trajectoryId: number;
  turnNumber: number;
  decisionIndex: number;
  state: StepState;
  stateSchemaVersion: number;
  legalActions: unknown[];
  actionTaken: Record<string, unknown> | null;
  actionSource: string;
  reward: number | null;
}

export interface Dataset {
  battles: BattleRecord[];
  turns: BattleTurnRecord[];
  trajectories: TrajectoryRecord[];
  steps: TrajectoryStepRecord[];
}

export type InvariantName =
  | "hidden_information"
  | "action_turn"
  | "state_rederivable"
  | "reward_propagation"
  | "schema_version"
  | "orphans";

export interface Violation {
  invariant: InvariantName;
  detail: string;
  battleTag?: string;
  playerSide?: string;
  turnNumber?: number;
  decisionIndex?: number;
}

export interface InvariantCheck {
  name: InvariantName;
  violations: number;
}

export interface AuditResult {
  checks: InvariantCheck[];
  violations: Violation[];
}
