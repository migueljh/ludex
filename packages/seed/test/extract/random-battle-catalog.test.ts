import { describe, expect, it } from "vitest";
import {
  loadRandomBattleCatalog, parseCatalogData,
} from "../../src/extract/random-battle-catalog.js";

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

// --- L-01 (CORRECTION PACKET, LINEAR_VERDICT CHANGES_REQUESTED sobre
// 76c630a): el shape "data.json" (gen 1, 8) NO es solo `{especie: {moves}}`.
// Una entrada puede traer varias listas de movimientos reconocidas ademas
// de (o en vez de) `moves`: gen 1 tiene `comboMoves`/`essentialMoves`/
// `exclusiveMoves`; gen 8 tiene `doublesMoves`/`noDynamaxMoves`, y varios
// Gmax de gen 8 (`venusaurgmax` entre ellos) NO TIENEN `moves` en absoluto,
// solo `doublesMoves`. Conteos verificados independientemente contra
// pokemon-showdown@0.11.10 (node -e directo sobre los data.json reales, no
// supuestos): gen 1 `moves` solo = 47, union completa = 69; gen 8 `moves`
// solo = 294, union completa = 351.

describe("loadRandomBattleCatalog gen 1 (shape data.json, campos multiples)", () => {
  const catalog = loadRandomBattleCatalog(1);

  it("resuelve el shape data.json sin reventar", () => {
    expect(catalog.speciesIds.size).toBeGreaterThan(0);
    expect(catalog.moveIds.size).toBeGreaterThan(0);
    expect(catalog.speciesIds.has("bulbasaur")).toBe(true);
  });

  it("produce 69 movimientos unicos (union completa, no solo el campo moves)", () => {
    expect(catalog.moveIds.size).toBe(69);
  });

  it("canario: incluye movimientos que SOLO estan en comboMoves/essentialMoves, no en moves", () => {
    // charmander (gen1/data.json): moves=[counter,seismictoss,slash];
    // essentialMoves=[bodyslam,fireblast]; comboMoves=[bodyslam,fireblast,
    // submission,swordsdance]. "submission" no esta en moves ni en
    // essentialMoves: solo lo trae comboMoves. Si el parser solo uniera
    // `moves`, faltaria sin que ningun conteo total lo hiciera obvio salvo
    // por casualidad.
    expect(catalog.moveIds.has("submission")).toBe(true);
    expect(catalog.moveIds.has("bodyslam")).toBe(true);
  });
});

describe("loadRandomBattleCatalog gen 8 (shape data.json, entradas solo-doublesMoves)", () => {
  it("completa sin excepcion", () => {
    expect(() => loadRandomBattleCatalog(8)).not.toThrow();
  });

  const catalog = loadRandomBattleCatalog(8);

  it("acepta una entrada real sin `moves`, solo `doublesMoves` (venusaurgmax)", () => {
    expect(catalog.speciesIds.has("venusaurgmax")).toBe(true);
    // earthpower/energyball/leechseed/protect/sleeppowder/sludgebomb son el
    // doublesMoves real de venusaurgmax: si el parser exigiera `moves` esta
    // entrada nunca se hubiera podido leer sin reventar antes de llegar aca.
    expect(catalog.moveIds.has("earthpower")).toBe(true);
    expect(catalog.moveIds.has("sludgebomb")).toBe(true);
  });

  it("produce 351 movimientos unicos (union completa: moves+doublesMoves+noDynamaxMoves)", () => {
    expect(catalog.moveIds.size).toBe(351);
  });
});

describe("sweep gen 1..9: todo catalogo carga sin reventar y no vacio", () => {
  for (let gen = 1; gen <= 9; gen++) {
    it(`gen ${gen}`, () => {
      const catalog = loadRandomBattleCatalog(gen);
      expect(catalog.speciesIds.size).toBeGreaterThan(0);
      expect(catalog.moveIds.size).toBeGreaterThan(0);
    });
  }
});

describe("parseCatalogData: shape data.json invalido falla ruidoso", () => {
  it("una lista reconocida presente pero mal tipada revienta", () => {
    expect(() =>
      parseCatalogData("<test>", {
        algunaespecie: { moves: ["tackle"], comboMoves: "no-es-un-array" },
      }),
    ).toThrow(/comboMoves/);
  });

  it("una entrada sin ninguna lista de movimientos reconocida revienta", () => {
    expect(() =>
      parseCatalogData("<test>", { algunaespecie: { level: 50 } }),
    ).toThrow(/ninguna lista/);
  });

  it("un campo desconocido (ni lista reconocida ni escalar conocido) revienta", () => {
    expect(() =>
      parseCatalogData("<test>", {
        algunaespecie: { moves: ["tackle"], campoInventado: ["algo"] },
      }),
    ).toThrow(/desconocido/);
  });

  it("el shape sets.json real sigue funcionando sin cambios (no se rompio nada)", () => {
    expect(() =>
      parseCatalogData("<test>", {
        algunaespecie: { sets: [{ movepool: ["tackle"] }] },
      }),
    ).not.toThrow();
  });
});

describe("loadRandomBattleCatalog: generación fuera de rango", () => {
  it("falla ruidosamente, nunca devuelve un catálogo vacío por defecto", () => {
    expect(() => loadRandomBattleCatalog(0)).toThrow();
    expect(() => loadRandomBattleCatalog(99)).toThrow();
  });
});
