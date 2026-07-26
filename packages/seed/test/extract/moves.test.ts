import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractMoves } from "../../src/extract/moves.js";

const gen6 = extractMoves(loadGen(6));
const gen9 = extractMoves(loadGen(9));
const byId = (id: string) => gen6.find((m) => m.showdownId === id)!;

describe("extractMoves", () => {
  it("devuelve los conteos conocidos", () => {
    // El mod lista 634 entradas, pero las 16 variantes de tipo de Hidden Power
    // repiten el id "hiddenpower": la clave natural (gen, id) conserva una.
    expect(gen6).toHaveLength(618);
    expect(gen9).toHaveLength(685);
  });

  it("deduplica las variantes de Hidden Power conservando la entrada base", () => {
    const hp = gen6.filter((m) => m.showdownId === "hiddenpower");
    expect(hp).toHaveLength(1);
    expect(hp[0].name).toBe("Hidden Power");
    expect(hp[0].type).toBe("Normal");
    expect(hp[0].power).toBe(60);
  });

  it("mapea basePower al campo power", () => {
    const ft = byId("flamethrower");
    expect(ft.name).toBe("Flamethrower");
    expect(ft.type).toBe("Fire");
    expect(ft.category).toBe("Special");
    expect(ft.power).toBe(90);
    expect(ft.accuracy).toBe(100);
    expect(ft.pp).toBe(15);
    expect(ft.priority).toBe(0);
    expect(ft.target).toBe("normal");
    expect(ft.flags).toMatchObject({ protect: 1, mirror: 1 });
  });

  it("convierte accuracy true en null", () => {
    expect(byId("swift").accuracy).toBeNull();
  });

  it("guarda 0 de potencia para movimientos de estado", () => {
    const th = byId("thunderwave");
    expect(th.category).toBe("Status");
    expect(th.power).toBe(0);
  });

  it("no repite showdownId", () => {
    expect(new Set(gen6.map((m) => m.showdownId)).size).toBe(gen6.length);
  });
});
