import { describe, expect, it } from "vitest";
import {
  GENERATION_LABELS, isAvailable, isAvailableForExtraction, loadGen, packageVersion,
} from "../../src/extract/dex.js";

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

  it("aisla la clausula de generacion, no solo la marca nonstandard", () => {
    // Entradas sinteticas a proposito. En la data real de Showdown TODO lo de
    // generaciones futuras ya viene marcado isNonstandard:'Future' (verificado:
    // cero entradas con gen > dex.gen y sin marca, en gen 6 y en gen 9). Sin
    // este test, un `return !entry.isNonstandard` pasaria las otras siete
    // aserciones y la comparacion de generacion quedaria sin cobertura, en el
    // predicado del que depende todo el pipeline.
    expect(isAvailable(dex, { gen: 5, isNonstandard: null })).toBe(true);
    expect(isAvailable(dex, { gen: 6, isNonstandard: null })).toBe(true);
    expect(isAvailable(dex, { gen: 7, isNonstandard: null })).toBe(false);
    expect(isAvailable(dex, { gen: 9, isNonstandard: null })).toBe(false);
  });
});

describe("isAvailableForExtraction", () => {
  const dex = loadGen(6);
  const dex9 = loadGen(9);

  it("conserva isAvailable como frontera base: acepta todo lo que ya aceptaba", () => {
    expect(isAvailableForExtraction(dex, dex.species.get("charizard"), "species")).toBe(true);
    expect(isAvailableForExtraction(dex, dex.moves.get("flamethrower"), "move")).toBe(true);
  });

  it("conserva isAvailable como frontera base: rechaza contenido Future igual que antes", () => {
    const incineroar = dex.species.get("incineroar");
    expect(isAvailableForExtraction(dex, incineroar, "species")).toBe(false);
  });

  it("positivo Gen 6: admite floetteeternal como species porque el catálogo random-battle de gen 6 la referencia", () => {
    const entry = dex.species.get("floetteeternal");
    expect(entry.isNonstandard).toBe("Unobtainable");
    expect(isAvailableForExtraction(dex, entry, "species")).toBe(true);
  });

  it("positivo Gen 6: admite lightofruin como move porque el catálogo random-battle de gen 6 lo referencia", () => {
    const entry = dex.moves.get("lightofruin");
    expect(entry.isNonstandard).toBe("Unobtainable");
    expect(isAvailableForExtraction(dex, entry, "move")).toBe(true);
  });

  it("no admite floetteeternal como move ni lightofruin como species (tipo de entidad debe coincidir)", () => {
    const floette = dex.species.get("floetteeternal");
    const lightOfRuin = dex.moves.get("lightofruin");
    expect(isAvailableForExtraction(dex, floette, "move")).toBe(false);
    expect(isAvailableForExtraction(dex, lightOfRuin, "species")).toBe(false);
  });

  it("no admite thousandarrows/thousandwaves: mismo isNonstandard que lightofruin, pero ningún set de gen 6 los usa", () => {
    const thousandArrows = dex.moves.get("thousandarrows");
    const thousandWaves = dex.moves.get("thousandwaves");
    expect(thousandArrows.isNonstandard).toBe("Unobtainable");
    expect(thousandWaves.isNonstandard).toBe("Unobtainable");
    expect(isAvailableForExtraction(dex, thousandArrows, "move")).toBe(false);
    expect(isAvailableForExtraction(dex, thousandWaves, "move")).toBe(false);
  });

  it("no admite paleowave/shadowstrike: CAP, no Unobtainable", () => {
    const paleowave = dex.moves.get("paleowave");
    const shadowstrike = dex.moves.get("shadowstrike");
    expect(paleowave.isNonstandard).toBe("CAP");
    expect(shadowstrike.isNonstandard).toBe("CAP");
    expect(isAvailableForExtraction(dex, paleowave, "move")).toBe(false);
    expect(isAvailableForExtraction(dex, shadowstrike, "move")).toBe(false);
  });

  it("no admite pikachupartner/pikachuworld/charizardgmax en gen 6: Future, generación posterior", () => {
    for (const id of ["pikachupartner", "pikachuworld", "charizardgmax"]) {
      const entry = dex.species.get(id);
      expect(isAvailableForExtraction(dex, entry, "species")).toBe(false);
    }
  });

  it("wrong generation: floetteeternal/lightofruin no entran bajo gen 9 (su catálogo no los referencia)", () => {
    const floette9 = dex9.species.get("floetteeternal");
    const lightOfRuin9 = dex9.moves.get("lightofruin");
    expect(isAvailableForExtraction(dex9, floette9, "species")).toBe(false);
    expect(isAvailableForExtraction(dex9, lightOfRuin9, "move")).toBe(false);
  });

  it("no admite ningún otro valor de isNonstandard por esta vía (entrada sintética)", () => {
    // Entrada sintética a propósito, igual que el test de aislamiento de
    // isAvailable de arriba: prueba el predicado, no depende de que la data
    // real tenga hoy un caso de cada valor.
    const synthetic = { gen: 6, id: "floetteeternal", isNonstandard: "CAP" as const };
    expect(isAvailableForExtraction(dex, synthetic, "species")).toBe(false);
  });

  it("una especie genuinamente desconocida sigue sin entrar por esta vía", () => {
    const synthetic = {
      gen: 6, id: "especie-inventada-que-no-existe", isNonstandard: "Unobtainable" as const,
    };
    // Aunque declare Unobtainable de gen 6, no está en el catálogo real:
    // la excepción no la admite.
    expect(isAvailableForExtraction(dex, synthetic, "species")).toBe(false);
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
