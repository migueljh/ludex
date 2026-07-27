import { describe, expect, it } from "vitest";
import { CalcError, runCalc } from "../src/calc.js";

/**
 * Todos los numeros de este archivo fueron MEDIDOS corriendo @smogon/calc@0.11.0
 * (ver .superpowers/sdd/kimi-calc.md, seccion de inspeccion). No estan
 * estimados ni copiados de memoria.
 */

describe("runCalc: casos medidos de gen 6", () => {
  it("STAB neutro: 252+ Atk Garchomp Earthquake vs Snorlax", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Garchomp", nature: "Adamant", evs: { atk: 252 } },
      defender: { species: "Snorlax" },
      move: { name: "Earthquake" },
    });
    expect(r.damage_rolls).toEqual([[
      255, 258, 261, 264, 267, 270, 273, 276,
      279, 282, 285, 288, 291, 294, 297, 301,
    ]]);
    expect(r.min_damage).toBe(255);
    expect(r.max_damage).toBe(301);
    // El paquete TRUNCA a 1 decimal (301/461 = 65.29... -> 65.2, no 65.3).
    expect(r.min_percent).toBe(55.3);
    expect(r.max_percent).toBe(65.2);
    expect(r.ko_chance).toEqual({ chance: 1, n: 2, text: "guaranteed 2HKO" });
    expect(r.defender_hp).toEqual({ cur: 461, max: 461 });
    expect(r.description).toBe(
      "252+ Atk Garchomp Earthquake vs. 0 HP / 0 Def Snorlax: 255-301 (55.3 - 65.2%) -- guaranteed 2HKO",
    );
  });

  it("super efectivo 4x: 252 Jolly Weavile Ice Punch vs Garchomp", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Weavile", nature: "Jolly", evs: { atk: 252 } },
      defender: { species: "Garchomp" },
      move: { name: "Ice Punch" },
    });
    expect(r.damage_rolls).toEqual([[
      484, 492, 496, 504, 508, 516, 520, 528,
      532, 540, 544, 552, 556, 564, 568, 576,
    ]]);
    expect(r.min_percent).toBe(135.5);
    expect(r.max_percent).toBe(161.3);
    expect(r.ko_chance).toEqual({ chance: 1, n: 1, text: "guaranteed OHKO" });
    expect(r.description).toContain("guaranteed OHKO");
  });

  it("inmune: Body Slam (Normal) contra Gengar (Fantasma) hace 0 y ko_chance es null", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Snorlax" },
      defender: { species: "Gengar" },
      move: { name: "Body Slam" },
    });
    expect(r.damage_rolls).toEqual([[0]]);
    expect(r.min_damage).toBe(0);
    expect(r.max_damage).toBe(0);
    expect(r.min_percent).toBe(0);
    expect(r.max_percent).toBe(0);
    // El paquete NO sabe describir este caso: desc() y kochance() lanzan
    // 'damage[damage.length - 1] === 0'. El servicio sintetiza la descripcion.
    expect(r.ko_chance).toBeNull();
    expect(r.description).toBe("Snorlax Body Slam vs. Gengar: 0-0 (0 - 0%)");
  });

  it("movimiento de estado: Thunder Wave hace 0 y ko_chance es null", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Garchomp" },
      defender: { species: "Gengar" },
      move: { name: "Thunder Wave" },
    });
    expect(r.damage_rolls).toEqual([[0]]);
    expect(r.ko_chance).toBeNull();
    // Para movimientos de estado el desc() del paquete SI funciona.
    expect(r.description).toBe("Garchomp Thunder Wave vs. Gengar: 0-0 (0 - 0%)");
  });

  it("daño fijo: Seismic Toss a nivel 50 hace 50 siempre, sin rolls", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Blissey", level: 50 },
      defender: { species: "Garchomp", level: 50 },
      move: { name: "Seismic Toss" },
    });
    expect(r.damage_rolls).toEqual([[50]]);
    expect(r.min_damage).toBe(50);
    expect(r.max_damage).toBe(50);
    expect(r.min_percent).toBe(27.3);
    expect(r.max_percent).toBe(27.3);
    expect(r.ko_chance).toEqual({ chance: 1, n: 4, text: "guaranteed 4HKO" });
  });

  it("poder variable: Gyro Ball de Ferrothorn (0 spe, Brave) vs Garchomp calcula 150 BP", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Ferrothorn", nature: "Brave", evs: { atk: 252 }, ivs: { spe: 0 } },
      defender: { species: "Garchomp" },
      move: { name: "Gyro Ball" },
    });
    expect(r.damage_rolls).toEqual([[
      225, 228, 229, 232, 235, 238, 241, 243,
      246, 249, 252, 253, 256, 259, 262, 265,
    ]]);
    expect(r.min_percent).toBe(63);
    expect(r.max_percent).toBe(74.2);
    expect(r.ko_chance).toEqual({ chance: 1, n: 2, text: "guaranteed 2HKO" });
    expect(r.description).toContain("Gyro Ball (150 BP)");
  });

  it("multi-golpe: Technician Breloom Bullet Seed devuelve un array de rolls por golpe", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Breloom", nature: "Adamant", evs: { atk: 252 }, ability: "Technician" },
      defender: { species: "Snorlax" },
      move: { name: "Bullet Seed" },
    });
    const perHit = [
      94, 96, 97, 99, 99, 100, 102, 103,
      103, 105, 106, 108, 108, 109, 111, 112,
    ];
    expect(r.damage_rolls).toEqual([perHit, perHit, perHit]);
    // min/max son el TOTAL de los golpes, como el range() del paquete.
    expect(r.min_damage).toBe(282);
    expect(r.max_damage).toBe(336);
    expect(r.min_percent).toBe(61.1);
    expect(r.max_percent).toBe(72.8);
    expect(r.description).toContain("Bullet Seed (3 hits)");
  });

  it("curHP: los porcentajes van contra maxHP pero el KO contra curHP", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Garchomp" },
      defender: { species: "Snorlax", curHP: 100 },
      move: { name: "Earthquake" },
    });
    expect(r.defender_hp).toEqual({ cur: 100, max: 461 });
    expect(r.min_percent).toBe(41.6);
    expect(r.max_percent).toBe(49);
    expect(r.ko_chance).toEqual({ chance: 1, n: 1, text: "guaranteed OHKO" });
  });
});

