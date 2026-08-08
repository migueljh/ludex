/** D44 (MON-11 R3, corregido R4): frontera canónica de `training`.
 *
 * `training` sólo incluye trayectorias con `source <> 'test'`,
 * `final_result IS NOT NULL`, AL MENOS UN paso, y TODOS sus pasos en
 * `state_schema_version = 2` -- una trayectoria mixta v1/v2 se excluye
 * COMPLETA, nunca se filtran pasos sueltos dentro de ella.
 *
 * R4 (BLOCKER 1, reproducción de Latwan `ZERO_STEP_SELECTED_BY_D44
 * count=1`): antes del `EXISTS`, una trayectoria `local`, finalizada, con
 * CERO pasos pasaba por vacuidad -- "ningún paso con otra versión" es
 * trivialmente cierto cuando no hay ningún paso. El caso de CERO pasos
 * tiene su propio test abajo, con la mutación dirigida que retira sólo el
 * `EXISTS` y confirma que ese test (y solo ese) se pone rojo.
 *
 * Los escenarios exigidos necesitan datos que la base compartida NO tiene
 * hoy (las 12 batallas `local` son 100% schema v1; no existe ninguna
 * trayectoria `local`/v2 ni mixta) -- por eso corren contra una base
 * descartable con fixtures sintéticas, nunca contra `DATABASE_URL`.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadDataset } from "../src/db.js";
import { createDisposableDatabase, type DisposableDatabase } from "./_disposable.js";

const requiresTestDatabase = process.env.TEST_DATABASE_URL === undefined;

interface BattleFixture {
  id: number;
  tag: string;
  source: "local" | "test";
}

interface TrajectoryFixture {
  id: number;
  battleId: number;
  finalResult: "win" | "loss" | "tie" | null;
  /** Versión de esquema de CADA paso, en orden. Un array `[2, 2, 1]`
   * produce una trayectoria mixta a propósito. */
  stepVersions: number[];
}

async function seed(
  db: DisposableDatabase,
  battles: BattleFixture[],
  trajectories: TrajectoryFixture[],
): Promise<void> {
  await db.pool.query(
    "INSERT INTO generations (id, gen_number, label) VALUES (1, 6, 'XY/ORAS')",
  );
  for (const b of battles) {
    await db.pool.query(
      `INSERT INTO battles (id, battle_tag, format, p1, p2, winner, played_by, source, identity_key)
       VALUES ($1, $2, 'gen6randombattle', 'A', 'B', NULL, 'bot', $3, $4)`,
      [b.id, b.tag, b.source, `fixture-identity-${b.id}`],
    );
  }
  for (const t of trajectories) {
    await db.pool.query(
      `INSERT INTO trajectories (id, battle_id, gen_id, format, player_side, final_result)
       VALUES ($1, $2, 1, 'gen6randombattle', 'p1', $3)`,
      [t.id, t.battleId, t.finalResult],
    );
    for (const [index, version] of t.stepVersions.entries()) {
      await db.pool.query(
        `INSERT INTO trajectory_steps
           (trajectory_id, turn_number, decision_index, state, state_schema_version,
            legal_actions, action_taken, action_source)
         VALUES ($1, $2, $2, '{}'::jsonb, $3, '[]'::jsonb, NULL, 'agent')`,
        [t.id, index, version],
      );
    }
  }
}

