/** Un fixture positivo y un negativo INDEPENDIENTE por cada una de las 11
 * claves rivales, más las inferencias legítimas que no son fuga. */

import { describe, expect, it } from "vitest";
import { auditDataset } from "../src/invariants.js";
import { OPPONENT_FIELDS, type Dataset, type OpponentField } from "../src/types.js";
import { auditedStep, baseDataset, faintedFixture, withOpponentField } from "./fixtures.js";

function fieldsViolated(dataset: Dataset): OpponentField[] {
  return [...new Set(
    auditDataset(dataset).violations
      .filter((violation) => violation.field !== undefined)
      .map((violation) => violation.field as OpponentField),
  )].sort();
}

/** El negativo de `fainted` es el del review: un `|faint|` público y una fila
 * que insiste en que el pokémon sigue en pie. Aísla el campo sin arrastrar a
 * `hp_fraction`, que en ese fixture ya vale 0 como corresponde. */
function faintedWithoutProof(): Dataset {
  return faintedFixture(false);
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

  it("`unknown_item` es el centinela de 'nadie lo reveló', no un comodín", () => {
    // El rival del banco nunca reveló su item y lo declara con el centinela.
    expect(fieldsViolated(baseDataset())).toEqual([]);
    // Pero el auditado SÍ reveló Leftovers: volver al centinela, o a `null`,
    // contradice el valor vigente igual que inventar otro item.
    expect(fieldsViolated(withOpponentField("item", "unknown_item"))).toEqual(["item"]);
    expect(fieldsViolated(withOpponentField("item", null))).toEqual(["item"]);
    expect(fieldsViolated(withOpponentField("ability", null))).toEqual(["ability"]);
  });

  it("recorta Hidden Power al id base: Showdown narra sólo 'Hidden Power'", () => {
    // `Move.retrieve_id` colapsa los 17 Hidden Power en uno: el id que el
    // protocolo revela es el mismo para todos.
    expect(fieldsViolated(baseDataset())).toEqual([]);
    const dataset = withOpponentField("moves", [
      { id: "psychic", pp: 15, max_pp: 16 },
      { id: "hiddenpowerfire", pp: 23, max_pp: 24 },
    ]);
    expect(fieldsViolated(dataset)).toEqual([]);
  });

  it("rechaza un movimiento que NO comparte el id base con nada narrado", () => {
    const dataset = withOpponentField("moves", [{ id: "hiddenblade", pp: 8, max_pp: 8 }]);
    expect(fieldsViolated(dataset)).toEqual(["moves"]);
  });

  it("acepta un movimiento con pp/max_pp en null sólo si el dex no lo trae", () => {
    // `psychic` sí está en el dex: su PP es derivable y exacto.
    expect(fieldsViolated(withOpponentField("moves", [
      { id: "psychic", pp: null, max_pp: null },
      { id: "hiddenpower", pp: 23, max_pp: 24 },
    ]))).toEqual(["moves"]);
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
      { id: "hiddenpower", pp: 23, max_pp: 24 },
    ]))).toEqual(["moves"]);
  });

  it("el PP es EXACTO: un uso narrado es exactamente un PP menos", () => {
    const base = [{ id: "hiddenpower", pp: 23, max_pp: 24 }];
    expect(fieldsViolated(withOpponentField("moves", [
      { id: "psychic", pp: 15, max_pp: 16 }, ...base,
    ]))).toEqual([]);
    // Ni de más ni de menos: el piso permisivo dejaba pasar cualquier valor
    // por debajo.
    for (const pp of [16, 14, 1]) {
      expect(fieldsViolated(withOpponentField("moves", [
        { id: "psychic", pp, max_pp: 16 }, ...base,
      ]))).toEqual(["moves"]);
    }
  });

  it("rechaza un boost fuera de [-6,6] y una clave de boost desconocida", () => {
    expect(fieldsViolated(withOpponentField("boosts", {
      accuracy: 0, atk: 7, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    }))).toEqual(["boosts"]);
    expect(fieldsViolated(withOpponentField("boosts", { atk: 1, crit: 1 }))).toEqual(["boosts"]);
  });

  it("el HP se compara contra el valor narrado, no contra una grilla", () => {
    expect(fieldsViolated(withOpponentField("hp_fraction", 0.55))).toEqual([]);
    // 0.5537 es un centésimo inventado y 0.54 es un centésimo perfectamente
    // formado que el protocolo tampoco narró: los dos son igual de falsos.
    expect(fieldsViolated(withOpponentField("hp_fraction", 0.5537))).toEqual(["hp_fraction"]);
    expect(fieldsViolated(withOpponentField("hp_fraction", 0.54))).toEqual(["hp_fraction"]);
  });

  it("exige que fainted coincida con el |faint| público, en los dos sentidos", () => {
    expect(fieldsViolated(faintedFixture(false))).toEqual(["fainted"]);
    expect(fieldsViolated(faintedFixture(true))).toEqual([]);
    // Y sin ningún `|faint|`, declararse debilitado tampoco pasa.
    expect(fieldsViolated(withOpponentField("fainted", true))).toContain("fainted");
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

  it("proyecta los boosts que el protocolo no desglosa por stat", () => {
    // `|-invertboost|` invierte el +1 atk público: el valor esperado pasa a
    // ser -1, y sólo ése.
    const dataset = withOpponentField("boosts", {
      accuracy: 0, atk: -1, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    });
    expect(fieldsViolated(dataset)).toEqual(["boosts"]);
    dataset.turns[1].protocolLines.push("|-invertboost|p2a: Mimien");
    expect(fieldsViolated(dataset)).toEqual([]);
  });

  it("`-copyboost` escribe en el OBJETIVO, y la fuente no cambia", () => {
    // `|-copyboost|FUENTE|OBJETIVO` (`abstract_battle.py:912-914`). Con Mimien
    // como FUENTE, sus boosts siguen siendo los suyos.
    const source = withOpponentField("boosts", {
      accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    });
    source.turns[1].protocolLines.push("|-copyboost|p2a: Mimien|p1a: Gengar");
    expect(fieldsViolated(source)).toEqual(["boosts"]);

    // Como OBJETIVO sí cambia: copia los de nuestro Gengar, que están en cero.
    const target = withOpponentField("boosts", {
      accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    });
    target.turns[1].protocolLines.push("|-copyboost|p1a: Gengar|p2a: Mimien");
    expect(fieldsViolated(target)).toEqual([]);
  });
});