describe("runCalc: modificadores medidos", () => {
  const base = {
    gen: 6,
    attacker: { species: "Garchomp" },
    defender: { species: "Snorlax" },
  } as const;

  it("boost +2 Atk", () => {
    const r = runCalc({ ...base, attacker: { species: "Garchomp", boosts: { atk: 2 } }, move: { name: "Earthquake" } });
    expect([r.min_damage, r.max_damage]).toEqual([382, 451]);
    expect(r.description).toContain("+2 0 Atk Garchomp Earthquake");
  });

  it("quemadura divide el daño físico", () => {
    const r = runCalc({ ...base, attacker: { species: "Garchomp", status: "brn" }, move: { name: "Earthquake" } });
    expect([r.min_damage, r.max_damage]).toEqual([96, 113]);
    expect(r.description).toContain("burned Garchomp");
  });

  it("clima Rain potencia Water", () => {
    const r = runCalc({ ...base, move: { name: "Aqua Tail" }, field: { weather: "Rain" } });
    expect([r.min_damage, r.max_damage]).toEqual([173, 204]);
    expect(r.description).toContain("in Rain");
  });

  it("Harsh Sunshine (no 'Harsh Sun') potencia Fuego: 252+ SpA Charizard Flamethrower vs Snorlax", () => {
    const attacker = { species: "Charizard", nature: "Modest", evs: { spa: 252 } };
    const sin = runCalc({ gen: 6, attacker, defender: { species: "Snorlax" }, move: { name: "Flamethrower" } });
    expect([sin.min_damage, sin.max_damage]).toEqual([132, 156]);
    const con = runCalc({ gen: 6, attacker, defender: { species: "Snorlax" }, move: { name: "Flamethrower" }, field: { weather: "Harsh Sunshine" } });
    // 1.5x: medido con el paquete. El string 'Harsh Sun' daria el numero sin
    // boost SIN error, asi que el contrato solo admite 'Harsh Sunshine'.
    expect([con.min_damage, con.max_damage]).toEqual([198, 234]);
    expect(con.description).toContain("Harsh Sunshine");
  });

  it("Reflect reduce el daño físico", () => {
    const r = runCalc({ ...base, move: { name: "Earthquake" }, field: { defenderSide: { isReflect: true } } });
    expect([r.min_damage, r.max_damage]).toEqual([96, 113]);
    expect(r.description).toContain("through Reflect");
  });

  it("crítico", () => {
    const r = runCalc({ ...base, move: { name: "Earthquake", isCrit: true } });
    expect([r.min_damage, r.max_damage]).toEqual([288, 339]);
    expect(r.description).toContain("on a critical hit");
  });
});

