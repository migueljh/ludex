import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createReadOnlyPool, loadDataset } from "./db.js";
import { auditDataset } from "./invariants.js";
import { renderBattle } from "./render.js";
import type { Violation } from "./types.js";

const envPath = fileURLToPath(new URL("../../../.env", import.meta.url));
if (existsSync(envPath)) process.loadEnvFile(envPath);

function usage(): string {
  return [
    "Uso:",
    "  dataset-audit audit [--gen N]",
    "  dataset-audit battle <battle-tag|id> [--gen N]",
  ].join("\n");
}

function parseGen(args: string[]): { gen?: number; rest: string[] } {
  const rest: string[] = [];
  let gen: number | undefined;
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] !== "--gen") {
      rest.push(args[index]);
      continue;
    }
    const raw = args[index + 1];
    if (raw === undefined || !/^\d+$/.test(raw)) {
      throw new Error("--gen requiere un entero positivo");
    }
    gen = Number(raw);
    index += 1;
  }
  return { gen, rest };
}

function violationLine(violation: Violation): string {
  const location = [
    violation.battleTag,
    violation.playerSide,
    violation.turnNumber === undefined ? undefined : `turno ${violation.turnNumber}`,
    violation.decisionIndex === undefined ? undefined : `decisión ${violation.decisionIndex}`,
  ].filter((part) => part !== undefined).join(" · ");
  return `  - ${violation.invariant}${location ? ` · ${location}` : ""}: ${violation.detail}`;
}

async function main(): Promise<void> {
  const [command, ...rawArgs] = process.argv.slice(2);
  if (command !== "audit" && command !== "battle") {
    throw new Error(usage());
  }
  const { gen, rest } = parseGen(rawArgs);
  const pool = createReadOnlyPool();
  try {
    const dataset = await loadDataset(pool, gen);
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
      `Dataset: ${dataset.battles.length} batallas · ${dataset.trajectories.length} trayectorias · ${dataset.steps.length} pasos${gen === undefined ? "" : ` · gen ${gen}`}`,
    );
    for (const check of result.checks) {
      console.log(`${check.violations === 0 ? "PASS" : "FAIL"} ${check.name}: ${check.violations}`);
    }
    if (result.violations.length > 0) {
      console.log("\nViolaciones:");
      console.log(result.violations.map(violationLine).join("\n"));
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
