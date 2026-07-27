import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { createCalcServer } from "../src/server.js";

let server: Server;
let base: string;

beforeAll(async () => {
  server = createCalcServer();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

const post = (body: unknown) =>
  fetch(`${base}/calc`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

describe("GET /health", () => {
  it("responde status, version del paquete y gens soportadas", async () => {
    const res = await fetch(`${base}/health`);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.status).toBe("ok");
    // Pineada en package.json; si el endpoint dice otra cosa, el bump no se
    // propago al endpoint de salud y calc_version no sirve para auditar.
    expect(body.calc_version).toBe("0.11.0");
    expect(body.gens_supported).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
  });
});

describe("POST /calc", () => {
  it("matchup medido via HTTP", async () => {
    const res = await post({
      gen: 6,
      attacker: { species: "Garchomp", nature: "Adamant", evs: { atk: 252 } },
      defender: { species: "Snorlax" },
      move: { name: "Earthquake" },
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.min_damage).toBe(255);
    expect(body.max_damage).toBe(301);
    expect(body.ko_chance).toEqual({ chance: 1, n: 2, text: "guaranteed 2HKO" });
  });

  it("especie inexistente: 400 con mensaje util, no 500", async () => {
    const res = await post({
      gen: 6,
      attacker: { species: "NotAPokemon" },
      defender: { species: "Snorlax" },
      move: { name: "Earthquake" },
    });
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error.code).toBe("unknown_species");
    expect(body.error.message).toContain("NotAPokemon");
    expect(body.error.message).toContain("gen 6");
  });

  it("gen invalido: 400", async () => {
    const res = await post({
      gen: 42,
      attacker: { species: "Garchomp" },
      defender: { species: "Snorlax" },
      move: { name: "Earthquake" },
    });
    expect(res.status).toBe(400);
    expect((await res.json()).error.code).toBe("invalid_gen");
  });

  it("JSON malformado: 400, no 500", async () => {
    const res = await post("{not json");
    expect(res.status).toBe(400);
    expect((await res.json()).error.code).toBe("invalid_json");
  });

  it("body vacio: 400", async () => {
    const res = await fetch(`${base}/calc`, { method: "POST" });
    expect(res.status).toBe(400);
  });

  it("GET a /calc es 405 y ruta desconocida 404", async () => {
    expect((await fetch(`${base}/calc`)).status).toBe(405);
    expect((await fetch(`${base}/nope`)).status).toBe(404);
  });
});
