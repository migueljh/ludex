import { isAvailable, type ModdedDex } from "./dex.js";
import type { AbilityRow, ItemRow } from "../types.js";

export function extractItems(dex: ModdedDex): ItemRow[] {
  return dex.items
    .all()
    .filter((i) => isAvailable(dex, i))
    .map((i) => ({
      showdownId: i.id,
      name: i.name,
      description: i.desc || i.shortDesc || null,
      properties: {
        isBerry: Boolean(i.isBerry),
        isGem: Boolean(i.isGem),
        isChoice: Boolean(i.isChoice),
        isPokeball: Boolean(i.isPokeball),
        megaStone: i.megaStone ?? null,
        megaEvolves: i.megaEvolves ?? null,
        naturalGift: i.naturalGift ?? null,
        fling: i.fling ?? null,
      },
    }));
}

export function extractAbilities(dex: ModdedDex): AbilityRow[] {
  return dex.abilities
    .all()
    .filter((a) => isAvailable(dex, a))
    .map((a) => ({
      showdownId: a.id,
      name: a.name,
      description: a.desc || a.shortDesc || null,
    }));
}
