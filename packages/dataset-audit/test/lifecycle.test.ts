/** Las mutaciones que el review del Tech Lead demostró que pasaban en silencio.
 *
 * Cada test lleva el NOMBRE EXACTO de la mutación reportada, para que el
 * mapeo entre el hallazgo y su canario sea uno a uno y verificable sin leer
 * código. Todas parten del fixture positivo y cambian una sola cosa.
 *
 * La causa común de las seis primeras era que el auditor sólo probaba
 * "revelado alguna vez": el dato existía en el protocolo, así que un valor
 * FALSO del mismo campo pasaba. Las dos siguientes eran excepciones abiertas
 * —`typechange` y `Transform`— que excusaban campos que no les corresponden.
 * Las tres últimas eran inferencias demasiado permisivas.
 */

import { describe, expect, it } from "vitest";
import { auditDataset } from "../src/invariants.js";
import type { Dataset, OpponentField } from "../src/types.js";
import {
  auditedOpponentOf,
  baseDataset,
  faintedFixture,
  withProtocolLine,
} from "./fixtures.js";

function fields(dataset: Dataset): OpponentField[] {
  return [...new Set(
    auditDataset(dataset).violations
      .filter((violation) => violation.field !== undefined)
      .map((violation) => violation.field as OpponentField),
  )].sort();
}

// ---------------------------------------------------------------------------
// Finding 1 — "revelado alguna vez" no demuestra el valor actual
// ---------------------------------------------------------------------------

