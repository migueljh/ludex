/** La proyección, contra poke-env 0.15.0 línea por línea.
 *
 * Cada test cita el comportamiento de la librería que reproduce. Si algún día
 * la proyección y poke-env difieren, gana poke-env y este archivo es donde se
 * ve la diferencia sin levantar Postgres ni Showdown.
 */

import { describe, expect, it } from "vitest";
import { buildSpeciesIndex } from "../src/protocol.js";
import {
  abilityOf,
  BattleProjection,
  buildDexView,
  isFainted,
  movesOf,
  typesOf,
  UNKNOWN_ITEM,
  type MonState,
} from "../src/projection.js";
import { cosmeticFormes, dexMoves, dexPokemon } from "./fixtures.js";

const dex = buildDexView(dexMoves(), 6);
const species = buildSpeciesIndex(dexPokemon(), 6, cosmeticFormes());

const SWITCH_MINE = "|switch|p1a: Gengar|Gengar, L80, M|280/280";
const SWITCH_OPP = "|switch|p2a: Lapras|Lapras, L80, F|100/100";

function project(lines: string[]): BattleProjection {
  const projection = new BattleProjection(dex, species, {
    side: "p1",
    abilities: new Map([["gengar", "levitate"]]),
    items: new Map([["gengar", "lifeorb"]]),
  });
  for (const line of lines) projection.apply(line);
  return projection;
}

function mon(projection: BattleProjection, ident: string): MonState {
  const found = projection.mons.get(ident);
  if (found === undefined) throw new Error(`no hay proyección para ${ident}`);
  return found;
}

function ppOf(projection: BattleProjection, ident: string, move: string): [unknown, unknown] {
  const state = movesOf(mon(projection, ident)).get(move);
  return [state?.ppMin, state?.ppMax];
}

describe("identidad y forma", () => {
  it("una Mega NO cambia la especie: `forme_change` usa store_species=False", () => {
    const projection = project([
      "|switch|p2a: Charizard|Charizard, L79, M|100/100",
      "|detailschange|p2a: Charizard|Charizard-Mega-X, L79, M",
    ]);
    const charizard = mon(projection, "p2:charizard");
    expect(charizard.species).toBe("charizard");
    // Los TIPOS y la ability de forma sí cambian.
    expect(typesOf(charizard)).toEqual(["FIRE", "DRAGON"]);
    expect(abilityOf(charizard)).toBe("toughclaws");
  });

  it("antes del detailschange, los tipos y la ability son los de la forma base", () => {
    const projection = project(["|switch|p2a: Charizard|Charizard, L79, M|100/100"]);
    const charizard = mon(projection, "p2:charizard");
    expect(typesOf(charizard)).toEqual(["FIRE", "FLYING"]);
    // Charizard tiene dos abilities posibles: el dex no determina ninguna.
    expect(abilityOf(charizard)).toBeNull();
  });

  it("un `-formechange` no trae nivel y no puede fabricar L100", () => {
    const projection = project([
      "|switch|p2a: Charizard|Charizard, L79, M|100/100",
      "|-formechange|p2a: Charizard|Charizard-Mega-X",
    ]);
    expect(mon(projection, "p2:charizard").level).toBe(79);
  });

  it("un details sin token L sí significa 100 (Showdown lo omite)", () => {
    expect(mon(project(["|switch|p2a: Lapras|Lapras, F|100/100"]), "p2:lapras").level).toBe(100);
  });
});

describe("HP, status y desmayo", () => {
  it("`0 fnt` desmaya; el desmayado NO deja la ranura hasta que entra su relevo", () => {
    const projection = project([SWITCH_OPP, "|-damage|p2a: Lapras|0 fnt", "|faint|p2a: Lapras"]);
    const lapras = mon(projection, "p2:lapras");
    expect([lapras.hp, lapras.status, isFainted(lapras), lapras.active])
      .toEqual([0, "fnt", true, true]);
  });

  it("un token de HP sin status BORRA el status: no es 'sin novedad'", () => {
    const projection = project([
      SWITCH_OPP,
      "|-status|p2a: Lapras|par",
      "|-heal|p2a: Lapras|100/100",
    ]);
    expect(mon(projection, "p2:lapras").status).toBeNull();
  });

  it("`-curestatus` sólo borra el status que nombra", () => {
    const projection = project([
      SWITCH_OPP,
      "|-status|p2a: Lapras|par",
      "|-curestatus|p2a: Lapras|slp",
    ]);
    expect(mon(projection, "p2:lapras").status).toBe("par");
  });
});

