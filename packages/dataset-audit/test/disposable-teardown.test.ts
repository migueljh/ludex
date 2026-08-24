/** MON-33 R4: el ciclo de vida de las bases descartables cierra TODAS las
 * conexiones del pool antes del `DROP DATABASE ... WITH (FORCE)` y ninguna
 * terminación administradora puede volverse un error no manejado.
 *
 * Reproducción del gate independiente de Latwan: Node 22,
 * `TEST_DATABASE_URL` apuntando al Postgres de mantenimiento y
 * `DATABASE_URL` a un clon descartable migrado, la suite completa terminó
 * 203 aserciones con exit 1 y tres errores PostgreSQL 57P01 no manejados
 * ("terminating connection due to administrator command"); Postgres logueó
 * el FATAL por backend matado y el contenedor quedó con RestartCount 0.
 *
 * Mecanismo (verificado contra pg@8.13.1 / pg-pool): `pool.end()` resuelve
 * apenas los clientes se DES-REGISTRAN del pool (`_clients.length === 0`),
 * no cuando sus sockets terminaron de cerrarse. `drop()` corría el
 * `DROP ... WITH (FORCE)` en esa ventana: si el FORCE le ganaba al cierre,
 * el backend era SIGTERMeado, mandaba el ErrorResponse 57P01 al cliente, y
 * como el pool no tenía listener de `'error'`, el `pool.emit('error')` se
 * volvía un `uncaughtException` que tiraba la suite. Flaky por naturaleza:
 * es una carrera de microsegundos entre el cierre del socket y el FORCE.
 *
 * Canarios de este archivo:
 * 1. `drop()` con un pool de 10 conexiones: cierra TODO, no deja base ni
 *    backends residuales, y no captura ningún error de conexión.
 * 2. Terminación administradora (mismo SIGTERM/57P01 que el FORCE, vía
 *    `pg_terminate_backend`) de conexiones idle: el error llega CAPTURADO y
 *    expuesto en `pool.connectionErrors` — nunca un `uncaughtException` —
 *    y el `drop()` posterior sigue sin dejar nada.
 * 3. El guard de base compartida sigue intacto.
 */

import { describe, expect, it } from "vitest";
import pg from "pg";
import {
  assertDisposable,
  createDisposableDatabase,
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

describe.skipIf(requiresTestDatabase)("ciclo de vida de bases descartables (MON-33 R4)", () => {
  it("drop con 10 conexiones de pool: 0 errores de conexión, 0 backends residuales, 0 bases huérfanas", async () => {
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

      const state = await databaseState(base, name);
      expect(state.exists).toBe(false);
      expect(state.backends).toBe(0);
      expect(db.pool.connectionErrors).toEqual([]);
    } finally {
      await db.drop().catch(() => {});
    }
  });

  it("terminación administradora de conexiones idle: 57P01 capturado y expuesto, nunca uncaughtException, drop posterior limpio", async () => {
    const base = process.env.TEST_DATABASE_URL!;
    const db = await createDisposableDatabase(base);
    const name = databaseName(db.url);
    try {
      // 3 consultas concurrentes -> 3 conexiones (1 de verificación + 2
      // nuevas), las tres idle al volver al pool: el FORCE del DROP las
      // habría matado igual que acá las mata pg_terminate_backend.
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

      // Sin listener de 'error' en el pool, estos tres 57P01 revientan el
      // proceso (reproducción de Latwan). Con la captura, quedan expuestos
      // acá: el error NO se traga, se inspecciona.
      expect(db.pool.connectionErrors).toHaveLength(3);
      expect((db.pool.connectionErrors[0] as { code?: string }).code).toBe("57P01");

      await db.drop();
      const state = await databaseState(base, name);
      expect(state.exists).toBe(false);
      expect(state.backends).toBe(0);
    } finally {
      await db.drop().catch(() => {});
    }
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
