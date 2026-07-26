import { isAvailable, type ModdedDex } from "./dex.js";
import type { MoveRow } from "../types.js";

/**
 * En gen 6 los 17 Hidden Power (base + 16 tipos) comparten id 'hiddenpower'.
 * Se conserva la entrada cuyo nombre normaliza a su propio id, que es la base
 * ("Hidden Power", Normal, 60) — deterministico, sin depender del orden de
 * iteracion. Las variantes tipadas son alias de presentacion: el tipo real lo
 * determinan los IVs del pokemon, no la entrada del movimiento, y el protocolo
 * de batalla siempre reporta 'Hidden Power'.
 */
function dedupeById(dex: ModdedDex, moves: readonly { id: string; name: string }[]) {
  const byId = new Map<string, { id: string; name: string }>();
  for (const m of moves) {
    const existing = byId.get(m.id);
    if (!existing || dex.toID(m.name) === m.id) byId.set(m.id, m);
  }
  return [...byId.values()];
}

export function extractMoves(dex: ModdedDex): MoveRow[] {
  const available = dex.moves.all().filter((m) => isAvailable(dex, m));
  return (dedupeById(dex, available) as typeof available)
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
