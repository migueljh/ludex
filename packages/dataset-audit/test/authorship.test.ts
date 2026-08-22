/** D65 (MON-31/Fase 3 S2): mezcla de autoría del dataset (`action_source` x
 * `approval_outcome`) y elegibilidad de las filas humanas para `training`.
 *
 * D65 5.3: "Las filas humanas entran a scope=training. El auditor reporta
 * mezcla agent/human y los tres outcomes. La coherencia D38 se consulta
 * sobre toda trajectory_steps, nunca sólo sobre los tags de la corrida. La
 * mezcla se prueba en Fase 3 con datos sintéticos."
 *
 * Los escenarios con DB necesitan datos sintéticos (mismo motivo que
 * `test/d44.test.ts`: la base compartida no tiene hoy ninguna fila
 * `action_source='human'` ni `approval_outcome` poblado) -- corren contra
 * una base descartable, nunca contra `DATABASE_URL`.
 */

import { describe, expect, it } from "vitest";
import { loadDataset } from "../src/db.js";
import { computeAuthorshipMix, renderAuthorshipReport } from "../src/render.js";
import type { Dataset, TrajectoryStepRecord } from "../src/types.js";
import { createDisposableDatabase, type DisposableDatabase } from "./_disposable.js";
import { baseDataset } from "./fixtures.js";

const requiresTestDatabase = process.env.TEST_DATABASE_URL === undefined;

// --- Unidad: computeAuthorshipMix / renderAuthorshipReport sobre datasets
// en memoria, sin DB -------------------------------------------------------

function stepWith(overrides: Partial<TrajectoryStepRecord>): TrajectoryStepRecord {
  const base = baseDataset().steps[0];
  return { ...base, ...overrides };
}

describe("computeAuthorshipMix", () => {
  it("cuenta por action_source y por approval_outcome sobre TODO dataset.steps", () => {
    const dataset: Dataset = {
      ...baseDataset(),
      steps: [
        stepWith({ decisionIndex: 0, actionSource: "agent", approvalOutcome: "human_approved" }),
        stepWith({ decisionIndex: 1, actionSource: "human", approvalOutcome: "human_override" }),
        stepWith({ decisionIndex: 2, actionSource: "agent", approvalOutcome: "timeout_auto" }),
        stepWith({ decisionIndex: 3, actionSource: "agent", approvalOutcome: null }),
        stepWith({ decisionIndex: 4, actionSource: "opponent", approvalOutcome: undefined }),
      ],
    };

    const mix = computeAuthorshipMix(dataset);

    expect(mix.total).toBe(5);
    expect(mix.bySource).toEqual({ agent: 3, human: 1, opponent: 1 });
    expect(mix.byApprovalOutcome).toEqual({
      human_approved: 1,
      human_override: 1,
      timeout_auto: 1,
      "(sin outcome)": 2,
    });
  });

  it("un dataset sin pasos reporta total 0, no un total vacuo que esconda un bug de carga", () => {
    const mix = computeAuthorshipMix({ ...baseDataset(), steps: [] });
    expect(mix.total).toBe(0);
    expect(mix.bySource).toEqual({});
    expect(mix.byApprovalOutcome).toEqual({});
  });

  it("trata ausencia de approvalOutcome igual que null (fixtures preexistentes sin el campo)", () => {
    const sinCampo = stepWith({ decisionIndex: 0 });
    delete (sinCampo as Partial<TrajectoryStepRecord>).approvalOutcome;
    const mix = computeAuthorshipMix({ ...baseDataset(), steps: [sinCampo] });
    expect(mix.byApprovalOutcome).toEqual({ "(sin outcome)": 1 });
  });
});

describe("renderAuthorshipReport", () => {
  it("lista el total, la mezcla por source y por outcome en orden alfabético", () => {
    const dataset: Dataset = {
      ...baseDataset(),
      steps: [
        stepWith({ decisionIndex: 0, actionSource: "human", approvalOutcome: "human_override" }),
        stepWith({ decisionIndex: 1, actionSource: "agent", approvalOutcome: "human_approved" }),
      ],
    };
    const output = renderAuthorshipReport(dataset);

    expect(output).toContain("Autoría de 2 decisiones");
    expect(output).toContain("agent: 1");
    expect(output).toContain("human: 1");
    expect(output).toContain("human_approved: 1");
    expect(output).toContain("human_override: 1");
    expect(output.indexOf("agent: 1")).toBeLessThan(output.indexOf("human: 1"));
  });
});

