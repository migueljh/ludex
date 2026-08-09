/** MON-11: `loadCosmeticAliases` tiene que fallar CERRADO -- nunca degradar
 * en silencio a una tabla vacía. Cada escenario controla `$LUDEX_SHOWDOWN_
 * DEX_DIR` a mano, apuntando a un directorio temporal fabricado por el
 * propio test: nunca depende de si ESTE worktree tiene `apps/agent/.venv`
 * o no. Eso es a propósito -- el bug real (170 falsos positivos de especie
 * en el diagnóstico de MON-11) pasó justamente porque un worktree sin
 * `.venv` degradaba a "cero alias" sin que nadie lo notara. */

import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CosmeticVocabularyError, loadCosmeticAliases } from "../src/cosmetic.js";

const ORIGINAL_ENV = process.env.LUDEX_SHOWDOWN_DEX_DIR;
let workDir: string;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "ludex-cosmetic-test-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
  if (ORIGINAL_ENV === undefined) delete process.env.LUDEX_SHOWDOWN_DEX_DIR;
  else process.env.LUDEX_SHOWDOWN_DEX_DIR = ORIGINAL_ENV;
});

function conAlias(gen: number, especies: Record<string, string[]>): void {
  const pokedex: Record<string, { cosmeticFormes: string[] }> = {};
  for (const [baseId, formes] of Object.entries(especies)) {
    pokedex[baseId] = { cosmeticFormes: formes };
  }
  writeFileSync(join(workDir, `gen${gen}pokedex.json`), JSON.stringify(pokedex));
}

describe("loadCosmeticAliases falla cerrado", () => {
  it("directorio inexistente: lanza CosmeticVocabularyError, nunca vuelve con directory undefined", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = join(workDir, "no-existe-esta-carpeta");
    expect(() => loadCosmeticAliases([6])).toThrow(CosmeticVocabularyError);
    expect(() => loadCosmeticAliases([6])).toThrow(/no se encontr[oó]/i);
  });

  it("directorio existe pero falta el pokedex de la generación pedida: lanza, no continúa vacío", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = workDir; // vacio, sin gen6pokedex.json
    expect(() => loadCosmeticAliases([6])).toThrow(CosmeticVocabularyError);
    expect(() => loadCosmeticAliases([6])).toThrow(/falta el pokedex/i);
  });

  it("JSON invalido en el pokedex: lanza en vez de tragarse el error y seguir", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = workDir;
    writeFileSync(join(workDir, "gen6pokedex.json"), "{ esto no es json valido");
    expect(() => loadCosmeticAliases([6])).toThrow(CosmeticVocabularyError);
    expect(() => loadCosmeticAliases([6])).toThrow(/no es JSON v[aá]lido/i);
  });

  it("pokedex valido pero sin ningun cosmeticFormes: cero alias tambien es fallo cerrado", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = workDir;
    writeFileSync(
      join(workDir, "gen6pokedex.json"),
      JSON.stringify({ pikachu: {}, charizard: { cosmeticFormes: [] } }),
    );
    expect(() => loadCosmeticAliases([6])).toThrow(CosmeticVocabularyError);
    expect(() => loadCosmeticAliases([6])).toThrow(/cero alias/i);
  });

  it("gens vacio (dataset sin filas): NO exige pokedex, pero SI exige que el directorio exista", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = join(workDir, "no-existe");
    expect(() => loadCosmeticAliases([])).toThrow(CosmeticVocabularyError);

    process.env.LUDEX_SHOWDOWN_DEX_DIR = workDir; // existe, vacio, sin gens pedidas
    const result = loadCosmeticAliases([]);
    expect(result.aliases).toEqual([]);
    expect(result.directory).toBe(workDir);
  });

  it("multiples generaciones: si falta UNA sola, falla cerrado (no reporta parcial)", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = workDir;
    conAlias(6, { vivillon: ["Vivillon-Tundra"] });
    // gen9pokedex.json no existe.
    expect(() => loadCosmeticAliases([6, 9])).toThrow(CosmeticVocabularyError);
    expect(() => loadCosmeticAliases([6, 9])).toThrow(/gen 9/);
  });

  it("directorio con dex valido y cosmeticFormes reales: no lanza, devuelve los alias exactos", () => {
    process.env.LUDEX_SHOWDOWN_DEX_DIR = workDir;
    conAlias(6, {
      vivillon: ["Vivillon-Tundra", "Vivillon-Polar"],
      florges: ["Florges-Yellow"],
    });
    const result = loadCosmeticAliases([6]);
    expect(result.directory).toBe(workDir);
    expect(result.aliases).toHaveLength(3);
    expect(result.aliases).toContainEqual({ gen: 6, aliasId: "vivillontundra", baseId: "vivillon" });
    expect(result.aliases).toContainEqual({ gen: 6, aliasId: "vivillonpolar", baseId: "vivillon" });
    expect(result.aliases).toContainEqual({ gen: 6, aliasId: "florgesyellow", baseId: "florges" });
  });

  it("LUDEX_SHOWDOWN_DEX_DIR apuntando a un directorio real vacio en vez de al dex real: falla igual", () => {
    // Contrapeso explicito del bug de MON-11: un $LUDEX_SHOWDOWN_DEX_DIR o
    // un apps/agent/.venv presentes pero SIN el dex real (worktree fresco,
    // `uv sync` nunca corrido) no puede colarse como "cero alias, seguimos".
    mkdirSync(join(workDir, "vacio-de-verdad"));
    process.env.LUDEX_SHOWDOWN_DEX_DIR = join(workDir, "vacio-de-verdad");
    expect(() => loadCosmeticAliases([6])).toThrow(CosmeticVocabularyError);
  });
});
