import { existsSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { GENERATION_LABELS, loadGen, packageVersion } from "./extract/dex.js";
import { extractSpecies } from "./extract/species.js";
import { extractMoves } from "./extract/moves.js";
import { extractAbilities, extractItems } from "./extract/simple.js";
import { extractTypeChart } from "./extract/typechart.js";
import { extractLearnsets } from "./extract/learnsets.js";
import { withPool } from "./load/client.js";
import {
  loadAbilities, loadItems, loadLearnsets, loadMoves, loadSpecies,
  loadTypeChart, upsertGeneration,
} from "./load/tables.js";
import { finishRun, startRun } from "./load/runs.js";

// El CLI corre en el host y necesita DATABASE_URL. Sin esto, `pnpm seed --gen 6`
// falla desde cualquier shell que no la tenga exportada a mano, que es el modo
// de falla que ya se corrigio para los tests en vitest.config.ts.
const envPath = fileURLToPath(new URL("../../../.env", import.meta.url));
if (existsSync(envPath)) process.loadEnvFile(envPath);

export async function seedGeneration(genNumber: number): Promise<Record<string, number>> {
  const dex = loadGen(genNumber);
  const label = GENERATION_LABELS[genNumber];
  const version = packageVersion();

  console.log(`Seedeando gen ${genNumber} (${label}) con pokemon-showdown@${version}`);

  const species = extractSpecies(dex);
  const moves = extractMoves(dex);
  const items = extractItems(dex);
  const abilities = extractAbilities(dex);
  const typeChart = extractTypeChart(dex);
  console.log("Resolviendo learnsets con herencia...");
  const learnsets = await extractLearnsets(dex);

  return withPool(async (pool) => {
    const genId = await upsertGeneration(pool, genNumber, label);
    const runId = await startRun(pool, genId, version);

    const counts: Record<string, number> = {
      pokemon: await loadSpecies(pool, genId, species),
      moves: await loadMoves(pool, genId, moves),
      items: await loadItems(pool, genId, items),
      abilities: await loadAbilities(pool, genId, abilities),
      typeChart: await loadTypeChart(pool, genId, typeChart),
      learnsets: await loadLearnsets(pool, genId, learnsets),
    };

    await finishRun(pool, runId, counts);
    for (const [table, n] of Object.entries(counts)) {
      console.log(`  ${table.padEnd(12)} ${n}`);
    }
    return counts;
  });
}

async function main(): Promise<void> {
  const { values } = parseArgs({ options: { gen: { type: "string" } } });
  if (!values.gen) {
    console.error("Uso: pnpm seed --gen <n>");
    process.exit(1);
  }
  await seedGeneration(Number(values.gen));
}

/**
 * Solo corre como CLI, no cuando lo importa un test. Se comparan las rutas
 * resueltas: comparar solo el nombre de archivo daria falsos positivos con
 * cualquier otro cli.ts del monorepo.
 */
const invokedPath = process.argv[1] ? realpathSync(process.argv[1]) : null;
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
