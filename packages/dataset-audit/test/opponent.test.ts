/** Un fixture positivo y un negativo INDEPENDIENTE por cada una de las 11
 * claves rivales, más las inferencias legítimas que no son fuga. */

import { describe, expect, it } from "vitest";
import { auditDataset } from "../src/invariants.js";
import { OPPONENT_FIELDS, type Dataset, type OpponentField } from "../src/types.js";
import { auditedStep, baseDataset, withOpponentField } from "./fixtures.js";

function fieldsViolated(dataset: Dataset): OpponentField[] {
  return [...new Set(
    auditDataset(dataset).violations
      .filter((violation) => violation.field !== undefined)
      .map((violation) => violation.field as OpponentField),
  )].sort();
}

function faintedWithoutProof(): Dataset {
  const dataset = withOpponentField("fainted", true);
  const audited = auditedStep(dataset).state.opponent!.pokemon![0];
  audited.hp_fraction = 0;
  return dataset;
}

function otherInvariants(dataset: Dataset): string[] {
  return [...new Set(
    auditDataset(dataset).violations
      .filter((violation) => violation.invariant !== "hidden_information")
      .map((violation) => violation.invariant),
  )].sort();
}

/** Cada negativo cambia UNA cosa y tiene que romper UN solo campo. */
const NEGATIVES: Record<OpponentField, () => Dataset> = {
  species: () => withOpponentField("species", "mewtwo"),
  hp_fraction: () => withOpponentField("hp_fraction", 0.5537),
  active: () => withOpponentField("active", "yes"),
  // "el rival está debilitado" es UN cambio semántico: `fainted` y
  // `hp_fraction` se mueven juntos, y con hp=0 el chequeo de HP no se dispara.
  fainted: () => faintedWithoutProof(),
  status: () => withOpponentField("status", "SLP"),
  level: () => withOpponentField("level", 81),
  item: () => withOpponentField("item", "choicescarf"),
  ability: () => withOpponentField("ability", "technician"),
  types: () => withOpponentField("types", ["PSYCHIC", "DARK"]),
  boosts: () => withOpponentField("boosts", {
    accuracy: 0, atk: 0, def: 2, evasion: 0, spa: 0, spd: 0, spe: 0,
  }),
  moves: () => withOpponentField("moves", [{ id: "shadowball", pp: 24, max_pp: 24 }]),
};

describe("los 11 campos observables del rival", () => {
  it("el fixture positivo no viola ninguna invariante", () => {
    const result = auditDataset(baseDataset());
    expect(result.violations).toEqual([]);
    // Canario: si el fixture no ejerce los 11 campos sobre las 3 entradas
    // rivales, "0 violaciones" no significa nada.
    expect(result.stats.opponentEntriesAudited).toBe(3);
    expect(result.stats.opponentFieldChecksRun).toBe(3 * OPPONENT_FIELDS.length);
  });

  it("cubre exactamente las 11 claves que define el serializador", () => {
    expect(Object.keys(NEGATIVES).sort()).toEqual([...OPPONENT_FIELDS].sort());
    expect(OPPONENT_FIELDS).toHaveLength(11);
  });

  for (const field of OPPONENT_FIELDS) {
    it(`detecta un valor de '${field}' sin respaldo público, y sólo ése`, () => {
      const dataset = NEGATIVES[field]();
      if (field === "species") {
        // `species` es la CLAVE de identidad: cambiarla deja sin respaldo a
        // todo lo que se verifica contra esa identidad. La cascada es el
        // comportamiento correcto, así que acá sólo se exige que el campo
        // propio esté entre los reportados.
        expect(fieldsViolated(dataset)).toContain("species");
      } else {
        expect(fieldsViolated(dataset)).toEqual([field]);
      }
      expect(otherInvariants(dataset)).toEqual([]);
    });
  }
});

describe("inferencias legítimas: no son fuga", () => {
  it("acepta la ability que el dex determina de forma única, sin línea que la narre", () => {
    // Furfrou sólo puede tener Fur Coat: decirlo no es información oculta.
    const dataset = baseDataset();
    expect(auditDataset(dataset).violations).toEqual([]);
  });

  it("acepta una forma cosmética ausente del dex local resolviéndola a su base", () => {
    const dataset = baseDataset();
    const benched = auditedStep(dataset).state.opponent!.pokemon![1];
    expect(benched.species).toBe("furfroupharaoh");
    expect(auditDataset(dataset).violations).toEqual([]);
  });

  it("acepta el nivel 100 que Showdown omite del details", () => {
    const dataset = withOpponentField("level", 100);
    expect(fieldsViolated(dataset)).toEqual([]);
  });

  it("acepta `unknown_item` y `null`: son centinelas de poke-env, no una afirmación", () => {
    expect(fieldsViolated(withOpponentField("item", "unknown_item"))).toEqual([]);
    expect(fieldsViolated(withOpponentField("item", null))).toEqual([]);
    expect(fieldsViolated(withOpponentField("ability", null))).toEqual([]);
  });

  it("recorta Hidden Power al id base: Showdown narra sólo 'Hidden Power'", () => {
    // El fixture ya trae `hiddenpowerice` respaldado por `|move|...|Hidden Power|`.
    expect(fieldsViolated(baseDataset())).toEqual([]);
    // Sin el recorte, un Hidden Power de otro tipo tampoco tendría que fallar:
    // el id base es el mismo y es lo único que el protocolo revela.
    const dataset = withOpponentField("moves", [{ id: "hiddenpowerfire", pp: 24, max_pp: null }]);
    expect(fieldsViolated(dataset)).toEqual([]);
  });

  it("rechaza un movimiento que NO comparte el id base con nada narrado", () => {
    const dataset = withOpponentField("moves", [{ id: "hiddenblade", pp: 8, max_pp: 8 }]);
    expect(fieldsViolated(dataset)).toEqual(["moves"]);
  });
});

