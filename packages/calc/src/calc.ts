import { Generations, Pokemon, Move, Field, calculate, toID } from "@smogon/calc";
import type { GenerationNum, State } from "@smogon/calc";

/**
 * Capa pura del servicio: recibe un descriptor JSON-able, valida contra los
 * datos del paquete para esa generacion y devuelve el resultado normalizado.
 * No sabe nada de HTTP ni de Ludex.
 */

export const SUPPORTED_GENS = [1, 2, 3, 4, 5, 6, 7, 8, 9] as const;

export class CalcError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status = 400,
  ) {
    super(message);
    this.name = "CalcError";
  }
}

// Declaracion `function`, no arrow asignada a const: el analisis de flujo de
// TS solo trata las llamadas never-returning como punto de corte (y por tanto
// refina los tipos despues del if) cuando la funcion esta declarada asi.
function fail(code: string, message: string): never {
  throw new CalcError(code, message);
}

const STATS = ["hp", "atk", "def", "spa", "spd", "spe"] as const;
export type StatName = (typeof STATS)[number];

const STATUSES = ["brn", "par", "slp", "frz", "psn", "tox"] as const;
export type StatusCondition = (typeof STATUSES)[number];

// Ojo: el paquete escribe 'Harsh Sunshine'. 'Harsh Sun' no lanza error pero
// Field lo ignora en silencio y el clima no se aplica (medido: Flamethrower
// 132-156 sin boost contra 198-234 con boost). Es la clase de error que este
// servicio existe para evitar, asi que el contrato solo admite el string exacto.
//
// Y el allowlist esta GATEADO POR GENERACION (medido contra @smogon/calc@0.11.0,
// ver kimi-calc.md): el paquete ignora en silencio valores que no existen en
// la mecanica de esa gen, y aceptar esos strings seria devolver "sin clima"
// disfrazado de "con clima". Regla: nunca aceptar un string que el paquete
// ignora en esa gen.
const ALL_GENS = [1, 2, 3, 4, 5, 6, 7, 8, 9] as const;
const WEATHER_GENS: Record<string, { gens: readonly number[]; rejectedHint?: string }> = {
  // Rain/Sun/Sand: el paquete los calcula (o son correctamente neutros) en
  // todas las gens y nunca cambiaron de nombre.
  "Rain": { gens: ALL_GENS },
  "Sun": { gens: ALL_GENS },
  "Sand": { gens: ALL_GENS },
  // Hail: no modifica el daño en gens 1-8 (correcto). En gen 9 el granizo se
  // renombro a Snow y el paquete IGNORA 'Hail' (medido: EQ 192-226 igual que
  // sin clima).
  "Hail": {
    gens: [1, 2, 3, 4, 5, 6, 7, 8],
    rejectedHint: "en gen 9 el granizo se llama 'Snow' (y da +50% de Defensa al tipo Hielo)",
  },
  // Snow: el clima con boost de Defensa existe solo en gen 9. El paquete lo
  // ignora en gens 3-6 y en 7-8 aplica un boost que en esos juegos no existia
  // bajo ese nombre; el contrato lo admite unicamente donde es real.
  "Snow": {
    gens: [9],
    rejectedHint: "existe a partir de gen 9; en esta generacion el granizo es 'Hail' (sin boost de Defensa)",
  },
  // Climas primordiales (ORAS): el paquete los ignora en gens 1-4 y los
  // calcula honestamente en gens 5-9 (medido). Se aceptan donde los calcula.
  "Harsh Sunshine": { gens: [5, 6, 7, 8, 9] },
  "Heavy Rain": { gens: [5, 6, 7, 8, 9] },
  "Strong Winds": { gens: [5, 6, 7, 8, 9] },
};
const WEATHERS = Object.keys(WEATHER_GENS) as (keyof typeof WEATHER_GENS)[];
// Terrenos: el paquete los ignora en gens 1-4 y los calcula en gens 5-9 (medido).
const TERRAIN_GENS: readonly number[] = [5, 6, 7, 8, 9];
const TERRAINS = ["Electric", "Grassy", "Psychic", "Misty"] as const;
const GAME_TYPES = ["Singles", "Doubles"] as const;