describe("pertenencia de item y ability por sufijo", () => {
  it("en `-heal` el `[of]` es engañoso: la ability es del ident", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|-heal|p2a: Lapras|100/100|[from] ability: Water Absorb|[of] p1a: Gengar",
    ]);
    expect(abilityOf(mon(projection, "p2:lapras"))).toBe("waterabsorb");
    expect(abilityOf(mon(projection, "p1:gengar"))).toBe("levitate");
  });

  it("en `-damage` el `[of]` SÍ es el dueño (Rocky Helmet)", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|-damage|p1a: Gengar|200/280|[from] item: Rocky Helmet|[of] p2a: Lapras",
    ]);
    expect(mon(projection, "p2:lapras").item).toBe("rockyhelmet");
    // El nuestro lo conoce por el request, no por esta línea.
    expect(mon(projection, "p1:gengar").item).toBe("lifeorb");
  });

  it("en `-ability` el `[of]` es el pokémon TRAZADO: se acepta, no se exige", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|-ability|p1a: Gengar|Water Absorb|[from] ability: Trace|[of] p2a: Lapras",
    ]);
    // poke-env escribe SÓLO sobre el que traza.
    expect(abilityOf(mon(projection, "p1:gengar"))).toBe("waterabsorb");
    expect(abilityOf(mon(projection, "p2:lapras"))).toBeNull();
    // Pero la línea prueba que el trazado la tiene: decirlo no es fuga.
    expect(mon(projection, "p2:lapras").admissibleAbilities.has("waterabsorb")).toBe(true);
  });

  it("un `-enditem` deja el item en null, que NO es el centinela", () => {
    const projection = project([
      SWITCH_OPP,
      "|-item|p2a: Lapras|Leftovers",
      "|-enditem|p2a: Lapras|Leftovers",
    ]);
    expect(mon(projection, "p2:lapras").item).toBeNull();
  });

  it("un Trick intercambia los items de los dos socios", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|-item|p1a: Gengar|Choice Specs",
      "|-item|p2a: Lapras|Leftovers",
      "|move|p1a: Gengar|Trick|p2a: Lapras",
      "|-activate|p1a: Gengar|move: Trick|[of] p2a: Lapras",
      "|-item|p2a: Lapras|Choice Specs|[from] move: Trick",
      "|-item|p1a: Gengar|Leftovers|[from] move: Trick",
    ]);
    expect(mon(projection, "p2:lapras").item).toBe("choicespecs");
    expect(mon(projection, "p1:gengar").item).toBe("leftovers");
  });

  it("una baya que ya se consumió no se le asigna al que se cura", () => {
    const projection = project([
      SWITCH_OPP,
      "|-heal|p2a: Lapras|100/100|[from] item: Sitrus Berry",
    ]);
    expect(mon(projection, "p2:lapras").item).toBe(UNKNOWN_ITEM);
  });
});

describe("ability: persistente contra temporal", () => {
  it("Skill Swap tapa la ability y `-endability` la devuelve", () => {
    const lines = [
      SWITCH_OPP,
      "|-ability|p2a: Lapras|Water Absorb",
      "|-ability|p2a: Lapras|Technician|[from] move: Skill Swap",
    ];
    expect(abilityOf(mon(project(lines), "p2:lapras"))).toBe("technician");
    expect(abilityOf(mon(project([...lines, "|-endability|p2a: Lapras"]), "p2:lapras")))
      .toBe("waterabsorb");
  });

  it("salir del campo también revierte el override temporal", () => {
    const projection = project([
      SWITCH_OPP,
      "|-ability|p2a: Lapras|Water Absorb",
      "|-ability|p2a: Lapras|Technician|[from] move: Skill Swap",
      "|switch|p2a: Ditto|Ditto, L80|100/100",
    ]);
    expect(abilityOf(mon(projection, "p2:lapras"))).toBe("waterabsorb");
  });

  it("una ability única en el dex es conocimiento público, no fuga", () => {
    expect(abilityOf(mon(project(["|switch|p2a: Dusknoir|Dusknoir, L80, M|100/100"]), "p2:dusknoir")))
      .toBe("pressure");
  });
});