describe("runCalc: frontera de generación", () => {
  // Acero dejo de resistir Siniestro en gen 6: el mismo matchup difiere.
  const matchup = (gen: number) => ({
    gen,
    attacker: { species: "Bisharp", nature: "Adamant", evs: { atk: 252 } },
    defender: { species: "Empoleon" },
    move: { name: "Night Slash" },
  });

  it("gen 5: Dark vs Steel es 0.5x", () => {
    const r = runCalc(matchup(5));
    expect(r.damage_rolls).toEqual([[
      68, 69, 69, 71, 72, 72, 73, 74,
      75, 75, 76, 77, 78, 78, 79, 81,
    ]]);
    expect(r.min_percent).toBe(22);
    expect(r.max_percent).toBe(26.2);
    expect(r.ko_chance).toEqual({ chance: 0.059661865234375, n: 4, text: "6% chance to 4HKO" });
  });

  it("gen 6: Dark vs Steel es 1x, el mismo matchup hace el doble", () => {
    const r = runCalc(matchup(6));
    expect(r.damage_rolls).toEqual([[
      136, 138, 139, 142, 144, 145, 147, 148,
      150, 151, 153, 154, 156, 157, 159, 162,
    ]]);
    expect(r.min_percent).toBe(44);
    expect(r.max_percent).toBe(52.4);
    expect(r.ko_chance).toEqual({ chance: 0.171875, n: 2, text: "17.2% chance to 2HKO" });
  });
});

describe("runCalc: nombres e ids", () => {
  it("acepta nombre legible, id normalizado, y nombres con unicode", () => {
    for (const species of ["Flabébé", "flabebe"]) {
      const r = runCalc({
        gen: 6,
        attacker: { species },
        defender: { species: "Mr. Mime" },
        move: { name: "hidden power" },
      });
      expect(r.defender_hp.max).toBeGreaterThan(0);
    }
    const r = runCalc({
      gen: 6,
      attacker: { species: "garchomp" },
      defender: { species: "snorlax" },
      move: { name: "Ice Beam" },
    });
    // El desc del paquete incluye los EVs: "0 SpA Garchomp Ice Beam vs. 0 HP / 0 SpD Snorlax: ..."
    expect(r.description).toContain("Garchomp Ice Beam vs.");
    expect(r.description).toContain("Snorlax");
  });
});

