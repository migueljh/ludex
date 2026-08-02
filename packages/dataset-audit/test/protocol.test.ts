/** Parseo puro del protocolo. El comportamiento con estado vive en
 * `projection.test.ts`; acá sólo se prueba cómo se lee una línea. */

import { describe, expect, it } from "vitest";
import {
  buildSpeciesIndex,
  canonicalIdentity,
  normalizeProtocolText,
  opponentSideOf,
  parseDetails,
  parseHpToken,
  parseIdent,
  retrieveMoveId,
} from "../src/protocol.js";
import { dexPokemon } from "./fixtures.js";

const speciesIndex = buildSpeciesIndex(dexPokemon());
const resolve = speciesIndex.resolve;

describe("normalización", () => {
  it("elimina toda la puntuación y los diacríticos, no sólo espacios y guiones", () => {
    expect(normalizeProtocolText("Mr. Mime")).toBe("mrmime");
    expect(normalizeProtocolText("Farfetch'd")).toBe("farfetchd");
    expect(normalizeProtocolText("Flabébé")).toBe("flabebe");
    expect(normalizeProtocolText("Charizard-Mega-X")).toBe("charizardmegax");
  });
});

describe("parseo de ident y details", () => {
  it("separa lado y nombre del ident, y descarta la letra de ranura", () => {
    expect(parseIdent("p2a: Mr. Mime")).toEqual({ side: "p2", name: "mrmime" });
    expect(parseIdent("p1b: Gengar")).toEqual({ side: "p1", name: "gengar" });
    expect(parseIdent("turn 3")).toBeUndefined();
    expect(parseIdent(undefined)).toBeUndefined();
  });

  it("extrae especie y nivel del details, y acepta que Showdown omita L100", () => {
    expect(parseDetails("Yanmega, L82, F")).toEqual({ species: "yanmega", level: 82 });
    // Showdown OMITE el nivel cuando es 100 y `_update_from_details` devuelve
    // 100 en ese caso (`pokemon.py:707-710`).
    expect(parseDetails("Mew")).toEqual({ species: "mew", level: 100 });
    expect(parseDetails("Gengar, L80, M, shiny")).toEqual({ species: "gengar", level: 80 });
  });

  it("parsea el token de HP con su status", () => {
    expect(parseHpToken("55/100 par")).toMatchObject({ fraction: 0.55, status: "par" });
    expect(parseHpToken("100/100")).toMatchObject({ fraction: 1, status: null });
    expect(parseHpToken("0 fnt")).toMatchObject({ fraction: 0, status: "fnt" });
  });

  it("el lado observado es siempre el otro", () => {
    expect(opponentSideOf("p1")).toBe("p2");
    expect(opponentSideOf("p2")).toBe("p1");
  });
});

describe("identidad canónica", () => {
  it("resuelve una forma alternativa a su especie base", () => {
    expect(canonicalIdentity("charizardmegax", resolve)).toBe("charizard");
    expect(canonicalIdentity("gengar", resolve)).toBe("gengar");
  });

  it("resuelve una forma COSMÉTICA ausente del dex local por su prefijo más largo", () => {
    // `Furfrou-Pharaoh` comparte la entrada de Furfrou en el dex de Showdown y
    // la tabla `pokemon` no la trae.
    expect(resolve("furfroupharaoh")?.showdownId).toBe("furfrou");
    // Resolver NO es validar: un sufijo inventado resuelve igual.
    expect(resolve("furfroubanana")?.showdownId).toBe("furfrou");
    expect(speciesIndex.resolvedExactly("furfroubanana")).toBe(false);
    expect(speciesIndex.resolvedExactly("furfrou")).toBe(true);
  });

  it("no inventa una base cuando el dex no conoce ni el prefijo", () => {
    expect(resolve("pokemoninexistente")).toBeUndefined();
    expect(canonicalIdentity("pokemoninexistente", resolve)).toBe("pokemoninexistente");
  });
});

describe("id de movimiento", () => {
  it("colapsa exactamente los tres casos que colapsa `Move.retrieve_id`", () => {
    expect(retrieveMoveId("Hidden Power")).toBe("hiddenpower");
    expect(retrieveMoveId("hiddenpowerice")).toBe("hiddenpower");
    expect(retrieveMoveId("return102")).toBe("return");
    expect(retrieveMoveId("frustration")).toBe("frustration");
    expect(retrieveMoveId("Shadow Ball")).toBe("shadowball");
    expect(retrieveMoveId("Solar Beam")).toBe("solarbeam");
  });
});
