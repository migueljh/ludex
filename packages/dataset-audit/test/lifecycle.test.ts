/** Las mutaciones que los reviews del Tech Lead demostraron que pasaban.
 *
 * Cada test lleva el NOMBRE EXACTO de la reproducción reportada, para que el
 * mapeo entre el hallazgo y su canario sea uno a uno y verificable sin leer
 * código. Cada negativo va con su POSITIVO contrapuesto: un auditor que
 * rechaza todo tampoco sirve, y sin el par la única forma de "arreglar" un
 * canario sería endurecer el campo hasta que no pase nada.
 *
 * La primera ronda entendió mal la causa: cambió "revelado alguna vez" por
 * "revelado con este valor en algún momento de la ventana", que sigue siendo
 * una pregunta POR CAMPO. Con eso, un HP del turno 3, un status del turno 4 y
 * unos boosts del turno 5 pasaban juntos aunque nunca hubieran coexistido. La
 * corrección es que la fila entera tiene que corresponder a UN instante.
 */

import { describe, expect, it } from "vitest";
import { auditDataset } from "../src/invariants.js";
import type { Dataset, OpponentField } from "../src/types.js";
import {
  auditedOpponentOf,
  auditedStep,
  baseDataset,
  faintedFixture,
  freshOpponent,
  soloFixture,
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
// Ronda 1 — "revelado alguna vez" no demuestra el valor actual
// ---------------------------------------------------------------------------

describe("Ronda 1 · el valor del turno, no sólo que el dato fue público", () => {
  it("wrong_hp_42_vs_public_55", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).hp_fraction = 0.42;
    expect(fields(dataset)).toEqual(["hp_fraction"]);
  });

  it("wrong_active_identity", () => {
    const dataset = baseDataset();
    const opponent = dataset.steps[1].state.opponent!.pokemon!;
    opponent[0].active = false;
    opponent[1].active = true;
    expect(fields(dataset)).toEqual(["active"]);
  });

  it("missing_public_status", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).status = null;
    expect(fields(dataset)).toEqual(["status"]);
  });

  it("wrong_boost_magnitude_2_vs_public_1", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).boosts = {
      accuracy: 0, atk: 2, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    };
    expect(fields(dataset)).toEqual(["boosts"]);
  });

  it("arbitrary_pp_1", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).moves = [{ id: "psychic", pp: 1, max_pp: 16 }];
    expect(fields(dataset)).toEqual(["moves"]);
  });

  it("fainted_false_after_public_faint", () => {
    expect(fields(faintedFixture(false))).toEqual(["fainted"]);
    expect(fields(faintedFixture(true))).toEqual([]);
  });

  it("typechange_excuses_hidden_ability", () => {
    const dataset = withProtocolLine(baseDataset(), "|-start|p2a: Mimien|typechange|Water");
    const audited = auditedOpponentOf(dataset);
    audited.types = ["WATER"];
    audited.ability = "technician";
    expect(fields(dataset)).toEqual(["ability"]);
  });

  it("transform_excuses_arbitrary_hidden_data", () => {
    const dataset = transformed();
    const audited = auditedOpponentOf(dataset);
    audited.ability = "hugepower";
    audited.types = ["DARK"];
    audited.moves = [{ id: "spore", pp: 5, max_pp: 5 }];
    expect(fields(dataset)).toEqual(["ability", "moves", "types"]);
  });

  it("explicit_L82_but_state_L100", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).level = 100;
    expect(fields(dataset)).toEqual(["level"]);
  });

  it("fabricated_suffix_species", () => {
    const dataset = baseDataset();
    dataset.steps[1].state.opponent!.pokemon![1].species = "furfroubanana";
    expect(fields(dataset)).toContain("species");
  });

  it("empty_boost_shape", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).boosts = {};
    expect(fields(dataset)).toEqual(["boosts"]);
  });

  it("el fixture positivo del que salen todas sigue sin violar nada", () => {
    expect(fields(baseDataset())).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Ronda 2 — la fila tiene que describir UN instante, no un collage
// ---------------------------------------------------------------------------

/** El rival auditado, transformado en NUESTRO Gengar. Lo copiado queda listo:
 * lo que cambie el test es la mutación, no el andamiaje. */
function transformed(imposter = true): Dataset {
  const dataset = withProtocolLine(
    baseDataset(),
    `|-transform|p2a: Mimien|p1a: Gengar${imposter ? "|[from] ability: Imposter" : ""}`,
  );
  const audited = auditedOpponentOf(dataset);
  audited.ability = "levitate";
  audited.types = ["GHOST", "POISON"];
  audited.moves = [{ id: "shadowball", pp: 5, max_pp: 5 }];
  // `transform()` copia los boosts del objetivo: el +1 atk propio se pierde.
  audited.boosts = { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 };
  return dataset;
}

describe("Ronda 2 · un único cursor coherente para toda la fila", () => {
  it("all_opponents_active_false", () => {
    // Con `active` validado en un solo sentido, un equipo entero en `false`
    // no contradecía nada: nadie afirmaba estar donde no estaba.
    const dataset = baseDataset();
    for (const entry of dataset.steps[1].state.opponent!.pokemon!) entry.active = false;
    expect(fields(dataset)).toEqual(["active"]);
  });

  it("typechange_water_but_state_dragon", () => {
    const dataset = withProtocolLine(baseDataset(), "|-start|p2a: Mimien|typechange|Water");
    auditedOpponentOf(dataset).types = ["DRAGON"];
    expect(fields(dataset)).toEqual(["types"]);
    // Positivo: los tipos NARRADOS sí se aceptan.
    const positive = withProtocolLine(baseDataset(), "|-start|p2a: Mimien|typechange|Water");
    auditedOpponentOf(positive).types = ["WATER"];
    expect(fields(positive)).toEqual([]);
  });

  it("soundproof_kept_after_skill_swap", () => {
    const line = "|-ability|p2a: Mimien|Technician|[from] move: Skill Swap";
    const dataset = withProtocolLine(baseDataset(), line);
    expect(auditedOpponentOf(dataset).ability).toBe("soundproof");
    expect(fields(dataset)).toEqual(["ability"]);
    // Positivo: la ability VIGENTE es la que el Skill Swap dejó.
    const positive = withProtocolLine(baseDataset(), line);
    auditedOpponentOf(positive).ability = "technician";
    expect(fields(positive)).toEqual([]);
  });

  it("leftovers_kept_after_enditem", () => {
    const line = "|-enditem|p2a: Mimien|Leftovers";
    const dataset = withProtocolLine(baseDataset(), line);
    expect(auditedOpponentOf(dataset).item).toBe("leftovers");
    expect(fields(dataset)).toEqual(["item"]);
    // Positivo: `end_item` deja `None`, que NO es el centinela.
    const positive = withProtocolLine(baseDataset(), line);
    auditedOpponentOf(positive).item = null;
    expect(fields(positive)).toEqual([]);
  });

  it("charizard_base_claims_mega_x_types_and_ability", () => {
    const SWITCH = "|switch|p2a: Charizard|Charizard, L79, M|100/100";
    const MEGA = "|detailschange|p2a: Charizard|Charizard-Mega-X, L79, M";
    const mega = (): Record<string, unknown> => ({
      // `forme_change` usa `store_species=False`: la especie sigue siendo la base.
      ...freshOpponent("charizard", 79, ["FIRE", "DRAGON"]),
      ability: "toughclaws",
    });
    // Antes de la evidencia pública, los tipos y la ability de la Mega son fuga.
    expect(fields(soloFixture([SWITCH], mega()))).toEqual(["ability", "types"]);
    // Después del `detailschange`, son el estado vigente.
    expect(fields(soloFixture([SWITCH, MEGA], mega()))).toEqual([]);
    // Y entonces son los de la forma BASE los que ya no corresponden: la Mega
    // impone SU ability, así que un `ability: null` tampoco pasa.
    expect(fields(soloFixture([SWITCH, MEGA], freshOpponent("charizard", 79, ["FIRE", "FLYING"]))))
      .toEqual(["ability", "types"]);
    // Positivo simétrico: antes de la Mega, la forma base es correcta.
    expect(fields(soloFixture([SWITCH], freshOpponent("charizard", 79, ["FIRE", "FLYING"]))))
      .toEqual([]);
  });

  it("spore_attributed_from_magic_bounce", () => {
    const line = "|move|p2a: Mimien|Spore|p1a: Gengar|[from] ability: Magic Bounce";
    const dataset = withProtocolLine(baseDataset(), line);
    const audited = auditedOpponentOf(dataset);
    // La línea SÍ revela la ability del que rebota; lo que no revela es el
    // movimiento rebotado, que es del atacante.
    audited.ability = "magicbounce";
    audited.moves = [
      { id: "psychic", pp: 15, max_pp: 16 },
      { id: "hiddenpower", pp: 23, max_pp: 24 },
      { id: "spore", pp: 24, max_pp: 24 },
    ];
    expect(fields(dataset)).toEqual(["moves"]);
    // Positivo: sin el movimiento rebotado, la misma fila pasa.
    const positive = withProtocolLine(baseDataset(), line);
    auditedOpponentOf(positive).ability = "magicbounce";
    expect(fields(positive)).toEqual([]);
  });

  it("transform_moveset_plus_transform", () => {
    // `_transform_moves` TAPA el moveset base: el `transform` que `_add_move`
    // agregó por Imposter queda debajo y no se serializa.
    const dataset = transformed();
    auditedOpponentOf(dataset).moves = [
      { id: "shadowball", pp: 5, max_pp: 5 },
      { id: "transform", pp: 16, max_pp: 16 },
    ];
    expect(fields(dataset)).toEqual(["moves"]);
    // Positivo: exactamente el moveset copiado, con el PP topeado en 5.
    expect(fields(transformed())).toEqual([]);
  });

  it("transform_resolved_but_moves_empty", () => {
    // T-01 (LINEAR_VERDICT): la ausencia sólo se reportaba cuando `copied`
    // era `undefined`. Un Transform RESUELTO con `moves: []` pasaba con cero
    // violaciones aunque le faltara el único movimiento copiado.
    const dataset = transformed();
    auditedOpponentOf(dataset).moves = [];
    expect(fields(dataset)).toEqual(["moves"]);
  });

  it("max_pp_after_move_was_used", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).moves = [
      { id: "psychic", pp: 16, max_pp: 16 },
      { id: "hiddenpower", pp: 23, max_pp: 24 },
    ];
    expect(fields(dataset)).toEqual(["moves"]);
    // Positivo: el PP exacto tras un uso narrado.
    expect(fields(baseDataset())).toEqual([]);
  });

  it("move_with_extra_schema_key", () => {
    const dataset = baseDataset();
    auditedOpponentOf(dataset).moves = [
      { id: "psychic", pp: 15, max_pp: 16, secret: "filtrado" },
      { id: "hiddenpower", pp: 23, max_pp: 24 },
    ];
    expect(fields(dataset)).toEqual(["moves"]);
  });

  it("values_that_never_coexisted_in_one_snapshot", () => {
    // El turno 1 narra, en este orden: +1 atk, daño a 55/100, +1 atk otra vez.
    // `hp=1.0` existió y `atk=2` existió, pero NUNCA a la vez.
    const lines = [
      "|switch|p2a: Mimien|Mr. Mime, L82, M|100/100",
      "|-boost|p2a: Mimien|atk|1",
      "|-damage|p2a: Mimien|55/100",
      "|-boost|p2a: Mimien|atk|1",
    ];
    const at = (hp: number, atk: number): Dataset => soloFixture(
      lines,
      {
        ...freshOpponent("mrmime", 82, ["PSYCHIC", "FAIRY"]),
        hp_fraction: hp,
        boosts: { accuracy: 0, atk, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 },
      },
      1, // la ventana abre en el turno 1: los cursores intermedios cuentan
    );
    // Los dos instantes que existieron de verdad.
    expect(fields(at(1, 1))).toEqual([]);
    expect(fields(at(0.55, 2))).toEqual([]);
    // El collage de los dos, con cada valor por separado impecable. Se reporta
    // contra el cursor que MÁS se le acerca —el primero al que sólo le sobra un
    // campo—; lo que demuestra el test es que la combinación no corresponde a
    // ningún instante de la batalla.
    expect(fields(at(1, 2))).toEqual(["boosts"]);
  });

  it("copyboost_unknown_applied_to_source", () => {
    // `|-copyboost|FUENTE|OBJETIVO`: la FUENTE no cambia, así que marcarla
    // desconocida borraba un boost público y aceptaba cualquier valor.
    const dataset = withProtocolLine(baseDataset(), "|-copyboost|p2a: Mimien|p1a: Gengar");
    auditedOpponentOf(dataset).boosts = {
      accuracy: 0, atk: 4, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    };
    expect(fields(dataset)).toEqual(["boosts"]);
    // Positivo: como fuente conserva su +1 atk.
    const positive = withProtocolLine(baseDataset(), "|-copyboost|p2a: Mimien|p1a: Gengar");
    expect(fields(positive)).toEqual([]);
    // Y como OBJETIVO sí copia: los de nuestro Gengar están en cero.
    const target = withProtocolLine(baseDataset(), "|-copyboost|p1a: Gengar|p2a: Mimien");
    auditedOpponentOf(target).boosts = {
      accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0,
    };
    expect(fields(target)).toEqual([]);
  });

  it("formechange_read_as_details_adds_level_100", () => {
    // `forme_change` no toca el nivel: leer `-formechange` como un `details`
    // inventaba un `L100` que la línea nunca dijo.
    const dataset = withProtocolLine(baseDataset(), "|-formechange|p2a: Mimien|Mr. Mime");
    auditedOpponentOf(dataset).level = 100;
    expect(fields(dataset)).toEqual(["level"]);
    // Positivo: el nivel narrado en el `|switch|` sigue siendo el vigente.
    const positive = withProtocolLine(baseDataset(), "|-formechange|p2a: Mimien|Mr. Mime");
    expect(fields(positive)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Ronda 3 — la frontera del dex falla CERRADA, y `pp: null` tiene dueño
// ---------------------------------------------------------------------------

describe("Ronda 3 · la frontera del dex y la semántica D31 de pp null", () => {
  const LAPRAS = "|switch|p2a: Lapras|Lapras, L80, F|100/100";

  it("furfrou_banana_narrated_and_claimed", () => {
    // El protocolo Y la fila coinciden en la especie inventada: la evidencia
    // pública no alcanza, tiene que gatearla el dex.
    const dataset = soloFixture(
      ["|switch|p2a: Furfrou|Furfrou-Banana, L85, F|100/100"],
      {
        ...freshOpponent("furfroubanana", 85, ["DRAGON"]),
        ability: "wonderguard",
      },
    );
    expect(fields(dataset)).toEqual(["species"]);
    // Positivo: la forma cosmética REAL sí resuelve, y con los tipos y la
    // ability de su base.
    const real = soloFixture(
      ["|switch|p2a: Furfrou|Furfrou-Pharaoh, L85, F|100/100"],
      { ...freshOpponent("furfroupharaoh", 85, ["NORMAL"]), ability: "furcoat" },
    );
    expect(fields(real)).toEqual([]);
    // Y sobre la forma real, un tipo inventado ya NO se excusa.
    const wrongTypes = soloFixture(
      ["|switch|p2a: Furfrou|Furfrou-Pharaoh, L85, F|100/100"],
      { ...freshOpponent("furfroupharaoh", 85, ["DRAGON"]), ability: "furcoat" },
    );
    expect(fields(wrongTypes)).toEqual(["types"]);
  });

  it("gholdengo_gen6_narrated_and_claimed", () => {
    // Gholdengo existe en el dex, pero en gen 9. Una batalla de gen 6 no
    // puede resolverlo: el índice es generation-scoped.
    const dataset = soloFixture(
      ["|switch|p2a: Gholdengo|Gholdengo, L80|100/100"],
      { ...freshOpponent("gholdengo", 80, ["STEEL", "GHOST"]), ability: "goodasgold" },
    );
    dataset.dexPokemon.push({
      gen: 9, showdownId: "gholdengo", baseSpecies: "gholdengo", forme: null,
      types: ["Steel", "Ghost"], abilities: ["Good as Gold"],
    });
    expect(fields(dataset)).toEqual(["species"]);
    // Positivo: la misma especie en SU generación no es una violación.
    const gen9 = soloFixture(
      ["|switch|p2a: Gholdengo|Gholdengo, L80|100/100"],
      { ...freshOpponent("gholdengo", 80, ["STEEL", "GHOST"]), ability: "goodasgold" },
    );
    gen9.dexPokemon.push({
      gen: 9, showdownId: "gholdengo", baseSpecies: "gholdengo", forme: null,
      types: ["Steel", "Ghost"], abilities: ["Good as Gold"],
    });
    gen9.trajectories[0].gen = 9;
    gen9.steps[0].state.gen = 9;
    gen9.cosmeticFormes = [];
    expect(fields(gen9)).toEqual([]);
  });

  it("pp_null_under_pressure_is_legitimate", () => {
    // `pressure_on_us()`: con NUESTRO activo con Pressure, el recorder no
    // puede decidir si el descuento fue de uno o de dos y escribe null.
    const dataset = soloFixture(
      [
        "|switch|p1a: Dusknoir|Dusknoir, L80, M|100/100",
        LAPRAS,
        "|move|p2a: Lapras|Shadow Ball|p1a: Dusknoir",
      ],
      {
        ...freshOpponent("lapras", 80, ["WATER", "ICE"]),
        moves: [{ id: "shadowball", pp: null, max_pp: 24 }],
      },
    );
    expect(fields(dataset)).toEqual([]);
  });

  it("pp_null_persists_after_losing_pressure", () => {
    // Una vez en null, `pp - 1 if isinstance(pp, int)` deja null para siempre.
    const dataset = soloFixture(
      [
        "|switch|p1a: Dusknoir|Dusknoir, L80, M|100/100",
        LAPRAS,
        "|move|p2a: Lapras|Shadow Ball|p1a: Dusknoir",
        "|switch|p1a: Gengar|Gengar, L80, M|280/280",
        "|move|p2a: Lapras|Shadow Ball|p1a: Gengar",
      ],
      {
        ...freshOpponent("lapras", 80, ["WATER", "ICE"]),
        moves: [{ id: "shadowball", pp: null, max_pp: 24 }],
      },
    );
    expect(fields(dataset)).toEqual([]);
  });

  it("pp_null_without_public_cause_is_rejected", () => {
    // Sin Pressure enfrente el PP es exacto: un null ahí es una omisión que
    // el contrato de D31 no habilita.
    const dataset = soloFixture(
      [LAPRAS, "|move|p2a: Lapras|Shadow Ball|p1a: Gengar"],
      {
        ...freshOpponent("lapras", 80, ["WATER", "ICE"]),
        moves: [{ id: "shadowball", pp: null, max_pp: 24 }],
      },
    );
    expect(fields(dataset)).toEqual(["moves"]);
    // Positivo: el mismo movimiento con su PP exacto pasa.
    const exact = soloFixture(
      [LAPRAS, "|move|p2a: Lapras|Shadow Ball|p1a: Gengar"],
      {
        ...freshOpponent("lapras", 80, ["WATER", "ICE"]),
        moves: [{ id: "shadowball", pp: 23, max_pp: 24 }],
      },
    );
    expect(fields(exact)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Otras protecciones que el rediseño no puede perder
// ---------------------------------------------------------------------------

describe("las excepciones siguen sin ser comodines", () => {
  it("un Transform sin objetivo resoluble NO excusa nada", () => {
    const dataset = withProtocolLine(baseDataset(), "|-transform|p2a: Mimien|p1a: Fantasma");
    const audited = auditedOpponentOf(dataset);
    audited.ability = "hugepower";
    audited.moves = [{ id: "spore", pp: 5, max_pp: 5 }];
    expect(fields(dataset)).toEqual(["ability", "moves"]);
  });

  it("el Transform TERMINA cuando el pokémon sale del campo", () => {
    const dataset = transformed();
    withProtocolLine(dataset, "|switch|p2a: Furfrou|Furfrou-Pharaoh, L85, F|100/100");
    const opponent = auditedStep(dataset).state.opponent!.pokemon!;
    opponent[0].active = false;
    opponent[1].active = true;
    // Ya no hay Transform vigente: lo copiado deja de estar excusado, y
    // `switch_out` revierte además el override temporal de la ability.
    expect(fields(dataset)).toEqual(["ability", "moves", "types"]);
  });

  it("una forma cosmética REAL, narrada verbatim, sigue pasando", () => {
    expect(fields(baseDataset())).toEqual([]);
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

describe("canario · las reproducciones de los dos reviews están todas cubiertas", () => {
  const ROUND_ONE = [
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
  const ROUND_TWO = [
    "all_opponents_active_false",
    "typechange_water_but_state_dragon",
    "soundproof_kept_after_skill_swap",
    "leftovers_kept_after_enditem",
    "charizard_base_claims_mega_x_types_and_ability",
    "spore_attributed_from_magic_bounce",
    "transform_moveset_plus_transform",
    "max_pp_after_move_was_used",
    "move_with_extra_schema_key",
    "values_that_never_coexisted_in_one_snapshot",
    "copyboost_unknown_applied_to_source",
    "formechange_read_as_details_adds_level_100",
  ];

  const ROUND_THREE = [
    "furfrou_banana_narrated_and_claimed",
    "gholdengo_gen6_narrated_and_claimed",
    "pp_null_under_pressure_is_legitimate",
    "pp_null_persists_after_losing_pressure",
    "pp_null_without_public_cause_is_rejected",
  ];

  const ROUND_FOUR = [
    "transform_resolved_but_moves_empty",
  ];

  it("hay un test con el nombre exacto de cada reproducción reportada", async () => {
    const source = await import("node:fs/promises")
      .then((fs) => fs.readFile(new URL("./lifecycle.test.ts", import.meta.url), "utf8"));
    for (const mutation of [...ROUND_ONE, ...ROUND_TWO, ...ROUND_THREE, ...ROUND_FOUR]) {
      expect(source, `falta el canario de ${mutation}`).toContain(`it("${mutation}"`);
    }
    expect(ROUND_ONE).toHaveLength(11);
    expect(ROUND_TWO).toHaveLength(12);
    expect(ROUND_THREE).toHaveLength(5);
    expect(ROUND_FOUR).toHaveLength(1);
  });
});
