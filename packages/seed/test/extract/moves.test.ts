import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractMoves } from "../../src/extract/moves.js";

const gen6 = extractMoves(loadGen(6));
const gen9 = extractMoves(loadGen(9));
const byId = (id: string) => gen6.find((m) => m.showdownId === id)!;

describe("extractMoves", () => {
  it("devuelve los conteos conocidos, ya deduplicados", () => {
    expect(gen6).toHaveLength(618);
    expect(gen9).toHaveLength(685);
  });

  it("colapsa los 17 Hidden Power de gen 6 en la entrada base", () => {
    // Los 17 (base + 16 tipos) comparten id 'hiddenpower'. La columna
    // moves.showdown_id es UNIQUE por generacion, asi que si extract no
    // deduplica, load colapsa a 618 igual y seed_runs.row_counts miente.
    const hp = gen6.filter((m) => m.showdownId === "hiddenpower");
    expect(hp).toHaveLength(1);
    expect(hp[0].name).toBe("Hidden Power");
    expect(hp[0].type).toBe("Normal");
    expect(hp[0].power).toBe(60);
    // En gen 9 el movimiento ya no existe.
    expect(gen9.some((m) => m.showdownId === "hiddenpower")).toBe(false);
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
