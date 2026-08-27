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
 * 203 aserciones y exit 1). R4 capturó y expuso los errores del pool en
 * `connectionErrors` y agregó un drenaje de `pg_stat_activity` antes del
 * FORCE.
 *
 * R5 (MON-33, revisión de Tasos adjudicada) endurece el teardown:
 * - T-01 fail-closed: `drop()` ya no devuelve éxito después de un drenaje
 *   agotado o de errores de conexión. Si la cota de drenaje vence, NO emite
 *   el DROP (el FORCE mataría conexiones que el helper no controla) y
 *   rechaza. Si el pool capturó errores asíncronos (57P01, etc.), el DROP
 *   corre igual (con cero backends no hay nada que matar) pero `drop()`
 *   rechaza: el teardown no fue limpio y no se devuelve éxito silencioso.
 *   Los errores quedan intactos en `pool.connectionErrors`, nunca filtrados.
 * - T-02 barrera determinística: `drop()` espera el evento `'remove'` del
 *   pool por CADA cliente (socket terminado de cerrar, I/O) y deja
 *   constancia en `removedAtDrop`. El poll de `pg_stat_activity` queda como
 *   defensa (backends ajenos), no como barrera primaria.
 * - T-03 idempotencia: `drop()` puede llamarse más de una vez sin fallar
 *   (el pool sólo se cierra una vez; un DROP ya emitido no se repite), así
 *   el `finally` no necesita catch-all y una limpieza que falla se relaya.
 * Canarios en `test/disposable-teardown.test.ts`.
 */

import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import pg from "pg";

const SCHEMA_PATH = fileURLToPath(new URL("../../../db/schema.sql", import.meta.url));
const DISPOSABLE_PREFIX = "ludex_test_";
const FORBIDDEN_NAMES = new Set(["", "ludex", "postgres", "template0", "template1"]);
/** Cota del drenaje de `pg_stat_activity`: si vence con backends vivos,
 * drop() rechaza SIN emitir el DROP. */
const DRAIN_TIMEOUT_MS = 5_000;
const DRAIN_POLL_MS = 100;
/** Cota de la barrera de eventos `'remove'` del pool. */
const BARRIER_TIMEOUT_MS = 5_000;
const BARRIER_POLL_MS = 10;

export class SharedDatabaseGuardError extends Error {}

/** Teardown descartable fallido: `drop()` rechaza en vez de devolver éxito. */
export class DisposableTeardownError extends Error {}

/** La barrera de sockets cerrados o el drenaje de `pg_stat_activity`
 * agotaron su cota: quedan conexiones vivas que el helper no controla.
 * drop() NO emitió el DROP (el FORCE las habría matado y su 57P01 habría
 * quedado invisible); la base queda y es barrible por el prefijo
 * `ludex_test_`. */
export class DisposableDrainTimeoutError extends DisposableTeardownError {}

/** El pool capturó errores de conexión asíncronos (p. ej. 57P01) durante el
 * ciclo de vida. El DROP sí corrió (el drenaje dio cero backends: no había
 * nada que el FORCE pudiera matar), pero drop() rechaza igual: el teardown
 * no fue limpio y no se devuelve éxito. Los errores quedan intactos en
 * `pool.connectionErrors`, nunca filtrados ni limpiados. */
export class DisposableConnectionError extends DisposableTeardownError {}

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
  /** Conexiones del pool cuyo evento `'remove'` (socket terminado de
   * cerrar) había completado cuando drop() emitió el `DROP DATABASE`. Con
   * la barrera intacta coincide con el total de clientes del pool; es la
   * constancia determinística de que el FORCE nunca corrió con sockets a
   * medio cerrar. */
  readonly removedAtDrop: number;
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
  const teardown = { poolEnded: false, dropped: false, removedAtDrop: 0 };

  /** Cierra el pool y espera la barrera de eventos `'remove'` (T-02).
   * `pool.end()` resuelve apenas los clientes se des-registran; el socket
   * de cada uno termina de cerrarse recién cuando el pool emite `'remove'`
   * (`client.end` -> `'close'` del stream, I/O). Sin esta espera el DROP
   * puede correr con sockets a medio cerrar y el FORCE los mata: 57P01. */
  async function endPoolAndBarrier(): Promise<void> {
    if (pool.ended || teardown.poolEnded) {
      return;
    }
    teardown.poolEnded = true;
    const removed = new Set<unknown>();
    const onRemove = (client: unknown): void => {
      removed.add(client);
    };
    pool.on("remove", onRemove);
    const expected = pool.totalCount;
    await pool.end();
    const deadline = Date.now() + BARRIER_TIMEOUT_MS;
    while (removed.size < expected && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, BARRIER_POLL_MS));
    }
    teardown.removedAtDrop = removed.size;
    pool.removeListener("remove", onRemove);
    if (teardown.removedAtDrop < expected) {
      throw new DisposableDrainTimeoutError(
        `drop() abortó antes del DROP: ${teardown.removedAtDrop}/${expected} conexiones `
        + `del pool terminaron de cerrarse dentro de la cota de ${BARRIER_TIMEOUT_MS} ms.`,
      );
    }
  }

  async function drop(): Promise<void> {
    // Idempotente (T-03): un DROP ya emitido no se repite; el `finally` de
    // los tests puede llamar drop() siempre sin catch-all.
    if (teardown.dropped) {
      return;
    }
    await endPoolAndBarrier();
    const cleanup = new pg.Client({ connectionString: maintenanceUrl });
    await cleanup.connect();
    let drainTimedOut = false;
    try {
      const deadline = Date.now() + DRAIN_TIMEOUT_MS;
      for (;;) {
        const { rows } = await cleanup.query<{ n: number }>(
          "SELECT count(*)::int AS n FROM pg_stat_activity WHERE datname = $1",
          [name],
        );
        if ((rows[0]?.n ?? 0) === 0) break;
        if (Date.now() >= deadline) {
          drainTimedOut = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, DRAIN_POLL_MS));
      }
      if (drainTimedOut) {
        // Fail-closed (T-01): backends ajenos al helper siguen vivos al
        // vencer la cota. NO se emite el DROP: el FORCE los mataría y su
        // 57P01 quedaría invisible. La base queda, barrible por el prefijo.
        throw new DisposableDrainTimeoutError(
          `drop() abortó sin DROP: quedan backends de '${name}' en pg_stat_activity `
          + `después de ${DRAIN_TIMEOUT_MS} ms.`,
        );
      }
      await cleanup.query(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`);
      teardown.dropped = true;
    } finally {
      await cleanup.end();
    }
    if (pool.connectionErrors.length > 0) {
      // Fail-closed (T-01): el ciclo de vida no fue limpio. El DROP corrió
      // con cero backends (no había nada que el FORCE pudiera matar), pero
      // drop() rechaza en vez de devolver éxito. Los errores quedan
      // intactos en `pool.connectionErrors`: nunca se filtran ni se limpian.
      throw new DisposableConnectionError(
        `drop() completó el DROP pero el teardown no fue limpio: `
        + `${pool.connectionErrors.length} error(es) de conexión capturados `
        + "en pool.connectionErrors (sin filtrar).",
      );
    }
  }

  return {
    url: disposableUrl,
    pool,
    get removedAtDrop(): number {
      return teardown.removedAtDrop;
    },
    drop,
  };
}
