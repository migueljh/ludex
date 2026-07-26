import { createRequire } from "node:module";
import { Dex } from "pokemon-showdown";

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
