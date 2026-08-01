import { describe, expect, it } from "vitest";
import { auditDataset } from "../src/invariants.js";
import {
  buildProtocolIndex,
  buildSpeciesIndex,
  identityKeys,
  moveEvidenceKeys,
  normalizeProtocolText,
  opponentSideOf,
  parseDetails,
  parseIdent,
  revealedBy,
} from "../src/protocol.js";
import type { BattleTurnRecord, Dataset } from "../src/types.js";
import { auditedStep, baseDataset, dexPokemon } from "./fixtures.js";

const speciesIndex = buildSpeciesIndex(dexPokemon());
const resolve = speciesIndex.resolve;

function indexOf(lines: string[]): ReturnType<typeof buildProtocolIndex> {
  const turns: BattleTurnRecord[] = [
    { battleId: 1, playerSide: "p1", turnNumber: 0, protocolLines: lines },
  ];
  return buildProtocolIndex(turns, speciesIndex);
}

describe("normalización", () => {
  it("elimina toda la puntuación y los diacríticos, no sólo espacios y guiones", () => {
    expect(normalizeProtocolText("Mr. Mime")).toBe("mrmime");
    expect(normalizeProtocolText("Farfetch'd")).toBe("farfetchd");
    expect(normalizeProtocolText("Flabébé")).toBe("flabebe");
    expect(normalizeProtocolText("Charizard-Mega-X")).toBe("charizardmegax");
  });
});

describe("parseo de ident y details", () => {
  it("separa lado y nombre del ident", () => {
    expect(parseIdent("p2a: Mr. Mime")).toEqual({ side: "p2", name: "mrmime" });
    expect(parseIdent("p1b: Gengar")).toEqual({ side: "p1", name: "gengar" });
    expect(parseIdent("turn 3")).toBeUndefined();
    expect(parseIdent(undefined)).toBeUndefined();
  });

  it("extrae especie y nivel del details, y acepta que Showdown omita L100", () => {
    expect(parseDetails("Yanmega, L82, F")).toEqual({ species: "yanmega", level: 82 });
    // Showdown OMITE el nivel cuando es 100, y el recorder lo interpreta así
    // (`_level_from_details`). Devolver 100 explícito es lo que permite exigir
    // el nivel sin dejar un comodín que un valor falso pueda aprovechar.
    expect(parseDetails("Mew")).toEqual({ species: "mew", level: 100 });
  });

  it("el lado observado es siempre el otro", () => {
    expect(opponentSideOf("p1")).toBe("p2");
    expect(opponentSideOf("p2")).toBe("p1");
  });
});

describe("identidad canónica", () => {
  it("resuelve una forma alternativa a su especie base", () => {
    expect(identityKeys("charizardmegax", resolve)).toEqual(["charizardmegax", "charizard"]);
    expect(identityKeys("gengar", resolve)).toEqual(["gengar"]);
  });

  it("resuelve una forma COSMÉTICA ausente del dex local por su prefijo más largo", () => {
    // `Furfrou-Pharaoh` comparte la entrada de Furfrou en el dex de Showdown y
    // la tabla `pokemon` no la trae.
    expect(resolve("furfroupharaoh")?.showdownId).toBe("furfrou");
    expect(identityKeys("furfroupharaoh", resolve)).toEqual(["furfroupharaoh", "furfrou"]);
  });

  it("no inventa una base cuando el dex no conoce ni el prefijo", () => {
    expect(resolve("pokemoninexistente")).toBeUndefined();
    expect(identityKeys("pokemoninexistente", resolve)).toEqual(["pokemoninexistente"]);
  });
});

describe("Hidden Power", () => {
  it("agrega el id base sólo para Hidden Power, no como regla genérica de prefijos", () => {
    expect(moveEvidenceKeys("hiddenpowerice")).toEqual(["hiddenpowerice", "hiddenpower"]);
    expect(moveEvidenceKeys("hiddenpower")).toEqual(["hiddenpower"]);
    expect(moveEvidenceKeys("shadowball")).toEqual(["shadowball"]);
    expect(moveEvidenceKeys("solarbeam")).toEqual(["solarbeam"]);
  });
});

describe("evidencia por TOKEN, no por substring de la línea", () => {
  it("ser el OBJETIVO de un movimiento no revela la especie del objetivo", () => {
    const index = indexOf([
      "|switch|p1a: Gengar|Gengar, L80, M|280/280",
      "|move|p1a: Gengar|Shadow Ball|p2a: Mr. Mime",
    ]);
    const evidence = index.get(1, "p1", "p2")!.evidence;
    // La línea NOMBRA a Mr. Mime, pero como objetivo: no es un `|switch|`.
    expect(revealedBy(evidence.species, ["mrmime"], 0)).toBe(false);
    expect(revealedBy(evidence.move, ["mrmime|shadowball"], 0)).toBe(false);
  });

  it("un `|switch|` sí lo revela", () => {
    const index = indexOf(["|switch|p2a: Mimien|Mr. Mime, L82, M|100/100"]);
    expect(revealedBy(index.get(1, "p1", "p2")!.evidence.species, ["mrmime"], 0)).toBe(true);
  });
});

