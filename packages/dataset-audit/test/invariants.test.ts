import { describe, expect, it } from "vitest";
import { actionKey, auditDataset, normalizeAction, REWARD_BY_RESULT } from "../src/invariants.js";
import {
  INVARIANTS,
  OPPONENT_FIELDS,
  SUPPORTED_STATE_VERSIONS,
  type Dataset,
  type FinalResult,
  type InvariantName,
} from "../src/types.js";
import { auditedStep, baseDataset } from "./fixtures.js";

function invariantsViolated(dataset: Dataset): InvariantName[] {
  return [...new Set(auditDataset(dataset).violations.map((v) => v.invariant))].sort();
}

describe("canario de cobertura", () => {
  it("el fixture base pasa las ocho invariantes y demuestra haber auditado algo", () => {
    const result = auditDataset(baseDataset());
    expect(result.violations).toEqual([]);
    expect(result.checks.map((check) => check.name)).toEqual([...INVARIANTS]);
    expect(result.opponentFields.map((field) => field.name)).toEqual([...OPPONENT_FIELDS]);
    // Sin esto, un loop que no itera nunca reportaría PASS en todo.
    expect(result.stats.stepsAudited).toBe(2);
    expect(result.stats.opponentEntriesAudited).toBeGreaterThan(0);
    expect(result.stats.protocolLinesScanned).toBeGreaterThan(0);
  });

  it("un dataset sin pasos no puede reportar trabajo hecho", () => {
    const dataset = baseDataset();
    dataset.steps = [];
    const result = auditDataset(dataset);
    expect(result.stats.stepsAudited).toBe(0);
    expect(result.stats.opponentEntriesAudited).toBe(0);
  });
});

describe("action_in_mask", () => {
  it("acepta una acción presente en su propia máscara", () => {
    expect(invariantsViolated(baseDataset())).toEqual([]);
  });

  it("rechaza una acción fuera de la máscara", () => {
    const dataset = baseDataset();
    auditedStep(dataset).actionTaken = { kind: "move", id: "psychic" };
    expect(invariantsViolated(dataset)).toEqual(["action_in_mask"]);
  });

  it("distingue la variante mega de la pelada: son decisiones distintas", () => {
    const dataset = baseDataset();
    auditedStep(dataset).legalActions = [{ kind: "move", id: "shadowball", mega: true }];
    auditedStep(dataset).state.legal_actions = auditedStep(dataset).legalActions;
    // `action_taken` es la variante SIN mega y la máscara sólo trae la mega.
    expect(invariantsViolated(dataset)).toEqual(["action_in_mask"]);
  });

  it("normaliza sólo los flags especiales que valen false", () => {
    expect(normalizeAction({ kind: "move", id: "x", mega: false })).toEqual({ kind: "move", id: "x" });
    expect(normalizeAction({ kind: "move", id: "x", mega: true }))
      .toEqual({ kind: "move", id: "x", mega: true });
    // Un campo cualquiera en false NO se normaliza: sólo los cuatro flags.
    expect(normalizeAction({ kind: "move", id: "x", inventado: false }))
      .toEqual({ kind: "move", id: "x", inventado: false });
    expect(actionKey({ id: "x", kind: "move" })).toBe(actionKey({ kind: "move", id: "x" }));
  });

  it("acepta un flag explícito en false contra una máscara que lo omite", () => {
    const dataset = baseDataset();
    auditedStep(dataset).actionTaken = {
      kind: "move", id: "shadowball", mega: false, z_move: false,
      dynamax: false, terastallize: false,
    };
    expect(invariantsViolated(dataset)).toEqual([]);
  });

  it("rechaza una fila sin acción: no representa una decisión", () => {
    const dataset = baseDataset();
    auditedStep(dataset).actionTaken = null;
    expect(invariantsViolated(dataset)).toEqual(["action_in_mask"]);
  });
});

describe("action_turn", () => {
  it("acepta state.turn anterior al turno corregido: la corrección sólo sube", () => {
    const dataset = baseDataset();
    auditedStep(dataset).state.turn = 0;
    expect(invariantsViolated(dataset)).toEqual([]);
  });

  it("rechaza un state.turn POSTERIOR a turn_number: sería un techo falso", () => {
    const dataset = baseDataset();
    auditedStep(dataset).state.turn = 2;
    expect(invariantsViolated(dataset)).toEqual(["action_turn"]);
  });

  it("rechaza un state.turn no numérico", () => {
    const dataset = baseDataset();
    auditedStep(dataset).state.turn = "1";
    expect(invariantsViolated(dataset)).toEqual(["action_turn"]);
  });
});

describe("decision_index", () => {
  it("rechaza un hueco en la secuencia de decisiones", () => {
    const dataset = baseDataset();
    auditedStep(dataset).decisionIndex = 2;
    expect(invariantsViolated(dataset)).toEqual(["decision_index"]);
  });

  it("rechaza una secuencia que no arranca en 0", () => {
    const dataset = baseDataset();
    for (const step of dataset.steps) step.decisionIndex += 1;
    expect(invariantsViolated(dataset)).toEqual(["decision_index"]);
  });

  it("acepta dos decisiones en el MISMO turno (cambio forzado tras un debilitamiento)", () => {
    const dataset = baseDataset();
    // El reemplazo tras un debilitamiento comparte `battle.turn` con la
    // decisión anterior: es exactamente lo que la PK por decision_index existe
    // para representar.
    dataset.steps.push({
      ...auditedStep(dataset),
      decisionIndex: 2,
      state: { ...auditedStep(dataset).state },
    });
    expect(invariantsViolated(dataset)).toEqual([]);
  });

  it("rechaza un turno que RETROCEDE respecto de la decisión anterior", () => {
    const dataset = baseDataset();
    // Copia del paso del turno 0 (rival válido en ese turno) puesta DESPUÉS
    // del paso del turno 1: una decisión posterior no puede resolverse antes.
    dataset.steps.push({
      ...dataset.steps[0],
      decisionIndex: 2,
      state: { ...dataset.steps[0].state },
    });
    expect(invariantsViolated(dataset)).toEqual(["decision_index"]);
  });
});

