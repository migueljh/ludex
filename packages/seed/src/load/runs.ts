import type { Pool } from "pg";

export async function startRun(
  pool: Pool, genId: number, packageVersion: string,
): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    `INSERT INTO seed_runs (gen_id, package_version, started_at)
     VALUES ($1, $2, now()) RETURNING id`,
    [genId, packageVersion],
  );
  return rows[0].id;
}

/**
 * `upsertBatch` devuelve cuantas filas se ENVIARON en la corrida, no cuantas
 * quedaron en la tabla: el pipeline es solo-upsert (no hay DELETE en ningun
 * lado, ver D13 en docs/DECISIONS.md), asi que una fila que un bump de
 * `pokemon-showdown` deje de mandar sigue viva para siempre y un contador de
 * escrituras la esconde. `finishRun` cuenta lo que realmente hay en la tabla
 * para esa generacion, asi la discrepancia queda visible en `seed_runs`.
 */
async function realCounts(pool: Pool, genId: number): Promise<Record<string, number>> {
  const count = async (sql: string): Promise<number> => {
    const { rows } = await pool.query<{ c: string }>(sql, [genId]);
    return Number(rows[0].c);
  };
  return {
    pokemon: await count(`SELECT count(*) AS c FROM pokemon WHERE gen_id = $1`),
    moves: await count(`SELECT count(*) AS c FROM moves WHERE gen_id = $1`),
    items: await count(`SELECT count(*) AS c FROM items WHERE gen_id = $1`),
    abilities: await count(`SELECT count(*) AS c FROM abilities WHERE gen_id = $1`),
    typeChart: await count(`SELECT count(*) AS c FROM type_chart WHERE gen_id = $1`),
    learnsets: await count(
      `SELECT count(*) AS c FROM learnsets l
       JOIN pokemon p ON p.id = l.pokemon_id WHERE p.gen_id = $1`,
    ),
  };
}

export async function finishRun(
  pool: Pool, runId: number, genId: number,
): Promise<Record<string, number>> {
  const counts = await realCounts(pool, genId);
  await pool.query(
    `UPDATE seed_runs SET finished_at = now(), row_counts = $2 WHERE id = $1`,
    [runId, JSON.stringify(counts)],
  );
  return counts;
}
