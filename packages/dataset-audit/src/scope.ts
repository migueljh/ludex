import type { Scope } from "./types.js";
import { SCOPES } from "./types.js";

/** Qué excluye cada scope, en una sola frase por regla. La lista es el
 * contrato del dataset y se refleja literalmente en el SQL de `db.ts`. */
export const SCOPE_RULES: Record<Scope, readonly string[]> = {
  all: [
    "no excluye nada: incluye también las filas source='test'",
  ],
  training: [
    "excluye battles.source = 'test' (filas sintéticas de los tests de integración, "
    + "ver db/migrations/20260727000007_battle_source_test.sql)",
    "excluye trajectories.final_result IS NULL (batalla sin terminar: reward nunca "
    + "se propagó, la fila no es entrenable)",
  ],
};

export function parseScope(raw: string | undefined): Scope {
  if (raw === undefined) return "all";
  if ((SCOPES as readonly string[]).includes(raw)) return raw as Scope;
  throw new Error(`--scope desconocido: '${raw}'. Válidos: ${SCOPES.join(", ")}`);
}
