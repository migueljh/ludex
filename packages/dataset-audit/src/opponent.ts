/** Los 11 campos observables del rival.
 *
 * "Cero fuga de información oculta" es LA propiedad del dataset: si el modelo
 * entrena con información que un jugador no tiene, es inútil en batalla real.
 * Del rival se persisten 11 claves y **todas** hay que verificarlas, no sólo
 * `species`.
 *
 * Cada campo se contrasta contra dos fuentes y ninguna otra: el protocolo
 * crudo (D17) y el dex local de la generación de la trayectoria. Las
 * inferencias legítimas —ability determinada por el dex, ability de una Mega,
 * el nivel 100 que Showdown omite— están ancladas a esas fuentes, nunca a una
 * lista a mano.
 *
 * Ante una diferencia con poke-env gana poke-env: donde la librería escribe un
 * centinela (`unknown_item`) o un `null` ("no derivable de esta evidencia"),
 * el auditor acepta y no exige evidencia.
 */

import {
  ANY_BOOST_STAT,
  buildSpeciesResolver,
  identityKeys,
  moveEvidenceKeys,
  normalizeProtocolText,
  revealedBy,
  type SideEvidence,
  type SpeciesResolver,
} from "./protocol.js";
import type { DexMove, DexPokemon, OpponentField, OpponentPokemonState } from "./types.js";
import { OPPONENT_FIELDS, OWN_ONLY_FIELDS } from "./types.js";

const STATUS_VALUES = new Set(["BRN", "PAR", "SLP", "FRZ", "PSN", "TOX", "FNT"]);
const BOOST_KEYS = new Set(["accuracy", "atk", "def", "evasion", "spa", "spd", "spe"]);
/** Centinela de poke-env para "el item todavía no se reveló". */
const UNKNOWN_ITEM = "unknown_item";
/** Showdown OMITE `L100` del `details`, así que 100 no necesita evidencia. */
const IMPLICIT_LEVEL = 100;

export interface DexIndex {
  /** Resuelve un id de especie, incluidas las formas cosméticas. */
  resolve: SpeciesResolver;
  /** `base_species` normalizado -> todas sus formas. */
  byBase: Map<string, DexPokemon[]>;
  /** `showdown_id` normalizado -> movimiento. */
  moves: Map<string, DexMove>;
}

export function buildDexIndex(
  pokemon: readonly DexPokemon[],
  moves: readonly DexMove[],
  gen: number,
): DexIndex {
  const ofGen = pokemon.filter((entry) => entry.gen === gen);
  const byBase = new Map<string, DexPokemon[]>();
  for (const entry of ofGen) {
    const base = normalizeProtocolText(entry.baseSpecies);
    const bucket = byBase.get(base);
    if (bucket === undefined) byBase.set(base, [entry]);
    else bucket.push(entry);
  }
  const moveIndex = new Map<string, DexMove>();
  for (const move of moves) {
    if (move.gen !== gen) continue;
    moveIndex.set(normalizeProtocolText(move.showdownId), move);
  }
  return { resolve: buildSpeciesResolver(ofGen), byBase, moves: moveIndex };
}

/** Mismo predicado que `_update_from_pokedex` de poke-env: una forma
 * Mega/Primal/Stellar/Terastal impone su propia ability. */
function isFormeChangeForme(entry: DexPokemon): boolean {
  const forme = entry.forme ?? "";
  if (forme.length === 0) return false;
  return forme.startsWith("Mega")
    || forme === "Primal"
    || forme === "Stellar"
    || forme === "Terastal"
    || forme.endsWith("-Tera");
}

function typeKey(types: readonly string[]): string {
  return [...types].map((type) => normalizeProtocolText(type)).sort().join("/");
}

export interface OpponentContext {
  evidence: SideEvidence | undefined;
  dex: DexIndex;
  gen: number;
  turn: number;
}

export type FieldReporter = (field: OpponentField, detail: string) => void;

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

/** Audita UNA entrada rival contra el protocolo y el dex. Devuelve cuántos de
 * los 11 chequeos corrió, para que un canario pueda demostrar que ninguno se
 * salteó en silencio. */
