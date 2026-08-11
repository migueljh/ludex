import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractSpecies } from "../../src/extract/species.js";

const gen6 = extractSpecies(loadGen(6));
const gen9 = extractSpecies(loadGen(9));
const byId = (rows: typeof gen6, id: string) => rows.find((r) => r.showdownId === id)!;

describe("extractSpecies", () => {
  it("devuelve los conteos conocidos de gen 6", () => {
    // D47/MON-24: 835 = 834 + floetteeternal (isNonstandard:'Unobtainable',
    // pero referenciada por el catálogo estándar random-battle de gen 6:
    // es la ÚNICA forma en que la línea de Floette aparece en
    // gen6randombattle -- no hay una entrada "floette" base en ese catálogo).
    expect(gen6).toHaveLength(835);
    expect(gen6.filter((s) => s.isDefault)).toHaveLength(721);
  });

  it("devuelve los conteos conocidos de gen 9", () => {
    expect(gen9).toHaveLength(874);
    expect(gen9.filter((s) => s.isDefault)).toHaveLength(733);
  });

  it("incluye megaevoluciones en gen 6 y ninguna en gen 9", () => {
    expect(gen6.filter((s) => s.forme?.startsWith("Mega"))).toHaveLength(48);
    expect(gen9.filter((s) => s.forme?.startsWith("Mega"))).toHaveLength(0);
  });

  it("separa forma base de forme", () => {
    const base = byId(gen6, "charizard");
    expect(base.forme).toBeNull();
    expect(base.isDefault).toBe(true);
    expect(base.baseSpecies).toBe("charizard");
    expect(base.baseSpeciesName).toBe("Charizard");

    const mega = byId(gen6, "charizardmegax");
    expect(mega.forme).toBe("Mega-X");
    expect(mega.isDefault).toBe(false);
    // D2: baseSpecies es el id normalizado (joinea con showdownId); el nombre
    // legible queda en baseSpeciesName.
    expect(mega.baseSpecies).toBe("charizard");
    expect(mega.baseSpeciesName).toBe("Charizard");
    expect(mega.dexNum).toBe(base.dexNum);
  });

  it("baseSpecies es siempre el showdownId de otra fila de la misma gen", () => {
    for (const rows of [gen6, gen9]) {
      const ids = new Set(rows.map((s) => s.showdownId));
      const huerfanos = rows.filter((s) => !ids.has(s.baseSpecies));
      expect(huerfanos).toEqual([]);
    }
  });

  it("mapea los campos escalares", () => {
    const base = byId(gen6, "charizard");
    expect(base.name).toBe("Charizard");
    expect(base.types).toEqual(["Fire", "Flying"]);
    expect(base.baseStats).toEqual({ hp: 78, atk: 84, def: 78, spa: 109, spd: 85, spe: 100 });
    expect(base.weightKg).toBe(90.5);
    expect(base.evolvesFrom).toBe("charmeleon");
    expect(base.tier).toBe("NU");
  });

  it("mapea habilidades normales y ocultas", () => {
    expect(byId(gen6, "charizard").abilities).toEqual({ "0": "Blaze", H: "Solar Power" });
    expect(byId(gen6, "charizardmegax").abilities).toEqual({ "0": "Tough Claws" });
  });

  it("normaliza la ausencia de preevolucion a null", () => {
    expect(byId(gen6, "charmander").evolvesFrom).toBeNull();
    expect(byId(gen6, "charizardmegax").evolvesFrom).toBeNull();
  });

  it("aplica el cambio de tipo de la mega", () => {
    expect(byId(gen6, "charizardmegax").types).toEqual(["Fire", "Dragon"]);
  });

  it("excluye contenido futuro y nonstandard", () => {
    expect(gen6.some((s) => s.showdownId === "incineroar")).toBe(false);
    expect(gen6.some((s) => s.showdownId === "missingno")).toBe(false);
    expect(gen6.some((s) => s.showdownId.startsWith("pokestar"))).toBe(false);
  });

  it("no repite showdownId", () => {
    expect(new Set(gen6.map((s) => s.showdownId)).size).toBe(gen6.length);
  });

  it("D47/MON-24: floetteeternal entra con fila directa y sus stats/ability propios, no los de floette", () => {
    const eternal = byId(gen6, "floetteeternal");
    expect(eternal.baseSpecies).toBe("floette");
    expect(eternal.forme).toBe("Eternal");
    expect(eternal.types).toEqual(["Fairy"]);
    // NO son los stats de la base (hp54/atk45/def47/spa75/spd98/spe52):
    // Floette-Eternal es una forma mecánica distinta, no cosmética.
    expect(eternal.baseStats).toEqual({
      hp: 74, atk: 65, def: 67, spa: 125, spd: 128, spe: 92,
    });
    expect(eternal.abilities).toEqual({ "0": "Flower Veil" });

    const base = byId(gen6, "floette");
    expect(base.baseStats).not.toEqual(eternal.baseStats);
  });

  it("D47/MON-24: floetteeternal NO aparece en gen 9 (su catálogo no la referencia)", () => {
    expect(gen9.some((s) => s.showdownId === "floetteeternal")).toBe(false);
  });

  it("D47/MON-24: sigue excluyendo Unobtainable no referenciado por el catálogo de gen 6", () => {
    // Mismo isNonstandard que floetteeternal, pero ningún set estándar de
    // gen 6 los usa: la excepción no es "todo Unobtainable pasa".
    expect(gen6.some((s) => s.showdownId === "pikachupartner")).toBe(false);
    expect(gen6.some((s) => s.showdownId === "pikachuworld")).toBe(false);
    expect(gen6.some((s) => s.showdownId === "charizardgmax")).toBe(false);
  });
});