// Espeja los flags del Side del paquete (ver inspeccion en kimi-calc.md).
const SIDE_BOOL_FLAGS = [
  "steelsurge", "vinelash", "wildfire", "cannonade", "volcalith",
  "isSR", "isReflect", "isLightScreen", "isProtected", "isSeeded",
  "isSaltCured", "isForesight", "isTailwind", "isHelpingHand",
  "isFlowerGift", "isPowerTrick", "isFriendGuard", "isAuroraVeil",
  "isBattery", "isPowerSpot", "isSteelySpirit",
] as const;

export interface PokemonDescriptor {
  species: string;
  level?: number;
  nature?: string;
  evs?: Partial<Record<StatName, number>>;
  ivs?: Partial<Record<StatName, number>>;
  ability?: string;
  item?: string;
  status?: StatusCondition;
  boosts?: Partial<Record<StatName, number>>;
  curHP?: number;
  gender?: "M" | "F" | "N";
}

export interface MoveDescriptor {
  name: string;
  isCrit?: boolean;
}

export type SideDescriptor = Record<string, boolean | number | string>;

export interface FieldDescriptor {
  gameType?: (typeof GAME_TYPES)[number];
  weather?: (typeof WEATHERS)[number];
  terrain?: (typeof TERRAINS)[number];
  isGravity?: boolean;
  attackerSide?: SideDescriptor;
  defenderSide?: SideDescriptor;
}

export interface CalcRequest {
  gen: number;
  attacker: PokemonDescriptor;
  defender: PokemonDescriptor;
  move: MoveDescriptor;
  field?: FieldDescriptor;
}

