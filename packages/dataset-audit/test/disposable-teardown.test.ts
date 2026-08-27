/** MON-33 R5: teardown descartable fail-closed con barrera determinística
 * de sockets cerrados.
 *
 * R4 dejó agujeros (revisión de Tasos, adjudicada):
 * - T-01: drop() podía devolver éxito después de que el drenaje agotara la
 *   cota y el FORCE matara sockets: el 57P01 quedaba en
 *   `pool.connectionErrors` sin que nadie lo mirara.
 * - T-02: los canarios de R4 no eran load-bearing: sacar sólo el poll de
 *   `pg_stat_activity` dejaba todo verde (12/12). El drenaje sigue como
 *   defensa, pero la barrera primaria ahora es determinística: drop()
 *   espera el evento `'remove'` del pool por CADA conexión (socket cerrado
 *   del todo) y deja constancia en `db.removedAtDrop`.
 * - T-03: el doble drop con catch-all escondía fallas de limpieza. drop()
 *   ahora es idempotente y los finally no tragan errores.
 *
 * Canarios:
 * 1. drop() con pool de 10 conexiones: barrera completa (removedAtDrop=10),
 *    sin errores de conexión, sin backends, sin base.
 * 2. pg-pool end() resuelve ANTES de que ningún socket termine de cerrarse
 *    (n>1): es la premisa que hace necesaria la barrera.
 * 3. drenaje agotado con un backend ajeno vivo: drop() rechaza SIN DROP
 *    (fail-closed); al cerrar el ajeno, drop() vuelve a funcionar.
 * 4. terminación administradora de conexiones idle: 57P01 expuesto sin
 *    filtrar, drop() rechaza el ciclo sucio y la base queda sin residuos.
 * 5. una limpieza que rechaza en el finally NO se traga.
 * 6. el guard de base compartida sigue intacto.
 */

import { describe, expect, it } from "vitest";
import pg from "pg";
import {
  assertDisposable,
  createDisposableDatabase,
  DisposableConnectionError,
  DisposableDrainTimeoutError,
  SharedDatabaseGuardError,
} from "./_disposable.js";

const requiresTestDatabase = process.env.TEST_DATABASE_URL === undefined;