describe("runCalc: clima y terreno gateados por generacion", () => {
  // Medido contra @smogon/calc@0.11.0 (ver kimi-calc.md §review): el paquete
  // ignora en silencio los climas primordiales y los terrenos en gens 1-4,
  // ignora 'Hail' en gen 9 (renombrado a 'Snow'), e ignora 'Snow' en gens 3-6
  // (en 7-8 lo calcula, pero el nombre del clima no existia aun). Regla: el
  // contrato nunca acepta un string que el paquete va a ignorar en esa gen.
  const eqVsWeavile = (gen: number, weather?: string) => ({
    gen,
    attacker: { species: "Garchomp" },
    defender: { species: "Weavile" },
    move: { name: "Earthquake" },
    ...(weather ? { field: { weather } } : {}),
  });

  it("gen 9: Snow se acepta y aplica el +50% de Defensa de los tipos Hielo", () => {
    const sin = runCalc(eqVsWeavile(9));
    expect(sin.damage_rolls).toEqual([[
      192, 193, 196, 198, 201, 202, 205, 207,
      210, 211, 214, 216, 219, 220, 223, 226,
    ]]);
    const con = runCalc(eqVsWeavile(9, "Snow"));
    expect(con.damage_rolls).toEqual([[
      127, 129, 130, 132, 133, 135, 136, 138,
      139, 141, 142, 144, 145, 147, 148, 151,
    ]]);
    expect(con.min_percent).toBe(45.1);
    expect(con.max_percent).toBe(53.7);
    expect(con.description).toContain("in Snow");
  });

  it("gen 9: Hail se rechaza apuntando a Snow (el paquete lo ignora: seria un calculo sin clima disfrazado)", () => {
    try {
      runCalc(eqVsWeavile(9, "Hail"));
      expect.unreachable("debio lanzar");
    } catch (e) {
      expect(e).toBeInstanceOf(CalcError);
      expect((e as CalcError).message).toMatch(/Hail/);
      expect((e as CalcError).message).toMatch(/Snow/);
    }
  });

  it("gen 6: Snow se rechaza apuntando a Hail (Snow existe desde gen 9)", () => {
    try {
      runCalc(eqVsWeavile(6, "Snow"));
      expect.unreachable("debio lanzar");
    } catch (e) {
      expect(e).toBeInstanceOf(CalcError);
      expect((e as CalcError).message).toMatch(/Snow/);
      expect((e as CalcError).message).toMatch(/Hail/);
    }
  });

  it("gen 8: Hail se acepta (no modifica el daño, correcto para esa gen)", () => {
    const r = runCalc(eqVsWeavile(8, "Hail"));
    expect([r.min_damage, r.max_damage]).toEqual([192, 226]);
  });

  it("climas primordiales: calculados en gen 5+, rechazados en gen <= 4 donde el paquete los ignora", () => {
    const attacker = { species: "Charizard", nature: "Modest", evs: { spa: 252 } };
    const con = runCalc({
      gen: 5, attacker, defender: { species: "Snorlax" },
      move: { name: "Flamethrower" }, field: { weather: "Harsh Sunshine" },
    });
    expect([con.min_damage, con.max_damage]).toEqual([210, 247]);
    try {
      runCalc({
        gen: 4, attacker, defender: { species: "Snorlax" },
        move: { name: "Flamethrower" }, field: { weather: "Harsh Sunshine" },
      });
      expect.unreachable("debio lanzar");
    } catch (e) {
      expect((e as CalcError).message).toMatch(/ignora/);
    }
  });

  it("terrenos: calculados en gen 5+, rechazados en gen <= 4 donde el paquete los ignora", () => {
    const con = runCalc({
      gen: 5,
      attacker: { species: "Pikachu", nature: "Modest", evs: { spa: 252 } },
      defender: { species: "Snorlax" },
      move: { name: "Thunderbolt" },
      field: { terrain: "Electric" },
    });
    expect([con.min_damage, con.max_damage]).toEqual([130, 154]);
    try {
      runCalc({
        gen: 4,
        attacker: { species: "Pikachu", nature: "Modest", evs: { spa: 252 } },
        defender: { species: "Snorlax" },
        move: { name: "Thunderbolt" },
        field: { terrain: "Electric" },
      });
      expect.unreachable("debio lanzar");
    } catch (e) {
      expect((e as CalcError).message).toMatch(/ignora/);
    }
  });
});

