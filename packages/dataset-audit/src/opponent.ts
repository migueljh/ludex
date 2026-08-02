/** Los 11 campos observables del rival, contra UN instante de la proyección.
 *
 * "Cero fuga de información oculta" es LA propiedad del dataset, pero no se
 * demuestra campo por campo: un HP del turno 3 con un status del turno 4 y unos
 * boosts del turno 5 pueden ser todos públicos y aun así describir un estado
 * que no existió nunca. Por eso acá se compara el equipo rival COMPLETO contra
 * un único cursor, y quien decide si la fila es admisible es el auditor, que
 * prueba los cursores de su ventana (`invariants.ts`).
 *
 * Las dos únicas fuentes admitidas siguen siendo el protocolo crudo (D17) y el
 * dex local. La tercera —el equipo PROPIO que la misma fila trae— no es fuga:
 * es lo que un Transform copia y lo que poke-env conoce por el `|request|`
 * privado, que el protocolo no contiene.
 */

import {
  canonicalIdentity,
  normalizeProtocolText,
  retrieveMoveId,
  type SpeciesIndex,
} from "./protocol.js";
import {
  abilityOf,
  BOOST_STATS,
  isFainted,
  movesOf,
  typesOf,
  UNKNOWN_ITEM,
  type BattleProjection,
  type DexView,
  type MonState,
  type MoveState,
} from "./projection.js";
import type { OpponentField, OpponentPokemonState } from "./types.js";
import { OWN_ONLY_FIELDS } from "./types.js";

const STATUS_VALUES = new Set(["BRN", "PAR", "SLP", "FRZ", "PSN", "TOX", "FNT"]);
/** Las claves EXACTAS que el serializador escribe en cada movimiento. */
const MOVE_KEYS = ["id", "pp", "max_pp"] as const;

export interface OpponentContext {
  projection: BattleProjection | undefined;
  dex: DexView;
  species: SpeciesIndex;
  gen: number;
  opponentSide: string;
  playerSide: string;
  /** El equipo propio, tal como lo trae la misma fila. */
  ownTeam: readonly OpponentPokemonState[];
}

export interface FieldMismatch {
  field?: OpponentField;
  detail: string;
}

/** Cómo se recorre la comparación.
 *
 * `limit` corta apenas se alcanza esa cantidad de desajustes: al auditor sólo
 * le interesa si un cursor es MEJOR que el mejor visto hasta ahora, así que
 * seguir contando desajustes de un cursor peor es trabajo tirado. `collect`
 * construye los mensajes, que es lo caro: se hace una sola vez por fila, sobre
 * el cursor que más se le acerca. */
export interface MatchOptions {
  limit: number;
  collect: boolean;
}

export interface MatchResult {
  /** Puntaje del cursor: 0 = lo explica entero. Un desajuste de IDENTIDAD (una
   * especie que el protocolo todavía no reveló) pesa mucho más que uno de
   * valor, porque si no el "cursor que más se acerca" terminaba siendo uno
   * anterior a que el rival entrara al campo, y el diagnóstico decía que la
   * especie no existe en vez de decir qué valor no cierra. */
  count: number;
  mismatches: FieldMismatch[];
}

const IDENTITY_WEIGHT = 100;

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function typeKey(types: readonly string[]): string {
  return [...types].map((type) => normalizeProtocolText(type)).sort().join("/");
}

/** El objetivo real de un Transform, resuelto contra nuestro propio equipo.
 *
 * poke-env copia el moveset y la ability del objetivo. Si el objetivo es
 * NUESTRO, la librería los conoce completos por el request privado y el
 * protocolo no: la fila misma es la única fuente honesta de ese dato. */
interface CopiedFrom {
  ability: string | null;
  moves: Map<string, { pp: number | undefined; maxPp: number | undefined }>;
}