describe("state_rederivable", () => {
  it("rechaza un paso sin protocol_lines para su battle/side/turn", () => {
    const dataset = baseDataset();
    dataset.turns = dataset.turns.filter((turn) => turn.turnNumber !== 1);
    const violations = auditDataset(dataset).violations
      .filter((violation) => violation.invariant === "state_rederivable");
    expect(violations.length).toBeGreaterThan(0);
  });

  it("rechaza protocol_lines vacías", () => {
    const dataset = baseDataset();
    dataset.turns[1].protocolLines = [];
    expect(invariantsViolated(dataset)).toContain("state_rederivable");
  });

  it("rechaza que state.legal_actions y la columna se contradigan", () => {
    const dataset = baseDataset();
    auditedStep(dataset).state.legal_actions = [{ kind: "move", id: "psychic" }];
    expect(invariantsViolated(dataset)).toEqual(["state_rederivable"]);
  });

  it("acepta metadata omitida y rechaza metadata que contradice a la trayectoria", () => {
    const omitido = baseDataset();
    // poke-env todavía no parseó |tier| en la primera request: `format` viene
    // null. Omitir es aceptable; afirmar otra cosa no.
    auditedStep(omitido).state.format = null;
    expect(invariantsViolated(omitido)).toEqual([]);

    const contradice = baseDataset();
    auditedStep(contradice).state.format = "gen9ou";
    expect(invariantsViolated(contradice)).toEqual(["state_rederivable"]);

    const rol = baseDataset();
    auditedStep(rol).state.player_role = "p2";
    expect(invariantsViolated(rol)).toEqual(["state_rederivable"]);
  });
});

describe("reward_propagation: win/loss/tie/null", () => {
  const CASES: Array<[FinalResult | null, number | null]> = [
    ["win", 1],
    ["loss", -1],
    ["tie", 0],
    [null, null],
  ];

  it("mapea cada resultado a su reward", () => {
    expect(REWARD_BY_RESULT).toEqual({ win: 1, loss: -1, tie: 0 });
  });

  for (const [result, reward] of CASES) {
    it(`acepta final_result=${String(result)} con reward=${String(reward)}`, () => {
      const dataset = baseDataset();
      dataset.trajectories[0].finalResult = result;
      for (const step of dataset.steps) step.reward = reward;
      expect(invariantsViolated(dataset)).toEqual([]);
    });
  }

  for (const [result, reward] of CASES) {
    for (const [otherResult, otherReward] of CASES) {
      if (otherReward === reward) continue;
      it(`rechaza final_result=${String(result)} con reward=${String(otherReward)}`, () => {
        const dataset = baseDataset();
        dataset.trajectories[0].finalResult = result;
        for (const step of dataset.steps) step.reward = otherReward;
        expect(invariantsViolated(dataset)).toEqual(["reward_propagation"]);
      });
    }
  }

  it("un empate NO se castiga como derrota", () => {
    const dataset = baseDataset();
    dataset.trajectories[0].finalResult = "tie";
    for (const step of dataset.steps) step.reward = -1;
    const violations = auditDataset(dataset).violations;
    expect(violations.every((violation) => violation.invariant === "reward_propagation")).toBe(true);
    expect(violations[0].detail).toContain("esperado 0");
  });
});

describe("schema_version: conviven las soportadas, se rechazan las desconocidas", () => {
  it("declara exactamente las versiones que este auditor sabe interpretar", () => {
    expect([...SUPPORTED_STATE_VERSIONS]).toEqual([1, 2]);
  });

  it("acepta v1 y v2 en el MISMO corpus", () => {
    const dataset = baseDataset();
    dataset.steps[0].stateSchemaVersion = 1;
    dataset.steps[0].state.schema_version = 1;
    expect(invariantsViolated(dataset)).toEqual([]);
  });

  for (const unknown of [0, 3, 99]) {
    it(`rechaza la versión desconocida ${unknown}`, () => {
      const dataset = baseDataset();
      auditedStep(dataset).stateSchemaVersion = unknown;
      auditedStep(dataset).state.schema_version = unknown;
      expect(invariantsViolated(dataset)).toEqual(["schema_version"]);
    });
  }

  it("rechaza que la columna y el JSON declaren versiones distintas", () => {
    const dataset = baseDataset();
    auditedStep(dataset).state.schema_version = 1;
    expect(invariantsViolated(dataset)).toEqual(["schema_version"]);
  });
});

describe("orphans", () => {
  it("detecta una trayectoria sin batalla, un paso sin trayectoria y un turno sin batalla", () => {
    const dataset = baseDataset();
    dataset.trajectories.push({
      id: 99, battleId: 777, gen: 6, format: "gen6randombattle",
      playerSide: "p1", finalResult: "win",
    });
    dataset.steps.push({ ...dataset.steps[0], trajectoryId: 888 });
    dataset.turns.push({
      battleId: 777, playerSide: "p1", turnNumber: 0, protocolLines: ["|start"],
    });
    const orphans = auditDataset(dataset).violations
      .filter((violation) => violation.invariant === "orphans");
    expect(orphans).toHaveLength(3);
  });
});
