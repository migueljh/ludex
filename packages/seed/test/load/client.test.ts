import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Pool } from "pg";
import { createPool, upsertBatch } from "../../src/load/client.js";

describe("upsertBatch", () => {
  let pool: Pool;

  beforeAll(async () => {
    pool = createPool();
    await pool.query(`
      CREATE TABLE IF NOT EXISTS upsert_probe (
        id serial PRIMARY KEY,
        k  text NOT NULL,
        v  text NOT NULL,
        UNIQUE (k)
      )`);
    await pool.query("TRUNCATE upsert_probe");
  });

  afterAll(async () => {
    await pool.query("DROP TABLE IF EXISTS upsert_probe");
    await pool.end();
  });

  it("inserta filas nuevas", async () => {
    const n = await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"],
      rows: [["a", "1"], ["b", "2"]],
    });
    expect(n).toBe(2);
  });

  it("actualiza en vez de duplicar cuando la clave ya existe", async () => {
    await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"],
      rows: [["a", "MODIFICADO"]],
    });
    const { rows } = await pool.query("SELECT k, v FROM upsert_probe ORDER BY k");
    expect(rows).toEqual([{ k: "a", v: "MODIFICADO" }, { k: "b", v: "2" }]);
  });

  it("corta en lotes sin pasarse del limite de parametros de Postgres", async () => {
    const many = Array.from({ length: 5000 }, (_, i) => [`k${i}`, `v${i}`]);
    const n = await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"], rows: many,
    });
    expect(n).toBe(5000);
    const { rows } = await pool.query("SELECT count(*)::int AS c FROM upsert_probe");
    expect(rows[0].c).toBe(5002);
  });

  it("no rompe con cero filas", async () => {
    expect(await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"], rows: [],
    })).toBe(0);
  });
});
