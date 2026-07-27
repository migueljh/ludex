import { afterAll, describe, expect, it } from "vitest";
import { createReadOnlyPool, loadDataset, READ_QUERIES } from "../src/db.js";

const pool = createReadOnlyPool();
afterAll(async () => { await pool.end(); });

describe("loadDataset", () => {
  it("usa solamente SELECT y carga el dataset real sin fijar su tamaño", async () => {
    expect(Object.values(READ_QUERIES).every((sql) => /^\s*SELECT\b/i.test(sql))).toBe(true);
    expect(Object.values(READ_QUERIES).every(
      (sql) => !/\b(?:INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b/i.test(sql),
    )).toBe(true);

    const dataset = await loadDataset(pool);
    expect(dataset.battles.length).toBeGreaterThan(0);
    expect(dataset.turns.length).toBeGreaterThan(0);
    expect(dataset.trajectories.length).toBeGreaterThan(0);
    expect(dataset.steps.length).toBeGreaterThan(0);
  });

  it("filtra por generación usando el gen real de la trayectoria", async () => {
    const dataset = await loadDataset(pool, 6);
    expect(dataset.trajectories.length).toBeGreaterThan(0);
    expect(new Set(dataset.trajectories.map((trajectory) => trajectory.gen))).toEqual(new Set([6]));
    const trajectoryIds = new Set(dataset.trajectories.map((trajectory) => trajectory.id));
    expect(dataset.steps.every((step) => trajectoryIds.has(step.trajectoryId))).toBe(true);
  });
});
