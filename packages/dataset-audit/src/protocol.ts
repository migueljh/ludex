/** Lectura del protocolo crudo: parseo puro, sin estado.
 *
 * El protocolo es la fuente de verdad del estado (D17). Este módulo sólo lo
 * *lee*: convierte una línea en sus piezas y resuelve especies contra el dex
 * local. Quién es el dueño de qué, y con qué valor, lo decide la proyección
 * (`projection.ts`), que es donde vive el ciclo de vida.
 *
 * Reglas del SKILL que este módulo respeta a propósito:
 *
 *  - Se compara LÍNEA POR LÍNEA, nunca sobre el protocolo concatenado.
 *  - Se compara por TOKEN, no por substring de la línea entera.
 *  - La identidad de un miembro es su `base_species` (`Pokemon.identifies_as`).
 *  - La normalización saca TODA la puntuación y los diacríticos, que es lo que
 *    hace `to_id_str`.
 */

import type { DexPokemon } from "./types.js";

export function normalizeProtocolText(value: string): string {
  return value
    .normalize("NFD")
    .replace(/\p{Mark}/gu, "")
    .replace(/[\p{Punctuation}\p{Separator}\p{Symbol}]/gu, "")
    .toLocaleLowerCase("en-US");
}

/** `Move.retrieve_id` (`move.py:561-575`).
 *
 * poke-env guarda el movimiento bajo un id COLAPSADO: los 17 Hidden Power
 * comparten `hiddenpower`, y Return/Frustration colapsan por potencia. No es
 * una regla genérica de prefijos: es exactamente esta lista de tres. */
export function retrieveMoveId(moveName: string): string {
  const id = normalizeProtocolText(moveName);
  if (id.startsWith("hiddenpower")) return "hiddenpower";
  if (id.startsWith("return")) return "return";
  if (id.startsWith("frustration")) return "frustration";
  return id;
}

/** `SPECIAL_MOVES` (`move.py:17`): poke-env nunca los guarda en el moveset. */
export const SPECIAL_MOVES = new Set(["struggle", "recharge", "fight"]);

export interface ProtocolIdent {
  side: string;
  name: string;
}

/** `p2a: Yanmega` -> `{ side: "p2", name: "yanmega" }`.
 *
 * poke-env descarta la letra de ranura para armar la clave del equipo
 * (`get_pokemon`, `abstract_battle.py:240-244`), así que la identidad de un
 * miembro es `lado + apodo` y una Mega no crea un miembro nuevo. */
export function parseIdent(raw: string | undefined): ProtocolIdent | undefined {
  if (raw === undefined) return undefined;
  const match = /^(p[1-9])[a-z]?:\s*(.+)$/i.exec(raw.trim());
  if (!match) return undefined;
  const name = normalizeProtocolText(match[2]);
  return name.length === 0 ? undefined : { side: match[1].toLowerCase(), name };
}

export function identKey(ident: ProtocolIdent): string {
  return `${ident.side}:${ident.name}`;
}

/** `Yanmega, L82, F` -> especie y nivel, con la semántica de
 * `_update_from_details` (`pokemon.py:669-714`).
 *
 * El nivel es 100 cuando el `details` NO trae token `L`: Showdown omite `L100`.
 * Eso vale para un `details`; un `-formechange` trae SÓLO la especie y no
 * cambia el nivel en absoluto, así que ese camino no pasa por acá. */
export function parseDetails(raw: string | undefined): {
  species: string;
  level: number;
} | undefined {
  if (raw === undefined) return undefined;
  const cleaned = raw.replace(/,\s*shiny/i, "");
  const parts = cleaned.split(",").map((part) => part.trim()).filter(
    (part) => !/^tera:/i.test(part),
  );
  const species = normalizeProtocolText(parts[0] ?? "");
  if (species.length === 0) return undefined;
  for (const part of parts.slice(1)) {
    const match = /^L(\d+)$/i.exec(part);
    if (match) return { species, level: Number(match[1]) };
  }
  return { species, level: 100 };
}

/** `55/100 par`, `0 fnt`, `100/100`.
 *
 * Espejo de `set_hp_status` (`pokemon.py:534-555`): `0 fnt` es un desmayo, el
 * token de status PISA el status previo, y su ausencia lo BORRA. */