// --- Integración: filas humanas contra una base descartable real ----------

interface StepFixture {
  decisionIndex: number;
  actionSource: "agent" | "human" | "opponent";
  approvalOutcome: "human_approved" | "human_override" | "timeout_auto" | null;
  withD38Metadata: boolean;
}

async function seedTrajectoryWithSteps(
  db: DisposableDatabase,
  battleId: number,
  trajectoryId: number,
  source: "local" | "test",
  steps: StepFixture[],
): Promise<void> {
  await db.pool.query(
    `INSERT INTO generations (id, gen_number, label) VALUES (1, 6, 'XY/ORAS')
     ON CONFLICT (id) DO NOTHING`,
  );
  await db.pool.query(
    `INSERT INTO battles (id, battle_tag, format, p1, p2, winner, played_by, source, identity_key)
     VALUES ($1, $2, 'gen6randombattle', 'A', 'B', 'A', 'bot', $3, $4)`,
    [battleId, `battle-authorship-${battleId}`, source, `fixture-identity-${battleId}`],
  );
  await db.pool.query(
    `INSERT INTO trajectories (id, battle_id, gen_id, format, player_side, final_result)
     VALUES ($1, $2, 1, 'gen6randombattle', 'p1', 'win')`,
    [trajectoryId, battleId],
  );
  for (const step of steps) {
    if (step.withD38Metadata) {
      await db.pool.query(
        `INSERT INTO trajectory_steps
           (trajectory_id, turn_number, decision_index, state, state_schema_version,
            legal_actions, action_taken, action_source, approval_outcome,
            rationale, confidence, alternatives, provider, model,
            decision_latency_ms, input_tokens, output_tokens,
            cached_input_tokens, reasoning_tokens)
         VALUES ($1, $2, $2, '{}'::jsonb, 2, '[]'::jsonb, '{"kind":"move","id":"x"}'::jsonb,
                 $3, $4, 'corto', 0.9, '[]'::jsonb, 'google', 'gemini-2.5-flash',
                 500, 10, 5, 2, 1)`,
        [trajectoryId, step.decisionIndex, step.actionSource, step.approvalOutcome],
      );
    } else {
      await db.pool.query(
        `INSERT INTO trajectory_steps
           (trajectory_id, turn_number, decision_index, state, state_schema_version,
            legal_actions, action_taken, action_source, approval_outcome)
         VALUES ($1, $2, $2, '{}'::jsonb, 2, '[]'::jsonb, '{"kind":"switch","species":"charizard"}'::jsonb,
                 $3, $4)`,
        [trajectoryId, step.decisionIndex, step.actionSource, step.approvalOutcome],
      );
    }
  }
}

