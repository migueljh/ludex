import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const tsxCli = fileURLToPath(new URL("../node_modules/tsx/dist/cli.mjs", import.meta.url));

describe("dataset-audit CLI", () => {
  it("audita una generación y muestra el resultado de las seis invariantes", () => {
    const result = spawnSync(process.execPath, [tsxCli, "src/cli.ts", "audit", "--gen", "6"], {
      cwd: packageRoot,
      encoding: "utf8",
      env: process.env,
      maxBuffer: 20 * 1024 * 1024,
    });

    expect([0, 1]).toContain(result.status);
    expect(result.stderr).toBe("");
    expect(result.stdout).toMatch(/Dataset: \d+ batallas/);
    for (const invariant of [
      "hidden_information",
      "action_turn",
      "state_rederivable",
      "reward_propagation",
      "schema_version",
      "orphans",
    ]) {
      expect(result.stdout).toContain(invariant);
    }
  }, 120_000);
});