describe.skipIf(requiresTestDatabase)("D44: frontera canónica de training", () => {
  let db: DisposableDatabase;

  beforeEach(async () => {
    db = await createDisposableDatabase(process.env.TEST_DATABASE_URL!);
  });

  afterEach(async () => {
    await db.drop();
  });

  it("local/v1 (terminada): presente en all, ausente en training", async () => {
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "local" }],
      [{ id: 1, battleId: 1, finalResult: "win", stepVersions: [1, 1] }],
    );
    const all = await loadDataset(db.pool, { scope: "all" });
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(all.trajectories.map((t) => t.id)).toEqual([1]);
    expect(training.trajectories).toHaveLength(0);
    expect(training.steps).toHaveLength(0);
  });

  it("local/v2 terminada: presente en ambos", async () => {
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "local" }],
      [{ id: 1, battleId: 1, finalResult: "win", stepVersions: [2, 2] }],
    );
    const all = await loadDataset(db.pool, { scope: "all" });
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(all.trajectories.map((t) => t.id)).toEqual([1]);
    expect(training.trajectories.map((t) => t.id)).toEqual([1]);
    expect(training.steps).toHaveLength(2);
  });

  it("test/v2 (terminada): presente en all, ausente en training", async () => {
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "test" }],
      [{ id: 1, battleId: 1, finalResult: "win", stepVersions: [2, 2] }],
    );
    const all = await loadDataset(db.pool, { scope: "all" });
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(all.trajectories.map((t) => t.id)).toEqual([1]);
    expect(training.trajectories).toHaveLength(0);
  });

  it("local/v2 sin terminar (final_result NULL): ausente en training", async () => {
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "local" }],
      [{ id: 1, battleId: 1, finalResult: null, stepVersions: [2, 2] }],
    );
    const all = await loadDataset(db.pool, { scope: "all" });
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(all.trajectories.map((t) => t.id)).toEqual([1]);
    expect(training.trajectories).toHaveLength(0);
  });

  it("trayectoria mixta v1/v2: ausente COMPLETA de training, nunca aparecen sus pasos v2 sueltos", async () => {
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "local" }],
      [{ id: 1, battleId: 1, finalResult: "win", stepVersions: [2, 1, 2] }],
    );
    const all = await loadDataset(db.pool, { scope: "all" });
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(all.steps).toHaveLength(3);
    expect(training.trajectories).toHaveLength(0);
    // El defecto exacto que esto previene: un filtro por-paso dejaría pasar
    // los 2 pasos v2 (decision_index 0 y 2) y esconder solo el v1.
    expect(training.steps).toHaveLength(0);
  });

  it("mezcla realista: solo la trayectoria local/v2 terminada entra a training", async () => {
    await seed(
      db,
      [
        { id: 1, tag: "b1", source: "local" },
        { id: 2, tag: "b2", source: "local" },
        { id: 3, tag: "b3", source: "test" },
        { id: 4, tag: "b4", source: "local" },
      ],
      [
        { id: 1, battleId: 1, finalResult: "win", stepVersions: [1, 1] }, // local/v1
        { id: 2, battleId: 2, finalResult: "win", stepVersions: [2, 2] }, // local/v2 -- unica elegible
        { id: 3, battleId: 3, finalResult: "win", stepVersions: [2, 2] }, // test/v2
        { id: 4, battleId: 4, finalResult: null, stepVersions: [2, 2] }, // local/v2 sin terminar
      ],
    );
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(training.trajectories.map((t) => t.id)).toEqual([2]);
    expect(training.steps.every((s) => s.trajectoryId === 2)).toBe(true);
  });

  it("local/final_result no nulo/CERO steps: presente en all, ausente en training (R4 BLOCKER 1, ZERO_STEP_SELECTED_BY_D44)", async () => {
    // Antes del fix, `NOT EXISTS (paso con version <> 2)` es VACUAMENTE
    // cierto sobre una trayectoria sin ningun paso: "toda v2" no significa
    // nada cuando no hay nada que verificar. `training` la aceptaba sin
    // haber comprobado schema alguno. El `EXISTS` explicito cierra esto:
    // una trayectoria sin pasos nunca es "toda v2", es indeterminada.
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "local" }],
      [{ id: 1, battleId: 1, finalResult: "win", stepVersions: [] }],
    );
    const all = await loadDataset(db.pool, { scope: "all" });
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(all.trajectories.map((t) => t.id)).toEqual([1]);
    expect(training.trajectories).toHaveLength(0);
    expect(training.steps).toHaveLength(0);
  });

  it("corpus vacío bajo D44: training reporta 0 trayectorias, no un total vacuo", async () => {
    await seed(
      db,
      [{ id: 1, tag: "b1", source: "local" }],
      [{ id: 1, battleId: 1, finalResult: "win", stepVersions: [1, 1, 1] }],
    );
    const training = await loadDataset(db.pool, { scope: "training" });

    expect(training.trajectories).toHaveLength(0);
    expect(training.steps).toHaveLength(0);
    expect(training.battles.every((b) => b.source !== "test")).toBe(true);
  });
});
