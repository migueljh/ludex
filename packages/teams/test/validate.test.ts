import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Pool } from "pg";
import { createPool } from "../src/db.js";
import { validateTeamText, type TeamIssue } from "../src/validate.js";

/**
 * Todos los casos fueron verificados contra la base real (gen 6 seedeada con
 * pokemon-showdown@0.11.10) antes de escribirse — ver
 * .superpowers/sdd/kimi-teams.md. Nada sale de memoria.
 */

let pool: Pool;
beforeAll(() => { pool = createPool(); });
afterAll(async () => { await pool.end(); });

// Equipo gen6ou legal, verificado fila por fila contra la base:
// - las 6 especies existen en gen 6 con esas habilidades
// - los 24 movimientos tienen fila en learnsets para esa especie en gen 6
// - los 5 objetos existen en gen 6
// Cubre apodos, genero, nivel 50, IVs parciales, Hidden Power tipado y DOS
// formas con learnset propio (Rotom-Wash por D10, Gourgeist-Super por D14).
const TEAM_LEGAL = `Chompy (Garchomp) (M) @ Life Orb
Ability: Rough Skin
Level: 50
EVs: 252 Atk / 4 SpD / 252 Spe
Jolly Nature
- Earthquake
- Outrage
- Swords Dance
- Fire Fang

Rotom-Wash @ Leftovers
Ability: Levitate
EVs: 252 HP / 200 Def / 56 Spe
Bold Nature
IVs: 0 Atk
- Hydro Pump
- Volt Switch
- Will-O-Wisp
- Pain Split

Pinky (Clefable) @ Rocky Helmet
Ability: Magic Guard
EVs: 252 HP / 172 Def / 84 Spe
Calm Nature
IVs: 0 Atk / 30 SpA
- Moonblast
- Hidden Power Fire
- Soft-Boiled
- Thunder Wave

Bisharp @ Black Glasses
Ability: Defiant
EVs: 252 Atk / 4 SpD / 252 Spe
Adamant Nature
- Knock Off
- Sucker Punch
- Iron Head
- Swords Dance

Azumarill @ Choice Band
Ability: Huge Power
EVs: 252 HP / 252 Atk / 4 SpD
Adamant Nature
- Play Rough
- Aqua Jet
- Waterfall
- Superpower

Gourgeist-Super @ Leftovers
Ability: Frisk
EVs: 252 HP / 252 Def / 4 SpD
Impish Nature
- Shadow Ball
- Seed Bomb
- Trick-or-Treat
- Will-O-Wisp
`;

const byField = (issues: TeamIssue[], field: string) => issues.filter((i) => i.field === field);