function resolveCopied(
  mon: MonState,
  context: OpponentContext,
): CopiedFrom | undefined {
  const target = mon.transformTarget;
  if (target === undefined || context.projection === undefined) return undefined;
  const source = context.projection.mons.get(`${target.side}:${target.name}`);
  if (source === undefined || source.species.length === 0) return undefined;
  if (target.side !== context.playerSide) return undefined;
  for (const mine of context.ownTeam) {
    if (typeof mine.species !== "string") continue;
    if (normalizeProtocolText(mine.species) !== source.species) continue;
    const moves = new Map<string, { pp: number | undefined; maxPp: number | undefined }>();
    if (Array.isArray(mine.moves)) {
      for (const raw of mine.moves) {
        const move = asRecord(raw);
        const id = move?.id;
        if (typeof id !== "string") continue;
        const max = typeof move?.max_pp === "number" ? move.max_pp : undefined;
        // `_transformed_move`: `min(5, max_pp)` desde gen 5, y sin generación
        // que lo permita derivar el PP no es derivable.
        const capped = max === undefined
          ? undefined
          : context.gen >= 5 ? Math.min(5, max) : max;
        moves.set(retrieveMoveId(id), { pp: capped, maxPp: capped });
      }
    }
    const ability = typeof mine.ability === "string"
      ? normalizeProtocolText(mine.ability)
      : null;
    return { ability, moves };
  }
  return undefined;
}

function describeMove(move: MoveState): string {
  const pp = move.ppMin === move.ppMax ? String(move.ppMin) : `${move.ppMin}-${move.ppMax}`;
  return `${move.id}(pp ${pp}/${String(move.maxPp)})`;
}