describe("runCalc: validacion de entrada", () => {
  const valid = {
    gen: 6,
    attacker: { species: "Garchomp" },
    defender: { species: "Snorlax" },
    move: { name: "Earthquake" },
  };
  const expectError = (req: unknown, code: string, match: RegExp) => {
    try {
      runCalc(req);
      expect.unreachable(`debio lanzar ${code}`);
    } catch (e) {
      expect(e).toBeInstanceOf(CalcError);
      expect((e as CalcError).code).toBe(code);
      expect((e as CalcError).message).toMatch(match);
    }
  };

  it("gen fuera de rango o no numerico", () => {
    expectError({ ...valid, gen: 0 }, "invalid_gen", /gen/);
    expectError({ ...valid, gen: 10 }, "invalid_gen", /gen/);
    expectError({ ...valid, gen: "6" }, "invalid_gen", /gen/);
    expectError({ ...valid, gen: undefined }, "invalid_gen", /gen/);
  });

  it("especie inexistente o de otra generacion dice cual y para que gen", () => {
    expectError({ ...valid, attacker: { species: "NotAPokemon" } }, "unknown_species", /NotAPokemon/);
    expectError({ ...valid, attacker: { species: "Incineroar" } }, "unknown_species", /Incineroar.*gen 6|gen 6.*Incineroar/);
    expectError({ ...valid, defender: { species: "Incineroar" } }, "unknown_species", /defender/);
  });

  it("movimiento inexistente o de otra generacion", () => {
    expectError({ ...valid, move: { name: "NotAMove" } }, "unknown_move", /NotAMove/);
    expectError({ ...valid, move: { name: "Zippy Zap" } }, "unknown_move", /Zippy Zap/);
  });

  it("item, habilidad, naturaleza y estado inexistentes", () => {
    expectError({ ...valid, attacker: { species: "Garchomp", item: "NotAnItem" } }, "unknown_item", /NotAnItem/);
    expectError({ ...valid, attacker: { species: "Garchomp", ability: "NotAnAbility" } }, "unknown_ability", /NotAnAbility/);
    expectError({ ...valid, attacker: { species: "Garchomp", nature: "NotANature" } }, "unknown_nature", /NotANature/);
    expectError({ ...valid, attacker: { species: "Garchomp", status: "notastatus" } }, "invalid_request", /status/);
  });

  it("boosts fuera de -6..6: 400 util, no 500 (el paquete indexa una tabla de 7 sin clampear y revienta)", () => {
    expectError({ ...valid, attacker: { species: "Garchomp", boosts: { atk: 99 } } }, "invalid_request", /boosts\.atk.*-6 y 6/);
    expectError({ ...valid, attacker: { species: "Garchomp", boosts: { atk: -7 } } }, "invalid_request", /-6 y 6/);
    expectError({ ...valid, defender: { species: "Snorlax", boosts: { def: 2.5 } } }, "invalid_request", /entero/);
    const ok = runCalc({ ...valid, attacker: { species: "Garchomp", boosts: { atk: 6 } } });
    expect(ok.max_damage).toBeGreaterThan(0);
  });

  it("evs e ivs fuera de rango: 400 util, no numeros absurdos en silencio", () => {
    // evs {atk: 999999} devolvia max_percent 8839.9 sin error (medido en review).
    expectError({ ...valid, attacker: { species: "Garchomp", evs: { atk: 999999 } } }, "invalid_request", /evs\.atk.*0 y 252/);
    expectError({ ...valid, attacker: { species: "Garchomp", evs: { atk: -1 } } }, "invalid_request", /0 y 252/);
    expectError({ ...valid, defender: { species: "Snorlax", ivs: { def: 32 } } }, "invalid_request", /ivs\.def.*0 y 31/);
    // Los limites validos pasan: 252 EVs y 31 IVs.
    const ok = runCalc({ ...valid, attacker: { species: "Garchomp", evs: { atk: 252 }, ivs: { atk: 31 } } });
    expect(ok.max_damage).toBeGreaterThan(0);
  });

  it("gen 1-2 con spa != spd: 400 con el mensaje del paquete, no 500", () => {
    // El paquete lanza Error('Special Attack and Special Defense must match
    // before Gen 3') al construir el Pokemon: es error del cliente, no un bug.
    expectError({
      gen: 2,
      attacker: { species: "Charizard", evs: { spa: 252 } },
      defender: { species: "Snorlax" },
      move: { name: "Flamethrower" },
    }, "invalid_request", /Special Attack and Special Defense must match/);
  });

  it("campos requeridos y forma del body", () => {
    expectError(null, "invalid_request", /./);
    // gen es la primera clave del contrato: sin ella no se puede validar nada mas.
    expectError({}, "invalid_gen", /gen/);
    expectError({ ...valid, attacker: {} }, "invalid_request", /species/);
    expectError({ ...valid, move: {} }, "invalid_request", /move/);
    expectError({ ...valid, move: { name: 42 } }, "invalid_request", /move/);
    expectError({ ...valid, attacker: { species: "Garchomp", level: 101 } }, "invalid_request", /level/);
    expectError({ ...valid, attacker: { species: "Garchomp", evs: { atk: "252" } } }, "invalid_request", /evs/);
    expectError({ ...valid, field: { weather: "NotAWeather" } }, "invalid_request", /weather/);
    expectError({ ...valid, field: { terrain: "NotATerrain" } }, "invalid_request", /terrain/);
  });

  it("no valida learnsets: Magikarp con Draco Meteor calcula (como la calc oficial)", () => {
    const r = runCalc({
      gen: 6,
      attacker: { species: "Magikarp" },
      defender: { species: "Garchomp" },
      move: { name: "Draco Meteor" },
    });
    expect(r.max_damage).toBeGreaterThan(0);
  });
});