describe("pertenencia de movimientos", () => {
  it("Magic Bounce no le atribuye al que rebota el movimiento rebotado", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|move|p2a: Lapras|Spore|p1a: Gengar|[from] ability: Magic Bounce",
    ]);
    expect([...movesOf(mon(projection, "p2:lapras")).keys()]).toEqual([]);
    expect(abilityOf(mon(projection, "p2:lapras"))).toBe("magicbounce");
  });

  it("Dancer descarta el evento entero", () => {
    const projection = project([
      SWITCH_OPP,
      "|move|p2a: Lapras|Petal Dance|p1a: Gengar|[from] ability: Dancer",
    ]);
    expect([...movesOf(mon(projection, "p2:lapras")).keys()]).toEqual([]);
  });

  it("un movimiento llamado por Copycat no es del actor; el PP lo paga Copycat", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|move|p2a: Lapras|Copycat|p2a: Lapras",
      "|move|p2a: Lapras|Shadow Ball|p1a: Gengar|[from]move: Copycat",
    ]);
    const moves = movesOf(mon(projection, "p2:lapras"));
    expect([...moves.keys()]).toEqual(["copycat"]);
    // Descuento preventivo de 1 al narrarse Copycat; el `overridden` no resta.
    expect(ppOf(projection, "p2:lapras", "copycat")).toEqual([31, 31]);
  });

  it("Sleep Talk SÍ revela el movimiento llamado: sólo puede llamar a los propios", () => {
    const projection = project([
      SWITCH_OPP,
      "|move|p2a: Lapras|Sleep Talk|p2a: Lapras",
      "|move|p2a: Lapras|Rest|p2a: Lapras|[from]move: Sleep Talk",
    ]);
    const moves = movesOf(mon(projection, "p2:lapras"));
    expect([...moves.keys()].sort()).toEqual(["rest", "sleeptalk"]);
    expect(ppOf(projection, "p2:lapras", "rest")).toEqual([16, 16]);
  });

  it("un `[from] lockedmove` no revela ni descuenta", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|move|p2a: Lapras|Outrage|p1a: Gengar",
      "|move|p2a: Lapras|Outrage|p1a: Gengar|[from]lockedmove",
    ]);
    expect(ppOf(projection, "p2:lapras", "outrage")).toEqual([15, 15]);
  });
});

describe("PP exacto", () => {
  it("baja de a uno por uso narrado", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|move|p2a: Lapras|Shadow Ball|p1a: Gengar",
      "|move|p2a: Lapras|Shadow Ball|p1a: Gengar",
    ]);
    expect(ppOf(projection, "p2:lapras", "shadowball")).toEqual([22, 22]);
  });

  it("baja de a dos contra Pressure, y la categoría de objetivo decide", () => {
    const attack = project([
      "|switch|p1a: Dusknoir|Dusknoir, L80, M|100/100",
      SWITCH_OPP,
      "|move|p2a: Lapras|Shadow Ball|p1a: Dusknoir",
    ]);
    expect(ppOf(attack, "p2:lapras", "shadowball")).toEqual([22, 22]);
    // `swordsdance` tiene target `self`: Pressure no aplica.
    const self = project([
      "|switch|p1a: Dusknoir|Dusknoir, L80, M|100/100",
      SWITCH_OPP,
      "|move|p2a: Lapras|Swords Dance|p2a: Lapras",
    ]);
    expect(ppOf(self, "p2:lapras", "swordsdance")).toEqual([31, 31]);
  });

  it("una Leppa Berry devuelve 10 PP sin pasarse del máximo", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|move|p2a: Lapras|Shadow Ball|p1a: Gengar",
      "|-activate|p2a: Lapras|item: Leppa Berry|Shadow Ball",
    ]);
    expect(ppOf(projection, "p2:lapras", "shadowball")).toEqual([24, 24]);
  });
});

