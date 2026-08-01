/** Los 11 campos observables del rival, contra la línea de tiempo.
 *
 * "Cero fuga de información oculta" es LA propiedad del dataset. Pero un campo
 * puede filtrar de dos maneras distintas y las dos importan:
 *
 *  - afirmando algo que NUNCA fue público (el item que nadie reveló);
 *  - afirmando algo que fue público en OTRO momento o con OTRO valor (un HP de
 *    42 cuando el protocolo narró 55).
 *
 * Lo primero se contrasta contra la evidencia de revelación; lo segundo,
 * contra la línea de tiempo. Un campo con ciclo de vida —HP, activo, status,
 * debilitado, boosts, PP, Transform— se valida SIEMPRE contra el valor
 * proyectado, no contra "alguna vez apareció".
 *
 * Las dos únicas fuentes admitidas siguen siendo el protocolo crudo (D17) y el
 * dex local. Ante una diferencia con poke-env gana poke-env: donde la librería
 * escribe un centinela (`unknown_item`) o un `null` ("no derivable"), el
 * auditor acepta y no exige evidencia.
 */

import {
  canonicalIdentity,
  identityKeys,
  moveEvidenceKeys,
  normalizeProtocolText,
  revealedBy,
  type SideEvidence,
  type SpeciesIndex,
} from "./protocol.js";
import {
  BOOST_STATS,
  boostsEqual,
  describeWindow,
  matchesWindow,
  turnWindow,
  usesAt,
  UNKNOWN,
  valueAt,
  type Boosts,
  type SideTimeline,
  type TurnWindow,
  type Unknown,
} from "./timeline.js";
import type { DexMove, DexPokemon, OpponentField, OpponentPokemonState } from "./types.js";
import { OPPONENT_FIELDS, OWN_ONLY_FIELDS } from "./types.js";

const STATUS_VALUES = new Set(["BRN", "PAR", "SLP", "FRZ", "PSN", "TOX", "FNT"]);
/** Centinela de poke-env para "el item todavía no se reveló". */
const UNKNOWN_ITEM = "unknown_item";
/** Claves que el serializador escribe en cada movimiento, siempre. */
const MOVE_KEYS = ["id", "pp", "max_pp"] as const;

export interface DexIndex {
  species: SpeciesIndex;
  /** `base_species` normalizado -> todas sus formas. */
  byBase: Map<string, DexPokemon[]>;
  /** `showdown_id` normalizado -> movimiento. */
  moves: Map<string, DexMove>;
}