/** Compara UNA entrada rival contra su miembro proyectado. */
function compareMon(
  entry: OpponentPokemonState,
  mon: MonState,
  context: OpponentContext,
  push: (field: OpponentField, detail: () => string) => boolean,
): boolean {
  const species = typeof entry.species === "string" ? entry.species : "";
  const copied = mon.transformTarget === undefined ? undefined : resolveCopied(mon, context);

  // --- level ---------------------------------------------------------------
  const level = entry.level;
  if (typeof level !== "number" || !Number.isInteger(level) || level < 1 || level > 100) {
    if (push("level", () => `level=${JSON.stringify(level)} no es un nivel válido`)) return true;
  } else if (level !== mon.level) {
    if (push("level", () => `level=${level}; el protocolo narró ${mon.level} para '${species}'`)) return true;
  }

  // --- hp_fraction ---------------------------------------------------------
  const hp = entry.hp_fraction;
  if (typeof hp !== "number" || Number.isNaN(hp) || hp < 0 || hp > 1) {
    if (push("hp_fraction", () => `hp_fraction=${JSON.stringify(hp)} no es una fracción válida`)) return true;
  } else if (mon.hp !== undefined && Math.abs(hp - mon.hp) > 1e-9) {
    if (push("hp_fraction", () => `hp_fraction=${hp}; el protocolo tiene ${mon.hp} para '${species}'`)) return true;
  }

  // --- active --------------------------------------------------------------
  // Se valida en los DOS sentidos: declarar `false` sobre el que está en el
  // campo es exactamente igual de falso que declarar `true` sobre el que no.
  if (typeof entry.active !== "boolean") {
    if (push("active", () => `active=${JSON.stringify(entry.active)} no es booleano`)) return true;
  } else if (entry.active !== mon.active) {
    if (push("active", () => `active=${entry.active}; el protocolo tiene active=${mon.active} para '${species}'`)) return true;
  }

  // --- fainted -------------------------------------------------------------
  if (typeof entry.fainted !== "boolean") {
    if (push("fainted", () => `fainted=${JSON.stringify(entry.fainted)} no es booleano`)) return true;
  } else if (entry.fainted !== isFainted(mon)) {
    if (push("fainted", () => `fainted=${entry.fainted}; el protocolo tiene ${isFainted(mon)} para '${species}'`)) return true;
  }

  // --- status --------------------------------------------------------------
  const status = entry.status;
  if (status !== null && status !== undefined
    && (typeof status !== "string" || !STATUS_VALUES.has(status))) {
    if (push("status", () => `status=${JSON.stringify(status)} no es un estado conocido`)) return true;
  } else {
    const observed = status === null || status === undefined
      ? null
      : normalizeProtocolText(status);
    if (observed !== mon.status) {
      if (push(
        "status",
        () => `status=${String(status)}; el protocolo tiene ${mon.status ?? "null"} para '${species}'`,
      )) return true;
    }
  }

  // --- item ----------------------------------------------------------------
  const item = entry.item;
  if (item !== null && item !== undefined && typeof item !== "string") {
    if (push("item", () => `item=${JSON.stringify(item)} no es textual ni null`)) return true;
  } else if (!mon.unresolved.has("item")) {
    // El centinela lo escribe poke-env sin pasar por `to_id_str`, así que
    // conserva el guión bajo: normalizarlo lo volvía un item inexistente.
    const observed = item === null || item === undefined
      ? null
      : item === UNKNOWN_ITEM ? UNKNOWN_ITEM : normalizeProtocolText(item);
    if (observed !== mon.item) {
      if (push(
        "item",
        () => `item=${observed ?? "null"}; el protocolo tiene ${mon.item ?? "null"} para '${species}'`
        + (mon.item === UNKNOWN_ITEM ? " (nadie lo reveló)" : ""),
      )) return true;
    }
  }

  // --- ability -------------------------------------------------------------
  const ability = entry.ability;
  if (ability !== null && ability !== undefined && typeof ability !== "string") {
    if (push("ability", () => `ability=${JSON.stringify(ability)} no es textual ni null`)) return true;
  } else {
    const observed = ability === null || ability === undefined
      ? null
      : normalizeProtocolText(ability);
    const expected = copied !== undefined && abilityOf(mon) === null
      ? copied.ability
      : abilityOf(mon);
    const unresolved = mon.unresolved.has("ability") && copied === undefined;
    const alsoOk = observed !== null && mon.admissibleAbilities.has(observed);
    if (!unresolved && observed !== expected && !alsoOk) {
      if (push(
        "ability",
        () => `ability=${observed ?? "null"}; el protocolo deriva ${expected ?? "null"} para '${species}'`,
      )) return true;
    }
  }

  // --- types ---------------------------------------------------------------
  const types = entry.types;
  if (!Array.isArray(types) || types.some((type) => typeof type !== "string")) {
    if (push("types", () => `types=${JSON.stringify(types)} no es una lista de textos`)) return true;
  } else if (!mon.unresolved.has("types")) {
    const expected = typesOf(mon);
    if (expected !== undefined && typeKey(types as string[]) !== typeKey(expected)) {
      if (push(
        "types",
        () => `types=${JSON.stringify(types)}; el protocolo y el dex dan ${JSON.stringify(expected)} para '${species}'`,
      )) return true;
    }
  }

  // --- boosts --------------------------------------------------------------
  const boosts = asRecord(entry.boosts);
  if (boosts === undefined) {
    if (push("boosts", () => `boosts=${JSON.stringify(entry.boosts)} no es un objeto`)) return true;
  } else {
    const missing = BOOST_STATS.filter((stat) => !(stat in boosts));
    const extra = Object.keys(boosts).filter(
      (key) => !(BOOST_STATS as readonly string[]).includes(key),
    );
    if (missing.length > 0 || extra.length > 0) {
      // El serializador escribe `dict(mon.boosts)`, que poke-env inicializa con
      // los siete stats. Una forma incompleta no es "sin boosts".
      if (push(
        "boosts",
        () => `boosts no tiene los 7 stats${missing.length > 0 ? `; faltan ${missing.join(", ")}` : ""}${extra.length > 0 ? `; sobran ${extra.join(", ")}` : ""}`,
      )) return true;
    } else if (!mon.unresolved.has("boosts")) {
      for (const stat of BOOST_STATS) {
        const value = boosts[stat];
        if (typeof value !== "number" || !Number.isInteger(value) || value < -6 || value > 6) {
          if (push("boosts", () => `boosts.${stat}=${JSON.stringify(value)} no es un boost válido`)) return true;
          break;
        }
        if (value !== (mon.boosts[stat] ?? 0)) {
          if (push(
            "boosts",
            () => `boosts=${JSON.stringify(boosts)}; el protocolo tiene ${JSON.stringify(mon.boosts)} para '${species}'`,
          )) return true;
          break;
        }
      }
    }
  }

  // --- moves ---------------------------------------------------------------
  const moves = entry.moves;
  if (!Array.isArray(moves)) {
    if (push("moves", () => `moves=${JSON.stringify(moves)} no es una lista`)) return true;
    return false;
  }
  const expectedMoves = movesOf(mon);
  const seen = new Set<string>();
  for (const raw of moves) {
    const move = asRecord(raw);
    if (move === undefined) {
      if (push("moves", () => `entrada de movimiento no es un objeto: ${JSON.stringify(raw)}`)) return true;
      return false;
    }
    const keys = Object.keys(move);
    const missing = MOVE_KEYS.filter((key) => !(key in move));
    const extra = keys.filter((key) => !(MOVE_KEYS as readonly string[]).includes(key));
    if (missing.length > 0 || extra.length > 0) {
      // La forma es parte del contrato de la versión: una clave de más es un
      // canal por el que se cuela información que el serializador no escribe.
      if (push(
        "moves",
        () => `movimiento con forma inválida${missing.length > 0 ? `; faltan ${missing.join(", ")}` : ""}${extra.length > 0 ? `; sobran ${extra.join(", ")}` : ""}: ${JSON.stringify(raw)}`,
      )) return true;
      return false;
    }
    const id = move.id;
    if (typeof id !== "string" || id.length === 0) {
      if (push("moves", () => `movimiento sin id: ${JSON.stringify(raw)}`)) return true;
      return false;
    }
    const key = retrieveMoveId(id);
    seen.add(key);
    if (copied !== undefined) {
      const target = copied.moves.get(key);
      if (target === undefined) {
        if (push(
          "moves",
          () => `'${id}' no está en el moveset que este Transform copió`,
        )) return true;
        return false;
      }
      if (target.pp !== undefined && move.pp !== target.pp) {
        if (push("moves", () => `pp=${JSON.stringify(move.pp)} de '${id}' copiado; corresponde ${target.pp}`)) return true;
        return false;
      }
      if (target.maxPp !== undefined && move.max_pp !== target.maxPp) {
        if (push("moves", () => `max_pp=${JSON.stringify(move.max_pp)} de '${id}' copiado; corresponde ${target.maxPp}`)) return true;
        return false;
      }
      continue;
    }
    if (mon.unresolved.has("moves")) continue;
    const expected = expectedMoves.get(key);
    if (expected === undefined) {
      if (push(
        "moves",
        () => `'${id}' de '${species}' no pertenece al moveset que el protocolo revela (${[...expectedMoves.values()].map(describeMove).join(", ") || "ninguno"})`,
      )) return true;
      return false;
    }
    if (expected.maxPp !== undefined && move.max_pp !== expected.maxPp) {
      if (push(
        "moves",
        () => `max_pp=${JSON.stringify(move.max_pp)} de '${id}'; el dex da ${expected.maxPp}`,
      )) return true;
      return false;
    }
    const pp = move.pp;
    if (pp === null || pp === undefined) {
      // D31: `pp: null` significa "no derivable con exactitud", no "omitido".
      // El recorder lo escribe cuando NUESTRO activo tiene Pressure —ahí no
      // puede decidir si el descuento fue de uno o de dos— y ese estado
      // persiste después. Se acepta ahí y sólo ahí.
      if (!expected.indeterminate) {
        if (push(
          "moves",
          () => `pp=null de '${id}' pero el valor era derivable: ${describeMove(expected)}`
            + " (ninguna línea pública dejó el PP indeterminado)",
        )) return true;
        return false;
      }
    } else if (expected.ppMin !== undefined && expected.ppMax !== undefined) {
      if (typeof pp !== "number" || pp < expected.ppMin || pp > expected.ppMax) {
        if (push(
          "moves",
          () => `pp=${JSON.stringify(pp)} de '${id}'; el protocolo deriva ${describeMove(expected)}`,
        )) return true;
        return false;
      }
    }
  }
  if (!mon.unresolved.has("moves") || copied !== undefined) {
    const expectedKeys = copied === undefined ? [...expectedMoves.keys()] : [...copied.moves.keys()];
    const absent = expectedKeys.filter((key) => !seen.has(key));
    if (absent.length > 0) {
      if (push(
        "moves",
        () => copied === undefined
          ? `faltan movimientos ya revelados de '${species}': ${absent.join(", ")}`
          : `faltan movimientos que este Transform copió de '${species}': ${absent.join(", ")}`,
      )) return true;
    }
  }
  return false;
}

