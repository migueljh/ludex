import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractTypeChart, typesForGen } from "../../src/extract/typechart.js";

const mult = (rows: ReturnType<typeof extractTypeChart>, atk: string, def: string) =>
  rows.find((r) => r.attackingType === atk && r.defendingType === def)!.multiplier;

describe("typesForGen", () => {
  it("deriva la lista de tipos por generacion sin hardcodearla", () => {
    expect(typesForGen(loadGen(1))).toHaveLength(15);
    expect(typesForGen(loadGen(2))).toHaveLength(17);
    expect(typesForGen(loadGen(5))).toHaveLength(17);
    expect(typesForGen(loadGen(6))).toHaveLength(18);
    expect(typesForGen(loadGen(9))).toHaveLength(19);
  });

  it("introduce Hada en gen 6 y Stellar en gen 9", () => {
    expect(typesForGen(loadGen(5))).not.toContain("Fairy");
    expect(typesForGen(loadGen(6))).toContain("Fairy");
    expect(typesForGen(loadGen(6))).not.toContain("Stellar");
    expect(typesForGen(loadGen(9))).toContain("Stellar");
  });

  it("excluye las claves de damageTaken que no son tipos", () => {
    for (const t of typesForGen(loadGen(6))) {
      expect(["psn", "tox", "sandstorm", "hail", "powder", "frz", "par"]).not.toContain(t);
    }
  });
});

describe("extractTypeChart", () => {
  const gen5 = extractTypeChart(loadGen(5));
  const gen6 = extractTypeChart(loadGen(6));

  it("produce la matriz completa", () => {
    expect(gen6).toHaveLength(324); // 18 x 18
    expect(gen5).toHaveLength(289); // 17 x 17
    expect(extractTypeChart(loadGen(9))).toHaveLength(361); // 19 x 19
  });

  it("traduce los codigos de damageTaken a multiplicadores", () => {
    expect(mult(gen6, "Fire", "Grass")).toBe(2);
    expect(mult(gen6, "Fire", "Water")).toBe(0.5);
    expect(mult(gen6, "Normal", "Normal")).toBe(1);
    expect(mult(gen6, "Normal", "Ghost")).toBe(0);
  });

  it("aplica la inmunidad de Hada a Dragon", () => {
    expect(mult(gen6, "Dragon", "Fairy")).toBe(0);
    expect(mult(gen6, "Fairy", "Dragon")).toBe(2);
  });

  it("quita las resistencias de Acero a Siniestro y Fantasma en gen 6", () => {
    expect(mult(gen5, "Dark", "Steel")).toBe(0.5);
    expect(mult(gen5, "Ghost", "Steel")).toBe(0.5);
    expect(mult(gen6, "Dark", "Steel")).toBe(1);
    expect(mult(gen6, "Ghost", "Steel")).toBe(1);
  });
});
