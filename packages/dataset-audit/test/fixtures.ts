/** Fixture positivo y sus mutaciones negativas.
 *
 * El fixture base es un dataset MÍNIMO pero completo cuyo estado está
 * respaldado, campo por campo, por líneas de protocolo reales y por un dex
 * local reducido. Cada test negativo parte de acá y cambia UNA sola cosa: si
 * un cambio dispara la violación de otra invariante, el fixture está mal
 * construido y el canario de `invariants.test.ts` lo detecta.
 */

import type {
  Dataset,
  DexMove,
  DexPokemon,
  OpponentPokemonState,
  TrajectoryStepRecord,
} from "../src/types.js";

export const BATTLE_ID = 10;
export const TRAJECTORY_ID = 20;

/** Dex reducido, con los valores tomados del dex real de gen 6:
 * `SELECT showdown_id, base_species, forme, types, abilities FROM pokemon`. */
export function dexPokemon(): DexPokemon[] {
  return [
    {
      gen: 6, showdownId: "mrmime", baseSpecies: "mrmime", forme: null,
      types: ["Psychic", "Fairy"],
      abilities: ["Soundproof", "Filter", "Technician"],
    },
    {
      gen: 6, showdownId: "furfrou", baseSpecies: "furfrou", forme: null,
      types: ["Normal"], abilities: ["Fur Coat"],
    },
    {
      gen: 6, showdownId: "charizard", baseSpecies: "charizard", forme: null,
      types: ["Fire", "Flying"], abilities: ["Blaze", "Solar Power"],
    },
    {
      gen: 6, showdownId: "charizardmegax", baseSpecies: "charizard", forme: "Mega-X",
      types: ["Fire", "Dragon"], abilities: ["Tough Claws"],
    },
    {
      gen: 6, showdownId: "gengar", baseSpecies: "gengar", forme: null,
      types: ["Ghost", "Poison"], abilities: ["Levitate"],
    },
    {
      gen: 6, showdownId: "lapras", baseSpecies: "lapras", forme: null,
      types: ["Water", "Ice"], abilities: ["Water Absorb", "Shell Armor", "Hydration"],
    },
    {
      gen: 6, showdownId: "ditto", baseSpecies: "ditto", forme: null,
      types: ["Normal"], abilities: ["Limber", "Imposter"],
    },
    // Ability única: el dex la determina y decirla no es fuga. Además es la
    // única forma de tener Pressure enfrente sin que nadie la narre.
    {
      gen: 6, showdownId: "dusknoir", baseSpecies: "dusknoir", forme: null,
      types: ["Ghost"], abilities: ["Pressure"],
    },
  ];
}

/** `Move.max_pp` de poke-env es `pp * 8 // 5`: Psychic tiene 10 PP -> 16.
 *
 * `target` y `flags` son los del dex real: con ellos `_pressure_on` es exacto
 * y el PP esperado es un número, no un rango. El dex de gen 6 no tiene
 * `hiddenpowerice`: `Move.retrieve_id` colapsa los 17 en `hiddenpower`. */
export function dexMoves(): DexMove[] {
  return [
    { gen: 6, showdownId: "psychic", pp: 10, target: "normal", flags: [] },
    { gen: 6, showdownId: "hiddenpower", pp: 15, target: "normal", flags: [] },
    { gen: 6, showdownId: "shadowball", pp: 15, target: "normal", flags: [] },
    { gen: 6, showdownId: "spore", pp: 15, target: "normal", flags: [] },
    { gen: 6, showdownId: "transform", pp: 10, target: "normal", flags: [] },
    { gen: 6, showdownId: "swordsdance", pp: 20, target: "self", flags: [] },
    { gen: 6, showdownId: "trick", pp: 10, target: "normal", flags: [] },
    { gen: 6, showdownId: "copycat", pp: 20, target: "self", flags: [] },
    { gen: 6, showdownId: "sleeptalk", pp: 10, target: "self", flags: [] },
    { gen: 6, showdownId: "rest", pp: 10, target: "self", flags: [] },
    { gen: 6, showdownId: "outrage", pp: 10, target: "randomNormal", flags: [] },
    { gen: 6, showdownId: "petaldance", pp: 10, target: "randomNormal", flags: [] },
    { gen: 6, showdownId: "reflecttype", pp: 15, target: "normal", flags: [] },
  ];
}

// `|turn|N` ABRE el bloque del turno N (`ProtocolRecorder.record`), así que
// cada bloque arranca con su propia línea de turno y contiene la narración de
// lo que pasó DURANTE ese turno.
export const PROTOCOL_TURN_0 = [
  "|rule|HP Percentage Mod: HP is shown in percentages",
  "|switch|p1a: Gengar|Gengar, L80, M|280/280",
  "|switch|p2a: Furfrou|Furfrou-Pharaoh, L85, F|100/100",
];