function databaseName(url: string): string {
  return new URL(url).pathname.replace(/^\//, "");
}

function maintenanceUrl(baseUrl: string): string {
  const parsed = new URL(baseUrl);
  parsed.pathname = "/postgres";
  parsed.search = "";
  return parsed.toString();
}

async function databaseState(baseUrl: string, name: string): Promise<{ exists: boolean; backends: number }> {
  const client = new pg.Client({ connectionString: maintenanceUrl(baseUrl) });
  await client.connect();
  try {
    const exists = await client.query("SELECT 1 FROM pg_database WHERE datname = $1", [name]);
    const backends = await client.query<{ n: number }>(
      "SELECT count(*)::int AS n FROM pg_stat_activity WHERE datname = $1", [name],
    );
    return { exists: (exists.rowCount ?? 0) > 0, backends: backends.rows[0]?.n ?? 0 };
  } finally {
    await client.end();
  }
}

/** Espera acotada con aserción final: si la condición nunca se cumple el
 * test FALLA (no puede pasar sin verificar). */
async function eventually(condition: () => boolean, label: string, timeoutMs = 5_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (condition()) return;
    if (Date.now() >= deadline) {
      throw new Error(`timeout (${timeoutMs} ms) esperando ${label}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

describe.skipIf(requiresTestDatabase)("ciclo de vida de bases descartables (MON-33 R5)", () => {
  it("drop con 10 conexiones de pool: barrera de removes completa, 0 errores de conexión, 0 backends residuales, 0 bases huérfanas", async () => {
    const base = process.env.TEST_DATABASE_URL!;
    const db = await createDisposableDatabase(base);
    const name = databaseName(db.url);
    try {
      // 10 consultas concurrentes fuerzan el pool al máximo (1 conexión ya
      // existe por la verificación de `verifiedPool`, 9 se crean acá): el
      // drop cierra las 10, no sólo la última que se usó.
      await Promise.all(Array.from({ length: 10 }, () => db.pool.query("SELECT 1")));
      expect(db.pool.totalCount).toBe(10);

      await db.drop();

      // Barrera determinística (T-02): cuando drop() emitió el DROP, las 10
      // conexiones habían completado el cierre (evento 'remove' del pool).
      // Sin la espera, esto es 0 con certeza: pool.end() resuelve al
      // des-registrarse los clientes, el socket cierra después.
      expect(db.removedAtDrop).toBe(10);

      const state = await databaseState(base, name);
      expect(state.exists).toBe(false);
      expect(state.backends).toBe(0);
      expect(db.pool.connectionErrors).toEqual([]);
    } finally {
      await db.drop();
    }
  });

  it("pg-pool end() resuelve antes de que ningún socket termine de cerrarse (premisa de la barrera, n>1)", async () => {
    const base = process.env.TEST_DATABASE_URL!;
    const db = await createDisposableDatabase(base);
    try {
      await Promise.all(Array.from({ length: 3 }, () => db.pool.query("SELECT 1")));
      const n = db.pool.totalCount;
      expect(n).toBeGreaterThan(1);

      let removed = 0;
      db.pool.on("remove", () => { removed += 1; });

      await db.pool.end();

      // Determinístico: pool.end() resuelve apenas los clientes se
      // des-registran; el 'remove' (socket cerrado del todo) es I/O y no
      // puede haber ocurrido todavía.
      expect(removed).toBe(0);

      // Y después SÍ completan todos: el canario verifica que el evento
      // llega (no puede pasar sin ejercerlo).
      await eventually(() => removed === n, `los ${n} eventos 'remove' del pool`);
    } finally {
      await db.drop();
    }
  });

  it("drop() rechaza SIN DROP cuando el drenaje agota la cota con un backend ajeno vivo (fail-closed)", async () => {
    const base = process.env.TEST_DATABASE_URL!;
    const db = await createDisposableDatabase(base);
    const name = databaseName(db.url);
    const foreign = new pg.Client({ connectionString: db.url });
    const foreignErrors: unknown[] = [];
    foreign.on("error", (error) => { foreignErrors.push(error); });
    await foreign.connect();
    try {
      // Una conexión AJENA al helper, viva: el drenaje nunca llega a cero.
      // Fail-closed (T-01): drop() debe rechazar en vez de devolver éxito.
      await expect(db.drop()).rejects.toThrow(DisposableDrainTimeoutError);

      // El FORCE NO corrió: la base sigue y el backend ajeno sigue vivo
      // (fail-closed significa no matar lo que el helper no controla).
      const state = await databaseState(base, name);
      expect(state.exists).toBe(true);
      expect(state.backends).toBeGreaterThan(0);
      expect(foreignErrors).toEqual([]);

      await foreign.end();
      await db.drop();

      const after = await databaseState(base, name);
      expect(after.exists).toBe(false);
      expect(after.backends).toBe(0);
    } finally {
      await foreign.end();
      await db.drop();
    }
  });

  it("terminación administradora de conexiones idle: 57P01 expuesto sin filtrar, drop() rechaza el ciclo sucio y no deja residuos", async () => {
    const base = process.env.TEST_DATABASE_URL!;
    const db = await createDisposableDatabase(base);
    const name = databaseName(db.url);
    try {
      // 3 consultas concurrentes -> 3 conexiones (1 de verificación + 2
      // nuevas), las tres idle al volver al pool.
      await Promise.all(Array.from({ length: 3 }, () => db.pool.query("SELECT 1")));

      const killer = new pg.Client({ connectionString: maintenanceUrl(base) });
      await killer.connect();
      try {
        await killer.query(
          "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
          [name],
        );
      } finally {
        await killer.end();
      }
      // El ErrorResponse viaja por el socket: darle tiempo a llegar.
      await new Promise((resolve) => setTimeout(resolve, 500));

      expect(db.pool.connectionErrors).toHaveLength(3);
      expect((db.pool.connectionErrors[0] as { code?: string }).code).toBe("57P01");

      // Fail-closed (T-01): el DROP corre (cero backends: no hay nada que
      // el FORCE pueda matar) pero drop() rechaza el ciclo sucio en vez de
      // devolver éxito, y NO filtra ni limpia los 57P01.
      await expect(db.drop()).rejects.toThrow(DisposableConnectionError);
      expect(db.pool.connectionErrors).toHaveLength(3);

      const state = await databaseState(base, name);
      expect(state.exists).toBe(false);
      expect(state.backends).toBe(0);
    } finally {
      await db.drop();
    }
  });

  it("una limpieza que rechaza en el finally no se traga: el error se relaya (T-03)", async () => {
    const base = process.env.TEST_DATABASE_URL!;
    const db = await createDisposableDatabase(base);
    const foreign = new pg.Client({ connectionString: db.url });
    await foreign.connect();
    let relaid: unknown;
    try {
      try {
        await db.pool.query("SELECT 1");
      } finally {
        // Sin catch-all: si la limpieza rechaza, el error sale. Acá el
        // backend ajeno mantiene el drenaje ocupado -> drop() rechaza.
        await db.drop();
      }
    } catch (error) {
      relaid = error;
    } finally {
      await foreign.end();
      await db.drop();
    }
    // El rechazo de la limpieza llegó (no fue tragado por un catch-all).
    expect(relaid).toBeInstanceOf(DisposableDrainTimeoutError);
  });

  it("el guard de base compartida sigue rechazando toda URL no descartable", () => {
    // URL sin credenciales reales: `assertDisposable` sólo mira el pathname.
    const base = "postgres://usuario:clave@127.0.0.1:15432";
    for (const name of ["ludex", "postgres", "template0", "template1", ""]) {
      expect(() => assertDisposable(`${base}/${name}`)).toThrow(SharedDatabaseGuardError);
    }
    expect(() => assertDisposable(`${base}/ludex_test_canario123`)).not.toThrow();
  });
});