describe("Finding 1 · el valor del turno, no sólo que el dato fue público", () => {
  it("wrong_hp_42_vs_public_55", () => {
    // El protocolo narra 100/100 y 55/100 para Mr. Mime. 42 no existe.
    const dataset = baseDataset();
    auditedOpponentOf(dataset).hp_fraction = 0.42;
    expect(fields(dataset)).toEqual(["hp_fraction"]);
  });

  it("wrong_active_identity", () => {
    // El protocolo tiene a Mr. Mime en el campo; la fila declara activa a la
    // que está en el banco.
    const dataset = baseDataset();
    const opponent = dataset.steps[1].state.opponent!.pokemon!;
    opponent[0].active = false;
    opponent[1].active = true;
    expect(fields(dataset)).toEqual(["active"]);
  });

  it("missing_public_status", () => {
    // `|-status|p2a: Mimien|par` es público y la fila dice que no tiene nada.
    const dataset = baseDataset();
    auditedOpponentOf(dataset).status = null;
    expect(fields(dataset)).toEqual(["status"]);
  });

  it("wrong_boost_magnitude_2_vs_public_1", () => {
    // El protocolo narra `|-boost|p2a: Mimien|atk|1`, no 2.
    const dataset = baseDataset();
    auditedOpponentOf(dataset).boosts = {
      accuracy: 0, atk: 2, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    };
    expect(fields(dataset)).toEqual(["boosts"]);
  });

  it("arbitrary_pp_1", () => {
    // Psychic tiene 16 PP y se narró UN uso: el piso derivable es 14.
    const dataset = baseDataset();
    auditedOpponentOf(dataset).moves = [{ id: "psychic", pp: 1, max_pp: 16 }];
    expect(fields(dataset)).toEqual(["moves"]);
  });

  it("fainted_false_after_public_faint", () => {
    // `|faint|p2a: Mimien` es público y la fila dice que sigue en pie.
    expect(fields(faintedFixture(false))).toEqual(["fainted"]);
    // El positivo del mismo fixture no viola nada.
    expect(fields(faintedFixture(true))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Finding 2 — typechange / Transform no pueden excusar cualquier cosa
// ---------------------------------------------------------------------------

describe("Finding 2 · las excepciones no habilitan fuga arbitraria", () => {
  it("typechange_excuses_hidden_ability", () => {
    // Un cambio de TIPOS no revela ni cambia una ability. `technician` es una
    // ability real de Mr. Mime pero el protocolo nunca la narró y el dex no la
    // determina (Mr. Mime tiene tres posibles).
    const dataset = withProtocolLine(baseDataset(), "|-start|p2a: Mimien|typechange|Water");
    auditedOpponentOf(dataset).ability = "technician";
    expect(fields(dataset)).toEqual(["ability"]);
  });

  it("typechange sigue excusando los TIPOS, que es lo suyo", () => {
    const dataset = withProtocolLine(baseDataset(), "|-start|p2a: Mimien|typechange|Water");
    auditedOpponentOf(dataset).types = ["WATER"];
    expect(fields(dataset)).toEqual([]);
  });

  it("transform_excuses_arbitrary_hidden_data", () => {
    // Transform copia a NUESTRO Gengar. Nada de esto salió de ahí.
    const dataset = withProtocolLine(baseDataset(), "|-transform|p2a: Mimien|p1a: Gengar");
    const audited = auditedOpponentOf(dataset);
    audited.ability = "hugepower";
    audited.types = ["DARK"];
    audited.moves = [{ id: "spore", pp: 5, max_pp: 5 }];
    expect(fields(dataset)).toEqual(["ability", "moves", "types"]);
  });

  it("Transform acepta exactamente lo que copió, con el PP topeado en 5", () => {
    // `_transformed_move`: `min(5, max_pp)` desde gen 5.
    const dataset = withProtocolLine(baseDataset(), "|-transform|p2a: Mimien|p1a: Gengar");
    const audited = auditedOpponentOf(dataset);
    audited.ability = "levitate";
    audited.types = ["GHOST", "POISON"];
    audited.moves = [{ id: "shadowball", pp: 5, max_pp: 5 }];
    expect(fields(dataset)).toEqual([]);
  });

  it("Transform acepta Imposter, que es la ability del que se transforma", () => {
    const dataset = withProtocolLine(baseDataset(), "|-transform|p2a: Mimien|p1a: Gengar");
    const audited = auditedOpponentOf(dataset);
    audited.ability = "imposter";
    audited.types = ["GHOST", "POISON"];
    audited.moves = [{ id: "transform", pp: null, max_pp: null }];
    expect(fields(dataset)).toEqual([]);
  });

  it("un Transform sin objetivo resoluble NO excusa nada", () => {
    // Si no se puede decir qué se copió, aceptar cualquier valor sería la fuga
    // que este chequeo existe para impedir.
    const dataset = withProtocolLine(baseDataset(), "|-transform|p2a: Mimien|p1a: Fantasma");
    const audited = auditedOpponentOf(dataset);
    audited.ability = "hugepower";
    audited.moves = [{ id: "spore", pp: 5, max_pp: 5 }];
    expect(fields(dataset)).toEqual(["ability", "moves"]);
  });

  it("el Transform TERMINA cuando el pokémon sale del campo", () => {
    const dataset = withProtocolLine(baseDataset(), "|-transform|p2a: Mimien|p1a: Gengar");
    withProtocolLine(dataset, "|switch|p2a: Furfrou|Furfrou-Pharaoh, L85, F|100/100");
    const audited = auditedOpponentOf(dataset);
    audited.active = false;
    // Salir del campo también limpia los boosts (`Pokemon.switch_out`).
    audited.boosts = { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 };
    audited.ability = "levitate";
    audited.moves = [{ id: "shadowball", pp: 5, max_pp: 5 }];
    // Ya no hay Transform vigente: lo copiado deja de estar excusado.
    expect(fields(dataset)).toEqual(["ability", "moves"]);
  });
});

// ---------------------------------------------------------------------------
// Finding 3 — inferencias permisivas
// ---------------------------------------------------------------------------

describe("Finding 3 · las inferencias legítimas no tapan corrupción", () => {
  it("explicit_L82_but_state_L100", () => {
    // Showdown omite `L100`, así que 100 sólo es válido cuando el details NO
    // trae nivel. Acá trae `L82`.
    const dataset = baseDataset();
    auditedOpponentOf(dataset).level = 100;
    expect(fields(dataset)).toEqual(["level"]);
  });

  it("un details sin nivel sí significa 100", () => {
    const dataset = baseDataset();
    dataset.turns[1].protocolLines[1] = "|switch|p2a: Mimien|Mr. Mime|100/100";
    auditedOpponentOf(dataset).level = 100;
    expect(fields(dataset)).toEqual([]);
  });

  it("fabricated_suffix_species", () => {
    // `furfroubanana` resuelve por prefijo a Furfrou, pero el protocolo nunca
    // lo narró y el dex no lo conoce: es una especie inventada.
    const dataset = baseDataset();
    dataset.steps[1].state.opponent!.pokemon![1].species = "furfroubanana";
    expect(fields(dataset)).toContain("species");
  });

  it("una forma cosmética REAL, narrada verbatim, sigue pasando", () => {
    expect(fields(baseDataset())).toEqual([]);
  });

  it("empty_boost_shape", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).boosts = {};
    expect(fields(dataset)).toEqual(["boosts"]);
  });

  it("rechaza también un boost al que le falta un solo stat", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).boosts = {
      accuracy: 0, atk: 1, def: 0, evasion: 0, spa: 0, spd: 0,
    };
    expect(fields(dataset)).toEqual(["boosts"]);
  });

  it("rechaza un movimiento sin la forma completa que promete el serializer", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).moves = [{ id: "psychic" }];
    expect(fields(dataset)).toEqual(["moves"]);
  });
});

// ---------------------------------------------------------------------------
// Canario de cobertura de este archivo
// ---------------------------------------------------------------------------

describe("canario · las mutaciones del review están todas cubiertas", () => {
  const REPORTED = [
    "wrong_hp_42_vs_public_55",
    "wrong_active_identity",
    "missing_public_status",
    "wrong_boost_magnitude_2_vs_public_1",
    "arbitrary_pp_1",
    "fainted_false_after_public_faint",
    "typechange_excuses_hidden_ability",
    "transform_excuses_arbitrary_hidden_data",
    "explicit_L82_but_state_L100",
    "fabricated_suffix_species",
    "empty_boost_shape",
  ];

  it("hay un test con el nombre exacto de cada mutación reportada", async () => {
    const source = await import("node:fs/promises")
      .then((fs) => fs.readFile(new URL("./lifecycle.test.ts", import.meta.url), "utf8"));
    for (const mutation of REPORTED) {
      expect(source, `falta el canario de ${mutation}`).toContain(`it("${mutation}"`);
    }
    expect(REPORTED).toHaveLength(11);
  });
});
