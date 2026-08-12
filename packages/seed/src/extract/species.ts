import { isAvailableForExtraction, type ModdedDex } from "./dex.js";
import type { SpeciesRow } from "../types.js";

/** Showdown usa "" en vez de null para forme y prevo. */
const orNull = (value: string | undefined | null): string | null =>
  value ? value : null;

export function extractSpecies(dex: ModdedDex): SpeciesRow[] {
  return dex.species
    .all()
    .filter((s) => isAvailableForExtraction(dex, s, "species"))
    .map((s) => ({
      showdownId: s.id,
      dexNum: s.num,
      name: s.name,
      // Misma normalizacion que evolvesFrom: el id, no el nombre legible (D2).
      baseSpecies: dex.species.get(s.baseSpecies).id,
      baseSpeciesName: s.baseSpecies,
      forme: orNull(s.forme),
      isDefault: s.name === s.baseSpecies,
      types: [...s.types],
      baseStats: {
        hp: s.baseStats.hp, atk: s.baseStats.atk, def: s.baseStats.def,
        spa: s.baseStats.spa, spd: s.baseStats.spd, spe: s.baseStats.spe,
      },
      abilities: { ...s.abilities } as Record<string, string>,
      weightKg: typeof s.weightkg === "number" ? s.weightkg : null,
      evolvesFrom: orNull(s.prevo) ? dex.species.get(s.prevo).id : null,
      tier: orNull(s.tier),
    }));
}
