import { isAvailable, type ModdedDex } from "./dex.js";
import type { MoveRow } from "../types.js";

export function extractMoves(dex: ModdedDex): MoveRow[] {
  // Las variantes de tipo de Hidden Power llegan como entradas separadas que
  // comparten el id base. La clave natural (gen_id, showdown_id) solo admite
  // una fila por id: se conserva la primera ocurrencia, que es la base.
  const seen = new Set<string>();
  return dex.moves
    .all()
    .filter((m) => isAvailable(dex, m))
    .filter((m) => {
      if (seen.has(m.id)) return false;
      seen.add(m.id);
      return true;
    })
    .map((m) => ({
      showdownId: m.id,
      name: m.name,
      type: m.type,
      category: m.category,
      power: m.basePower,
      // Showdown usa true para "nunca falla".
      accuracy: m.accuracy === true ? null : m.accuracy,
      pp: m.pp,
      priority: m.priority,
      target: m.target,
      flags: { ...m.flags } as Record<string, number>,
      description: m.desc || m.shortDesc || null,
    }));
}
