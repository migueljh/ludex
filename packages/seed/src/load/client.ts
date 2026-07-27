// Mismo problema de interop que en extract/dex.ts: pg asigna sus propiedades
// dinamicamente en el constructor, asi que `import { Pool }` no resuelve bajo
// el loader ESM nativo. El import de TIPO si puede ser nombrado, porque se
// borra en compilacion y nunca llega al runtime.
import pg from "pg";
import type { Pool } from "pg";

/** Postgres admite 65535 parametros por sentencia; se deja margen. */
const MAX_PARAMS = 30_000;

export function createPool(): Pool {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("Falta DATABASE_URL. Copiar .env.example a .env.");
  }
  return new pg.Pool({ connectionString });
}

export async function withPool<T>(fn: (pool: Pool) => Promise<T>): Promise<T> {
  const pool = createPool();
  try {
    return await fn(pool);
  } finally {
    await pool.end();
  }
}

export interface UpsertOptions {
  table: string;
  columns: string[];
  conflict: string[];
  rows: unknown[][];
  /** Columnas a no pisar en el UPDATE. Por defecto se pisan todas menos las de conflicto. */
  updateColumns?: string[];
}

export async function upsertBatch(pool: Pool, opts: UpsertOptions): Promise<number> {
  const { table, columns, conflict, rows } = opts;
  if (rows.length === 0) return 0;

  const updateColumns =
    opts.updateColumns ?? columns.filter((c) => !conflict.includes(c));
  const setClause =
    updateColumns.length > 0
      ? `DO UPDATE SET ${updateColumns.map((c) => `${c} = EXCLUDED.${c}`).join(", ")}`
      : "DO NOTHING";

  const batchSize = Math.max(1, Math.floor(MAX_PARAMS / columns.length));
  let written = 0;

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    for (let start = 0; start < rows.length; start += batchSize) {
      const batch = rows.slice(start, start + batchSize);
      const values: unknown[] = [];
      const tuples = batch.map((row, r) => {
        const placeholders = row.map((_, c) => `$${r * columns.length + c + 1}`);
        values.push(...row);
        return `(${placeholders.join(", ")})`;
      });
      await client.query(
        `INSERT INTO ${table} (${columns.join(", ")}) VALUES ${tuples.join(", ")}
         ON CONFLICT (${conflict.join(", ")}) ${setClause}`,
        values,
      );
      written += batch.length;
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
  return written;
}
