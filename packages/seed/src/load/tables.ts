import type { Pool } from "pg";
import { upsertBatch } from "./client.js";
import type {
  AbilityRow, ItemRow, LearnsetRow, MoveRow, SpeciesRow, TypeChartRow,
} from "../types.js";

export async function upsertGeneration(
  pool: Pool, genNumber: number, label: string,
): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    `INSERT INTO generations (gen_number, label) VALUES ($1, $2)
     ON CONFLICT (gen_number) DO UPDATE SET label = EXCLUDED.label
     RETURNING id`,
    [genNumber, label],
  );
  return rows[0].id;
}

export const loadSpecies = (pool: Pool, genId: number, rows: SpeciesRow[]) =>
  upsertBatch(pool, {
    table: "pokemon",
    columns: ["gen_id", "showdown_id", "dex_num", "name", "base_species", "forme",
      "is_default", "types", "base_stats", "abilities", "weight_kg", "evolves_from", "tier"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((s) => [genId, s.showdownId, s.dexNum, s.name, s.baseSpecies, s.forme,
      s.isDefault, s.types, JSON.stringify(s.baseStats), JSON.stringify(s.abilities),
      s.weightKg, s.evolvesFrom, s.tier]),
  });

export const loadMoves = (pool: Pool, genId: number, rows: MoveRow[]) =>
  upsertBatch(pool, {
    table: "moves",
    columns: ["gen_id", "showdown_id", "name", "type", "category", "power",
      "accuracy", "pp", "priority", "target", "flags", "description"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((m) => [genId, m.showdownId, m.name, m.type, m.category, m.power,
      m.accuracy, m.pp, m.priority, m.target, JSON.stringify(m.flags), m.description]),
  });

export const loadItems = (pool: Pool, genId: number, rows: ItemRow[]) =>
  upsertBatch(pool, {
    table: "items",
    columns: ["gen_id", "showdown_id", "name", "description", "properties"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((i) => [genId, i.showdownId, i.name, i.description, JSON.stringify(i.properties)]),
  });

export const loadAbilities = (pool: Pool, genId: number, rows: AbilityRow[]) =>
  upsertBatch(pool, {
    table: "abilities",
    columns: ["gen_id", "showdown_id", "name", "description"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((a) => [genId, a.showdownId, a.name, a.description]),
  });

export const loadTypeChart = (pool: Pool, genId: number, rows: TypeChartRow[]) =>
  upsertBatch(pool, {
    table: "type_chart",
    columns: ["gen_id", "attacking_type", "defending_type", "multiplier"],
    conflict: ["gen_id", "attacking_type", "defending_type"],
    rows: rows.map((t) => [genId, t.attackingType, t.defendingType, t.multiplier]),
  });

/** Necesita los ids ya insertados de pokemon y moves. */
export async function loadLearnsets(
  pool: Pool, genId: number, rows: LearnsetRow[],
): Promise<number> {
  const idsFor = async (table: string) => {
    const { rows: found } = await pool.query<{ showdown_id: string; id: number }>(
      `SELECT showdown_id, id FROM ${table} WHERE gen_id = $1`, [genId],
    );
    return new Map(found.map((r) => [r.showdown_id, r.id]));
  };
  const speciesIds = await idsFor("pokemon");
  const moveIds = await idsFor("moves");

  const tuples = rows.map((r) => {
    const speciesId = speciesIds.get(r.speciesId);
    const moveId = moveIds.get(r.moveId);
    if (speciesId === undefined) throw new Error(`Especie no cargada: ${r.speciesId}`);
    if (moveId === undefined) throw new Error(`Movimiento no cargado: ${r.moveId}`);
    return [speciesId, moveId, JSON.stringify(r.methods)];
  });

  return upsertBatch(pool, {
    table: "learnsets",
    columns: ["pokemon_id", "move_id", "learn_methods"],
    conflict: ["pokemon_id", "move_id"],
    rows: tuples,
  });
}
