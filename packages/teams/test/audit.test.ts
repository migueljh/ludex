import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const tsxCli = fileURLToPath(new URL("../node_modules/tsx/dist/cli.mjs", import.meta.url));

describe("learnset audit", () => {
  it("aísla las fuentes por movimiento y encuentra la familia D14", () => {
    const jsonPath = join(tmpdir(), `ludex-teams-audit-${process.pid}.json`);
    const output = execFileSync(process.execPath, [tsxCli, "src/audit.ts", jsonPath], {
      cwd: packageRoot,
      encoding: "utf8",
      env: process.env,
      maxBuffer: 10 * 1024 * 1024,
    });

    // Reutilizar PokemonSources hace que cada movimiento contamine al
    // siguiente y produce este falso positivo masivo en gen 6.
    const arceusBugGen6 = output.match(/6:arceusbug[\s\S]*?(?=\n(?:6|9):)/)?.[0] ?? "";
    expect(arceusBugGen6).not.toContain("db_extra: blastburn");

    // Casos medidos contra pokemon-showdown@0.11.10 y la base real.
    expect(output).toContain("9:ninetalesalola");
    const rows = JSON.parse(readFileSync(jsonPath, "utf8")) as Array<{
      gen: number; species: string; move: string; direction: string;
    }>;
    expect(rows).toContainEqual(expect.objectContaining({
      gen: 9, species: "ninetalesalola", move: "moonblast", direction: "db_missing",
    }));
    expect(rows).toContainEqual(expect.objectContaining({
      gen: 9, species: "ninetalesalola", move: "ember", direction: "db_extra",
    }));
  }, 120_000);
});
