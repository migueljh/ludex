import { beforeAll, describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractSpecies } from "../../src/extract/species.js";
import { extractMoves } from "../../src/extract/moves.js";
import { extractTypeChart } from "../../src/extract/typechart.js";
import { extractLearnsets } from "../../src/extract/learnsets.js";
import type { LearnsetRow } from "../../src/types.js";

const dex = loadGen(6);
const sortMethods = (row: LearnsetRow) => ({
  ...row,
  methods: [...row.methods].sort((a, b) =>
    `${a.gen}${a.method}${a.level ?? ""}${a.sourceSpecies}`.localeCompare(
      `${b.gen}${b.method}${b.level ?? ""}${b.sourceSpecies}`)),
});

describe("golden files gen 6", () => {
  it("especie base con evolucion", () => {
    const species = extractSpecies(dex);
    expect(species.find((s) => s.showdownId === "charizard")).toMatchSnapshot();
  });

  it("forma alternativa que comparte dex_num", () => {
    const species = extractSpecies(dex);
    expect(species.find((s) => s.showdownId === "charizardmegax")).toMatchSnapshot();
  });

  it("movimiento con flags y precision", () => {
    expect(extractMoves(dex).find((m) => m.showdownId === "flamethrower")).toMatchSnapshot();
  });

  it("fila de tabla de tipos con inmunidad", () => {
    const chart = extractTypeChart(dex);
    expect(chart.filter((r) => r.attackingType === "Dragon")).toMatchSnapshot();
  });

  describe("learnsets", () => {
    let rows: LearnsetRow[];
    beforeAll(async () => { rows = await extractLearnsets(dex); }, 180_000);

    it("movimiento propio con metodos de varias generaciones", () => {
      expect(sortMethods(
        rows.find((r) => r.speciesId === "charizard" && r.moveId === "flamethrower")!,
      )).toMatchSnapshot();
    });

    it("movimiento heredado de la preevolucion", () => {
      expect(sortMethods(
        rows.find((r) => r.speciesId === "charizard" && r.moveId === "dragondance")!,
      )).toMatchSnapshot();
    });
  });
});