describe("pertenencia de item y ability por sufijo", () => {
  function abilityOf(lines: string[], key: string, side = "p2"): boolean {
    return revealedBy(indexOf(lines).get(1, "p1", side)!.evidence.ability, [key], 0);
  }
  function itemOf(lines: string[], key: string, side = "p2"): boolean {
    return revealedBy(indexOf(lines).get(1, "p1", side)!.evidence.item, [key], 0);
  }

  const SWITCH_OPP = "|switch|p2a: Lapras|Lapras, L80, F|100/100";
  const SWITCH_MINE = "|switch|p1a: Gengar|Gengar, L80, M|280/280";

  it("en `-heal` el `[of]` es engañoso: la ability es del ident", () => {
    const lines = [
      SWITCH_OPP, SWITCH_MINE,
      "|-heal|p2a: Lapras|100/100|[from] ability: Water Absorb|[of] p1a: Gengar",
    ];
    expect(abilityOf(lines, "lapras|waterabsorb")).toBe(true);
    expect(abilityOf(lines, "gengar|waterabsorb", "p1")).toBe(false);
  });

  it("en `-damage` el `[of]` SÍ es el dueño (Rocky Helmet, Rough Skin)", () => {
    const lines = [
      SWITCH_OPP, SWITCH_MINE,
      "|-damage|p1a: Gengar|200/280|[from] item: Rocky Helmet|[of] p2a: Lapras",
    ];
    expect(itemOf(lines, "lapras|rockyhelmet")).toBe(true);
    expect(itemOf(lines, "gengar|rockyhelmet", "p1")).toBe(false);
  });

  it("en `-ability` el `[of]` es el pokémon TRAZADO, y la copia es suya", () => {
    const lines = [
      SWITCH_OPP, SWITCH_MINE,
      "|-ability|p1a: Gengar|Water Absorb|[from] ability: Trace|[of] p2a: Lapras",
    ];
    // Gengar tiene Trace; Lapras tiene la ability copiada.
    expect(abilityOf(lines, "gengar|trace", "p1")).toBe(true);
    expect(abilityOf(lines, "lapras|waterabsorb")).toBe(true);
    expect(abilityOf(lines, "lapras|trace")).toBe(false);
  });

  it("Frisk es del `[of]`; Pickpocket y Magician son del ident", () => {
    const frisk = [
      SWITCH_OPP, SWITCH_MINE,
      "|-item|p1a: Gengar|Life Orb|[from] ability: Frisk|[of] p2a: Lapras",
    ];
    expect(abilityOf(frisk, "lapras|frisk")).toBe(true);

    const pickpocket = [
      SWITCH_OPP, SWITCH_MINE,
      "|-item|p2a: Lapras|Life Orb|[from] ability: Pickpocket|[of] p1a: Gengar",
    ];
    expect(abilityOf(pickpocket, "lapras|pickpocket")).toBe(true);
    expect(abilityOf(pickpocket, "gengar|pickpocket", "p1")).toBe(false);
  });

  it("un Trick revela el item para los DOS socios del intercambio", () => {
    const lines = [
      SWITCH_OPP, SWITCH_MINE,
      "|move|p1a: Gengar|Trick|p2a: Lapras",
      "|-activate|p1a: Gengar|move: Trick|[of] p2a: Lapras",
      "|-item|p2a: Lapras|Choice Specs|[from] move: Trick",
      "|-item|p1a: Gengar|Leftovers|[from] move: Trick",
    ];
    // Lapras recibió Choice Specs y ENTREGÓ Leftovers: las dos cosas son
    // públicas, y sin la segunda el auditor reportaría una fuga falsa.
    expect(itemOf(lines, "lapras|choicespecs")).toBe(true);
    expect(itemOf(lines, "lapras|leftovers")).toBe(true);
  });
});

describe("el índice se arma línea por línea, nunca sobre el protocolo concatenado", () => {
  it("un nombre partido entre dos líneas no cuenta como revelado", () => {
    const index = indexOf([
      "|switch|p2a: Char|Charizard, L80, M|100/100",
      "|-message|izard-Mega-X",
    ]);
    const evidence = index.get(1, "p1", "p2")!.evidence;
    expect(revealedBy(evidence.species, ["charizard"], 0)).toBe(true);
    expect(revealedBy(evidence.species, ["charizardmegax"], 0)).toBe(false);
  });
});

describe("Transform: inferencia legítima, no fuga", () => {
  function withTransform(): Dataset {
    const dataset = baseDataset();
    const audited = auditedStep(dataset).state.opponent!.pokemon![0];
    // Moveset copiado de NUESTRO pokémon: nada de esto sale del rival.
    audited.moves = [{ id: "shadowball", pp: 24, max_pp: 24 }];
    audited.types = ["GHOST", "POISON"];
    return dataset;
  }

  it("sin la línea de Transform, el moveset copiado es una violación", () => {
    const fields = auditDataset(withTransform()).violations.map((v) => v.field);
    expect(fields).toContain("moves");
    expect(fields).toContain("types");
  });

  it("con `|-transform|` el moveset y los tipos copiados se aceptan", () => {
    const dataset = withTransform();
    dataset.turns[1].protocolLines.push(
      "|-transform|p2a: Mimien|p1a: Gengar|[from] ability: Imposter",
    );
    // Con el Transform vigente, el moveset copiado topea su PP en 5.
    auditedStep(dataset).state.opponent!.pokemon![0].moves = [
      { id: "shadowball", pp: 5, max_pp: 5 },
    ];
    auditedStep(dataset).state.opponent!.pokemon![0].ability = "levitate";
    expect(auditDataset(dataset).violations).toEqual([]);
  });
});