describe("validateTeamText", () => {
  it("valida un equipo legal completo de gen 6, con sus rarezas", async () => {
    const r = await validateTeamText(TEAM_LEGAL, 6, { pool });
    expect(r.sets).toBe(6);
    expect(r.issues).toEqual([]);
    expect(r.ok).toBe(true);
  });

  it("una forma con learnset propio ademas del equipo: Deoxys-Attack y Psycho Boost", async () => {
    const r = await validateTeamText(
      `Deoxys-Attack @ Life Orb
Ability: Pressure
EVs: 4 Atk / 252 SpA / 252 Spe
Naive Nature
- Psycho Boost
- Superpower
`, 6, { pool });
    expect(r.issues).toEqual([]);
    expect(r.ok).toBe(true);
  });

  it("movimiento que la especie no aprende: ilegal, no inexistente", async () => {
    // Verificado: no hay fila (garchomp, icebeam) en learnsets de gen 6.
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Rough Skin
- Ice Beam
- Earthquake
`, 6, { pool });
    expect(r.ok).toBe(false);
    expect(r.issues).toHaveLength(1);
    const [issue] = r.issues;
    expect(issue.field).toBe("move");
    expect(issue.kind).toBe("illegal");
    expect(issue.pokemon).toBe("Garchomp");
    expect(issue.move).toBe("Ice Beam");
    expect(issue.message).toContain("Ice Beam");
    expect(issue.message).toContain("Garchomp");
  });

  it("movimiento inexistente: unknown, distinto de ilegal", async () => {
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Rough Skin
- Not A Real Move
`, 6, { pool });
    expect(r.issues).toHaveLength(1);
    expect(r.issues[0].field).toBe("move");
    expect(r.issues[0].kind).toBe("unknown");
    expect(r.issues[0].message).toContain("Not A Real Move");
  });

  it("movimiento real pero de otra generacion: unknown con pista de gen", async () => {
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Rough Skin
- Zippy Zap
`, 6, { pool });
    expect(r.issues).toHaveLength(1);
    expect(r.issues[0].kind).toBe("unknown");
    expect(r.issues[0].message).toContain("gen 6");
    expect(r.issues[0].message).toContain("gen 7");
  });

  it("especie de otra generacion: unknown con pista de gen", async () => {
    const r = await validateTeamText(
      `Incineroar @ Leftovers
Ability: Intimidate
- Flare Blitz
`, 6, { pool });
    expect(r.issues.some((i) => i.field === "species" && i.kind === "unknown")).toBe(true);
    const issue = r.issues.find((i) => i.field === "species")!;
    expect(issue.message).toContain("Incineroar");
    expect(issue.message).toContain("gen 7");
  });

  it("EVs: mas de 252 en un stat y mas de 510 en total son dos errores distintos", async () => {
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Rough Skin
EVs: 300 Atk / 252 SpA / 252 Spe
- Earthquake
`, 6, { pool });
    const evs = byField(r.issues, "evs");
    // 300 en Atk (>252) y 804 en total (>510): ambos reportados
    expect(evs.length).toBeGreaterThanOrEqual(2);
    expect(evs.some((i) => i.message.includes("252") && i.message.includes("Atk"))).toBe(true);
    expect(evs.some((i) => i.message.includes("510"))).toBe(true);
  });

  it("IVs fuera de 0-31 y nivel fuera de 1-100", async () => {
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Rough Skin
Level: 150
IVs: 40 Atk
- Earthquake
`, 6, { pool });
    expect(r.issues.some((i) => i.field === "ivs" && i.kind === "invalid")).toBe(true);
    expect(r.issues.some((i) => i.field === "level" && i.kind === "invalid")).toBe(true);
  });

  it("habilidad que no le corresponde a la especie: ilegal; inexistente: unknown", async () => {
    // Garchomp en gen 6: {"0": "Sand Veil", "H": "Rough Skin"} — Levitate no esta.
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Levitate
- Earthquake
`, 6, { pool });
    expect(r.issues).toHaveLength(1);
    expect(r.issues[0].field).toBe("ability");
    expect(r.issues[0].kind).toBe("illegal");

    const r2 = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Not An Ability
- Earthquake
`, 6, { pool });
    expect(r2.issues).toHaveLength(1);
    expect(r2.issues[0].field).toBe("ability");
    expect(r2.issues[0].kind).toBe("unknown");
  });

  it("objeto de otra generacion: unknown con pista de gen", async () => {
    const r = await validateTeamText(
      `Garchomp @ Booster Energy
Ability: Rough Skin
- Earthquake
`, 6, { pool });
    expect(r.issues).toHaveLength(1);
    expect(r.issues[0].field).toBe("item");
    expect(r.issues[0].kind).toBe("unknown");
    expect(r.issues[0].message).toContain("gen 9");
  });

  it("naturaleza inexistente", async () => {
    const r = await validateTeamText(
      `Garchomp @ Life Orb
Ability: Rough Skin
NotANature Nature
- Earthquake
`, 6, { pool });
    expect(r.issues.some((i) => i.field === "nature")).toBe(true);
  });

  it("equipo vacio, equipo de 7, y acumulacion de errores", async () => {
    const vacio = await validateTeamText("", 6, { pool });
    expect(vacio.ok).toBe(false);
    expect(vacio.issues.some((i) => i.field === "team")).toBe(true);

    const siete = await validateTeamText(
      Array(7).fill("Garchomp\n- Earthquake\n").join("\n\n"), 6, { pool });
    expect(siete.sets).toBe(7);
    expect(siete.ok).toBe(false);
    expect(siete.issues.some((i) => i.field === "team")).toBe(true);
  });

  it("cuatro errores en cuatro sets distintos salen los cuatro, no solo el primero", async () => {
    const r = await validateTeamText(
      `Incineroar @ Leftovers
Ability: Intimidate
- Flare Blitz

Garchomp @ Life Orb
Ability: Rough Skin
- Ice Beam

Bisharp @ Black Glasses
Ability: Defiant
- Not A Real Move

Azumarill @ Choice Band
Ability: Huge Power
EVs: 300 Atk
- Aqua Jet
`, 6, { pool });
    expect(r.issues).toHaveLength(4);
    expect(r.issues.map((i) => i.field).sort()).toEqual(["evs", "move", "move", "species"]);
    expect(r.issues.map((i) => i.kind).sort()).toEqual(["illegal", "invalid", "unknown", "unknown"]);
  });

  it("gen no seedeada en la base: error claro, no un crash", async () => {
    // La base tiene gens 6 y 9. Pedir gen 7 es un problema de entorno, no del equipo.
    await expect(validateTeamText(TEAM_LEGAL, 7, { pool })).rejects.toThrow(/gen 7/);
  });
});
