import { beforeAll, describe, expect, it } from "vitest";
import { isAvailable, loadGen } from "../../src/extract/dex.js";
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

  it("da a las megaevoluciones (sin learnset propio) el de su especie base", () => {
    // Las megas no tienen entrada propia en getLearnsetData, asi que su cadena
    // de herencia no aporta nada ademas de lo de baseSpecies: el conteo debe
    // coincidir exactamente.
    const megaMoves = rows.filter((r) => r.speciesId === "charizardmegax");
    const baseMoves = rows.filter((r) => r.speciesId === "charizard");
    expect(megaMoves.length).toBe(baseMoves.length);
    expect(megaMoves.length).toBeGreaterThan(100);
    expect(of("charizardmegax", "dragondance")).toBeDefined();
  });

  it("las formas con learnset propio conservan sus movimientos ademas de heredar", () => {
    // Rotom-Wash SI tiene entrada propia en getLearnsetData (a diferencia de
    // las megas) y Hidrobomba es su STAB de firma. inheritanceChain debe
    // anteponer el id propio a la cadena, no reemplazarlo por baseSpecies:
    // si vuelve a reemplazar, esta fila desaparece sin que ningun otro test
    // lo note.
    const hydroPump = of("rotomwash", "hydropump");
    expect(hydroPump).toBeDefined();
    expect(hydroPump!.methods.every((m) => m.sourceSpecies === "rotomwash")).toBe(true);

    const rotomWashMoves = rows.filter((r) => r.speciesId === "rotomwash");
    const rotomMoves = rows.filter((r) => r.speciesId === "rotom");
    expect(rotomWashMoves.length).toBeGreaterThan(rotomMoves.length);
  });

  it("no genera filas para especies fuera de la generacion", () => {
    expect(rows.some((r) => r.speciesId === "incineroar")).toBe(false);
  });

  it("no genera filas para movimientos fuera de la generacion", () => {
    const dex = loadGen(6);
    const gen6MoveIds = new Set(
      dex.moves.all().filter((m) => isAvailable(dex, m)).map((m) => m.id),
    );
    expect(rows.every((r) => gen6MoveIds.has(r.moveId))).toBe(true);
  });

  it("no repite el par (especie, movimiento)", () => {
    const keys = rows.map((r) => `${r.speciesId}|${r.moveId}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("resuelve mas filas de las que hay directas: el canario de la herencia", () => {
    // 49321 son los pares (especie, movimiento) con gen <= 6 que existen
    // DIRECTAMENTE en getLearnsetData, sin resolver herencia. La herencia solo
    // puede sumar filas, nunca restar, asi que 62198 > 49321 es la senal de que
    // la cadena corrio. Si esto baja a ~49321, se rompio la resolucion por
    // baseSpecies (las 48 megas de gen 6 quedandose sin movimientos) o la de
    // prevo. Sin esta asercion la regresion pasa con los tests en verde.
    expect(rows.length).toBeGreaterThan(49321);
    expect(rows).toHaveLength(62198);
  });
});

const D14_MISSING_GEN9 = [
  ["arcaninehisui", "headsmash"],
  ["dugtrioalola", "thrash"],
  ["electrodehisui", "leechseed"],
  ["electrodehisui", "worryseed"],
  ["golemalola", "screech"],
  ["golemalola", "zapcannon"],
  ["graveleralola", "screech"],
  ["graveleralola", "zapcannon"],
  ["mukalola", "assurance"],
  ["mukalola", "clearsmog"],
  ["ninetalesalola", "moonblast"],
  ["persianalola", "flatter"],
  ["persianalola", "partingshot"],
  ["sandslashalola", "mirrorcoat"],
  ["zoroarkhisui", "comeuppance"],
] as const;

const D14_ADDITIONAL_EVENT_MOVES_GEN9 = [
  ["lycanrocdusk", "happyhour"],
  ["ninetalesalola", "celebrate"],
  ["polteageistantique", "celebrate"],
] as const;

/** La cadena anterior a D14: forma propia + rama exclusiva de baseSpecies. */
function legacyInheritanceChain(dex: ReturnType<typeof loadGen>, speciesId: string): string[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  const own = dex.species.get(speciesId);
  let current = own;
  if (own.name !== own.baseSpecies) {
    chain.push(own.id);
    seen.add(own.id);
    current = dex.species.get(own.baseSpecies);
  }
  while (current?.exists && !seen.has(current.id)) {
    seen.add(current.id);
    chain.push(current.id);
    if (!current.prevo) break;
    current = dex.species.get(current.prevo);
  }
  return chain;
}

async function legacyMoveIds(
  dex: ReturnType<typeof loadGen>,
  speciesId: string,
): Promise<Set<string>> {
  const legalMoves = new Set(
    dex.moves.all().filter((move) => isAvailable(dex, move)).map((move) => move.id),
  );
  const result = new Set<string>();
  for (const ancestorId of legacyInheritanceChain(dex, speciesId)) {
    const learnset = (await dex.species.getLearnsetData(ancestorId))?.learnset ?? {};
    for (const [moveId, codes] of Object.entries(learnset)) {
      if (
        legalMoves.has(moveId)
        && (codes as string[]).some((code) => Number(code[0]) <= dex.gen)
      ) {
        result.add(moveId);
      }
    }
  }
  return result;
}

describe("D14: herencia aditiva de formas", () => {
  const extracted = new Map<number, LearnsetRow[]>();

  beforeAll(async () => {
    extracted.set(6, await extractLearnsets(loadGen(6)));
    extracted.set(9, await extractLearnsets(loadGen(9)));
  }, 180_000);

  it("suma los 15 movimientos faltantes de las líneas regionales", () => {
    const keys = new Set(
      extracted.get(9)!.map((row) => `${row.speciesId}/${row.moveId}`),
    );
    for (const [speciesId, moveId] of D14_MISSING_GEN9) {
      expect(keys, `${speciesId}/${moveId}`).toContain(`${speciesId}/${moveId}`);
    }
  });

  it("incluye también los tres movimientos de evento de las ramas propias", () => {
    const keys = new Set(
      extracted.get(9)!.map((row) => `${row.speciesId}/${row.moveId}`),
    );
    for (const [speciesId, moveId] of D14_ADDITIONAL_EVENT_MOVES_GEN9) {
      expect(keys, `${speciesId}/${moveId}`).toContain(`${speciesId}/${moveId}`);
    }
  });

  it("no resta ningún movimiento de la cadena legacy, especie por especie", async () => {
    for (const gen of [6, 9] as const) {
      const dex = loadGen(gen);
      const currentBySpecies = new Map<string, Set<string>>();
      for (const row of extracted.get(gen)!) {
        const moves = currentBySpecies.get(row.speciesId) ?? new Set<string>();
        moves.add(row.moveId);
        currentBySpecies.set(row.speciesId, moves);
      }
      for (const species of dex.species.all().filter((entry) => isAvailable(dex, entry))) {
        const legacy = await legacyMoveIds(dex, species.id);
        const current = currentBySpecies.get(species.id) ?? new Set<string>();
        const lost = [...legacy].filter((moveId) => !current.has(moveId));
        expect(lost, `gen ${gen} ${species.id} perdió movimientos`).toEqual([]);
      }
    }
  }, 180_000);

  it("conserva las cuatro formas Gourgeist de gen 6 con 66 movimientos", () => {
    const rows = extracted.get(6)!;
    for (const speciesId of [
      "gourgeist", "gourgeistsmall", "gourgeistlarge", "gourgeistsuper",
    ]) {
      expect(
        rows.filter((row) => row.speciesId === speciesId),
        speciesId,
      ).toHaveLength(66);
    }
  });

  it("los totales por generación nunca bajan del baseline", () => {
    expect(extracted.get(6)).toHaveLength(62_198);
    expect(extracted.get(9)).toHaveLength(65_642);
  });
});
