import { isAvailableForExtraction, type ModdedDex } from "./dex.js";
import type { LearnMethod, LearnMethodName, LearnsetRow } from "../types.js";

const METHOD_BY_LETTER: Record<string, LearnMethodName> = {
  L: "level",
  M: "machine",
  T: "tutor",
  E: "egg",
  S: "event",
  D: "dream",
  V: "transfer",
  C: "tradeback",
  R: "reminder",
};

/** Formato: <gen><letra><resto>. Ej: "6L47", "6M", "6S5", "9M". */
export function parseLearnCode(code: string, sourceSpecies: string): LearnMethod {
  const match = /^(\d)([A-Z])(\d*)$/.exec(code);
  if (!match) {
    throw new Error(`Codigo de aprendizaje invalido: ${JSON.stringify(code)}`);
  }
  const [, genPart, letter, rest] = match;
  const method = METHOD_BY_LETTER[letter];
  if (!method) {
    throw new Error(
      `Metodo de aprendizaje desconocido ${JSON.stringify(letter)} en el codigo ${code}. ` +
        `Conocidos: ${Object.keys(METHOD_BY_LETTER).join(", ")}. ` +
        `Si el paquete agrego un codigo nuevo, mapearlo antes de seedear.`,
    );
  }
  const parsed: LearnMethod = { gen: Number(genPart), method, sourceSpecies };
  // Solo el nivel usa el sufijo numerico; en los eventos es el indice y se descarta.
  if (method === "level" && rest !== "") parsed.level = Number(rest);
  return parsed;
}

/**
 * La herencia de formas es ADITIVA: recorre la rama propia de la forma y la
 * rama de baseSpecies, deduplicadas. Elegir `own.prevo || own.baseSpecies`
 * pierde una de las dos: las formas regionales necesitan su prevo regional,
 * mientras Gourgeist necesita conservar tambien Gourgeist -> Pumpkaboo.
 */
function inheritanceChain(dex: ModdedDex, speciesId: string): string[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  const own = dex.species.get(speciesId);

  const appendBranch = (startId: string): void => {
    let current = dex.species.get(startId);
    while (current?.exists && !seen.has(current.id)) {
      seen.add(current.id);
      chain.push(current.id);
      if (!current.prevo) break;
      current = dex.species.get(current.prevo);
    }
  };

  appendBranch(own.id);
  if (own.name !== own.baseSpecies) appendBranch(dex.species.get(own.baseSpecies).id);
  return chain;
}

export async function extractLearnsets(dex: ModdedDex): Promise<LearnsetRow[]> {
  const species = dex.species.all().filter((s) => isAvailableForExtraction(dex, s, "species"));
  const legalMoveIds = new Set(
    dex.moves.all().filter((m) => isAvailableForExtraction(dex, m, "move")).map((m) => m.id),
  );

  // Cache: la mega y la base comparten cadena, no vale la pena releer el archivo.
  const directCache = new Map<string, Record<string, string[]>>();
  const directLearnset = async (id: string): Promise<Record<string, string[]>> => {
    const cached = directCache.get(id);
    if (cached) return cached;
    const data = await dex.species.getLearnsetData(id);
    const learnset = (data?.learnset ?? {}) as Record<string, string[]>;
    directCache.set(id, learnset);
    return learnset;
  };

  const rows: LearnsetRow[] = [];
  for (const s of species) {
    const methodsByMove = new Map<string, LearnMethod[]>();
    for (const ancestorId of inheritanceChain(dex, s.id)) {
      const learnset = await directLearnset(ancestorId);
      for (const [moveId, codes] of Object.entries(learnset)) {
        if (!legalMoveIds.has(moveId)) continue;
        for (const code of codes) {
          const method = parseLearnCode(code, ancestorId);
          if (method.gen > dex.gen) continue;
          const existing = methodsByMove.get(moveId);
          if (existing) existing.push(method);
          else methodsByMove.set(moveId, [method]);
        }
      }
    }
    for (const [moveId, methods] of methodsByMove) {
      rows.push({ speciesId: s.id, moveId, methods });
    }
  }
  return rows;
}