export interface CalcResponse {
  /** Un array de rolls por golpe: [[16 rolls]] normal, [[50]] daño fijo, [[0]] sin efecto, N arrays si multi-golpe. */
  damage_rolls: number[][];
  min_damage: number;
  max_damage: number;
  /** Truncado a 1 decimal contra maxHP del defensor, igual que el desc() del paquete. */
  min_percent: number;
  max_percent: number;
  /** null cuando el paquete no puede calcularlo (daño 0: inmunidad o movimiento de estado). */
  ko_chance: { chance: number | undefined; n: number; text: string } | null;
  description: string;
  defender_hp: { cur: number; max: number };
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function checkKeys(obj: Record<string, unknown>, allowed: readonly string[], path: string): void {
  for (const k of Object.keys(obj)) {
    if (!allowed.includes(k)) fail("invalid_request", `${path}: clave desconocida '${k}'`);
  }
}

const STAT_RANGES = {
  // El paquete indexa una tabla fija de 7 stages con el boost, sin clampear:
  // fuera de -6..6 revienta con TypeError (que el server mapearia a un 500
  // opaco). Un boost fuera de rango es un bug plausible del agente (Danza
  // Espada + Intimidacion mal acumuladas), asi que el error tiene que ser claro.
  boosts: { min: -6, max: 6 },
  // Fuera de rango no revienta: MIENTE (evs {atk: 999999} daba max_percent
  // 8839.9 sin error). Rangos del juego.
  evs: { min: 0, max: 252 },
  ivs: { min: 0, max: 31 },
} as const;

function checkStatTable(
  v: unknown,
  path: string,
  kind: keyof typeof STAT_RANGES,
): Partial<Record<StatName, number>> | undefined {
  if (v === undefined) return undefined;
  if (!isObj(v)) fail("invalid_request", `${path} debe ser un objeto {hp?, atk?, ...}`);
  checkKeys(v, STATS, path);
  const { min, max } = STAT_RANGES[kind];
  for (const [k, n] of Object.entries(v)) {
    if (typeof n !== "number" || !Number.isFinite(n)) {
      fail("invalid_request", `${path}.${k} debe ser un numero, recibido: ${JSON.stringify(n)}`);
    }
    if (!Number.isInteger(n) || n < min || n > max) {
      fail("invalid_request", `${path}.${k} debe ser un entero entre ${min} y ${max}; recibido ${n}`);
    }
  }
  return v as Partial<Record<StatName, number>>;
}

function optString(v: unknown, path: string): string | undefined {
  if (v === undefined) return undefined;
  if (typeof v !== "string" || v.length === 0) fail("invalid_request", `${path} debe ser un string no vacio`);
  return v;
}

function optBool(v: unknown, path: string): boolean | undefined {
  if (v === undefined) return undefined;
  if (typeof v !== "boolean") fail("invalid_request", `${path} debe ser boolean`);
  return v;
}

function buildPokemon(
  gen: ReturnType<typeof Generations.get>,
  genNum: number,
  raw: unknown,
  side: "attacker" | "defender",
): Pokemon {
  if (!isObj(raw)) fail("invalid_request", `${side} es requerido y debe ser un objeto`);
  checkKeys(raw, ["species", "level", "nature", "evs", "ivs", "ability", "item", "status", "boosts", "curHP", "gender"], side);

  const speciesName = optString(raw.species, `${side}.species`);
  if (speciesName === undefined) fail("invalid_request", `${side}.species es requerido`);
  // Ojo: los objetos de data del paquete NO tienen `exists` (eso es del
  // wrapper Pokemon); la existencia se juzga por si get() devolvio algo.
  const species = gen.species.get(toID(speciesName));
  if (!species) {
    fail("unknown_species", `unknown species '${speciesName}' for gen ${genNum} (${side})`);
  }

  let level: number | undefined;
  if (raw.level !== undefined) {
    if (typeof raw.level !== "number" || !Number.isInteger(raw.level) || raw.level < 1 || raw.level > 100) {
      fail("invalid_request", `${side}.level debe ser un entero entre 1 y 100`);
    }
    level = raw.level;
  }

  const natureName = optString(raw.nature, `${side}.nature`);
  const nature = natureName === undefined ? undefined : gen.natures.get(toID(natureName));
  if (natureName !== undefined && !nature) {
    fail("unknown_nature", `unknown nature '${natureName}' (${side})`);
  }

  const abilityName = optString(raw.ability, `${side}.ability`);
  const ability = abilityName === undefined ? undefined : gen.abilities.get(toID(abilityName));
  if (abilityName !== undefined && !ability) {
    fail("unknown_ability", `unknown ability '${abilityName}' for gen ${genNum} (${side})`);
  }

  const itemName = optString(raw.item, `${side}.item`);
  const item = itemName === undefined ? undefined : gen.items.get(toID(itemName));
  if (itemName !== undefined && !item) {
    fail("unknown_item", `unknown item '${itemName}' for gen ${genNum} (${side})`);
  }

  const status = optString(raw.status, `${side}.status`);
  if (status !== undefined && !(STATUSES as readonly string[]).includes(status)) {
    fail("invalid_request", `${side}.status debe ser uno de ${STATUSES.join(", ")}; recibido '${status}'`);
  }

  const gender = optString(raw.gender, `${side}.gender`);
  if (gender !== undefined && !["M", "F", "N"].includes(gender)) {
    fail("invalid_request", `${side}.gender debe ser 'M', 'F' o 'N'`);
  }

  if (raw.curHP !== undefined && (typeof raw.curHP !== "number" || !Number.isFinite(raw.curHP) || raw.curHP < 0)) {
    fail("invalid_request", `${side}.curHP debe ser un numero >= 0`);
  }

  return new Pokemon(gen, species.name, {
    level,
    nature: nature?.name,
    evs: checkStatTable(raw.evs, `${side}.evs`, "evs"),
    ivs: checkStatTable(raw.ivs, `${side}.ivs`, "ivs"),
    boosts: checkStatTable(raw.boosts, `${side}.boosts`, "boosts"),
    ability: ability?.name,
    item: item?.name,
    status: status as PokemonDescriptor["status"],
    curHP: raw.curHP as number | undefined,
    gender: gender as PokemonDescriptor["gender"],
  });
}

function buildMove(gen: ReturnType<typeof Generations.get>, genNum: number, raw: unknown): Move {
  if (!isObj(raw)) fail("invalid_request", "move es requerido y debe ser un objeto {name, isCrit?}");
  checkKeys(raw, ["name", "isCrit"], "move");
  const name = optString(raw.name, "move.name");
  if (name === undefined) fail("invalid_request", "move.name es requerido");
  const move = gen.moves.get(toID(name));
  if (!move) fail("unknown_move", `unknown move '${name}' for gen ${genNum}`);
  return new Move(gen, move.name, { isCrit: optBool(raw.isCrit, "move.isCrit") });
}

function buildSide(raw: unknown, path: string): SideDescriptor | undefined {
  if (raw === undefined) return undefined;
  if (!isObj(raw)) fail("invalid_request", `${path} debe ser un objeto de flags`);
  for (const [k, v] of Object.entries(raw)) {
    if ((SIDE_BOOL_FLAGS as readonly string[]).includes(k)) {
      if (typeof v !== "boolean") fail("invalid_request", `${path}.${k} debe ser boolean`);
    } else if (k === "spikes") {
      if (typeof v !== "number" || !Number.isInteger(v) || v < 0 || v > 3) {
        fail("invalid_request", `${path}.spikes debe ser un entero entre 0 y 3`);
      }
    } else if (k === "isSwitching") {
      if (v !== "in" && v !== "out") fail("invalid_request", `${path}.isSwitching debe ser 'in' u 'out'`);
    } else {
      fail("invalid_request", `${path}: flag desconocido '${k}'`);
    }
  }
  return raw as SideDescriptor;
}

function buildField(raw: unknown, genNum: number): Field | undefined {
  if (raw === undefined) return undefined;
  if (!isObj(raw)) fail("invalid_request", "field debe ser un objeto");
  checkKeys(raw, ["gameType", "weather", "terrain", "isGravity", "attackerSide", "defenderSide"], "field");

  const gameType = optString(raw.gameType, "field.gameType");
  if (gameType !== undefined && !(GAME_TYPES as readonly string[]).includes(gameType)) {
    fail("invalid_request", `field.gameType debe ser uno de ${GAME_TYPES.join(", ")}; recibido '${gameType}'`);
  }
  const weather = optString(raw.weather, "field.weather");
  if (weather !== undefined) {
    const entry = WEATHER_GENS[weather];
    if (!entry) {
      fail("invalid_request", `field.weather debe ser uno de ${WEATHERS.join(", ")}; recibido '${weather}'`);
    }
    if (!entry.gens.includes(genNum)) {
      fail("invalid_request",
        `field.weather '${weather}' no aplica en gen ${genNum}: ${entry.rejectedHint ?? "el paquete lo ignora en esta generacion (medido: no tiene ningun efecto)"}`);
    }
  }
  const terrain = optString(raw.terrain, "field.terrain");
  if (terrain !== undefined) {
    if (!(TERRAINS as readonly string[]).includes(terrain)) {
      fail("invalid_request", `field.terrain debe ser uno de ${TERRAINS.join(", ")}; recibido '${terrain}'`);
    }
    if (!TERRAIN_GENS.includes(genNum)) {
      fail("invalid_request",
        `field.terrain '${terrain}' no aplica en gen ${genNum}: el paquete ignora los terrenos antes de gen 5 (medido: no tienen ningun efecto)`);
    }
  }

  // Partial<State.Field> es el tipo exacto del constructor del paquete; los
  // strings ya pasaron la validacion contra su dominio.
  const options: Partial<State.Field> = {
    gameType: gameType as State.Field["gameType"],
    weather: weather as State.Field["weather"],
    terrain: terrain as State.Field["terrain"],
    isGravity: optBool(raw.isGravity, "field.isGravity"),
    attackerSide: buildSide(raw.attackerSide, "field.attackerSide") as State.Side,
    defenderSide: buildSide(raw.defenderSide, "field.defenderSide") as State.Side,
  };
  return new Field(options);
}

/** El paquete TRUNCA a 1 decimal (301/461 = 65.29 -> 65.2); medido, no deducido. */
const percent = (damage: number, maxHP: number): number =>
  maxHP <= 0 ? 0 : Math.floor((damage / maxHP) * 1000) / 10;

export function runCalc(request: unknown): CalcResponse {
  if (!isObj(request)) fail("invalid_request", "el body debe ser un objeto {gen, attacker, defender, move, field?}");
  checkKeys(request, ["gen", "attacker", "defender", "move", "field"], "body");

  const genNum = request.gen;
  if (typeof genNum !== "number" || !Number.isInteger(genNum) ||
      !(SUPPORTED_GENS as readonly number[]).includes(genNum)) {
    fail("invalid_gen", `gen debe ser un entero entre 1 y 9; recibido: ${JSON.stringify(genNum)}`);
  }
  const gen = Generations.get(genNum as GenerationNum);

  // La validacion de arriba cubre la entrada; lo que el paquete rechace con un
  // Error propio (p. ej. 'Special Attack and Special Defense must match before
  // Gen 3' si spa != spd en gens 1-2) es un error del CLIENTE y se devuelve
  // como 400 con el mensaje del paquete. Los TypeError son bugs (del paquete o
  // nuestros) y siguen yendo al 500 del server, logueados.
  let result: ReturnType<typeof calculate>;
  let defender: Pokemon;
  try {
    const attacker = buildPokemon(gen, genNum, request.attacker, "attacker");
    defender = buildPokemon(gen, genNum, request.defender, "defender");
    const move = buildMove(gen, genNum, request.move);
    const field = buildField(request.field, genNum);

    result = field
      ? calculate(gen, attacker, defender, move, field)
      : calculate(gen, attacker, defender, move);
  } catch (e) {
    if (e instanceof CalcError || e instanceof TypeError) throw e;
    if (e instanceof Error) {
      throw new CalcError("invalid_request", `combinacion rechazada por el paquete: ${e.message}`);
    }
    throw e;
  }

  // damage puede ser: escalar (0 o daño fijo), number[] (16 rolls de un golpe)
  // o number[][] (un array por golpe en multi-golpe). Se normaliza a number[][].
  const raw = result.damage;
  const rolls: number[][] =
    typeof raw === "number" ? [[raw]]
    : Array.isArray(raw[0]) ? (raw as number[][])
    : [raw as number[]];

  const [minDamage, maxDamage] = result.range();
  const maxHP = defender.maxHP();

  // El paquete lanza 'damage[damage.length - 1] === 0' para daño 0 con
  // movimiento dañino (inmunidad), y tambien en kochance() para cualquier
  // daño 0 (incluidos los de estado). Se captura y se responde honesto.
  let description: string;
  try {
    description = result.desc();
  } catch {
    const d = result.rawDesc;
    description = `${d.attackerName} ${d.moveName} vs. ${d.defenderName}: 0-0 (0 - 0%)`;
  }
  let koChance: CalcResponse["ko_chance"] = null;
  try {
    koChance = result.kochance();
  } catch { /* daño 0: no hay KO chance que calcular */ }

  return {
    damage_rolls: rolls,
    min_damage: minDamage,
    max_damage: maxDamage,
    min_percent: percent(minDamage, maxHP),
    max_percent: percent(maxDamage, maxHP),
    ko_chance: koChance,
    description,
    defender_hp: { cur: defender.curHP(), max: maxHP },
  };
}
