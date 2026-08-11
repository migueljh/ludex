import { describe, expect, it } from "vitest";
import { loadRandomBattleCatalog } from "../../src/extract/random-battle-catalog.js";

// MON-24/D47: el catálogo estándar random-battle de cada generación (el
// mismo `sets.json`/`data.json` que trae el paquete pineado `pokemon-showdown`)
// es la fuente que decide si una entrada `isNonstandard: "Unobtainable"` es
// battle-legal en esa generación pese a no ser obtenible cartridge-wise.
// Conteos verificados independientemente contra pokemon-showdown@0.11.10
// (medidos con node -e directo sobre dist/data/random-battles/gen6/sets.json,
// no supuestos): 483 especies, 273 movimientos únicos en los movepools.

describe("loadRandomBattleCatalog gen 6", () => {
  const catalog = loadRandomBattleCatalog(6);

  it("recorre el catálogo real: 483 especies referenciadas", () => {
    expect(catalog.speciesIds.size).toBe(483);
  });

  it("recorre el catálogo real: 273 movimientos únicos en los movepools", () => {
    expect(catalog.moveIds.size).toBe(273);
  });

  it("canario de iteración: ningún conjunto queda vacío", () => {
    // Si el parser no recorre nada (p.ej. porque el shape esperado no
    // matcheó y el loop nunca entró), estos conjuntos quedarían vacíos y
    // toda la excepción se volvería un no-op silencioso.
    expect(catalog.speciesIds.size).toBeGreaterThan(0);
    expect(catalog.moveIds.size).toBeGreaterThan(0);
  });

  it("incluye floetteeternal como especie referenciada", () => {
    expect(catalog.speciesIds.has("floetteeternal")).toBe(true);
  });

  it("incluye lightofruin como movimiento referenciado", () => {
    expect(catalog.moveIds.has("lightofruin")).toBe(true);
  });

  it("no confunde especies con movimientos", () => {
    // floetteeternal es especie, nunca debe aparecer en moveIds; lightofruin
    // es movimiento, nunca debe aparecer en speciesIds.
    expect(catalog.moveIds.has("floetteeternal")).toBe(false);
    expect(catalog.speciesIds.has("lightofruin")).toBe(false);
  });

  it("no referencia especies fuera del catálogo estándar de gen 6", () => {
    // pikachupartner/pikachuworld/charizardgmax son contenido Future de
    // generaciones posteriores: ni siquiera aparecen en el catálogo de gen 6
    // (independiente de que dex.gen los excluya después por isNonstandard).
    for (const id of ["pikachupartner", "pikachuworld", "charizardgmax"]) {
      expect(catalog.speciesIds.has(id)).toBe(false);
    }
  });

  it("no referencia thousandarrows/thousandwaves pese a compartir isNonstandard con lightofruin", () => {
    // El discriminador real: thousandarrows/thousandwaves también son
    // isNonstandard:'Unobtainable' en gen 6 (igual que lightofruin), pero
    // NINGÚN set estándar de gen6 los usa. Si el catálogo los incluyera por
    // error, la excepción dejaría de ser "referenciado por el catálogo" y
    // pasaría a ser "cualquier Unobtainable de esa generación".
    expect(catalog.moveIds.has("thousandarrows")).toBe(false);
    expect(catalog.moveIds.has("thousandwaves")).toBe(false);
  });

  it("cachea por generación: la misma instancia en llamadas repetidas", () => {
    expect(loadRandomBattleCatalog(6)).toBe(catalog);
  });
});

describe("loadRandomBattleCatalog gen 9 (shape sets.json, catálogo distinto)", () => {
  it("no referencia floetteeternal/lightofruin: Floette no se usa en gen9randombattle", () => {
    const catalog = loadRandomBattleCatalog(9);
    expect(catalog.speciesIds.has("floetteeternal")).toBe(false);
    expect(catalog.moveIds.has("lightofruin")).toBe(false);
  });
});

describe("loadRandomBattleCatalog gen 1 (shape data.json)", () => {
  it("resuelve el shape data.json ({especie: {level, moves: string[]}}) sin reventar", () => {
    const catalog = loadRandomBattleCatalog(1);
    expect(catalog.speciesIds.size).toBeGreaterThan(0);
    expect(catalog.moveIds.size).toBeGreaterThan(0);
    // bulbasaur es una entrada real y estable del data.json de gen 1.
    expect(catalog.speciesIds.has("bulbasaur")).toBe(true);
  });
});

describe("loadRandomBattleCatalog: generación fuera de rango", () => {
  it("falla ruidosamente, nunca devuelve un catálogo vacío por defecto", () => {
    expect(() => loadRandomBattleCatalog(0)).toThrow();
    expect(() => loadRandomBattleCatalog(99)).toThrow();
  });
});
