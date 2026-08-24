/** Base Postgres descartable para tests de dataset-audit que necesitan
 * fixtures sintéticas exactas (MON-11, D44) -- nunca la base compartida.
 *
 * `db.test.ts` ya corre "contra Postgres real", pero SOLO lee lo que ya
 * existe (nunca inserta). D44 necesita escenarios que la base compartida no
 * tiene naturalmente (las 12 batallas `local` son 100% schema v1: no hay
 * ninguna trayectoria `local`/v2 hoy, ni mixta v1/v2) -- así que hacen falta
 * fixtures sintéticas, y eso es una escritura. Nunca se escribe en la base
 * compartida: cada test toma una base Postgres nueva (`ludex_test_<uuid>`),
 * le aplica `db/schema.sql`, y la dropea en un `finally` alrededor del uso.
 *
 * Mismo alcance que `apps/agent/tests/db/_disposable.py`: el `finally`
 * cubre salida normal, excepción, y cancelación cooperativa -- nunca un
 * `SIGKILL` ni una caída del proceso, ahí ningún `finally` corre y la base
 * queda huérfana (mitigación: el prefijo `ludex_test_` la hace barrible a
 * mano, nunca contra `ludex`).
 *
 * R4 (MON-33, flake de Latwan): `pool.end()` de pg-pool resuelve apenas los
 * clientes se DES-REGISTRAN del pool, no cuando sus sockets terminaron de
 * cerrarse. El `DROP ... WITH (FORCE)` corría en esa ventana; si el FORCE le
 * ganaba al cierre, el backend moría SIGTERMeado, le mandaba el ErrorResponse
 * 57P01 al cliente idle, y sin listener de `'error'` en el pool el
 * `pool.emit('error')` se volvía un `uncaughtException` (suite entera con
 * 203 aserciones y exit 1). Dos endurecimientos: (1) `drop()` espera el
 * drenaje REAL de `pg_stat_activity` antes del FORCE; (2) el pool captura y
 * EXPONE los errores de conexión asíncronos en `connectionErrors` en vez de
 * dejar que revienten el proceso -- nunca se tragan, el test los inspecciona.
 * Canarios en `test/disposable-teardown.test.ts`.
 */

import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import pg from "pg";

const SCHEMA_PATH = fileURLToPath(new URL("../../../db/schema.sql", import.meta.url));
const DISPOSABLE_PREFIX = "ludex_test_";
const FORBIDDEN_NAMES = new Set(["", "ludex", "postgres", "template0", "template1"]);
/** Cota de espera del drenaje de `pg_stat_activity` antes de recurrir al
 * `WITH (FORCE)` (que queda como último recurso para conexiones que el helper
 * no puede cerrar, p. ej. clientes huérfanos de un bug ajeno). */
const DRAIN_TIMEOUT_MS = 5_000;
const DRAIN_POLL_MS = 100;

export class SharedDatabaseGuardError extends Error {}

function databaseName(url: string): string {
  return new URL(url).pathname.replace(/^\//, "");
}

function withDatabase(url: string, name: string): string {
  const parsed = new URL(url);
  parsed.pathname = `/${name}`;
  parsed.search = "";
  return parsed.toString();
}

export function assertDisposable(url: string): void {
  const name = databaseName(url);
  if (FORBIDDEN_NAMES.has(name) || !name.startsWith(DISPOSABLE_PREFIX)) {
    throw new SharedDatabaseGuardError(
      `'${name || "(vacío)"}' no es una base descartable: se esperaba un nombre `
      + `que empiece con '${DISPOSABLE_PREFIX}'. Los tests de dataset-audit (MON-11, `
      + "D44) nunca corren fixtures sintéticas contra la base compartida.",
    );
  }
}

/** Pool verificado cuyo evento `'error'` (terminaciones de conexión fuera de
 * una query: 57P01 de un FORCE, `pg_terminate_backend`, caída del server)
 * queda CAPTURADO y expuesto en `connectionErrors` en lugar de convertirse en
 * un `uncaughtException` que tira el proceso entero. No se traga nada: los
 * errores de las queries siguen viajando por la promesa de la query, y los de
 * conexión quedan acá, listos para que el test los inspeccione. */
export interface DisposablePool extends pg.Pool {
  readonly connectionErrors: unknown[];
}

/** Confirma `current_database()` sobre la conexión REAL antes de devolver un
 * pool utilizable -- no basta con el chequeo de string de `assertDisposable`
 * (ver el equivalente Python, R2). */
export async function verifiedPool(url: string): Promise<DisposablePool> {
  const pool = new pg.Pool({ connectionString: url });
  const connectionErrors: unknown[] = [];
  pool.on("error", (error) => {
    connectionErrors.push(error);
  });
  try {
    const { rows } = await pool.query<{ current_database: string }>("SELECT current_database()");
    const name = rows[0]?.current_database ?? "";
    if (FORBIDDEN_NAMES.has(name) || !name.startsWith(DISPOSABLE_PREFIX)) {
      throw new SharedDatabaseGuardError(
        `current_database()='${name}' no es descartable -- ninguna sentencia `
        + "mutadora puede correr sobre este pool.",
      );
    }
    return Object.assign(pool, { connectionErrors });
  } catch (error) {
    await pool.end();
    throw error;
  }
}

export interface DisposableDatabase {
  url: string;
  pool: DisposablePool;
  drop(): Promise<void>;
}

export async function createDisposableDatabase(baseUrl: string): Promise<DisposableDatabase> {
  const name = `${DISPOSABLE_PREFIX}${randomUUID().replace(/-/g, "").slice(0, 16)}`;
  const disposableUrl = withDatabase(baseUrl, name);
  assertDisposable(disposableUrl);
  const maintenanceUrl = withDatabase(baseUrl, "postgres");

  const maintenance = new pg.Client({ connectionString: maintenanceUrl });
  await maintenance.connect();
  try {
    await maintenance.query(`CREATE DATABASE "${name}"`);
  } finally {
    await maintenance.end();
  }

  const schemaClient = new pg.Client({ connectionString: disposableUrl });
  await schemaClient.connect();
  try {
    await schemaClient.query(readFileSync(SCHEMA_PATH, "utf8"));
  } finally {
    await schemaClient.end();
  }

  const pool = await verifiedPool(disposableUrl);

  return {
    url: disposableUrl,
    pool,
    async drop() {
      await pool.end();
      const cleanup = new pg.Client({ connectionString: maintenanceUrl });
      await cleanup.connect();
      try {
        // `pool.end()` resolvió, pero los sockets pueden seguir cerrando. El
        // FORCE mataría cualquier backend que todavía no procesó su cierre y
        // ese 57P01 termina (a lo sumo) en `pool.connectionErrors` -- mejor:
        // esperar el drenaje real, y sólo recurrir al FORCE si queda algo que
        // el helper no controla.
        const deadline = Date.now() + DRAIN_TIMEOUT_MS;
        for (;;) {
          const { rows } = await cleanup.query<{ n: number }>(
            "SELECT count(*)::int AS n FROM pg_stat_activity WHERE datname = $1",
            [name],
          );
          if ((rows[0]?.n ?? 0) === 0) break;
          if (Date.now() >= deadline) break;
          await new Promise((resolve) => setTimeout(resolve, DRAIN_POLL_MS));
        }
        await cleanup.query(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`);
      } finally {
        await cleanup.end();
      }
    },
  };
}