describe("chequeos estructurales del rival", () => {
  it("rechaza `stats`, que el serializador produce sólo para el lado propio", () => {
    const dataset = withOpponentField("stats", { atk: 200 });
    const violations = auditDataset(dataset).violations;
    expect(violations).toHaveLength(1);
    expect(violations[0].invariant).toBe("hidden_information");
    expect(violations[0].field).toBeUndefined();
    expect(violations[0].detail).toContain("stats");
  });

  it("rechaza un equipo rival de siete", () => {
    const dataset = baseDataset();
    const opponent = auditedStep(dataset).state.opponent!.pokemon!;
    const filler = { ...opponent[1] };
    while (opponent.length <= 6) opponent.push({ ...filler });
    const details = auditDataset(dataset).violations
      .filter((violation) => violation.detail.includes("miembros"));
    expect(details).toHaveLength(1);
    expect(details[0].detail).toContain("7 miembros");
  });

  it("rechaza dos rivales activos a la vez", () => {
    const dataset = baseDataset();
    const opponent = auditedStep(dataset).state.opponent!.pokemon!;
    opponent[1].active = true;
    expect(fieldsViolated(dataset)).toEqual(["active"]);
  });

  it("rechaza un max_pp que no coincide con el dex local", () => {
    // Psychic tiene 10 PP en el dex: `Move.max_pp` es 10*8//5 = 16.
    expect(fieldsViolated(withOpponentField("moves", [
      { id: "psychic", pp: 10, max_pp: 32 },
    ]))).toEqual(["moves"]);
    expect(fieldsViolated(withOpponentField("moves", [
      { id: "psychic", pp: 10, max_pp: 16 },
    ]))).toEqual([]);
  });

  it("rechaza pp por encima de su máximo y acepta pp null (no derivable)", () => {
    expect(fieldsViolated(withOpponentField("moves", [
      { id: "psychic", pp: 17, max_pp: 16 },
    ]))).toEqual(["moves"]);
    expect(fieldsViolated(withOpponentField("moves", [
      { id: "psychic", pp: null, max_pp: null },
    ]))).toEqual([]);
  });

  it("rechaza un boost fuera de [-6,6] y una clave de boost desconocida", () => {
    expect(fieldsViolated(withOpponentField("boosts", {
      accuracy: 0, atk: 7, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    }))).toEqual(["boosts"]);
    expect(fieldsViolated(withOpponentField("boosts", { atk: 1, crit: 1 }))).toEqual(["boosts"]);
  });

  it("rechaza un HP fuera de la grilla de centésimos con HP Percentage Mod activo", () => {
    expect(fieldsViolated(withOpponentField("hp_fraction", 0.55))).toEqual([]);
    expect(fieldsViolated(withOpponentField("hp_fraction", 0.5537))).toEqual(["hp_fraction"]);
  });

  it("no exige la grilla de centésimos si el protocolo no declara la regla", () => {
    const dataset = withOpponentField("hp_fraction", 0.5537);
    const turn0 = dataset.turns.find((turn) => turn.turnNumber === 0)!;
    turn0.protocolLines = turn0.protocolLines.filter(
      (line) => !line.startsWith("|rule|HP Percentage Mod"),
    );
    expect(fieldsViolated(dataset)).toEqual([]);
  });

  it("rechaza fainted=true sin |faint| público y lo acepta con él", () => {
    expect(fieldsViolated(faintedWithoutProof())).toEqual(["fainted"]);
    const dataset = faintedWithoutProof();
    auditedStep(dataset).state.opponent!.pokemon![0].status = "FNT";
    dataset.turns[1].protocolLines.push("|faint|p2a: Mimien");
    expect(fieldsViolated(dataset)).toEqual([]);
  });

  it("exige que el boost sea del MISMO stat que narró el protocolo", () => {
    // El fixture sólo trae `|-boost|p2a: Mimien|atk|1`.
    expect(fieldsViolated(withOpponentField("boosts", {
      accuracy: 0, atk: 1, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    }))).toEqual([]);
    expect(fieldsViolated(withOpponentField("boosts", {
      accuracy: 0, atk: 0, def: 2, evasion: 0, spa: 0, spd: 0, spe: 0,
    }))).toEqual(["boosts"]);
  });

  it("acepta cualquier stat después de un boost que el protocolo no desglosa", () => {
    const dataset = withOpponentField("boosts", {
      accuracy: 0, atk: 0, def: -1, evasion: 0, spa: 0, spd: 0, spe: 0,
    });
    expect(fieldsViolated(dataset)).toEqual(["boosts"]);
    dataset.turns[1].protocolLines.push("|-invertboost|p2a: Mimien");
    expect(fieldsViolated(dataset)).toEqual([]);
  });
});
