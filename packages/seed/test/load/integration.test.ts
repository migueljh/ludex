import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Pool } from "pg";
import { createPool } from "../../src/load/client.js";
import { seedGeneration } from "../../src/cli.js";

// D47/MON-24: pokemon 835 (+floetteeternal), moves 619 (+lightofruin),
// learnsets 62253 (+55, el learnset de floetteeternal). items/abilities/
// typeChart no cambian: la excepcion tipada no los toca (verdict item 8).
const GEN6_COUNTS = {
  pokemon: 835, moves: 619, items: 283, abilities: 191, typeChart: 324,
  learnsets: 62253,
};

describe("seedGeneration", () => {
  let pool: Pool;
  let counts: Record<string, number>;

  beforeAll(async () => {
    pool = createPool();
    counts = await seedGeneration(6);
  }, 600_000);

  afterAll(async () => { await pool.end(); });

  it("carga los conteos conocidos de gen 6", async () => {
    expect(counts).toMatchObject(GEN6_COUNTS);
    const genId = (await pool.query("SELECT id FROM generations WHERE gen_number = 6")).rows[0].id;
    for (const [table, expected] of [
      ["pokemon", GEN6_COUNTS.pokemon], ["moves", GEN6_COUNTS.moves],
      ["items", GEN6_COUNTS.items], ["abilities", GEN6_COUNTS.abilities],
      ["type_chart", GEN6_COUNTS.typeChart],
    ] as const) {
      const { rows } = await pool.query(
        `SELECT count(*)::int AS c FROM ${table} WHERE gen_id = $1`, [genId],
      );
      expect({ table, c: rows[0].c }).toEqual({ table, c: expected });
    }
  });

  it("registra la corrida con la version del paquete", async () => {
    const { rows } = await pool.query(
      `SELECT package_version, finished_at, row_counts FROM seed_runs ORDER BY id DESC LIMIT 1`,
    );
    expect(rows[0].package_version).toBe("0.11.10");
    expect(rows[0].finished_at).not.toBeNull();
    expect(rows[0].row_counts).toMatchObject(GEN6_COUNTS);
  });

  it("preserva los metodos de learnset sin aplanarlos", async () => {
    const { rows } = await pool.query(`
      SELECT l.learn_methods FROM learnsets l
      JOIN pokemon p ON p.id = l.pokemon_id
      JOIN moves   m ON m.id = l.move_id
      JOIN generations g ON g.id = p.gen_id
      WHERE g.gen_number = 6 AND p.showdown_id = 'charizard' AND m.showdown_id = 'dragondance'`);
    expect(rows).toHaveLength(1);
    expect(rows[0].learn_methods).toContainEqual({
      gen: 6, method: "egg", sourceSpecies: "charmander",
    });
  });

  it("ACTUALIZA las filas existentes al reseedear, no las ignora", async () => {
    // Comparar solo conteos no prueba nada: pasa igual si los upserts insertan
    // duplicados que una constraint descarta en silencio. Hay que mutar y verificar.
    const genId = (await pool.query("SELECT id FROM generations WHERE gen_number = 6")).rows[0].id;
    await pool.query(
      `UPDATE pokemon SET tier = 'BOGUS', weight_kg = 999 WHERE gen_id = $1 AND showdown_id = 'charizard'`,
      [genId],
    );
    await pool.query(
      `UPDATE moves SET power = 1 WHERE gen_id = $1 AND showdown_id = 'flamethrower'`,
      [genId],
    );

    await seedGeneration(6);

    const poke = await pool.query(
      `SELECT tier, weight_kg FROM pokemon WHERE gen_id = $1 AND showdown_id = 'charizard'`, [genId],
    );
    expect(poke.rows[0].tier).toBe("NU");
    expect(Number(poke.rows[0].weight_kg)).toBe(90.5);

    const move = await pool.query(
      `SELECT power FROM moves WHERE gen_id = $1 AND showdown_id = 'flamethrower'`, [genId],
    );
    expect(move.rows[0].power).toBe(90);

    const total = await pool.query(
      `SELECT count(*)::int AS c FROM pokemon WHERE gen_id = $1`, [genId],
    );
    expect(total.rows[0].c).toBe(GEN6_COUNTS.pokemon);
  }, 600_000);

  it("convive con otra generacion sin colisionar", async () => {
    await seedGeneration(9);
    // Filtrado a (6, 9): un GROUP BY sin filtro depende de que la base tenga
    // EXACTAMENTE esas dos generaciones. La primera vez que alguien corra
    // `pnpm seed --gen 7` contra esta misma base, ese toEqual queda roto de
    // forma permanente aunque nada este mal. Este test verifica lo suyo, no
    // el estado global de la base.
    const { rows } = await pool.query(`
      SELECT g.gen_number, count(*)::int AS c
      FROM pokemon p JOIN generations g ON g.id = p.gen_id
      WHERE g.gen_number IN (6, 9)
      GROUP BY g.gen_number ORDER BY g.gen_number`);
    expect(rows).toEqual([
      { gen_number: 6, c: 835 },
      { gen_number: 9, c: 874 },
    ]);
  }, 900_000);
});
