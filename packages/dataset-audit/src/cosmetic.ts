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
  /** De dónde salió. Siempre definido: `loadCosmeticAliases` NUNCA devuelve
   * un resultado degradado -- si no puede resolver el vocabulario completo,
   * lanza `CosmeticVocabularyError` en vez de volver con `undefined`/vacío
   * (MON-11, ver D33 y el CHECKPOINT de MON-18: un auditor sin este dex
   * clasificaba 170 especies reales como falsos positivos). */
  directory: string;
}

/** Lanzada cuando el vocabulario cosmético no se puede resolver por completo:
 * falta `.venv`/el dex empaquetado, falta el pokedex de alguna generación
 * pedida, el JSON es inválido, o el resultado quedaría en cero alias. Nunca
 * se degrada en silencio a una tabla vacía -- eso es exactamente lo que
 * produjo 170 falsos positivos de especie en el diagnóstico de MON-11. */
export class CosmeticVocabularyError extends Error {}

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

/** Los alias cosméticos de las generaciones pedidas.
 *
 * Falla CERRADO en cada punto donde antes se degradaba en silencio a una
 * tabla vacía (MON-11): directorio del dex no encontrado, pokedex faltante
 * para alguna generación pedida, JSON inválido, o cero alias resultantes
 * pese a haber generaciones pedidas. Con `gens` vacío (dataset sin filas)
 * sólo se valida que el directorio exista -- no hay generación cuyo
 * pokedex deba estar presente.
 */
export function loadCosmeticAliases(gens: Iterable<number>): CosmeticSource {
  const directory = candidateDirectories().find((candidate) => existsSync(candidate));
  if (directory === undefined) {
    throw new CosmeticVocabularyError(
      "No se encontró el dex local de poke-env para resolver alias cosméticos (D32). " +
      "Se probó apps/agent/.venv/lib/*/site-packages/poke_env/data/static/pokedex " +
      "(o $LUDEX_SHOWDOWN_DEX_DIR, si está seteada) y no existe ninguno de los dos. " +
      "Corré `uv sync` en apps/agent, o seteá LUDEX_SHOWDOWN_DEX_DIR. El auditor NUNCA " +
      "degrada a cero alias: una forma cosmética sin resolver pasa como especie " +
      "desconocida y contamina cada violación de esa fila.",
    );
  }
  const gensRequested = [...new Set(gens)];
  const aliases: CosmeticAlias[] = [];
  const missingPokedex: number[] = [];
  for (const gen of gensRequested) {
    const file = join(directory, `gen${gen}pokedex.json`);
    if (!existsSync(file)) {
      missingPokedex.push(gen);
      continue;
    }
    let parsed: Record<string, PokedexEntry>;
    try {
      parsed = JSON.parse(readFileSync(file, "utf8")) as Record<string, PokedexEntry>;
    } catch (cause) {
      throw new CosmeticVocabularyError(
        `El pokedex de gen ${gen} en ${file} no es JSON válido: ` +
        `${cause instanceof Error ? cause.message : String(cause)}`,
      );
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
  if (missingPokedex.length > 0) {
    throw new CosmeticVocabularyError(
      `Falta el pokedex empaquetado para gen ${missingPokedex.join(", ")} en ${directory}. ` +
      "El auditor no puede resolver formas cosméticas de esas generaciones sin él.",
    );
  }
  if (gensRequested.length > 0 && aliases.length === 0) {
    throw new CosmeticVocabularyError(
      `El dex en ${directory} no produjo ningún alias cosmético para gen ` +
      `${gensRequested.join(", ")}. Todas las generaciones que este proyecto audita ` +
      "tienen al menos una forma cosmética real (D32); cero alias significa un dex " +
      "vacío o corrupto, no un dato legítimo -- nunca se sigue en silencio.",
    );
  }
  return { aliases, directory };
}
