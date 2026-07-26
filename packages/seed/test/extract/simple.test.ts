import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractAbilities, extractItems } from "../../src/extract/simple.js";

const items6 = extractItems(loadGen(6));
const items9 = extractItems(loadGen(9));
const abil6 = extractAbilities(loadGen(6));
const abil9 = extractAbilities(loadGen(9));

describe("extractItems", () => {
  it("devuelve los conteos conocidos", () => {
    expect(items6).toHaveLength(283);
    expect(items9).toHaveLength(248);
  });

  it("incluye piedras activadoras en gen 6 y ninguna en gen 9", () => {
    expect(items6.some((i) => i.showdownId === "charizarditex")).toBe(true);
    expect(items9.some((i) => i.showdownId === "charizarditex")).toBe(false);
  });

  it("mapea nombre y descripcion", () => {
    const lefties = items6.find((i) => i.showdownId === "leftovers")!;
    expect(lefties.name).toBe("Leftovers");
    expect(lefties.description).toContain("1/16");
  });
});

describe("extractAbilities", () => {
  it("devuelve los conteos conocidos", () => {
    expect(abil6).toHaveLength(191);
    expect(abil9).toHaveLength(310);
  });

  it("mapea nombre y descripcion", () => {
    const levitate = abil6.find((a) => a.showdownId === "levitate")!;
    expect(levitate.name).toBe("Levitate");
    expect(levitate.description).toBeTruthy();
  });
});