describe.skipIf(requiresTestDatabase)("filas humanas: elegibilidad de training y mezcla real", () => {
  it("human/human_override (grupo D38 NULL) dentro de una trayectoria elegible entra a training", async () => {
    const db = await createDisposableDatabase(process.env.TEST_DATABASE_URL!);
    try {
      await seedTrajectoryWithSteps(db, 1, 1, "local", [
        { decisionIndex: 0, actionSource: "agent", approvalOutcome: "human_approved", withD38Metadata: true },
        { decisionIndex: 1, actionSource: "human", approvalOutcome: "human_override", withD38Metadata: false },
        { decisionIndex: 2, actionSource: "agent", approvalOutcome: "timeout_auto", withD38Metadata: true },
      ]);

      const all = await loadDataset(db.pool, { scope: "all" });
      const training = await loadDataset(db.pool, { scope: "training" });

      expect(all.steps).toHaveLength(3);
      expect(training.trajectories.map((t) => t.id)).toEqual([1]);
      expect(training.steps).toHaveLength(3);
      const human = training.steps.find((s) => s.actionSource === "human");
      expect(human).toBeDefined();
      expect(human!.approvalOutcome).toBe("human_override");

      const mix = computeAuthorshipMix(training);
      expect(mix.total).toBe(3);
      expect(mix.bySource).toEqual({ agent: 2, human: 1 });
      expect(mix.byApprovalOutcome).toEqual({
        human_approved: 1, human_override: 1, timeout_auto: 1,
      });
    } finally {
      await db.drop();
    }
  });

  it("una trayectoria mixta v1/v2 con filas humanas se excluye COMPLETA de training (D44 no hace excepción por action_source)", async () => {
    const db = await createDisposableDatabase(process.env.TEST_DATABASE_URL!);
    try {
      await db.pool.query(
        "INSERT INTO generations (id, gen_number, label) VALUES (1, 6, 'XY/ORAS')",
      );
      await db.pool.query(
        `INSERT INTO battles (id, battle_tag, format, p1, p2, winner, played_by, source, identity_key)
         VALUES (1, 'battle-authorship-mixed', 'gen6randombattle', 'A', 'B', 'A', 'bot', 'local', 'fixture-identity-mixed')`,
      );
      await db.pool.query(
        `INSERT INTO trajectories (id, battle_id, gen_id, format, player_side, final_result)
         VALUES (1, 1, 1, 'gen6randombattle', 'p1', 'win')`,
      );
      // Un paso humano en schema v2, uno agente en v1: la trayectoria mixta
      // se excluye completa, el paso humano NO se rescata por ser humano.
      await db.pool.query(
        `INSERT INTO trajectory_steps
           (trajectory_id, turn_number, decision_index, state, state_schema_version,
            legal_actions, action_taken, action_source, approval_outcome)
         VALUES (1, 0, 0, '{}'::jsonb, 2, '[]'::jsonb, '{"kind":"switch","species":"charizard"}'::jsonb,
                 'human', 'human_override')`,
      );
      await db.pool.query(
        `INSERT INTO trajectory_steps
           (trajectory_id, turn_number, decision_index, state, state_schema_version,
            legal_actions, action_taken, action_source)
         VALUES (1, 1, 1, '{}'::jsonb, 1, '[]'::jsonb, '{"kind":"move","id":"x"}'::jsonb, 'agent')`,
      );

      const all = await loadDataset(db.pool, { scope: "all" });
      const training = await loadDataset(db.pool, { scope: "training" });

      expect(all.steps).toHaveLength(2);
      expect(training.trajectories).toHaveLength(0);
      expect(training.steps).toHaveLength(0);
    } finally {
      await db.drop();
    }
  });

  it("source='test' con filas humanas queda excluido de training igual que cualquier otra fila de test", async () => {
    const db = await createDisposableDatabase(process.env.TEST_DATABASE_URL!);
    try {
      await seedTrajectoryWithSteps(db, 1, 1, "test", [
        { decisionIndex: 0, actionSource: "human", approvalOutcome: "human_override", withD38Metadata: false },
      ]);

      const all = await loadDataset(db.pool, { scope: "all" });
      const training = await loadDataset(db.pool, { scope: "training" });

      expect(all.steps).toHaveLength(1);
      expect(training.trajectories).toHaveLength(0);
      expect(training.steps).toHaveLength(0);
    } finally {
      await db.drop();
    }
  });

  it("la mezcla real global no depende de cuántas battle_tags distintas haya (consulta global, no por lote)", async () => {
    const db = await createDisposableDatabase(process.env.TEST_DATABASE_URL!);
    try {
      await seedTrajectoryWithSteps(db, 1, 1, "local", [
        { decisionIndex: 0, actionSource: "human", approvalOutcome: "human_override", withD38Metadata: false },
      ]);
      await seedTrajectoryWithSteps(db, 2, 2, "local", [
        { decisionIndex: 0, actionSource: "agent", approvalOutcome: "human_approved", withD38Metadata: true },
      ]);

      const training = await loadDataset(db.pool, { scope: "training" });
      const mix = computeAuthorshipMix(training);

      expect(mix.total).toBe(2);
      expect(mix.bySource).toEqual({ agent: 1, human: 1 });
    } finally {
      await db.drop();
    }
  });
});
