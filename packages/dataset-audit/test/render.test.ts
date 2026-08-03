import { describe, expect, it } from "vitest";
import { renderBattle } from "../src/render.js";
import { baseDataset } from "./fixtures.js";

describe("renderBattle", () => {
  it("muestra la batalla, su source y las decisiones en orden de decision_index", () => {
    const output = renderBattle(baseDataset(), "battle-gen6randombattle-fixture");
    expect(output).toContain("battle-gen6randombattle-fixture");
    expect(output).toContain("source local");
    expect(output).toContain("decisión 0");
    expect(output).toContain("decisión 1");
    expect(output.indexOf("decisión 0")).toBeLessThan(output.indexOf("decisión 1"));
    expect(output).toContain("MOVE shadowball");
  });

  it("acepta el id numérico además del tag", () => {
    expect(renderBattle(baseDataset(), 10)).toContain("(#10)");
  });

  it("oculta las líneas privadas del stream", () => {
    const dataset = baseDataset();
    dataset.turns[1].protocolLines.push('|request|{"side":{"pokemon":[]}}');
    const output = renderBattle(dataset, 10);
    expect(output).not.toContain("|request|");
  });

  it("falla ruidoso si la batalla no existe", () => {
    expect(() => renderBattle(baseDataset(), "battle-inexistente")).toThrow(/no encontrada/);
  });
});
