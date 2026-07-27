import { describe, expect, it } from "vitest";
import { auditDataset, normalizeProtocolText } from "../src/invariants.js";
import type { Dataset } from "../src/types.js";

function validDataset(): Dataset {
  return {
    battles: [{
      id: 10, battleTag: "battle-gen6randombattle-fixture", format: "gen6randombattle",
      p1: "LudexBot", p2: "Rival", winner: "LudexBot",
    }],
    turns: [{
      battleId: 10, playerSide: "p1", turnNumber: 1,
      protocolLines: [
        "|switch|p2a: Mimien|Mr. Mime, L82, M|100/100",
        "|move|p2a: Mimien|Psychic|p1a: Gengar",
      ],
    }],
    trajectories: [{
      id: 20, battleId: 10, gen: 6, format: "gen6randombattle",
      playerSide: "p1", finalResult: "win",
    }],
    steps: [{
      trajectoryId: 20,
      turnNumber: 1,
      decisionIndex: 0,
      state: {
        turn: 1,
        schema_version: 1,
        opponent: { pokemon: [{ species: "Mr. Mime" }] },
      },
      stateSchemaVersion: 1,
      legalActions: [{ kind: "move", id: "shadowball" }],
      actionTaken: { kind: "move", id: "shadowball" },
      actionSource: "agent",
      reward: 1,
    }],
  };
}

describe("normalizeProtocolText", () => {
  it("elimina toda la puntuación y diacríticos, no solo espacios y guiones", () => {
    expect(normalizeProtocolText("Mr. Mime")).toBe("mrmime");
    expect(normalizeProtocolText("Farfetch'd")).toBe("farfetchd");
    expect(normalizeProtocolText("Flabébé")).toBe("flabebe");
  });
});

describe("auditDataset", () => {
  it("acepta un dataset consistente con una especie revelada en una línea", () => {
    const result = auditDataset(validDataset());
    expect(result.violations).toEqual([]);
    expect(result.checks.map((check) => check.name)).toEqual([
      "hidden_information",
      "action_turn",
      "state_rederivable",
      "reward_propagation",
      "schema_version",
      "orphans",
    ]);
  });

  it("detecta fuga y no concatena líneas no relacionadas", () => {
    const dataset = validDataset();
    const opponent = dataset.steps[0].state.opponent;
    if (!opponent) throw new Error("fixture sin opponent");
    opponent.pokemon = [{ species: "Farfetch'd" }];
    dataset.turns[0].protocolLines = ["|message|Far", "|message|fetch'd"];

    const result = auditDataset(dataset);
    expect(result.violations).toContainEqual(expect.objectContaining({
      invariant: "hidden_information",
      battleTag: "battle-gen6randombattle-fixture",
      turnNumber: 1,
      detail: expect.stringContaining("Farfetch'd"),
    }));
  });

  it("detecta que la acción está asociada a otro turno", () => {
    const dataset = validDataset();
    dataset.steps[0].state.turn = 2;

    expect(auditDataset(dataset).violations).toContainEqual(expect.objectContaining({
      invariant: "action_turn",
      turnNumber: 1,
      decisionIndex: 0,
      detail: expect.stringContaining("state.turn=2"),
    }));
  });

  it("detecta un paso sin protocolo crudo para re-derivarlo", () => {
    const dataset = validDataset();
    dataset.turns[0].protocolLines = [];

    expect(auditDataset(dataset).violations).toContainEqual(expect.objectContaining({
      invariant: "state_rederivable",
      turnNumber: 1,
    }));
  });

  it("detecta reward ausente o incorrecto en una trayectoria terminada", () => {
    const missing = validDataset();
    missing.steps[0].reward = null;
    expect(auditDataset(missing).violations).toContainEqual(expect.objectContaining({
      invariant: "reward_propagation",
      detail: expect.stringContaining("esperado 1"),
    }));

    const wrong = validDataset();
    wrong.trajectories[0].finalResult = "loss";
    expect(auditDataset(wrong).violations).toContainEqual(expect.objectContaining({
      invariant: "reward_propagation",
      detail: expect.stringContaining("esperado -1"),
    }));
  });

  it("detecta versiones mezcladas y desacuerdo entre columna y JSON", () => {
    const dataset = validDataset();
    dataset.steps.push({
      ...dataset.steps[0],
      decisionIndex: 1,
      stateSchemaVersion: 2,
      state: { ...dataset.steps[0].state, schema_version: 1 },
    });

    const violations = auditDataset(dataset).violations.filter(
      (violation) => violation.invariant === "schema_version",
    );
    expect(violations.length).toBeGreaterThanOrEqual(2);
    expect(violations.some((violation) => violation.detail.includes("1, 2"))).toBe(true);
    expect(violations.some((violation) => violation.detail.includes("JSON=1"))).toBe(true);
  });

  it("detecta cada dirección de fila huérfana", () => {
    const dataset = validDataset();
    dataset.trajectories.push({ ...dataset.trajectories[0], id: 21, battleId: 999 });
    dataset.steps.push({ ...dataset.steps[0], trajectoryId: 999, decisionIndex: 1 });
    dataset.turns.push({ ...dataset.turns[0], battleId: 999, turnNumber: 2 });

    const details = auditDataset(dataset).violations
      .filter((violation) => violation.invariant === "orphans")
      .map((violation) => violation.detail);
    expect(details).toEqual(expect.arrayContaining([
      expect.stringContaining("trayectoria 21"),
      expect.stringContaining("paso de trayectoria 999"),
      expect.stringContaining("turno sin batalla"),
    ]));
  });
});
