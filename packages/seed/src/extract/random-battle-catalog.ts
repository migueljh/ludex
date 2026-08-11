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

/**
 * Campos de LISTA DE MOVIMIENTOS reconocidos del shape "data.json". La union
 * completa entre generaciones: gen 1 usa moves/comboMoves/essentialMoves/
 * exclusiveMoves, gen 8 usa moves/doublesMoves/noDynamaxMoves. Ninguna
 * entrada real mezcla campos de una generacion con los de otra, asi que
 * listar la union entera y validarla contra lo que HAYA en cada entrada es
 * seguro y evita ramificar por generacion en el flujo productivo (L-01: no
 * hardcodear una generacion aca).
 *
 * Medido contra pokemon-showdown@0.11.10: `moves` NO siempre esta presente
 * -- 12 entradas Gmax de gen 8 (venusaurgmax entre ellas) solo traen
 * `doublesMoves`. Por eso la union exige al menos UNA lista reconocida, no
 * `moves` especificamente.
 */
const DATA_JSON_MOVE_LIST_FIELDS = [
  "moves", "comboMoves", "essentialMoves", "exclusiveMoves",
  "doublesMoves", "noDynamaxMoves",
] as const;

/** Campos escalares reconocidos del shape "data.json": no son listas de
 * movimientos: se ignoran sin error, nunca se validan como string[]. */
const DATA_JSON_SCALAR_FIELDS = ["level", "doublesLevel"] as const;

const DATA_JSON_KNOWN_FIELDS = new Set<string>([
  ...DATA_JSON_MOVE_LIST_FIELDS, ...DATA_JSON_SCALAR_FIELDS,
]);

/**
 * Shape "data.json" (gen 1, 8): { [speciesId]: { moves?, comboMoves?,
 * essentialMoves?, exclusiveMoves?, doublesMoves?, noDynamaxMoves?, level?,
 * doublesLevel? } }. Une TODAS las listas de movimientos reconocidas
 * presentes en la entrada -- nunca solo `moves` (L-01). Un campo que no es
 * ni lista reconocida ni escalar reconocido, o una lista reconocida con
 * tipo invalido, falla ruidoso: nunca se ignora en silencio, porque eso
 * podria excluir movimientos legales sin que ningun conteo lo delate.
 */
function movesFromDataShape(filePath: string, speciesId: string, entry: Record<string, unknown>): Set<string> {
  for (const field of Object.keys(entry)) {
    if (!DATA_JSON_KNOWN_FIELDS.has(field)) {
      throw new Error(
        `catalogo random-battle invalido en ${filePath}: "${speciesId}" tiene un campo desconocido "${field}"`,
      );
    }
  }
  const moves = new Set<string>();
  let foundRecognizedList = false;
  for (const field of DATA_JSON_MOVE_LIST_FIELDS) {
    if (!(field in entry)) continue;
    foundRecognizedList = true;
    const value = entry[field];
    if (!isStringArray(value)) {
      throw new Error(
        `catalogo random-battle invalido en ${filePath}: "${speciesId}".${field} no es string[]`,
      );
    }
    for (const moveId of value) moves.add(moveId);
  }
  if (!foundRecognizedList) {
    throw new Error(
      `catalogo random-battle invalido en ${filePath}: "${speciesId}" no tiene ninguna lista ` +
        `de movimientos reconocida (${DATA_JSON_MOVE_LIST_FIELDS.join(", ")})`,
    );
  }
  return moves;
}

/**
 * Procesa el catalogo YA PARSEADO de un archivo (JSON valido, todavia sin
 * tipar). Separado de la lectura de disco para poder probar shapes
 * sintéticos invalidos sin tocar el filesystem (ver
 * `random-battle-catalog.test.ts`).
 */
export function parseCatalogData(
  filePath: string, parsed: unknown,
): { speciesIds: Set<string>; moveIds: Set<string> } {
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
    const moves = "sets" in value
      ? movesFromSetsShape(filePath, speciesId, value)
      : movesFromDataShape(filePath, speciesId, value);
    for (const moveId of moves) moveIds.add(moveId);
  }
  if (speciesIds.size === 0) {
    throw new Error(`catalogo random-battle vacio en ${filePath}: 0 especies referenciadas`);
  }
  return { speciesIds, moveIds };
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
  return parseCatalogData(filePath, parsed);
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
