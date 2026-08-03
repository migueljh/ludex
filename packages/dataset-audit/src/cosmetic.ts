/** Alias cosméticos del dex de Showdown, por generación (D32).
 *
 * El respaldo por prefijo más largo fallaba ABIERTO: `furfroubanana` resolvía
 * a `furfrou` igual que `furfroupharaoh`, y con la especie "resuelta" el
 * auditor se abstenía de `types` y `ability`. Una fila inventada pasaba con
 * cero violaciones.
 *
 * D32 fija el único criterio admitido y este módulo lo reproduce tal cual:
 *
 *  - **`cosmeticFormes` explícito del dex es el criterio.** Una forma es
 *    cosmética sólo si figura en la lista `cosmeticFormes` de otra entrada.
 *    Las formas MECÁNICAS —Mega, Primal, Arceus por tipo, Castform por clima,
 *    `floetteeternal`— no figuran en ninguna y por lo tanto no se resuelven.
 *  - **Generation-scoped.** Cada generación tiene su propio archivo, así que
 *    un `gholdengo` (gen 9) simplemente no existe en gen 6.
 *  - **Postgres decide disponibilidad.** Esta tabla sólo dice "este id es un
 *    alias de aquella base"; que la BASE exista en la generación lo decide la
 *    tabla `pokemon` (ver `buildSpeciesIndex`).
 *
 * La fuente es el mismo dex local empaquetado que usa el agente
 * (`PokeEnvSpeciesVocabulary`): sin internet, sin fetch runtime y sin ninguna
 * lista de especies escrita a mano. Si no se puede leer, la tabla queda VACÍA
 * y el auditor falla cerrado — nunca al revés.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { normalizeProtocolText } from "./protocol.js";

export interface CosmeticAlias {
  gen: number;
  /** El `showdown_id` visible, que se conserva tras resolver. */
  aliasId: string;
  /** La especie base a la que cae. */
  baseId: string;
}

export interface CosmeticSource {
  aliases: CosmeticAlias[];
  /** De dónde salió. `undefined` = no se pudo leer, y entonces todo alias
   * cosmético falla cerrado. */
  directory: string | undefined;
}

/** Dónde vive el dex empaquetado. `LUDEX_SHOWDOWN_DEX_DIR` lo sobreescribe. */
function candidateDirectories(): string[] {
  const fromEnv = process.env.LUDEX_SHOWDOWN_DEX_DIR;
  if (fromEnv !== undefined && fromEnv.length > 0) return [fromEnv];
  const repoRoot = fileURLToPath(new URL("../../..", import.meta.url));
  const lib = join(repoRoot, "apps", "agent", ".venv", "lib");
  if (!existsSync(lib)) return [];
  const out: string[] = [];
  for (const python of readdirSync(lib)) {
    const candidate = join(
      lib, python, "site-packages", "poke_env", "data", "static", "pokedex",
    );
    if (existsSync(candidate)) out.push(candidate);
  }
  return out;
}

interface PokedexEntry {
  baseSpecies?: unknown;
  cosmeticFormes?: unknown;
}

/** Los alias cosméticos de las generaciones pedidas. */
export function loadCosmeticAliases(gens: Iterable<number>): CosmeticSource {
  const directory = candidateDirectories().find((candidate) => existsSync(candidate));
  if (directory === undefined) return { aliases: [], directory: undefined };
  const aliases: CosmeticAlias[] = [];
  for (const gen of new Set(gens)) {
    const file = join(directory, `gen${gen}pokedex.json`);
    if (!existsSync(file)) continue;
    let parsed: Record<string, PokedexEntry>;
    try {
      parsed = JSON.parse(readFileSync(file, "utf8")) as Record<string, PokedexEntry>;
    } catch {
      continue;
    }
    for (const [baseId, entry] of Object.entries(parsed)) {
      const formes = entry?.cosmeticFormes;
      if (!Array.isArray(formes) || formes.length === 0) continue;
      for (const forme of formes) {
        if (typeof forme !== "string") continue;
        const aliasId = normalizeProtocolText(forme);
        if (aliasId.length === 0 || aliasId === baseId) continue;
        aliases.push({ gen, aliasId, baseId: normalizeProtocolText(baseId) });
      }
    }
  }
  return { aliases, directory };
}