export const PROTOCOL_TURN_1 = [
  "|turn|1",
  "|switch|p2a: Mimien|Mr. Mime, L82, M|100/100",
  "|-ability|p2a: Mimien|Soundproof",
  "|move|p2a: Mimien|Psychic|p1a: Gengar",
  "|move|p2a: Mimien|Hidden Power|p1a: Gengar",
  "|-status|p2a: Mimien|par",
  "|-boost|p2a: Mimien|atk|1",
  "|-damage|p2a: Mimien|55/100 par",
  "|-heal|p2a: Mimien|55/100 par|[from] item: Leftovers",
];

// El paso auditado vive en el turno 2, DESPUÉS de que el turno 1 dejó todo
// resuelto: así "quién está activo" y "qué status tiene" tienen una única
// respuesta admisible y una mutación no puede escudarse en el estado
// intermedio de su propio turno.
export const PROTOCOL_TURN_2 = [
  "|turn|2",
  "|upkeep",
];

/** La entrada que auditan los tests de los 11 campos. Todo lo que afirma está
 * respaldado por `PROTOCOL_TURN_1` o por el dex reducido. */
export function auditedOpponent(): OpponentPokemonState {
  return {
    species: "mrmime",
    hp_fraction: 0.55,
    active: true,
    fainted: false,
    status: "PAR",
    level: 82,
    item: "leftovers",
    ability: "soundproof",
    types: ["PSYCHIC", "FAIRY"],
    boosts: { accuracy: 0, atk: 1, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 },
    // Un uso narrado de cada uno, sin Pressure enfrente: el PP es exacto.
    moves: [
      { id: "psychic", pp: 15, max_pp: 16 },
      { id: "hiddenpower", pp: 23, max_pp: 24 },
    ],
  };
}

/** Nuestro propio lado. No es fuga —ya lo tenemos— y es exactamente lo que un
 * Transform copia, así que el auditor lo necesita para poder exigir que un
 * Transform sólo acepte los datos que realmente copió. */
export function ownTeam(): OpponentPokemonState[] {
  return [{
    species: "gengar",
    hp_fraction: 1,
    active: true,
    fainted: false,
    status: null,
    level: 80,
    item: "lifeorb",
    ability: "levitate",
    types: ["GHOST", "POISON"],
    boosts: { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 },
    moves: [{ id: "shadowball", pp: 24, max_pp: 24 }],
    stats: { atk: 120, def: 130, spa: 240, spd: 150, spe: 220 },
  }];
}

/** Segunda entrada: forma COSMÉTICA (ausente del dex local) con la ability
 * determinada por el dex de su especie base. Ejerce las dos inferencias
 * legítimas a la vez. */
export function benchedOpponent(): OpponentPokemonState {
  return {
    species: "furfroupharaoh",
    hp_fraction: 1,
    active: false,
    fainted: false,
    status: null,
    level: 85,
    item: "unknown_item",
    ability: "furcoat",
    types: ["NORMAL"],
    boosts: { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 },
    moves: [],
  };
}

const LEGAL_ACTIONS = [
  { kind: "move", id: "shadowball" },
  { kind: "move", id: "shadowball", mega: true },
  { kind: "switch", species: "charizard" },
];

function step(
  decisionIndex: number,
  turnNumber: number,
  opponent: OpponentPokemonState[],
): TrajectoryStepRecord {
  return {
    trajectoryId: TRAJECTORY_ID,
    turnNumber,
    decisionIndex,
    state: {
      turn: turnNumber,
      schema_version: 2,
      gen: 6,
      format: "gen6randombattle",
      player_role: "p1",
      legal_actions: LEGAL_ACTIONS,
      me: { pokemon: ownTeam() },
      opponent: { pokemon: opponent },
    },
    stateSchemaVersion: 2,
    legalActions: LEGAL_ACTIONS,
    actionTaken: { kind: "move", id: "shadowball" },
    actionSource: "agent",
    actionPath: "llm",
    reward: 1,
  };
}

