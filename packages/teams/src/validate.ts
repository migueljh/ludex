import type { Pool } from "pg";
import showdown from "pokemon-showdown";
import type { ModdedDex, PokemonSet } from "./types.js";
import { TeamData, createPool } from "./db.js";

// `import { Teams }` nombrado falla bajo el loader ESM nativo de Node con este
// paquete (mismo motivo que en packages/seed/src/extract/dex.ts): el import
// por defecto + desestructuracion funciona con cualquier modulo CJS.
const { Teams, Dex } = showdown;

export type IssueKind =
  /** La entidad no existe en esa generacion (inventada o de otra gen). */
  | "unknown"
  /** Existe, pero no es legal para esa especie (movimiento, habilidad). */
  | "illegal"
  /** Viola un rango del juego (EVs, IVs, nivel, tamaño del equipo). */
  | "invalid";

export interface TeamIssue {
  /** Nombre legible de la especie del set; ausente en problemas de equipo. */
  pokemon?: string;
  field: "team" | "species" | "move" | "ability" | "item" | "nature" | "evs" | "ivs" | "level";
  kind: IssueKind;
  message: string;
  /** El movimiento en cuestion, cuando field es "move". */
  move?: string;
}

export interface TeamValidation {
  ok: boolean;
  gen: number;
  /** Cuantos pokemon parseó el parser de Showdown. */
  sets: number;
  issues: TeamIssue[];
}

export interface ValidateOptions {
  /**
   * Pool compartido (tests, consumidores repetidos). Si no se pasa, se crea
   * uno desde DATABASE_URL y se cierra al terminar.
   */
  pool?: Pool;
}

const STATS = ["hp", "atk", "def", "spa", "spd", "spe"] as const;
/** Como los escribe el export del Teambuilder ("252 Atk / 4 SpD"). */
const STAT_LABELS: Record<(typeof STATS)[number], string> = {
  hp: "HP", atk: "Atk", def: "Def", spa: "SpA", spd: "SpD", spe: "Spe",
};
const MAX_TEAM_SIZE = 6;
const MAX_EV_PER_STAT = 252;
const MAX_EV_TOTAL = 510;
const MAX_IV = 31;

function dexFor(gen: number): ModdedDex {
  if (!Number.isInteger(gen) || gen < 1 || gen > 9) {
    throw new Error(`gen debe ser un entero entre 1 y 9; recibido: ${gen}`);
  }
  return Dex.mod(`gen${gen}`);
}

/**
 * Parsea texto del Teambuilder de Showdown (via Teams.import del paquete
 * pineado, nunca un parser a mano) y valida el equipo contra la base:
 * existencia por generacion, aprendizaje segun learnsets, habilidad de la
 * especie, rangos del juego. Devuelve TODOS los problemas juntos; no corta
 * en el primero.
 */
export async function validateTeamText(
  text: string,
  gen: number,
  opts: ValidateOptions = {},
): Promise<TeamValidation> {
  const dex = dexFor(gen);
  const ownPool = opts.pool ? null : createPool();
  const pool = opts.pool ?? ownPool!;
  try {
    const data = await TeamData.forGen(pool, gen);
    return await validateSets(text, gen, dex, data);
  } finally {
    if (ownPool) await ownPool.end();
  }
}

async function validateSets(
  text: string,
  gen: number,
  dex: ModdedDex,
  data: TeamData,
): Promise<TeamValidation> {
  const issues: TeamIssue[] = [];
  const sets = typeof text === "string" ? (Teams.import(text) ?? []) : [];

  if (sets.length === 0) {
    issues.push({
      field: "team", kind: "invalid",
      message: "no se pudo parsear ningun pokemon del texto (formato de export del Teambuilder)",
    });
    return { ok: false, gen, sets: 0, issues };
  }
  if (sets.length > MAX_TEAM_SIZE) {
    issues.push({
      field: "team", kind: "invalid",
      message: `el equipo tiene ${sets.length} pokemon; el maximo es ${MAX_TEAM_SIZE}`,
    });
  }

  for (const set of sets) {
    issues.push(...await validateSet(set, gen, dex, data));
  }
  return { ok: issues.length === 0, gen, sets: sets.length, issues };
}

