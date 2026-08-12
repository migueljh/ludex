import { createRequire } from "node:module";
// `import { Dex }` falla bajo el loader ESM nativo de Node: esbuild compila
// pokemon-showdown de forma que sus exports quedan como getters no
// configurables, que cjs-module-lexer no detecta (reporta cero exports
// nombrados). Vitest no lo muestra porque usa su propio interop. El import por
// defecto mas desestructuracion funciona con cualquier modulo CJS y evita tener
// que parchear el paquete.
import showdown from "pokemon-showdown";
import { loadRandomBattleCatalog } from "./random-battle-catalog.js";

const { Dex } = showdown;
const require = createRequire(import.meta.url);

export type ModdedDex = ReturnType<typeof Dex.mod>;

export const GENERATION_LABELS: Record<number, string> = {
  1: "RBY", 2: "GSC", 3: "RSE", 4: "DPPt/HGSS", 5: "BW/BW2",
  6: "XY/ORAS", 7: "SM/USUM", 8: "SwSh", 9: "SV",
};

export function loadGen(genNumber: number): ModdedDex {
  if (!Number.isInteger(genNumber) || !(genNumber in GENERATION_LABELS)) {
    throw new Error(
      `Generacion no soportada: ${genNumber}. Validas: ${Object.keys(GENERATION_LABELS).join(", ")}`,
    );
  }
  return Dex.mod(`gen${genNumber}`);
}

/**
 * Los mods de Showdown NO filtran por generacion: Dex.mod('gen' + N).species.all()
 * devuelve todas las entradas del dex, con el contenido posterior a esa generacion
 * marcado isNonstandard:'Future'.
 * Este predicado es el unico filtro que separa una generacion de otra.
 */
export function isAvailable(
  dex: ModdedDex,
  entry: { gen: number; isNonstandard?: string | null },
): boolean {
  return entry.gen <= dex.gen && !entry.isNonstandard;
}

export function packageVersion(): string {
  return require("pokemon-showdown/package.json").version as string;
}

/** Distingue el tipo de entidad que evalúa `isAvailableForExtraction`: una
 * especie nunca puede colarse por la excepción vía un id de movimiento, y
 * viceversa (D47/MON-24). */
export type CatalogEntityKind = "species" | "move";

/**
 * `isAvailable()` más una excepción separada, declarativa y tipada para
 * contenido `isNonstandard:"Unobtainable"` que el catálogo estándar
 * random-battle de la MISMA generación referencia realmente (D47/MON-24).
 *
 * `isNonstandard:"Unobtainable"` describe obtenibilidad real-world (una
 * distribución de evento ya terminada), no legalidad de batalla del
 * simulador: Floette-Eternal (`floetteeternal`) es la ÚNICA forma en que la
 * línea de Floette aparece en `gen6randombattle` -- no existe una entrada
 * "floette" base en `data/random-battles/gen6/sets.json` del paquete
 * pineado -- y su movimiento de firma `lightofruin` está en su movepool ahí
 * mismo. `isAvailable()` en sí NO cambia (D32/D33 y sus canarios siguen
 * intactos); esta función es la única vía nueva.
 *
 * La excepción exige las CUATRO condiciones a la vez:
 * 1. `entry.gen <= dex.gen` (nunca contenido de una generación posterior);
 * 2. `entry.isNonstandard === "Unobtainable"` exactamente (CAP, Future o
 *    cualquier otro valor quedan fuera, aunque el catálogo los referenciara);
 * 3. el id está en el conjunto del catálogo de ESA generación, del tipo
 *    (species/move) correspondiente;
 * 4. el tipo de entidad coincide: un movimiento nunca entra por aparecer
 *    como clave de especie del catálogo, ni al revés (los dos conjuntos del
 *    catálogo son independientes).
 *
 * `thousandarrows`/`thousandwaves` comparten el mismo
 * `isNonstandard:"Unobtainable"` que `lightofruin` en gen 6, pero ningún set
 * estándar los usa: por eso siguen excluidos (canario D12, ver
 * `.claude/showdown-data/SKILL.md`). `paleowave`/`shadowstrike` son CAP, no
 * Unobtainable: tampoco entran. `pikachupartner`/`pikachuworld`/
 * `charizardgmax` son Future de generaciones posteriores: siguen
 * excluidos por (1) y (2), sin necesidad de mirar el catálogo.
 */
export function isAvailableForExtraction(
  dex: ModdedDex,
  entry: { gen: number; id: string; isNonstandard?: string | null },
  kind: CatalogEntityKind,
): boolean {
  if (isAvailable(dex, entry)) return true;
  if (entry.gen > dex.gen) return false;
  if (entry.isNonstandard !== "Unobtainable") return false;
  const catalog = loadRandomBattleCatalog(dex.gen);
  const referenced = kind === "species" ? catalog.speciesIds : catalog.moveIds;
  return referenced.has(entry.id);
}
