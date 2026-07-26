import type { ModdedDex } from "./dex.js";
import type { TypeChartRow } from "../types.js";

/** Codigos de damageTaken de Showdown. */
const MULTIPLIER_BY_CODE: Record<number, number> = {
  0: 1,    // dano normal
  1: 2,    // super efectivo
  2: 0.5,  // resiste
  3: 0,    // inmune
};

/**
 * types.all() devuelve 19 tipos en TODAS las generaciones, asi que no sirve.
 * La interseccion de las claves de damageTaken de todos los tipos si da la
 * lista correcta por generacion: 15 en gen 1, 17 en gen 2 y 5, 18 en gen 6,
 * 19 en gen 9. Ademas descarta claves que no son tipos (psn, tox, sandstorm).
 */
export function typesForGen(dex: ModdedDex): string[] {
  const keySets = dex.types.all().map(
    (t) => new Set(Object.keys(t.damageTaken).filter((k) => dex.types.get(k).exists)),
  );
  if (keySets.length === 0) return [];
  let shared = keySets[0];
  for (const keys of keySets.slice(1)) {
    shared = new Set([...shared].filter((k) => keys.has(k)));
  }
  return [...shared].sort();
}

export function extractTypeChart(dex: ModdedDex): TypeChartRow[] {
  const types = typesForGen(dex);
  const rows: TypeChartRow[] = [];
  for (const defendingType of types) {
    const damageTaken = dex.types.get(defendingType).damageTaken;
    for (const attackingType of types) {
      const code = damageTaken[attackingType];
      if (code === undefined) {
        throw new Error(
          `gen${dex.gen}: falta damageTaken[${attackingType}] en el tipo ${defendingType}`,
        );
      }
      const multiplier = MULTIPLIER_BY_CODE[code];
      if (multiplier === undefined) {
        throw new Error(
          `gen${dex.gen}: codigo de damageTaken desconocido ${code} en ${attackingType}->${defendingType}`,
        );
      }
      rows.push({ attackingType, defendingType, multiplier });
    }
  }
  return rows;
}
