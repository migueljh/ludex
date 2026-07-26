import { beforeAll, describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractLearnsets, parseLearnCode } from "../../src/extract/learnsets.js";
import type { LearnsetRow } from "../../src/types.js";

describe("parseLearnCode", () => {
  it("parsea nivel con su numero", () => {
    expect(parseLearnCode("6L47", "charizard")).toEqual({
      gen: 6, method: "level", level: 47, sourceSpecies: "charizard",
    });
  });

  it("parsea metodos sin argumento", () => {
    expect(parseLearnCode("6M", "charizard")).toEqual({
      gen: 6, method: "machine", sourceSpecies: "charizard",
    });
    expect(parseLearnCode("5T", "charmander")).toEqual({
      gen: 5, method: "tutor", sourceSpecies: "charmander",
    });
    expect(parseLearnCode("6E", "charmander")).toEqual({
      gen: 6, method: "egg", sourceSpecies: "charmander",
    });
  });

  it("ignora el sufijo numerico de los eventos", () => {
    expect(parseLearnCode("6S5", "charizard")).toEqual({
      gen: 6, method: "event", sourceSpecies: "charizard",
    });
  });

  it("falla ruidosamente ante un codigo desconocido", () => {
    expect(() => parseLearnCode("6Z", "charizard")).toThrow(/desconocido/i);
    expect(() => parseLearnCode("basura", "charizard")).toThrow(/invalido/i);
  });
});

describe("extractLearnsets gen 6", () => {
  let rows: LearnsetRow[];
  const of = (speciesId: string, moveId: string) =>
    rows.find((r) => r.speciesId === speciesId && r.moveId === moveId);

  beforeAll(async () => {
    rows = await extractLearnsets(loadGen(6));
  }, 180_000);

  it("descarta los codigos de generaciones futuras", () => {
    const ft = of("charizard", "flamethrower")!;
    expect(ft.methods.every((m) => m.gen <= 6)).toBe(true);
    expect(ft.methods).toContainEqual({ gen: 6, method: "machine", sourceSpecies: "charizard" });
    expect(ft.methods).toContainEqual({ gen: 6, method: "level", level: 47, sourceSpecies: "charizard" });
  });

  it("conserva metodos de generaciones anteriores sin aplanarlos", () => {
    const ft = of("charizard", "flamethrower")!;
    expect(ft.methods).toContainEqual({ gen: 5, method: "machine", sourceSpecies: "charizard" });
    expect(ft.methods.filter((m) => m.gen === 6).length).toBeGreaterThan(0);
    expect(ft.methods.filter((m) => m.gen < 6).length).toBeGreaterThan(0);
  });

  it("hereda movimientos huevo desde la preevolucion", () => {
    // Charizard no aprende Dragon Dance por si mismo en gen 6: sus codigos son
    // 9M, 8M y 7S9, todos posteriores. Charmander la tiene como 6E.
    const dd = of("charizard", "dragondance")!;
    expect(dd).toBeDefined();
    expect(dd.methods).toContainEqual({ gen: 6, method: "egg", sourceSpecies: "charmander" });
    expect(dd.methods.every((m) => m.sourceSpecies !== "charizard")).toBe(true);
  });

  it("da a las formas el learnset de su especie base", () => {
    const megaMoves = rows.filter((r) => r.speciesId === "charizardmegax");
    const baseMoves = rows.filter((r) => r.speciesId === "charizard");
    expect(megaMoves.length).toBe(baseMoves.length);
    expect(megaMoves.length).toBeGreaterThan(100);
    expect(of("charizardmegax", "dragondance")).toBeDefined();
  });

  it("no genera filas para especies fuera de la generacion", () => {
    expect(rows.some((r) => r.speciesId === "incineroar")).toBe(false);
  });

  it("no genera filas para movimientos fuera de la generacion", () => {
    const gen6MoveIds = new Set(
      loadGen(6).moves.all().filter((m) => m.gen <= 6 && !m.isNonstandard).map((m) => m.id),
    );
    expect(rows.every((r) => gen6MoveIds.has(r.moveId))).toBe(true);
  });

  it("no repite el par (especie, movimiento)", () => {
    const keys = rows.map((r) => `${r.speciesId}|${r.moveId}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("nunca deja methods vacio", () => {
    expect(rows.every((r) => r.methods.length > 0)).toBe(true);
  });
});
