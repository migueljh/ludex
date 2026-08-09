import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { INVARIANTS, OPPONENT_FIELDS } from "../src/types.js";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const tsxCli = fileURLToPath(new URL("../node_modules/tsx/dist/cli.mjs", import.meta.url));

function run(
  args: string[],
  extraEnv: Record<string, string> = {},
): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync(process.execPath, [tsxCli, "src/cli.ts", ...args], {
    cwd: packageRoot,
    encoding: "utf8",
    env: { ...process.env, ...extraEnv },
    maxBuffer: 64 * 1024 * 1024,
  });
  return { status: result.status, stdout: result.stdout, stderr: result.stderr };
}

describe("dataset-audit CLI", () => {
  it("audita el scope all y reporta las ocho invariantes y los 11 campos", () => {
    const result = run(["audit", "--scope", "all"]);
    // 0 = sin violaciones, 1 = violaciones encontradas. Cualquier otra cosa
    // es un crash y no un veredicto.
    expect([0, 1]).toContain(result.status);
    expect(result.stderr).toBe("");
    expect(result.stdout).toMatch(/Dataset: \d+ batallas.*scope all/);
    for (const invariant of INVARIANTS) expect(result.stdout).toContain(invariant);
    for (const field of OPPONENT_FIELDS) {
      expect(result.stdout).toContain(`hidden_information/${field}`);
    }
    // La medición se publica junto al resultado.
    expect(result.stdout).toMatch(/Queries: 6 \(esperadas 6\)/);
    expect(result.stdout).toMatch(/pasos auditados: [1-9]\d*/);
  }, 300_000);

  it("audita el scope training: hoy es un corpus VACÍO bajo D44 y lo avisa explícito", () => {
    // MON-11 R3 (D44): las 12 batallas `local` de este corpus son 100%
    // schema v1 -- ninguna trayectoria es elegible para `training` todavía.
    // Este test documenta ESE estado a propósito: si algún día deja de ser
    // 0, hay que revisar esta aserción, no relajarla. El auditor tiene que
    // avisarlo, no reportar "PASS" en silencio sobre cero filas.
    const result = run(["audit", "--scope", "training"]);
    expect([0, 1]).toContain(result.status);
    expect(result.stderr).toBe("");
    expect(result.stdout).toMatch(/Dataset: \d+ batallas · 0 trayectorias · 0 pasos · scope training/);
    for (const invariant of INVARIANTS) expect(result.stdout).toContain(invariant);
    for (const field of OPPONENT_FIELDS) {
      expect(result.stdout).toContain(`hidden_information/${field}`);
    }
    expect(result.stdout).toMatch(/Queries: 6 \(esperadas 6\)/);
    expect(result.stdout).toMatch(/pasos auditados: 0\b/);
    expect(result.stdout).toContain("corpus de entrenamiento VACÍO bajo D44");
    expect(result.stdout).toContain("no certifican un corpus limpio");
  }, 300_000);

  it("--gen conserva el mismo contrato filtrado por generación", () => {
    const result = run(["audit", "--scope", "all", "--gen", "6"]);
    expect([0, 1]).toContain(result.status);
    expect(result.stdout).toContain("scope all · gen 6");
    for (const invariant of INVARIANTS) expect(result.stdout).toContain(invariant);
  }, 300_000);

  it("sale con código 1 y explica el uso ante un scope desconocido", () => {
    const result = run(["audit", "--scope", "entrenamiento"]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("scope desconocido");
  }, 120_000);

  it("sale con código 1 ante un comando desconocido", () => {
    const result = run(["auditar"]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("Uso:");
  }, 120_000);

  it("sale con código distinto de cero si no puede resolver el vocabulario cosmético (MON-11)", () => {
    // Extremo a extremo: sin un dex real (LUDEX_SHOWDOWN_DEX_DIR apuntando
    // a la nada), el proceso completo tiene que terminar con error
    // explícito, nunca con "0 alias cosméticos" y un veredicto silencioso.
    const result = run(
      ["audit", "--scope", "training"],
      { LUDEX_SHOWDOWN_DEX_DIR: "/no/existe/este/directorio/mon11" },
    );
    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/no se encontr[oó] el dex local de poke-env/i);
  }, 120_000);
});
