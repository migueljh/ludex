import { describe, expect, it } from "vitest";
import { GENERATION_LABELS, isAvailable, loadGen, packageVersion } from "../../src/extract/dex.js";

describe("loadGen", () => {
  it("carga el mod de la generacion pedida", () => {
    expect(loadGen(6).gen).toBe(6);
    expect(loadGen(9).gen).toBe(9);
  });

  it("rechaza generaciones fuera de rango", () => {
    expect(() => loadGen(0)).toThrow(/generacion/i);
    expect(() => loadGen(99)).toThrow(/generacion/i);
  });
});

describe("isAvailable", () => {
  const dex = loadGen(6);

  it("acepta contenido de la generacion o anterior sin marca", () => {
    expect(isAvailable(dex, dex.species.get("charizard"))).toBe(true);
    expect(isAvailable(dex, dex.species.get("greninja"))).toBe(true);
  });

  it("rechaza contenido de generaciones futuras", () => {
    const incineroar = dex.species.get("incineroar");
    expect(incineroar.gen).toBe(7);
    expect(incineroar.isNonstandard).toBe("Future");
    expect(isAvailable(dex, incineroar)).toBe(false);
  });

  it("rechaza cualquier cosa marcada como nonstandard", () => {
    expect(isAvailable(dex, dex.species.get("missingno"))).toBe(false);
    expect(isAvailable(dex, dex.species.get("floetteeternal"))).toBe(false);
  });

  it("filtra el dex completo a los conteos conocidos de gen 6", () => {
    const kept = dex.species.all().filter((s) => isAvailable(dex, s));
    expect(dex.species.all().length).toBe(1425);
    expect(kept.length).toBe(834);
  });
});

describe("metadatos", () => {
  it("tiene etiqueta para las generaciones seedeables", () => {
    expect(GENERATION_LABELS[6]).toBe("XY/ORAS");
    expect(GENERATION_LABELS[9]).toBe("SV");
  });

  it("reporta la version exacta pineada del paquete", () => {
    expect(packageVersion()).toBe("0.11.10");
  });
});