describe("boosts", () => {
  it("`-copyboost|FUENTE|OBJETIVO` escribe en el SEGUNDO: la fuente no cambia", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|-boost|p2a: Lapras|atk|2",
      "|-copyboost|p2a: Lapras|p1a: Gengar|[from] move: Psych Up",
    ]);
    expect(mon(projection, "p2:lapras").boosts.atk).toBe(2);
    expect(mon(projection, "p1:gengar").boosts.atk).toBe(2);
    expect(mon(projection, "p2:lapras").unresolved.has("boosts")).toBe(false);
  });

  it("`-clearallboost` alcanza sólo a los dos activos", () => {
    const projection = project([
      SWITCH_OPP, SWITCH_MINE,
      "|-boost|p2a: Lapras|atk|2",
      "|switch|p2a: Ditto|Ditto, L80|100/100",
      "|-boost|p2a: Ditto|spe|1",
      "|-clearallboost",
    ]);
    expect(mon(projection, "p2:ditto").boosts.spe).toBe(0);
    // Lapras ya había limpiado los suyos al salir del campo.
    expect(mon(projection, "p2:lapras").boosts.atk).toBe(0);
  });

  it("salir del campo limpia los boosts (`switch_out`)", () => {
    const projection = project([
      SWITCH_OPP,
      "|-boost|p2a: Lapras|atk|2",
      "|switch|p2a: Ditto|Ditto, L80|100/100",
    ]);
    expect(mon(projection, "p2:lapras").boosts.atk).toBe(0);
  });
});

describe("typechange y Transform", () => {
  it("un typechange fija los tipos narrados y termina al salir del campo", () => {
    const lines = [SWITCH_OPP, "|-start|p2a: Lapras|typechange|Fire"];
    expect(typesOf(mon(project(lines), "p2:lapras"))).toEqual(["FIRE"]);
    const out = project([...lines, "|switch|p2a: Ditto|Ditto, L80|100/100"]);
    expect(typesOf(mon(out, "p2:lapras"))).toEqual(["WATER", "ICE"]);
  });

  it("un `-end|typechange` también lo revierte", () => {
    const projection = project([
      SWITCH_OPP,
      "|-start|p2a: Lapras|typechange|Fire",
      "|-end|p2a: Lapras|typechange",
    ]);
    expect(typesOf(mon(projection, "p2:lapras"))).toEqual(["WATER", "ICE"]);
  });

  it("Reflect Type copia los tipos del pokémon citado en el `[of]`", () => {
    const projection = project([
      SWITCH_MINE, SWITCH_OPP,
      "|-start|p2a: Lapras|typechange|[from] move: Reflect Type|[of] p1a: Gengar",
    ]);
    expect(typesOf(mon(projection, "p2:lapras"))).toEqual(["GHOST", "POISON"]);
  });

  it("Transform copia tipos del DEX, boosts y moveset, y NO agrega `transform`", () => {
    const projection = project([
      SWITCH_MINE,
      "|-boost|p1a: Gengar|spa|2",
      "|move|p1a: Gengar|Shadow Ball|p2a: Ditto",
      "|switch|p2a: Ditto|Ditto, L80|100/100",
      "|-transform|p2a: Ditto|p1a: Gengar|[from] ability: Imposter",
    ]);
    const ditto = mon(projection, "p2:ditto");
    expect(typesOf(ditto)).toEqual(["GHOST", "POISON"]);
    expect(ditto.boosts.spa).toBe(2);
    // `_transform_moves` TAPA el moveset base: `transform` queda debajo.
    expect([...movesOf(ditto).keys()]).toEqual(["shadowball"]);
    // `min(5, max_pp)` desde gen 5.
    expect(ppOf(projection, "p2:ditto", "shadowball")).toEqual([5, 5]);
    expect(abilityOf(ditto)).toBe("levitate");
  });

  it("el Transform termina al salir del campo", () => {
    const projection = project([
      SWITCH_MINE,
      "|switch|p2a: Ditto|Ditto, L80|100/100",
      "|-transform|p2a: Ditto|p1a: Gengar",
      "|switch|p2a: Lapras|Lapras, L80, F|100/100",
    ]);
    const ditto = mon(projection, "p2:ditto");
    expect(typesOf(ditto)).toEqual(["NORMAL"]);
    expect([...movesOf(ditto).keys()]).toEqual([]);
  });
});

describe("el equipo rival sale del protocolo, no de cualquier ident", () => {
  it("un `-sidestart` nombrado como el JUGADOR no crea un miembro", () => {
    const projection = project([SWITCH_OPP, "|-sidestart|p2: Rival|Spikes"]);
    expect(projection.teamOf("p2").map((state) => state.species)).toEqual(["lapras"]);
  });

  it("ser el OBJETIVO de un movimiento no crea ni revela al objetivo", () => {
    const projection = project([SWITCH_MINE, "|move|p1a: Gengar|Shadow Ball|p2a: Lapras"]);
    expect(projection.teamOf("p2").filter((state) => state.species.length > 0)).toEqual([]);
  });
});