export function buildDexIndex(
  pokemon: readonly DexPokemon[],
  moves: readonly DexMove[],
  gen: number,
  speciesIndex: SpeciesIndex,
): DexIndex {
  const byBase = new Map<string, DexPokemon[]>();
  for (const entry of pokemon) {
    if (entry.gen !== gen) continue;
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
  return { species: speciesIndex, byBase, moves: moveIndex };
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

/** `Move.max_pp` de poke-env es `pp * 8 // 5`. */
function dexMaxPp(dex: DexIndex, moveId: string): number | undefined {
  const entry = dex.moves.get(normalizeProtocolText(moveId));
  return entry?.pp === null || entry?.pp === undefined
    ? undefined
    : Math.floor(entry.pp * 8 / 5);
}

export interface OpponentContext {
  evidence: SideEvidence | undefined;
  timeline: SideTimeline | undefined;
  dex: DexIndex;
  gen: number;
  turn: number;
  /** `state.turn`: dónde arranca la ventana observable de la fila. */
  stateTurn: unknown;
  /** Nuestro propio equipo, tal como lo trae la misma fila. Es lo que un
   * Transform copia, y es información que ya tenemos: no es fuga. */
  ownTeam: readonly OpponentPokemonState[];
  playerSide: string;
}

export type FieldReporter = (field: OpponentField, detail: string) => void;

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

/** Lo que un Transform copió, resuelto contra nuestro propio equipo. */
interface CopiedFrom {
  species: string;
  ability: unknown;
  moveIds: Set<string>;
}

function resolveTransformTarget(
  target: string,
  context: OpponentContext,
): CopiedFrom | undefined {
  const [side, name] = target.split(":");
  if (side !== context.playerSide) return undefined;
  for (const mine of context.ownTeam) {
    if (typeof mine.species !== "string") continue;
    if (canonicalIdentity(mine.species, context.dex.species.resolve) !== name) continue;
    const moveIds = new Set<string>(["transform"]);
    if (Array.isArray(mine.moves)) {
      for (const move of mine.moves) {
        const id = asRecord(move)?.id;
        if (typeof id === "string") moveIds.add(normalizeProtocolText(id));
      }
    }
    return { species: mine.species, ability: mine.ability, moveIds };
  }
  return undefined;
}

/** Audita UNA entrada rival. Devuelve cuántos de los 11 chequeos corrió, para
 * que un canario pueda demostrar que ninguno se salteó en silencio. */
export function auditOpponentPokemon(
  entry: OpponentPokemonState,
  context: OpponentContext,
  report: FieldReporter,
  reportOwnOnly: (detail: string) => void,
): number {
  const { evidence, timeline, dex, gen, turn } = context;
  const window: TurnWindow = turnWindow(context.stateTurn, turn);

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
  if (!speciesValid) report("species", "species ausente o no textual");
  const species = speciesValid ? rawSpecies : "";
  const normalizedSpecies = normalizeProtocolText(species);
  const keys = speciesValid ? identityKeys(species, dex.species.resolve) : [];
  const identity = speciesValid ? canonicalIdentity(species, dex.species.resolve) : "";
  const dexEntry = speciesValid ? dex.species.resolve(species) : undefined;
  const formes = dex.byBase.get(identity) ?? (dexEntry ? [dexEntry] : []);
  const mon = timeline?.mons.get(identity);

  if (speciesValid) {
    // Narrada verbatim en un `details` es la prueba fuerte. Si no, la especie
    // tiene que existir EXACTAMENTE en el dex y su identidad canónica tiene
    // que haber sido revelada: sin ese corte, el respaldo por prefijo aceptaba
    // cualquier sufijo inventado sobre una especie real ('furfroubanana').
    const narratedVerbatim = evidence !== undefined
      && revealedBy(evidence.speciesExact, [normalizedSpecies], turn);
    const knownForme = dex.species.resolvedExactly(species)
      && evidence !== undefined && revealedBy(evidence.species, keys, turn);
    if (!narratedVerbatim && !knownForme) {
      report(
        "species",
        dex.species.resolvedExactly(species)
          ? `'${species}' aparece en state antes de ser revelado por el protocolo`
          : `'${species}' no existe en el dex local y el protocolo nunca lo narró: especie fabricada`,
      );
    }
  }

  // --- Transform: qué se copió, y de quién -------------------------------
  const transformTarget = mon === undefined ? undefined : valueAt(mon.transform, turn) ?? undefined;
  const transformTargetBefore = mon === undefined
    ? undefined
    : valueAt(mon.transform, turn - 1) ?? undefined;
  const activeTransform = transformTarget !== undefined && transformTarget !== ""
    ? transformTarget
    : transformTargetBefore !== undefined && transformTargetBefore !== ""
      ? transformTargetBefore
      : undefined;
  // Un Transform que el protocolo no permite resolver NO habilita nada: sin
  // saber qué se copió, aceptar cualquier valor es exactamente la fuga que
  // este chequeo tiene que impedir.
  const copied = activeTransform === undefined
    ? undefined
    : resolveTransformTarget(activeTransform, context);
  const typeChanged = evidence !== undefined && revealedBy(evidence.typeChange, keys, turn);

  // --- fainted -------------------------------------------------------------
  const fainted = entry.fainted;
  if (typeof fainted !== "boolean") {
    report("fainted", `fainted=${JSON.stringify(fainted)} no es booleano`);
  } else if (mon !== undefined) {
    const faintTurn = mon.faintTurn;
    const atStart = faintTurn !== undefined && faintTurn < window.from;
    const atEnd = faintTurn !== undefined && faintTurn <= window.to;
    if (fainted !== atStart && fainted !== atEnd) {
      report(
        "fainted",
        `fainted=${fainted} pero el protocolo dice ${atEnd} para '${species}' en la ventana ${window.from}-${window.to}`,
      );
    }
  } else if (fainted) {
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
    // que no cae en esa grilla sólo puede venir del objeto propio.
    report(
      "hp_fraction",
      `hp_fraction=${hp} no es un centésimo exacto con HP Percentage Mod activo`,
    );
  } else if (mon !== undefined) {
    if (!matchesWindow(mon.hp, window, hp, (a, b) => Math.abs(a - b) < 1e-9)) {
      report(
        "hp_fraction",
        `hp_fraction=${hp} pero el protocolo narró ${describeWindow(mon.hp, window, String)} para '${species}' en la ventana ${window.from}-${window.to}`,
      );
    }
  }

  // --- active --------------------------------------------------------------
  if (typeof entry.active !== "boolean") {
    report("active", `active=${JSON.stringify(entry.active)} no es booleano`);
  } else if (entry.active && timeline !== undefined) {
    if (!matchesWindow(timeline.active, window, identity, (a, b) => a === b)) {
      report(
        "active",
        `'${species}' figura activo pero el protocolo tiene a ${describeWindow(timeline.active, window, String)} en la ventana ${window.from}-${window.to}`,
      );
    }
  }

  // --- status --------------------------------------------------------------
  const status = entry.status;
  const observedStatus = status === null || status === undefined
    ? null
    : typeof status === "string" ? status.toLowerCase() : undefined;
  if (status !== null && status !== undefined
    && (typeof status !== "string" || !STATUS_VALUES.has(status))) {
    report("status", `status=${JSON.stringify(status)} no es un estado conocido`);
  } else if (mon !== undefined && observedStatus !== undefined) {
    if (!matchesWindow<string | null>(mon.status, window, observedStatus, (a, b) => a === b)) {
      report(
        "status",
        `status=${String(status)} pero el protocolo tiene ${describeWindow<string | null>(mon.status, window, (value) => String(value))} para '${species}' en la ventana ${window.from}-${window.to}`,
      );
    }
  } else if (observedStatus !== null && observedStatus !== undefined) {
    const wanted = keys.map((key) => `${key}|${observedStatus}`);
    if (evidence === undefined || !revealedBy(evidence.status, wanted, turn)) {
      report("status", `status=${String(status)} sin línea pública que lo revele para '${species}'`);
    }
  }

  // --- level ---------------------------------------------------------------
  const level = entry.level;
  if (typeof level !== "number" || !Number.isInteger(level) || level < 1 || level > 100) {
    report("level", `level=${JSON.stringify(level)} no es un nivel válido`);
  } else if (mon !== undefined && mon.levels.size > 0 && !mon.levels.has(level)) {
    // Showdown omite `L100` y el recorder lo interpreta como 100, así que todo
    // `details` narra un nivel: 100 nunca es un comodín que pueda pisar un
    // `L82` explícito.
    report(
      "level",
      `level=${level} pero el protocolo narró ${[...mon.levels].join("/")} para '${species}'`,
    );
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
      // exactamente una ability posible, decirla no es fuga.
      const dexDetermined = gen >= 3 && formes.some((forme) => {
        if (isFormeChangeForme(forme)) {
          return normalizeProtocolText(forme.abilities[0] ?? "") === normalized;
        }
        return forme.abilities.length === 1
          && normalizeProtocolText(forme.abilities[0]) === normalized;
      });
      // Un Transform copia la ability del objetivo. Imposter es la del propio
      // transformador. Un cambio de TIPOS no revela ni cambia una ability:
      // excusarla con `typeChanged` habilitaba fuga arbitraria.
      const copiedAbility = copied !== undefined
        && (normalized === "imposter"
          || (typeof copied.ability === "string"
            && normalizeProtocolText(copied.ability) === normalized));
      if (!revealed && !dexDetermined && !copiedAbility) {
        report(
          "ability",
          `ability='${ability}' no está revelada por el protocolo, ni determinada por el dex, ni copiada por Transform para '${species}'`,
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
  } else if (copied !== undefined) {
    // `apply_transform`: los tipos son los del DEX de la especie copiada, no
    // los tipos actuales del objetivo.
    const copiedEntry = dex.species.resolve(copied.species);
    const expected = copiedEntry === undefined ? undefined : typeKey(copiedEntry.types);
    if (expected !== undefined && typeKey(types as string[]) !== expected) {
      report(
        "types",
        `types=${JSON.stringify(types)} no son los de '${copied.species}', que es lo que este Transform copió`,
      );
    }
  } else if (!typeChanged) {
    // Un cambio de forma NO cambia `species` (`forme_change` usa
    // `store_species=False`) pero SÍ cambia los tipos, así que se aceptan los
    // tipos de cualquier forma de la misma especie base. Un `typechange` sí
    // produce tipos fuera del dex: ahí el chequeo se abstiene.
    const allowed = new Set(formes.map((forme) => typeKey(forme.types)));
    if (allowed.size > 0 && !allowed.has(typeKey(types as string[]))) {
      report(
        "types",
        `types=${JSON.stringify(types)} no corresponde a ninguna forma de '${identity}' en el dex de gen ${gen}`,
      );
    }
  }

  // --- boosts --------------------------------------------------------------
  const boosts = asRecord(entry.boosts);
  if (boosts === undefined) {
    report("boosts", `boosts=${JSON.stringify(entry.boosts)} no es un objeto`);
  } else {
    const missing = BOOST_STATS.filter((stat) => !(stat in boosts));
    const extra = Object.keys(boosts).filter(
      (key) => !(BOOST_STATS as readonly string[]).includes(key),
    );
    if (missing.length > 0 || extra.length > 0) {
      // El serializador escribe `dict(mon.boosts)`, que poke-env inicializa
      // con los siete stats. Una forma incompleta no es "sin boosts": es una
      // fila que no cumple el formato que promete su propia versión.
      report(
        "boosts",
        `boosts no tiene los 7 stats${missing.length > 0 ? `; faltan ${missing.join(", ")}` : ""}${extra.length > 0 ? `; sobran ${extra.join(", ")}` : ""}`,
      );
    } else {
      const outOfRange = Object.entries(boosts).find(([, value]) =>
        typeof value !== "number" || !Number.isInteger(value) || value < -6 || value > 6
      );
      if (outOfRange !== undefined) {
        report("boosts", `boosts.${outOfRange[0]}=${JSON.stringify(outOfRange[1])} fuera de [-6,6]`);
      } else if (mon !== undefined) {
        const observed = boosts as Boosts;
        if (!matchesWindow<Boosts | Unknown>(mon.boosts, window, observed, boostsEqual)) {
          report(
            "boosts",
            `boosts=${JSON.stringify(observed)} no coinciden con los que narró el protocolo (${describeWindow<Boosts | Unknown>(mon.boosts, window, (value) => value === UNKNOWN ? "desconocidos" : JSON.stringify(value))}) para '${species}' en la ventana ${window.from}-${window.to}`,
          );
        }
      } else if (Object.values(boosts).some((value) => value !== 0)) {
        report("boosts", `boosts no nulos para '${species}' sin ninguna línea pública de boost`);
      }
    }
  }

  // --- moves ---------------------------------------------------------------
  const moves = entry.moves;
  if (!Array.isArray(moves)) {
    report("moves", `moves=${JSON.stringify(moves)} no es una lista`);
  } else {
    let reported = false;
    for (const raw of moves) {
      if (reported) break;
      const move = asRecord(raw);
      const missing = move === undefined ? [...MOVE_KEYS] : MOVE_KEYS.filter((key) => !(key in move));
      if (move === undefined || missing.length > 0) {
        report("moves", `entrada de movimiento incompleta (faltan ${missing.join(", ")}): ${JSON.stringify(raw)}`);
        reported = true;
        continue;
      }
      const id = move.id;
      if (typeof id !== "string" || id.length === 0) {
        report("moves", `moves trae una entrada sin id: ${JSON.stringify(raw)}`);
        reported = true;
        continue;
      }
      const normalizedId = normalizeProtocolText(id);
      const moveKeys = moveEvidenceKeys(id);
      const revealed = evidence !== undefined
        && revealedBy(
          evidence.move,
          moveKeys.flatMap((moveKey) => keys.map((key) => `${key}|${moveKey}`)),
          turn,
        );
      // Un Transform copia el moveset del objetivo, que es NUESTRO: es
      // información que ya tenemos. Pero sólo vale para los movimientos que
      // ese objetivo realmente tiene.
      const copiedMove = copied !== undefined && copied.moveIds.has(normalizedId);
      if (!revealed && !copiedMove) {
        report(
          "moves",
          copied === undefined
            ? `movimiento '${id}' de '${species}' nunca fue narrado públicamente`
            : `movimiento '${id}' de '${species}' no está en el moveset que este Transform copió`,
        );
        reported = true;
        continue;
      }
      const dexMax = dexMaxPp(dex, id);
      // `_transformed_move`: un movimiento copiado topea en `min(5, max_pp)`
      // desde gen 5, y queda en null si la generación no permite derivarlo.
      const expectedMaxPp = copiedMove && copied !== undefined
        ? (gen >= 5 && dexMax !== undefined ? Math.min(5, dexMax) : undefined)
        : dexMax;
      const maxPp = move.max_pp;
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
      const pp = move.pp;
      if (pp !== null && pp !== undefined) {
        const limit = typeof maxPp === "number" ? maxPp : expectedMaxPp;
        if (typeof pp !== "number" || pp < 0 || (limit !== undefined && pp > limit)) {
          report("moves", `pp=${JSON.stringify(pp)} de '${id}' fuera de [0, ${String(limit)}]`);
          reported = true;
          continue;
        }
        // El PP no es un número libre: baja de a uno por uso narrado, o de a
        // dos bajo Pressure. Sin este piso, un `pp: 1` arbitrario pasaba con
        // sólo estar el movimiento revelado.
        if (mon !== undefined && typeof limit === "number") {
          const uses = Math.max(usesAt(mon, normalizedId, turn), ...moveKeys.map(
            (moveKey) => usesAt(mon, moveKey, turn),
          ));
          const floor = limit - 2 * uses;
          if (typeof pp === "number" && pp < floor) {
            report(
              "moves",
              `pp=${pp} de '${id}' es menor que el piso derivable ${floor} (max_pp ${limit}, ${uses} uso(s) narrado(s))`,
            );
            reported = true;
          }
        }
      }
    }
  }

  return OPPONENT_FIELDS.length;
}
