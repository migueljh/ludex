import { describe, expect, it } from "vitest";
import { renderBattle } from "../src/render.js";
import type { Dataset } from "../src/types.js";

const dataset: Dataset = {
  battles: [{
    id: 10, battleTag: "battle-gen6randombattle-fixture", format: "gen6randombattle",
    p1: "LudexBot", p2: "Rival", winner: "LudexBot",
  }],
  turns: [{
    battleId: 10, playerSide: "p1", turnNumber: 1,
    protocolLines: [
      "|turn|1",
      "|request|{\"active\":[]}",
      "|move|p1a: Gengar|Shadow Ball|p2a: Mr. Mime",
      "|-damage|p2a: Mr. Mime|0 fnt",
    ],
  }],
  trajectories: [{
    id: 20, battleId: 10, gen: 6, format: "gen6randombattle",
    playerSide: "p1", finalResult: "win",
  }],
  steps: [{
    trajectoryId: 20, turnNumber: 1, decisionIndex: 0,
    state: {
      turn: 1, schema_version: 1,
      opponent: { pokemon: [{ species: "mrmime", active: true, hp_fraction: 1 }] },
    },
    stateSchemaVersion: 1,
    legalActions: [{ kind: "move", id: "shadowball" }],
    actionTaken: { kind: "move", id: "shadowball" },
    actionSource: "agent",
    reward: 1,
  }],
};

describe("renderBattle", () => {
  it("muestra acción, hechos del juego y conocimiento rival por decisión", () => {
    const output = renderBattle(dataset, "battle-gen6randombattle-fixture");
    expect(output).toContain("battle-gen6randombattle-fixture");
    expect(output).toContain("LudexBot vs Rival");
    expect(output).toContain("Turno 1 · decisión 0 · p1");
    expect(output).toContain("Acción: MOVE shadowball");
    expect(output).toContain("|move|p1a: Gengar|Shadow Ball|p2a: Mr. Mime");
    expect(output).not.toContain("|request|");
    expect(output).toContain("Rival conocido: mrmime");
  });

  it("acepta ID numérico y da un error accionable si no existe", () => {
    expect(renderBattle(dataset, 10)).toContain("battle-gen6randombattle-fixture");
    expect(() => renderBattle(dataset, "missing")).toThrow(/missing/);
  });

  it("une la decisión inicial en state.turn 0 con su resultado en protocolo turno 1", () => {
    const initial: Dataset = {
      ...dataset,
      turns: [
        { ...dataset.turns[0], turnNumber: 0, protocolLines: ["|start"] },
        { ...dataset.turns[0], turnNumber: 1 },
      ],
      steps: [{
        ...dataset.steps[0],
        turnNumber: 0,
        state: { ...dataset.steps[0].state, turn: 0 },
      }],
    };

    const output = renderBattle(initial, 10);
    expect(output).toContain("Pasó (protocolo turno 1):");
    expect(output).toContain("|move|p1a: Gengar|Shadow Ball|p2a: Mr. Mime");
  });
});
