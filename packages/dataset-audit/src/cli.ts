import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createReadOnlyPool, EXPECTED_QUERY_COUNT, loadDataset, type Queryable } from "./db.js";
import { auditDataset } from "./invariants.js";
import { renderBattle } from "./render.js";
import { parseScope, SCOPE_RULES } from "./scope.js";
import type { Scope, Violation } from "./types.js";
import { SCOPES } from "./types.js";

const envPath = fileURLToPath(new URL("../../../.env", import.meta.url));
if (existsSync(envPath)) process.loadEnvFile(envPath);

/** Cuántos ejemplos se imprimen por invariante. El TOTAL siempre se informa
 * completo: esto acota la salida, no la evidencia. */
const EXAMPLES_PER_INVARIANT = 20;

function usage(): string {
  return [
    "Uso:",
    `  dataset-audit audit [--scope ${SCOPES.join("|")}] [--gen N]`,
    "  dataset-audit battle <battle-tag|id> [--scope ...] [--gen N]",
    "",
    "Scopes:",
    ...SCOPES.flatMap((scope) => [
      `  ${scope}`,
      ...SCOPE_RULES[scope].map((rule) => `    - ${rule}`),
    ]),
  ].join("\n");
}

interface ParsedArgs {
  scope: Scope;
  gen?: number;
  rest: string[];
}

function parseArgs(args: string[]): ParsedArgs {
  const rest: string[] = [];
  let gen: number | undefined;
  let scope: string | undefined;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--gen") {
      const raw = args[index + 1];
      if (raw === undefined || !/^\d+$/.test(raw)) {
        throw new Error("--gen requiere un entero positivo");
      }
      gen = Number(raw);
      index += 1;
      continue;
    }
    if (arg === "--scope") {
      scope = args[index + 1];
      if (scope === undefined) throw new Error(`--scope requiere un valor (${SCOPES.join(", ")})`);
      index += 1;
      continue;
    }
    rest.push(arg);
  }
  return { scope: parseScope(scope), gen, rest };
}

function violationLine(violation: Violation): string {
  const location = [
    violation.battleTag,
    violation.playerSide,
    violation.turnNumber === undefined ? undefined : `turno ${violation.turnNumber}`,
    violation.decisionIndex === undefined ? undefined : `decisión ${violation.decisionIndex}`,
  ].filter((part) => part !== undefined).join(" · ");
  const name = violation.field === undefined
    ? violation.invariant
    : `${violation.invariant}/${violation.field}`;
  return `  - ${name}${location ? ` · ${location}` : ""}: ${violation.detail}`;
}

async function main(): Promise<void> {
  const [command, ...rawArgs] = process.argv.slice(2);
  if (command !== "audit" && command !== "battle") {
    throw new Error(usage());
  }
  const { scope, gen, rest } = parseArgs(rawArgs);
  const pool = createReadOnlyPool();
  // El auditor es read-only por diseño; el contador existe para poder
  // publicar la medición junto al resultado, no para cambiar el resultado.
  let queries = 0;
  const counting: Queryable = {
    query: async (text: string, values?: unknown[]) => {
      queries += 1;
      return await pool.query(text, values as unknown[]);
    },
  };
  const startedAt = Date.now();
  try {
    const dataset = await loadDataset(counting, { scope, gen });
    if (command === "battle") {
      if (rest.length !== 1) throw new Error(usage());
      const rawSelector = rest[0];
      const selector = /^\d+$/.test(rawSelector) ? Number(rawSelector) : rawSelector;
      console.log(renderBattle(dataset, selector));
      return;
    }
    if (rest.length !== 0) throw new Error(usage());

    const result = auditDataset(dataset);
    console.log(
      `Dataset: ${dataset.battles.length} batallas · ${dataset.trajectories.length} trayectorias · ${dataset.steps.length} pasos · scope ${scope}${gen === undefined ? "" : ` · gen ${gen}`}`,
    );
    for (const check of result.checks) {
      console.log(`${check.violations === 0 ? "PASS" : "FAIL"} ${check.name}: ${check.violations}`);
      if (check.name !== "hidden_information") continue;
      for (const field of result.opponentFields) {
        console.log(
          `  ${field.violations === 0 ? "PASS" : "FAIL"} hidden_information/${field.name}: ${field.violations}`,
        );
      }
    }
    console.log(
      `\nQueries: ${queries} (esperadas ${EXPECTED_QUERY_COUNT}) · líneas de protocolo indexadas: ${result.stats.protocolLinesScanned} · pasos auditados: ${result.stats.stepsAudited} · entradas rivales: ${result.stats.opponentEntriesAudited} · chequeos de campo: ${result.stats.opponentFieldChecksRun} · ${((Date.now() - startedAt) / 1000).toFixed(1)} s`,
    );

    // Un auditor que no recorrió ningún paso no puede afirmar nada: falla
    // ruidoso en vez de reportar todo en PASS.
    if (dataset.steps.length > 0 && result.stats.stepsAudited === 0) {
      throw new Error("el auditor no visitó ningún paso pese a que el dataset tiene filas");
    }

    // Split por versión de esquema: v1 y v2 conviven en el mismo corpus y sus
    // defectos tienen causas distintas (v2 nace con D31). Un total agregado
    // esconde de quién es cada violación.
    const versions = [...new Set(dataset.steps.map((step) => step.stateSchemaVersion))].sort();
    if (versions.length > 0) {
      console.log("\nSplit por state_schema_version:");
      for (const version of versions) {
        const rows = dataset.steps.filter((step) => step.stateSchemaVersion === version).length;
        const own = result.violations.filter((violation) => violation.schemaVersion === version);
        console.log(`  v${version}: ${rows} filas · ${own.length} violaciones`);
        const byKey = new Map<string, number>();
        for (const violation of own) {
          const key = violation.field === undefined
            ? violation.invariant
            : `${violation.invariant}/${violation.field}`;
          byKey.set(key, (byKey.get(key) ?? 0) + 1);
        }
        for (const [key, count] of [...byKey].sort((a, b) => b[1] - a[1])) {
          console.log(`    ${key}: ${count}`);
        }
      }
      const orphan = result.violations.filter((v) => v.schemaVersion === undefined).length;
      if (orphan > 0) console.log(`  sin fila asociada (huérfanos): ${orphan}`);
    }

    if (result.violations.length > 0) {
      console.log(`\nViolaciones (${result.violations.length}):`);
      const shown = new Map<string, number>();
      for (const violation of result.violations) {
        const key = violation.field === undefined
          ? violation.invariant
          : `${violation.invariant}/${violation.field}`;
        const count = shown.get(key) ?? 0;
        if (count < EXAMPLES_PER_INVARIANT) console.log(violationLine(violation));
        shown.set(key, count + 1);
      }
      for (const [key, count] of shown) {
        if (count > EXAMPLES_PER_INVARIANT) {
          console.log(`  … ${key}: ${count - EXAMPLES_PER_INVARIANT} violaciones más`);
        }
      }
      process.exitCode = 1;
    }
  } finally {
    await pool.end();
  }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