/** ¿La fila entera describe ESTE instante de la proyección?
 *
 * Devuelve la lista de desajustes: vacía significa que el cursor explica la
 * fila completa. Con `stopAtFirst` sólo interesa si hay o no desajuste, que es
 * el camino caliente (se prueba un cursor por línea de la ventana). */
export function matchOpponentTeam(
  entries: readonly OpponentPokemonState[],
  context: OpponentContext,
  options: MatchOptions,
): MatchResult {
  const out: FieldMismatch[] = [];
  let count = 0;
  const push = (
    field: OpponentField | undefined,
    detail: () => string,
    weight = 1,
  ): boolean => {
    count += weight;
    if (options.collect) {
      const rendered = detail();
      out.push(field === undefined ? { detail: rendered } : { field, detail: rendered });
    }
    return count >= options.limit;
  };
  const result = (): MatchResult => ({ count, mismatches: out });
  const projection = context.projection;
  if (projection === undefined) return result();

  const projected = new Map<string, MonState>();
  for (const mon of projection.teamOf(context.opponentSide)) {
    if (mon.species.length > 0) projected.set(mon.species, mon);
  }

  const claimed = new Set<string>();
  for (const entry of entries) {
    for (const forbidden of OWN_ONLY_FIELDS) {
      if (forbidden in entry) {
        if (push(undefined, () => `la entrada rival trae '${forbidden}', que el serializador produce sólo para mine=True`)) {
          return result();
        }
      }
    }
    const raw = entry.species;
    if (typeof raw !== "string" || raw.length === 0) {
      if (push("species", () => `species=${JSON.stringify(raw)} ausente o no textual`)) return result();
      continue;
    }
    const normalized = normalizeProtocolText(raw);
    const mon = projected.get(normalized);
    if (mon === undefined) {
      const known = [...projected.keys()];
      if (push(
        "species",
        () => `'${raw}' no es ninguna especie que el protocolo haya revelado para ${context.opponentSide}`
        + (known.length > 0 ? ` (reveladas: ${known.join(", ")})` : "")
        + (canonicalIdentity(normalized, context.species) !== normalized
          ? "; una Mega conserva la especie base (`store_species=False`)"
          : ""),
        IDENTITY_WEIGHT,
      )) return result();
      continue;
    }
    if (claimed.has(normalized)) {
      if (push("species", () => `'${raw}' aparece dos veces en el mismo equipo rival`)) return result();
      continue;
    }
    claimed.add(normalized);
    if (mon.dexUnknown) {
      // Fallo CERRADO. Antes esto marcaba `types` y `ability` como no
      // derivables y el comparador dejaba de auditarlos: una fila con
      // `Furfrou-Banana`, tipos DRAGON y Wonder Guard pasaba con cero
      // violaciones porque el prefijo la "resolvía".
      if (push(
        "species",
        () => `'${raw}' no existe en el dex de gen ${context.gen}: no tiene fila propia`
          + " y no es miembro de ningún `cosmeticFormes` (D32), así que ni sus tipos"
          + " ni su ability son auditables",
        IDENTITY_WEIGHT,
      )) return result();
      continue;
    }
    if (compareMon(entry, mon, context, push)) return result();
  }

  // Un miembro que la fila no trae sólo se reporta si NINGUNA entrada lo
  // nombra: cuando la fila lo llama `slowbromega`, el defecto es UNO (la
  // especie inventada), no dos.
  const canonicalClaims = new Set(
    entries.map((entry) => typeof entry.species === "string"
      ? canonicalIdentity(entry.species, context.species)
      : ""),
  );
  for (const [species, mon] of projected) {
    if (claimed.has(species)) continue;
    if (canonicalClaims.has(canonicalIdentity(species, context.species))) continue;
    if (push(
      "species",
      () => `el protocolo ya reveló a '${species}'${mon.active ? " (y está en el campo)" : ""} y la fila no lo trae`,
      IDENTITY_WEIGHT,
    )) return result();
  }
  return result();
}
