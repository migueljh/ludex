import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractMoves } from "../../src/extract/moves.js";

const gen6 = extractMoves(loadGen(6));
const gen9 = extractMoves(loadGen(9));
const byId = (id: string) => gen6.find((m) => m.showdownId === id)!;

describe("extractMoves", () => {
  it("devuelve los conteos conocidos, ya deduplicados", () => {
    // D47/MON-24: 619 = 618 + lightofruin (isNonstandard:'Unobtainable',
    // pero es el movimiento propio de floetteeternal en el catálogo estándar
    // random-battle de gen 6).
    expect(gen6).toHaveLength(619);
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

  it("clasifica power_kind en los cinco casos", () => {
    // fixed_damage se deriva de `damage` ('level'), NO de basePowerCallback:
    // seismictoss no tiene callback y un derivador basado en callback lo
    // clasificaria mal.
    expect(byId("seismictoss").powerKind).toBe("fixed_damage");
    expect(byId("dragonrage").powerKind).toBe("fixed_damage");
    expect(byId("gyroball").powerKind).toBe("variable");
    expect(byId("toxic").powerKind).toBe("status");
    expect(byId("flamethrower").powerKind).toBe("standard");
    expect(byId("superfang").powerKind).toBe("special");
  });

  it("fija la distribucion medida de power_kind (pokemon-showdown@0.11.10)", () => {
    // Valores medidos sobre los 619/685 movimientos unicos; no estimados.
    // D47/MON-24: lightofruin es category Special, basePower 140, sin
    // basePowerCallback ni damage -> powerKind 'standard' (335 + 1 = 336).
    const dist = (rows: typeof gen6) => {
      const d: Record<string, number> = {};
      for (const m of rows) d[m.powerKind] = (d[m.powerKind] ?? 0) + 1;
      return d;
    };
    expect(dist(gen6)).toEqual({
      status: 225, standard: 336, variable: 38, special: 16, fixed_damage: 4,
    });
    expect(dist(gen9)).toEqual({
      standard: 416, status: 214, variable: 39, special: 14, fixed_damage: 2,
    });
  });

  it("no repite showdownId", () => {
    expect(new Set(gen6.map((m) => m.showdownId)).size).toBe(gen6.length);
  });

  it("D47/MON-24: lightofruin entra con fila directa y sus datos reales", () => {
    const move = byId("lightofruin");
    expect(move.name).toBe("Light of Ruin");
    expect(move.type).toBe("Fairy");
    expect(move.category).toBe("Special");
    expect(move.power).toBe(140);
    expect(move.accuracy).toBe(90);
    expect(move.pp).toBe(5);
    expect(move.powerKind).toBe("standard");
  });

  it("D47/MON-24: lightofruin NO aparece en gen 9 (su catálogo no lo referencia)", () => {
    expect(gen9.some((m) => m.showdownId === "lightofruin")).toBe(false);
  });

  it("D47/MON-24: sigue excluyendo Unobtainable no referenciado y CAP", () => {
    expect(gen6.some((m) => m.showdownId === "thousandarrows")).toBe(false);
    expect(gen6.some((m) => m.showdownId === "thousandwaves")).toBe(false);
    expect(gen6.some((m) => m.showdownId === "paleowave")).toBe(false);
    expect(gen6.some((m) => m.showdownId === "shadowstrike")).toBe(false);
  });
});