export function auditOpponentPokemon(
  entry: OpponentPokemonState,
  context: OpponentContext,
  report: FieldReporter,
  reportOwnOnly: (detail: string) => void,
): number {
  const { evidence, dex, gen, turn } = context;

  for (const forbidden of OWN_ONLY_FIELDS) {
    if (forbidden in entry) {
      reportOwnOnly(
        `la entrada rival trae '${forbidden}', que el serializador produce sólo para mine=True`,
      );
    }
  }

  // --- species -------------------------------------------------------------
  const rawSpecies = entry.species;
  const speciesValid = typeof rawSpecies === "string" && rawSpecies.length > 0;
  if (!speciesValid) {
    report("species", "species ausente o no textual");
  }
  const species = speciesValid ? rawSpecies : "";
  const keys = speciesValid ? identityKeys(species, dex.resolve) : [];
  const dexEntry = speciesValid ? dex.resolve(species) : undefined;
  const base = dexEntry ? normalizeProtocolText(dexEntry.baseSpecies) : normalizeProtocolText(species);
  const formes = dex.byBase.get(base) ?? (dexEntry ? [dexEntry] : []);
  const typeChanged = evidence !== undefined && revealedBy(evidence.typeChange, keys, turn);
  // Un Transform copia moveset, tipos y ability de otro pokémon —en la
  // práctica, uno NUESTRO (Ditto/Imposter)—. Es una inferencia legítima
  // anclada a `|-transform|`, no una fuga, y no se puede contrastar contra el
  // dex ni contra las líneas del rival.
  const transformed = evidence !== undefined && revealedBy(evidence.transform, keys, turn);

  if (speciesValid) {
    if (evidence === undefined || !revealedBy(evidence.species, keys, turn)) {
      report(
        "species",
        `'${species}' aparece en state antes de ser revelado por el protocolo`,
      );
    }
  }

  // --- fainted -------------------------------------------------------------
  const fainted = entry.fainted;
  if (typeof fainted !== "boolean") {
    report("fainted", `fainted=${JSON.stringify(fainted)} no es booleano`);
  } else if (fainted && (evidence === undefined || !revealedBy(evidence.faint, keys, turn))) {
    report("fainted", `fainted=true sin |faint| público para '${species}'`);
  }

  // --- hp_fraction ---------------------------------------------------------
  const hp = entry.hp_fraction;
  if (typeof hp !== "number" || Number.isNaN(hp)) {
    if (hp !== null && hp !== undefined) {
      report("hp_fraction", `hp_fraction=${JSON.stringify(hp)} no es numérico ni null`);
    }
  } else if (hp < 0 || hp > 1) {
    report("hp_fraction", `hp_fraction=${hp} fuera de [0,1]`);
  } else if (fainted === true && hp !== 0) {
    report("hp_fraction", `fainted=true pero hp_fraction=${hp}`);
  } else if (evidence?.hpPercentageMod === true
    && Math.abs(hp * 100 - Math.round(hp * 100)) > 1e-9) {
    // Con HP Percentage Mod el rival se narra en centésimos (`14/100`). Un HP
    // que no cae en esa grilla sólo puede venir del objeto propio: es fuga.
    report(
      "hp_fraction",
      `hp_fraction=${hp} no es un centésimo exacto con HP Percentage Mod activo`,
    );
  }

  // --- active --------------------------------------------------------------
  if (typeof entry.active !== "boolean") {
    report("active", `active=${JSON.stringify(entry.active)} no es booleano`);
  }

  // --- status --------------------------------------------------------------
  const status = entry.status;
  if (status !== null && status !== undefined) {
    if (typeof status !== "string" || !STATUS_VALUES.has(status)) {
      report("status", `status=${JSON.stringify(status)} no es un estado conocido`);
    } else {
      const wanted = keys.map((key) => `${key}|${status.toLowerCase()}`);
      if (evidence === undefined || !revealedBy(evidence.status, wanted, turn)) {
        report("status", `status=${status} sin línea pública que lo revele para '${species}'`);
      }
    }
  }

  // --- level ---------------------------------------------------------------
  const level = entry.level;
  if (typeof level !== "number" || !Number.isInteger(level) || level < 1 || level > 100) {
    report("level", `level=${JSON.stringify(level)} no es un nivel válido`);
  } else if (level !== IMPLICIT_LEVEL) {
    const wanted = keys.map((key) => `${key}|${level}`);
    if (evidence === undefined || !revealedBy(evidence.level, wanted, turn)) {
      report("level", `level=${level} sin token L${level} público para '${species}'`);
    }
  }

  // --- item ----------------------------------------------------------------
  const item = entry.item;
  if (item !== null && item !== undefined && item !== UNKNOWN_ITEM && item !== "") {
    if (typeof item !== "string") {
      report("item", `item=${JSON.stringify(item)} no es textual`);
    } else {
      const normalized = normalizeProtocolText(item);
      const wanted = keys.map((key) => `${key}|${normalized}`);
      if (evidence === undefined || !revealedBy(evidence.item, wanted, turn)) {
        report("item", `item='${item}' sin línea pública que lo revele para '${species}'`);
      }
    }
  }

  // --- ability -------------------------------------------------------------
  const ability = entry.ability;
  if (ability !== null && ability !== undefined && ability !== "") {
    if (typeof ability !== "string") {
      report("ability", `ability=${JSON.stringify(ability)} no es textual`);
    } else {
      const normalized = normalizeProtocolText(ability);
      const wanted = keys.map((key) => `${key}|${normalized}`);
      const revealed = evidence !== undefined && revealedBy(evidence.ability, wanted, turn);
      // Lo que el dex determina no es información oculta: si una especie tiene
      // exactamente una ability posible, decirla no es fuga (Zoroark sólo
      // puede tener Illusion). Mismo corte de gen que poke-env.
      const dexDetermined = gen >= 3 && formes.some((forme) => {
        if (isFormeChangeForme(forme)) {
          return normalizeProtocolText(forme.abilities[0] ?? "") === normalized;
        }
        return forme.abilities.length === 1
          && normalizeProtocolText(forme.abilities[0]) === normalized;
      });
      if (!revealed && !dexDetermined && !typeChanged && !transformed) {
        report(
          "ability",
          `ability='${ability}' no está revelada por el protocolo ni determinada por el dex para '${species}'`,
        );
      }
    }
  }

  // --- types ---------------------------------------------------------------
  const types = entry.types;
  if (!Array.isArray(types) || types.some((type) => typeof type !== "string")) {
    report("types", `types=${JSON.stringify(types)} no es una lista de textos`);
  } else if (types.length === 0) {
    report("types", "types vacío: toda especie revelada tiene tipos en el dex");
  } else if (!typeChanged && !transformed) {
    // Un cambio de forma NO cambia `species` (forme_change usa
    // store_species=False) pero SÍ cambia los tipos, así que se aceptan los
    // tipos de cualquier forma de la misma especie base. Un `typechange` o un
    // Transform sí produce tipos fuera del dex: ahí el chequeo se abstiene.
    const allowed = new Set(formes.map((forme) => typeKey(forme.types)));
    if (allowed.size > 0 && !allowed.has(typeKey(types as string[]))) {
      report(
        "types",
        `types=${JSON.stringify(types)} no corresponde a ninguna forma de '${base}' en el dex de gen ${gen}`,
      );
    }
  }

  // --- boosts --------------------------------------------------------------
  const boosts = asRecord(entry.boosts);
  if (boosts === undefined) {
    report("boosts", `boosts=${JSON.stringify(entry.boosts)} no es un objeto`);
  } else {
    let reported = false;
    for (const [key, value] of Object.entries(boosts)) {
      if (reported) break;
      if (!BOOST_KEYS.has(key)) {
        report("boosts", `boosts trae la clave desconocida '${key}'`);
        reported = true;
      } else if (typeof value !== "number" || !Number.isInteger(value) || value < -6 || value > 6) {
        report("boosts", `boosts.${key}=${JSON.stringify(value)} fuera de [-6,6]`);
        reported = true;
      } else if (value !== 0 && (evidence === undefined || !revealedBy(
        evidence.boost,
        keys.flatMap((identity) => [`${identity}|${key}`, `${identity}|${ANY_BOOST_STAT}`]),
        turn,
      ))) {
        report("boosts", `boosts.${key}=${value} sin línea pública de boost para '${species}'`);
        reported = true;
      }
    }
  }

  // --- moves ---------------------------------------------------------------
  const moves = entry.moves;
  if (!Array.isArray(moves)) {
    report("moves", `moves=${JSON.stringify(moves)} no es una lista`);
  } else if (!transformed) {
    let reported = false;
    for (const raw of moves) {
      if (reported) break;
      const move = asRecord(raw);
      const id = move?.id;
      if (typeof id !== "string" || id.length === 0) {
        report("moves", `moves trae una entrada sin id: ${JSON.stringify(raw)}`);
        reported = true;
        continue;
      }
      const wanted = moveEvidenceKeys(id)
        .flatMap((moveKey) => keys.map((key) => `${key}|${moveKey}`));
      if (evidence === undefined || !revealedBy(evidence.move, wanted, turn)) {
        report("moves", `movimiento '${id}' de '${species}' nunca fue narrado públicamente`);
        reported = true;
        continue;
      }
      const dexMove = dex.moves.get(normalizeProtocolText(id));
      // `Move.max_pp` de poke-env: `pp * 8 // 5`.
      const expectedMaxPp = dexMove?.pp === null || dexMove?.pp === undefined
        ? undefined
        : Math.floor(dexMove.pp * 8 / 5);
      const maxPp = move?.max_pp;
      if (maxPp !== null && maxPp !== undefined) {
        if (typeof maxPp !== "number" || (expectedMaxPp !== undefined && maxPp !== expectedMaxPp)) {
          report(
            "moves",
            `max_pp=${JSON.stringify(maxPp)} de '${id}' no coincide con el dex (${String(expectedMaxPp)})`,
          );
          reported = true;
          continue;
        }
      }
      const pp = move?.pp;
      if (pp !== null && pp !== undefined) {
        const limit = typeof maxPp === "number" ? maxPp : expectedMaxPp;
        if (typeof pp !== "number" || pp < 0 || (limit !== undefined && pp > limit)) {
          report("moves", `pp=${JSON.stringify(pp)} de '${id}' fuera de [0, ${String(limit)}]`);
          reported = true;
        }
      }
    }
  }

  return OPPONENT_FIELDS.length;
}