async function validateSet(
  set: PokemonSet,
  gen: number,
  dex: ModdedDex,
  data: TeamData,
): Promise<TeamIssue[]> {
  const issues: TeamIssue[] = [];
  const label = set.species || set.name || "(sin especie)";

  // --- especie: existe en el paquete? existe en la base para esta gen? ---
  const resolved = dex.species.get(set.species);
  if (!resolved.exists) {
    issues.push({
      pokemon: label, field: "species", kind: "unknown",
      message: `la especie '${set.species}' no existe (gen ${gen})`,
    });
    // Sin especie no hay como chequear habilidad ni aprendizaje: se corta aca
    // para no cascadear errores que no aportan. EVs/IVs/nivel igual se revisan.
    issues.push(...validateNumbers(set, label));
    return issues;
  }
  const speciesRow = await data.species(resolved.id);
  if (!speciesRow) {
    issues.push({
      pokemon: label, field: "species", kind: "unknown",
      message: `'${resolved.name}' no existe en gen ${gen} (aparece en gen ${resolved.gen})`,
    });
    issues.push(...validateNumbers(set, label));
    return issues;
  }
  const speciesName = speciesRow.name;

  // --- habilidad: existe? le corresponde a la especie? ---
  // Si el export no trae linea Ability, Showdown la asigna por defecto: no es error.
  if (set.ability) {
    const ability = dex.abilities.get(set.ability);
    if (!ability.exists) {
      issues.push({
        pokemon: speciesName, field: "ability", kind: "unknown",
        message: `la habilidad '${set.ability}' no existe (gen ${gen})`,
      });
    } else if (!speciesRow.abilities.includes(ability.name)) {
      issues.push({
        pokemon: speciesName, field: "ability", kind: "illegal",
        message: `${speciesName} no puede tener la habilidad '${ability.name}' en gen ${gen}; las suyas: ${speciesRow.abilities.join(", ")}`,
      });
    }
  }

  // --- objeto: opcional; si hay, existe en esta gen? ---
  if (set.item) {
    const item = dex.items.get(set.item);
    if (!item.exists) {
      issues.push({
        pokemon: speciesName, field: "item", kind: "unknown",
        message: `el objeto '${set.item}' no existe (gen ${gen})`,
      });
    } else if (!(await data.itemExists(item.id))) {
      issues.push({
        pokemon: speciesName, field: "item", kind: "unknown",
        message: `'${item.name}' no existe en gen ${gen} (aparece en gen ${item.gen})`,
      });
    }
  }

  // --- naturaleza: existe? (extra no pedido; un typo aca juega en silencio) ---
  if (set.nature && !dex.natures.get(set.nature).exists) {
    issues.push({
      pokemon: speciesName, field: "nature", kind: "unknown",
      message: `la naturaleza '${set.nature}' no existe`,
    });
  }

  // --- movimientos: existen? los aprende segun la base? ---
  for (const moveName of set.moves) {
    // 'Hidden Power Fire' comparte id con la base en gen 6 (medido): el
    // paquete resuelve 'hiddenpower' y la base tiene la entrada deduplicada.
    const move = dex.moves.get(moveName);
    if (!move.exists) {
      issues.push({
        pokemon: speciesName, field: "move", kind: "unknown", move: moveName,
        message: `el movimiento '${moveName}' no existe (gen ${gen})`,
      });
      continue;
    }
    if (!(await data.moveExists(move.id))) {
      issues.push({
        pokemon: speciesName, field: "move", kind: "unknown", move: move.name,
        message: `'${move.name}' no existe en gen ${gen} (aparece en gen ${move.gen})`,
      });
      continue;
    }
    if (!(await data.learnsMove(resolved.id, move.id))) {
      issues.push({
        pokemon: speciesName, field: "move", kind: "illegal", move: move.name,
        message: `${speciesName} no puede aprender '${move.name}' en gen ${gen}`,
      });
    }
  }

  issues.push(...validateNumbers(set, speciesName));
  return issues;
}

function validateNumbers(set: PokemonSet, label: string): TeamIssue[] {
  const issues: TeamIssue[] = [];

  let evTotal = 0;
  for (const stat of STATS) {
    const ev = set.evs?.[stat] ?? 0;
    evTotal += ev;
    if (!Number.isInteger(ev) || ev < 0 || ev > MAX_EV_PER_STAT) {
      issues.push({
        pokemon: label, field: "evs", kind: "invalid",
        message: `EVs de ${STAT_LABELS[stat]} fuera de rango: ${ev} (maximo ${MAX_EV_PER_STAT} por stat)`,
      });
    }
    const iv = set.ivs?.[stat] ?? MAX_IV;
    if (!Number.isInteger(iv) || iv < 0 || iv > MAX_IV) {
      issues.push({
        pokemon: label, field: "ivs", kind: "invalid",
        message: `IVs de ${STAT_LABELS[stat]} fuera de rango: ${iv} (0 a ${MAX_IV})`,
      });
    }
  }
  if (evTotal > MAX_EV_TOTAL) {
    issues.push({
      pokemon: label, field: "evs", kind: "invalid",
      message: `EVs en total: ${evTotal} supera el maximo de ${MAX_EV_TOTAL}`,
    });
  }

  if (!Number.isInteger(set.level) || set.level < 1 || set.level > 100) {
    issues.push({
      pokemon: label, field: "level", kind: "invalid",
      message: `nivel fuera de rango: ${set.level} (1 a 100)`,
    });
  }
  return issues;
}