export function baseDataset(): Dataset {
  return {
    battles: [{
      id: BATTLE_ID,
      battleTag: "battle-gen6randombattle-fixture",
      format: "gen6randombattle",
      p1: "LudexBot",
      p2: "Rival",
      winner: "LudexBot",
      source: "local",
      playedBy: "bot",
    }],
    turns: [
      { battleId: BATTLE_ID, playerSide: "p1", turnNumber: 0, protocolLines: [...PROTOCOL_TURN_0] },
      { battleId: BATTLE_ID, playerSide: "p1", turnNumber: 1, protocolLines: [...PROTOCOL_TURN_1] },
      { battleId: BATTLE_ID, playerSide: "p1", turnNumber: 2, protocolLines: [...PROTOCOL_TURN_2] },
    ],
    trajectories: [{
      id: TRAJECTORY_ID,
      battleId: BATTLE_ID,
      gen: 6,
      format: "gen6randombattle",
      playerSide: "p1",
      finalResult: "win",
    }],
    steps: [
      step(0, 0, [{ ...benchedOpponent(), active: true }]),
      step(1, 2, [auditedOpponent(), benchedOpponent()]),
    ],
    dexPokemon: dexPokemon(),
    dexMoves: dexMoves(),
  };
}

/** El paso que auditan los tests de campos rivales (`decision_index = 1`). */
export function auditedStep(dataset: Dataset): TrajectoryStepRecord {
  const found = dataset.steps.find((candidate) => candidate.decisionIndex === 1);
  if (found === undefined) throw new Error("el fixture perdió su paso auditado");
  return found;
}

/** El rival auditado, dentro del dataset dado. */
export function auditedOpponentOf(dataset: Dataset): OpponentPokemonState {
  const opponent = auditedStep(dataset).state.opponent?.pokemon;
  if (opponent === undefined) throw new Error("el fixture perdió su rival");
  return opponent[0];
}

/** Añade una línea al protocolo del turno 1: lo que pasa ahí ya está resuelto
 * cuando el paso auditado (turno 2) toma su snapshot. */
export function withProtocolLine(dataset: Dataset, line: string, turnNumber = 1): Dataset {
  const turn = dataset.turns.find((candidate) => candidate.turnNumber === turnNumber);
  if (turn === undefined) throw new Error(`el fixture perdió su turno ${turnNumber}`);
  turn.protocolLines.push(line);
  return dataset;
}

/** El rival auditado, debilitado con su `|faint|` público. Positivo por
 * defecto: `fainted` en `true` es lo que el protocolo dice. */
export function faintedFixture(fainted: boolean): Dataset {
  const dataset = withProtocolLine(baseDataset(), "|faint|p2a: Mimien");
  const audited = auditedOpponentOf(dataset);
  audited.fainted = fainted;
  audited.hp_fraction = 0;
  audited.status = "FNT";
  // `faint()` (`pokemon.py:422-430`) NO desactiva: el debilitado sigue en la
  // ranura hasta que entra su reemplazo.
  audited.active = true;
  return dataset;
}

/** Dataset con UN SOLO rival revelado y una secuencia de protocolo a medida.
 *
 * Sirve para ejercer una regla puntual (una Mega, un `-formechange`, un
 * Transform) sin arrastrar el resto del fixture. El paso auditado vive en el
 * turno 2; `stateTurn` abre la ventana antes para poder ejercer los cursores
 * INTERMEDIOS de un turno. */
export function soloFixture(
  turnOneLines: string[],
  entry: OpponentPokemonState,
  stateTurn = 2,
): Dataset {
  const dataset = baseDataset();
  dataset.turns = [
    { battleId: BATTLE_ID, playerSide: "p1", turnNumber: 0, protocolLines: [
      "|switch|p1a: Gengar|Gengar, L80, M|280/280",
    ] },
    { battleId: BATTLE_ID, playerSide: "p1", turnNumber: 1, protocolLines: ["|turn|1", ...turnOneLines] },
    { battleId: BATTLE_ID, playerSide: "p1", turnNumber: 2, protocolLines: ["|turn|2", "|upkeep"] },
  ];
  const step = dataset.steps[1];
  step.decisionIndex = 0;
  step.state.turn = stateTurn;
  step.state.opponent = { pokemon: [entry] };
  dataset.steps = [step];
  return dataset;
}

/** El rival de `soloFixture`, con todo en su valor de recién revelado. */
export function freshOpponent(
  species: string,
  level: number,
  types: string[],
): OpponentPokemonState {
  return {
    species,
    hp_fraction: 1,
    active: true,
    fainted: false,
    status: null,
    level,
    item: "unknown_item",
    ability: null,
    types,
    boosts: { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 },
    moves: [],
  };
}

/** Devuelve un dataset con UN campo del rival auditado cambiado. */
export function withOpponentField(key: string, value: unknown): Dataset {
  const dataset = baseDataset();
  const opponent = auditedStep(dataset).state.opponent?.pokemon;
  if (opponent === undefined) throw new Error("el fixture perdió su rival");
  if (value === undefined) delete opponent[0][key];
  else opponent[0][key] = value;
  return dataset;
}
