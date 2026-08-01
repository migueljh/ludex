/** Alcance de la auditoría.
 *
 * `all` audita TODO lo grabado, incluidas las filas `source='test'`.
 * `training` audita sólo lo que un consumidor del dataset de entrenamiento
 * tiene permitido leer (ver `TRAINING_ELIGIBILITY` en `scope.ts`).
 */
export type Scope = "all" | "training";

export const SCOPES: readonly Scope[] = ["all", "training"] as const;

/** Versiones de `state_schema_version` soportadas e interpretables.
 *
 * v1 y v2 comparten la MISMA forma de estado (las 11 claves rivales están en
 * las dos); v2 (`1d49b77`, D31) corrige la exactitud del lado rival, no su
 * estructura. Por eso conviven en un mismo corpus. Una versión fuera de este
 * conjunto no es interpretable por este auditor y se rechaza.
 */
export const SUPPORTED_STATE_VERSIONS: readonly number[] = [1, 2] as const;

export type BattleSource = "challenge" | "ladder" | "local" | "import" | "test";

export interface BattleRecord {
  id: number;
  battleTag: string;
  format: string;
  p1: string;
  p2: string;
  winner: string | null;
  source: BattleSource;
  playedBy: string;
}

export interface BattleTurnRecord {
  battleId: number;
  playerSide: string;
  turnNumber: number;
  protocolLines: string[];
}

/** El enum real es `battle_result AS ENUM ('win','loss','tie')` y la columna
 * es nullable: `NULL` significa "la batalla no terminó", no "empate". */
export type FinalResult = "win" | "loss" | "tie";

export interface TrajectoryRecord {
  id: number;
  battleId: number;
  gen: number;
  format: string;
  playerSide: string;
  finalResult: FinalResult | null;
}

/** Las 11 claves observables del rival, en el orden de
 * `serializer.py::_pokemon(mine=False)`. `stats` es la clave 12 y existe sólo
 * para `mine=True`: que NO aparezca acá es en sí una invariante. */
export const OPPONENT_FIELDS = [
  "species",
  "hp_fraction",
  "active",
  "fainted",
  "status",
  "level",
  "item",
  "ability",
  "types",
  "boosts",
  "moves",
] as const;

export type OpponentField = (typeof OPPONENT_FIELDS)[number];

/** Claves que el serializador produce SÓLO para el lado propio. Si alguna
 * aparece del lado rival es una fuga por definición. */
export const OWN_ONLY_FIELDS = ["stats"] as const;

export interface OpponentPokemonState {
  species?: unknown;
  [key: string]: unknown;
}

export interface StepState {
  turn?: unknown;
  schema_version?: unknown;
  /** Nuestro propio lado. No es fuga: es lo que un Transform puede copiar. */
  me?: {
    pokemon?: OpponentPokemonState[];
    [key: string]: unknown;
  };
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
  actionPath: string | null;
  reward: number | null;
}

/** Entrada del dex local (tabla `pokemon`). Nunca se consulta internet. */
export interface DexPokemon {
  gen: number;
  showdownId: string;
  baseSpecies: string;
  forme: string | null;
  types: string[];
  abilities: string[];
}

export interface DexMove {
  gen: number;
  showdownId: string;
  pp: number | null;
}

export interface Dataset {
  battles: BattleRecord[];
  turns: BattleTurnRecord[];
  trajectories: TrajectoryRecord[];
  steps: TrajectoryStepRecord[];
  dexPokemon: DexPokemon[];
  dexMoves: DexMove[];
}

export type InvariantName =
  | "hidden_information"
  | "action_in_mask"
  | "action_turn"
  | "decision_index"
  | "state_rederivable"
  | "reward_propagation"
  | "schema_version"
  | "orphans";

export const INVARIANTS: readonly InvariantName[] = [
  "hidden_information",
  "action_in_mask",
  "action_turn",
  "decision_index",
  "state_rederivable",
  "reward_propagation",
  "schema_version",
  "orphans",
] as const;

export interface Violation {
  invariant: InvariantName;
  /** Sólo para `hidden_information`: cuál de las 11 claves rivales falló. */
  field?: OpponentField;
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

export interface OpponentFieldCheck {
  name: OpponentField;
  violations: number;
}

/** Contadores de trabajo real. Existen para que un canario pueda demostrar
 * que el costo NO crece con `trajectory_steps` (ver `test/cost.test.ts`) y
 * que el auditor efectivamente recorrió algo (un loop que no itera nunca
 * "pasa" sin verificar nada). */
export interface AuditStats {
  /** Pasos efectivamente auditados. Si es 0 el auditor no verificó nada. */
  stepsAudited: number;
  /** Entradas de pokémon rival efectivamente auditadas. */
  opponentEntriesAudited: number;
  /** Chequeos de campo rival efectivamente ejecutados (11 por entrada). */
  opponentFieldChecksRun: number;
  /** Líneas de protocolo leídas para construir el índice. Constante respecto
   * de la cantidad de pasos: cada línea del corpus se lee exactamente una vez. */
  protocolLinesScanned: number;
}

export interface AuditResult {
  checks: InvariantCheck[];
  opponentFields: OpponentFieldCheck[];
  violations: Violation[];
  stats: AuditStats;
}