export function parseHpToken(raw: string | undefined): {
  fraction: number;
  max: number;
  status: string | null;
  fainted: boolean;
} | undefined {
  if (raw === undefined) return undefined;
  const token = raw.trim();
  if (/^0\s+fnt$/i.test(token)) {
    return { fraction: 0, max: 100, status: "fnt", fainted: true };
  }
  const match = /^(\d+)(?:\/(\d+))?(?:\s+([a-z]+))?/i.exec(token);
  if (!match) return undefined;
  const current = Number(match[1]);
  const total = match[2] === undefined ? undefined : Number(match[2]);
  if (total !== undefined && total === 0) return undefined;
  const fraction = total === undefined ? (current === 0 ? 0 : 1) : current / total;
  const rawStatus = match[3]?.toLowerCase() ?? null;
  return {
    fraction,
    max: total ?? 100,
    status: rawStatus,
    fainted: current === 0,
  };
}

/** El dex local de UNA generación, con la resolución cosmética de D32.
 *
 * Antes esto resolvía por el prefijo más largo y sobre el dex de TODAS las
 * generaciones a la vez. Fallaba abierto por partida doble: `furfroubanana`
 * resolvía a `furfrou` igual que `furfroupharaoh`, y un `gholdengo` (que sólo
 * existe en gen 9) resolvía dentro de una batalla de gen 6. Con la especie
 * "resuelta", la proyección marcaba `types` y `ability` como no derivables y
 * el comparador dejaba de auditarlos: cero violaciones sobre una fila
 * inventada.
 *
 * El orden ahora es el de D32, y no admite terceras vías:
 *
 *  1. **La fila directa de ESTA generación gana.** Es lo que dice Postgres, y
 *     Postgres es quien decide disponibilidad.
 *  2. Si no hay fila, sólo un miembro REAL de `cosmeticFormes` cae a su base,
 *     y sólo si esa base tiene fila en esta generación. El id visible se
 *     conserva (D32) para poder correlacionar por especie revelada.
 *  3. Cualquier otra cosa es **desconocida** y el auditor falla cerrado.
 */
export interface SpeciesIndex {
  gen: number;
  /** La fila directa de esta generación, si existe. */
  row(speciesId: string): DexPokemon | undefined;
  /** La base de un alias cosmético REAL, si lo es. */
  cosmeticBase(speciesId: string): string | undefined;
  /** La entrada efectiva: fila directa, o la base de un alias cosmético. */
  resolve(speciesId: string): DexPokemon | undefined;
  /** ¿El dex de esta generación conoce esta especie? `false` = fallo cerrado. */
  knows(speciesId: string): boolean;
}

export function buildSpeciesIndex(
  entries: Iterable<DexPokemon>,
  gen: number,
  cosmeticAliases: Iterable<{ gen: number; aliasId: string; baseId: string }> = [],
): SpeciesIndex {
  const rows = new Map<string, DexPokemon>();
  for (const entry of entries) {
    if (entry.gen !== gen) continue;
    rows.set(normalizeProtocolText(entry.showdownId), entry);
  }
  const cosmetic = new Map<string, string>();
  for (const alias of cosmeticAliases) {
    if (alias.gen !== gen) continue;
    cosmetic.set(alias.aliasId, alias.baseId);
  }
  const row = (speciesId: string): DexPokemon | undefined =>
    rows.get(normalizeProtocolText(speciesId));
  const cosmeticBase = (speciesId: string): string | undefined => {
    const normalized = normalizeProtocolText(speciesId);
    if (rows.has(normalized)) return undefined;
    const base = cosmetic.get(normalized);
    return base !== undefined && rows.has(base) ? base : undefined;
  };
  const resolve = (speciesId: string): DexPokemon | undefined => {
    const direct = row(speciesId);
    if (direct !== undefined) return direct;
    const base = cosmeticBase(speciesId);
    return base === undefined ? undefined : rows.get(base);
  };
  return {
    gen,
    row,
    cosmeticBase,
    resolve,
    knows: (speciesId) => resolve(speciesId) !== undefined,
  };
}

/** Identidad canónica de una especie: su `base_species` según el dex local.
 * Es el criterio de `Pokemon.identifies_as`, no una comparación por `species`
 * (que rompe con toda forma alternativa: Arceus-Poison, Rotom-Wash). */
export function canonicalIdentity(species: string, index: SpeciesIndex): string {
  const normalized = normalizeProtocolText(species);
  const entry = index.resolve(normalized);
  return entry ? normalizeProtocolText(entry.baseSpecies) : normalized;
}

/** El lado observado desde `playerSide`. `p1` observa a `p2` y viceversa. */
export function opponentSideOf(playerSide: string): string {
  return playerSide === "p1" ? "p2" : "p1";
}
