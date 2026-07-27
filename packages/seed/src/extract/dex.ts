import { createRequire } from "node:module";
// `import { Dex }` falla bajo el loader ESM nativo de Node: esbuild compila
// pokemon-showdown de forma que sus exports quedan como getters no
// configurables, que cjs-module-lexer no detecta (reporta cero exports
// nombrados). Vitest no lo muestra porque usa su propio interop. El import por
// defecto mas desestructuracion funciona con cualquier modulo CJS y evita tener
// que parchear el paquete.
import showdown from "pokemon-showdown";

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
