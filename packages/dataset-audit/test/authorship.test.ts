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
import {
  computeAuthorshipMix,
  opponentUsername,
  renderAuthorshipReport,
} from "../src/render.js";
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

// --- MON-40/Fase 3 S9: identidad del rival normalizada por rol p1/p2 ------
//
// `battles.p1`/`p2` no dicen por sí solos quién es el rival: hay que
// resolverlos contra `trajectories.player_side` (D-pendiente, mismo criterio
// que ya usa `cli._persist_one` para escribir `p1`/`p2` según el rol real).

describe("opponentUsername", () => {
  it("player_side='p1' => el rival es p2", () => {
    const battle = baseDataset().battles[0];
    expect(opponentUsername(battle, "p1")).toBe("Rival");
  });

  it("player_side='p2' => el rival es p1", () => {
    const battle = baseDataset().battles[0];
    expect(opponentUsername(battle, "p2")).toBe("LudexBot");
  });

  it("un player_side que no es 'p1' ni 'p2' falla cerrado, nunca asume un lado", () => {
    const battle = baseDataset().battles[0];
    expect(() => opponentUsername(battle, "p3")).toThrow(/p3/);
  });
});

describe("renderAuthorshipReport", () => {
  it("lista la identidad del rival por trayectoria, normalizada por player_side", () => {
    const output = renderAuthorshipReport(baseDataset());
    expect(output).toContain(`Rival de la trayectoria ${20} (p1): Rival`);
  });

  it("una trayectoria de p2 muestra a p1 como rival, no repite el propio nombre", () => {
    const dataset: Dataset = {
      ...baseDataset(),
      trajectories: [{ ...baseDataset().trajectories[0], playerSide: "p2" }],
    };
    const output = renderAuthorshipReport(dataset);
    expect(output).toContain("Rival de la trayectoria 20 (p2): LudexBot");
    expect(output).not.toContain("Rival de la trayectoria 20 (p2): Rival");
  });


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

// --- MON-40 R3 (TASOS REVIEW PACKET T-03, MINOR): identidad del rival -----
// contra una base descartable real, ejercitando p1 Y p2, sobre el dataset
// COMPLETO cargado por `loadDataset` (no un fixture en memoria: el mapeo
// `opponentUsername` dentro de `renderAuthorshipReport` nunca se ejercia
// contra un `Dataset` que de verdad vino de Postgres).

describe.skipIf(requiresTestDatabase)(
  "identidad del rival contra una base descartable real (p1 y p2)",
  () => {
    it("el reporte de autoría resuelve el rival de una trayectoria p1 Y de una p2, sobre el dataset completo cargado", async () => {
      const db = await createDisposableDatabase(process.env.TEST_DATABASE_URL!);
      try {
        await db.pool.query(
          "INSERT INTO generations (id, gen_number, label) VALUES (1, 6, 'XY/ORAS')",
        );
        // Una batalla, DOS trayectorias -- una por lado, cada una con su
        // propio player_side. `p1`/`p2` de `battles` no dicen por si solos
        // quien es el rival de CADA trayectoria: `opponentUsername` tiene
        // que resolverlo por rol, y este test lo ejercita con datos que
        // realmente pasaron por `loadDataset`, no un `Dataset` armado a
        // mano en memoria.
        await db.pool.query(
          `INSERT INTO battles (id, battle_tag, format, p1, p2, winner, played_by, source, identity_key)
           VALUES (1, 'battle-authorship-rival-p1p2', 'gen6randombattle',
                   'LudexBotReal', 'RivalReal', 'LudexBotReal', 'bot', 'local',
                   'fixture-identity-rival-p1p2')`,
        );
        await db.pool.query(
          `INSERT INTO trajectories (id, battle_id, gen_id, format, player_side, final_result)
           VALUES (10, 1, 1, 'gen6randombattle', 'p1', 'win')`,
        );
        await db.pool.query(
          `INSERT INTO trajectories (id, battle_id, gen_id, format, player_side, final_result)
           VALUES (20, 1, 1, 'gen6randombattle', 'p2', 'loss')`,
        );
        await db.pool.query(
          `INSERT INTO trajectory_steps
             (trajectory_id, turn_number, decision_index, state, state_schema_version,
              legal_actions, action_taken, action_source)
           VALUES (10, 0, 0, '{}'::jsonb, 2, '[]'::jsonb, '{"kind":"move","id":"x"}'::jsonb, 'agent')`,
        );
        await db.pool.query(
          `INSERT INTO trajectory_steps
             (trajectory_id, turn_number, decision_index, state, state_schema_version,
              legal_actions, action_taken, action_source)
           VALUES (20, 0, 0, '{}'::jsonb, 2, '[]'::jsonb, '{"kind":"move","id":"y"}'::jsonb, 'opponent')`,
        );

        const dataset = await loadDataset(db.pool, { scope: "all" });
        expect(dataset.trajectories.map((t) => t.id).sort()).toEqual([10, 20]);

        const output = renderAuthorshipReport(dataset);
        expect(output).toContain("Rival de la trayectoria 10 (p1): RivalReal");
        expect(output).toContain("Rival de la trayectoria 20 (p2): LudexBotReal");

        // opponentUsername directo sobre las filas REALES devueltas por
        // loadDataset, no un BattleRecord fabricado a mano.
        const battle = dataset.battles.find((b) => b.id === 1)!;
        expect(opponentUsername(battle, "p1")).toBe("RivalReal");
        expect(opponentUsername(battle, "p2")).toBe("LudexBotReal");
      } finally {
        await db.drop();
      }
    });
  },
);
