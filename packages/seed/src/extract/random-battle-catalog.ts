import { createRequire } from "node:module";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";

// Mismo motivo que dex.ts: createRequire resuelve contra el paquete pineado
// real instalado (node_modules), nunca contra cwd -- indispensable aca porque
// el archivo que buscamos NO se importa como modulo JS, se lee como JSON
// crudo desde una ruta que depende de DONDE vive pokemon-showdown, no de
// donde corre este script.
const require = createRequire(import.meta.url);

/**
 * El catalogo estandar random-battle de una generacion (data/random-battles/
 * genN/{sets,data}.json del paquete pineado pokemon-showdown) es la unica
 * fuente que decide si una entrada `isNonstandard:"Unobtainable"` es
 * battle-legal en esa generacion pese a no ser obtenible cartridge-wise.
 * `speciesIds` son las claves de nivel superior del catalogo; `moveIds` es la
 * union de todos los movimientos referenciados en los movepools de esa
 * generacion. Los dos conjuntos son independientes: una especie nunca entra
 * en moveIds ni viceversa (D47/MON-24, ver dex.ts:isAvailableForExtraction).
 */
export interface RandomBattleCatalog {
  readonly genNumber: number;
  readonly speciesIds: ReadonlySet<string>;
  readonly moveIds: ReadonlySet<string>;
}

const cache = new Map<number, RandomBattleCatalog>();

function packageRandomBattlesDir(): string {
  const pkgJsonPath = require.resolve("pokemon-showdown/package.json");
  return join(dirname(pkgJsonPath), "dist", "data", "random-battles");
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === "string");
}

/**
 * Shape "sets.json" (gen 2-7, 9): { [speciesId]: { sets: [{ movepool: string[], ... }] } }.
 * Un set sin `movepool` valido es un shape inesperado: falla ruidoso, no se
 * saltea en silencio (eso ampliaria disponibilidad por omision si el shape
 * cambia y el parser lo ignora).
 */
function movesFromSetsShape(filePath: string, speciesId: string, entry: Record<string, unknown>): Set<string> {
  const sets = entry.sets;
  if (!Array.isArray(sets) || sets.length === 0) {
    throw new Error(
      `catalogo random-battle invalido en ${filePath}: "${speciesId}".sets no es un array no vacio`,
    );
  }
  const moves = new Set<string>();
  for (const set of sets) {
    if (!isPlainObject(set) || !isStringArray(set.movepool)) {
      throw new Error(
        `catalogo random-battle invalido en ${filePath}: un set de "${speciesId}" no tiene movepool: string[]`,
      );
    }
    for (const moveId of set.movepool) moves.add(moveId);
  }
  return moves;
}

/** Shape "data.json" (gen 1, 8): { [speciesId]: { moves: string[] } }. */
function movesFromDataShape(filePath: string, speciesId: string, entry: Record<string, unknown>): Set<string> {
  if (!isStringArray(entry.moves)) {
    throw new Error(
      `catalogo random-battle invalido en ${filePath}: "${speciesId}".moves no es string[]`,
    );
  }
  return new Set(entry.moves);
}

function parseCatalogFile(filePath: string): { speciesIds: Set<string>; moveIds: Set<string> } {
  let raw: string;
  try {
    raw = readFileSync(filePath, "utf8");
  } catch (err) {
    throw new Error(`no se pudo leer el catalogo random-battle en ${filePath}: ${(err as Error).message}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new Error(`catalogo random-battle invalido (JSON malformado) en ${filePath}: ${(err as Error).message}`);
  }
  if (!isPlainObject(parsed)) {
    throw new Error(`catalogo random-battle invalido en ${filePath}: la raiz no es un objeto`);
  }

  const speciesIds = new Set<string>();
  const moveIds = new Set<string>();
  for (const [speciesId, value] of Object.entries(parsed)) {
    if (!isPlainObject(value)) {
      throw new Error(
        `catalogo random-battle invalido en ${filePath}: la entrada "${speciesId}" no es un objeto`,
      );
    }
    speciesIds.add(speciesId);
    let moves: Set<string>;
    if ("sets" in value) {
      moves = movesFromSetsShape(filePath, speciesId, value);
    } else if ("moves" in value) {
      moves = movesFromDataShape(filePath, speciesId, value);
    } else {
      throw new Error(
        `catalogo random-battle invalido en ${filePath}: "${speciesId}" no tiene "sets" ni "moves" ` +
          `(shape desconocido; nunca se amplia disponibilidad por defecto)`,
      );
    }
    for (const moveId of moves) moveIds.add(moveId);
  }
  if (speciesIds.size === 0) {
    throw new Error(`catalogo random-battle vacio en ${filePath}: 0 especies referenciadas`);
  }
  return { speciesIds, moveIds };
}

/**
 * Carga (y cachea) el catalogo estandar random-battle de una generacion desde
 * el paquete pineado. Prueba "sets.json" primero (gen 2-7, 9), despues
 * "data.json" (gen 1, 8, formato mas viejo). Si ninguno de los dos existe, o
 * el que existe tiene un shape que no reconoce, falla ruidosamente: nunca
 * devuelve un catalogo vacio por defecto, porque eso ampliaria
 * disponibilidad en silencio para toda entrada Unobtainable de esa
 * generacion (ver isAvailableForExtraction en dex.ts).
 */
export function loadRandomBattleCatalog(genNumber: number): RandomBattleCatalog {
  const cached = cache.get(genNumber);
  if (cached) return cached;

  const dir = join(packageRandomBattlesDir(), `gen${genNumber}`);
  const setsPath = join(dir, "sets.json");
  const dataPath = join(dir, "data.json");

  let filePath: string;
  if (existsSync(setsPath)) filePath = setsPath;
  else if (existsSync(dataPath)) filePath = dataPath;
  else {
    throw new Error(
      `no existe catalogo random-battle estandar para gen ${genNumber} ` +
        `(se buscaron ${setsPath} y ${dataPath})`,
    );
  }

  const { speciesIds, moveIds } = parseCatalogFile(filePath);
  const catalog: RandomBattleCatalog = { genNumber, speciesIds, moveIds };
  cache.set(genNumber, catalog);
  return catalog;
}
