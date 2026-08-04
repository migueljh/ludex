import { afterAll, describe, expect, it } from "vitest";
import {
  createReadOnlyPool,
  EXPECTED_QUERY_COUNT,
  loadDataset,
  READ_QUERIES,
  type Queryable,
} from "../src/db.js";
import { parseScope, SCOPE_RULES } from "../src/scope.js";
import { SCOPES } from "../src/types.js";

/** Pool falso que cuenta llamadas y devuelve `rows` fabricadas. Existe para
 * que el canario de conteo de queries no dependa de Postgres.
 *
 * `gen_number: 6` en cada fila (MON-11): este canario sólo verifica
 * CANTIDAD de queries, no contenido, pero `loadDataset` deriva de
 * `trajectories[].gen` qué generaciones pedirle a `loadCosmeticAliases` --
 * que desde MON-11 falla cerrado si no encuentra un pokedex real para la
 * generación pedida (nunca degrada a cero alias). Una fila `{}` sin
 * `gen_number` pedía la generación `undefined`, que ningún pokedex real
 * puede satisfacer. 6 es una generación real que sí tiene pokedex
 * empaquetado. */
function countingPool(rowsPerQuery: number): { pool: Queryable; calls: string[] } {
  const calls: string[] = [];
  const pool: Queryable = {
    query: async (text: string) => {
      calls.push(text);
      return { rows: Array.from({ length: rowsPerQuery }, () => ({ gen_number: 6 })) };
    },
  };
  return { pool, calls };
}

describe("read-only por construcción", () => {
  it("todas las queries son SELECT y ninguna escribe", () => {
    for (const [name, sql] of Object.entries(READ_QUERIES)) {
      expect(sql.trimStart().startsWith("SELECT"), `${name} no arranca con SELECT`).toBe(true);
      expect(
        /\b(?:INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE|GRANT|COPY)\b/i.test(sql),
        `${name} contiene una sentencia de escritura`,
      ).toBe(false);
    }
  });
});

describe("canario: el número de queries NO crece con el dataset", () => {
  it("un dataset chico y uno grande cuestan lo mismo", async () => {
    const small = countingPool(1);
    const large = countingPool(50_000);
    await loadDataset(small.pool);
    await loadDataset(large.pool);

    expect(small.calls).toHaveLength(EXPECTED_QUERY_COUNT);
    expect(large.calls).toHaveLength(EXPECTED_QUERY_COUNT);
    expect(large.calls).toEqual(small.calls);
    // Valor exacto pineado: agregar una query por paso lo rompe, y agregar
    // una query fija obliga a actualizar el número a conciencia.
    expect(EXPECTED_QUERY_COUNT).toBe(6);
  });

  it("el scope y la generación se aplican en el SQL, no filtrando en memoria", async () => {
    const { pool, calls } = countingPool(0);
    await loadDataset(pool, { scope: "training", gen: 6 });
    expect(calls).toHaveLength(EXPECTED_QUERY_COUNT);
    const dataset = calls.filter((sql) => /FROM (battles|battle_turns|trajectories|trajectory_steps)/.test(sql));
    expect(dataset).toHaveLength(4);
    for (const sql of dataset) expect(sql).toContain("$1");
  });
});

describe("parseScope", () => {
  it("acepta los scopes declarados y rechaza cualquier otro", () => {
    expect(parseScope(undefined)).toBe("all");
    for (const scope of SCOPES) expect(parseScope(scope)).toBe(scope);
    expect(() => parseScope("entrenamiento")).toThrow(/scope desconocido/);
  });

  it("documenta qué excluye cada scope", () => {
    expect(SCOPE_RULES.all).toHaveLength(1);
    expect(SCOPE_RULES.training.join(" ")).toContain("source = 'test'");
    expect(SCOPE_RULES.training.join(" ")).toContain("final_result IS NULL");
  });
});

describe("contra PostgreSQL real", () => {
  const pool = createReadOnlyPool();
  afterAll(async () => { await pool.end(); });

  it("carga el dataset completo con scope all, incluidas las filas source='test'", async () => {
    const dataset = await loadDataset(pool, { scope: "all" });
    expect(dataset.battles.length).toBeGreaterThan(0);
    expect(dataset.turns.length).toBeGreaterThan(0);
    expect(dataset.trajectories.length).toBeGreaterThan(0);
    expect(dataset.steps.length).toBeGreaterThan(0);
    expect(dataset.dexPokemon.length).toBeGreaterThan(0);
    expect(dataset.dexMoves.length).toBeGreaterThan(0);
    expect(new Set(dataset.battles.map((battle) => battle.source))).toContain("test");
  }, 120_000);

  it("el scope training excluye toda fila no elegible para entrenamiento", async () => {
    const all = await loadDataset(pool, { scope: "all" });
    const training = await loadDataset(pool, { scope: "training" });

    expect(training.battles.every((battle) => battle.source !== "test")).toBe(true);
    expect(training.trajectories.every((t) => t.finalResult !== null)).toBe(true);
    const battleIds = new Set(training.battles.map((battle) => battle.id));
    expect(training.turns.every((turn) => battleIds.has(turn.battleId))).toBe(true);
    const trajectoryIds = new Set(training.trajectories.map((t) => t.id));
    expect(training.steps.every((step) => trajectoryIds.has(step.trajectoryId))).toBe(true);
    // Y es un subconjunto propio: si fuera igual, el filtro no está filtrando.
    expect(training.steps.length).toBeLessThan(all.steps.length);
  }, 120_000);

  it("--gen conserva el mismo contrato, filtrado por generación", async () => {
    const training = await loadDataset(pool, { scope: "training" });
    const gen = training.trajectories[0]?.gen;
    expect(gen).toBeDefined();
    const filtered = await loadDataset(pool, { scope: "training", gen });

    expect(new Set(filtered.trajectories.map((t) => t.gen))).toEqual(new Set([gen]));
    expect(filtered.battles.every((battle) => battle.source !== "test")).toBe(true);
    expect(filtered.trajectories.every((t) => t.finalResult !== null)).toBe(true);
    expect(filtered.dexPokemon.every((entry) => entry.gen === gen)).toBe(true);
    expect(filtered.dexMoves.every((entry) => entry.gen === gen)).toBe(true);
  }, 120_000);

  it("rechaza una generación que no es un entero positivo", async () => {
    await expect(loadDataset(pool, { gen: 0 })).rejects.toThrow(/entero positivo/);
    await expect(loadDataset(pool, { gen: 6.5 })).rejects.toThrow(/entero positivo/);
  });
});
