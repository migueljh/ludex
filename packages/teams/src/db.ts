// Mismo patron de interop que packages/seed/src/load/client.ts: pg asigna sus
// propiedades dinamicamente en el constructor, asi que el import de runtime es
// el default y el de tipo es nombrado.
import pg from "pg";
import type { Pool } from "pg";

export function createPool(): Pool {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("Falta DATABASE_URL. Copiar .env.example a .env.");
  }
  return new pg.Pool({ connectionString });
}

export interface SpeciesRow {
  showdownId: string;
  name: string;
  abilities: string[];
}

/** Todas las consultas del validador, aisladas. Solo lectura. */
export class TeamData {
  constructor(private readonly pool: Pool, private readonly genId: number) {}

  static async forGen(pool: Pool, genNumber: number): Promise<TeamData> {
    const { rows } = await pool.query<{ id: number }>(
      "SELECT id FROM generations WHERE gen_number = $1", [genNumber],
    );
    if (rows.length === 0) {
      throw new Error(
        `gen ${genNumber} no esta seedeada en la base. Correr primero: pnpm seed --gen ${genNumber}`,
      );
    }
    return new TeamData(pool, rows[0].id);
  }

  async species(showdownId: string): Promise<SpeciesRow | null> {
    const { rows } = await this.pool.query<{
      showdown_id: string; name: string; abilities: Record<string, string>;
    }>(
      "SELECT showdown_id, name, abilities FROM pokemon WHERE gen_id = $1 AND showdown_id = $2",
      [this.genId, showdownId],
    );
    if (rows.length === 0) return null;
    return {
      showdownId: rows[0].showdown_id,
      name: rows[0].name,
      abilities: Object.values(rows[0].abilities),
    };
  }

  async moveExists(showdownId: string): Promise<boolean> {
    const { rows } = await this.pool.query(
      "SELECT 1 FROM moves WHERE gen_id = $1 AND showdown_id = $2",
      [this.genId, showdownId],
    );
    return rows.length > 0;
  }

  async itemExists(showdownId: string): Promise<boolean> {
    const { rows } = await this.pool.query(
      "SELECT 1 FROM items WHERE gen_id = $1 AND showdown_id = $2",
      [this.genId, showdownId],
    );
    return rows.length > 0;
  }

  async abilityExists(showdownId: string): Promise<boolean> {
    const { rows } = await this.pool.query(
      "SELECT 1 FROM abilities WHERE gen_id = $1 AND showdown_id = $2",
      [this.genId, showdownId],
    );
    return rows.length > 0;
  }

  /**
   * El corazon del validador: ¿hay fila (especie, movimiento) en learnsets
   * para esta gen? Los metodos no se consultan: son para la regla de
   * legalidad del torneo (D3), otro consumidor.
   */
  async learnsMove(speciesId: string, moveId: string): Promise<boolean> {
    const { rows } = await this.pool.query(
      `SELECT 1 FROM learnsets l
       JOIN pokemon p ON p.id = l.pokemon_id
       JOIN moves m ON m.id = l.move_id
       WHERE p.gen_id = $1 AND p.showdown_id = $2 AND m.showdown_id = $3`,
      [this.genId, speciesId, moveId],
    );
    return rows.length > 0;
  }
}
