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

export async function finishRun(
  pool: Pool, runId: number, counts: Record<string, number>,
): Promise<void> {
  await pool.query(
    `UPDATE seed_runs SET finished_at = now(), row_counts = $2 WHERE id = $1`,
    [runId, JSON.stringify(counts)],
  );
}
