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

  it("audita el scope training: MON-16 dejó un corpus no vacío y sano (D44/MON-29)", () => {
    // MON-16 corrió 16 batallas locales deliberadamente. De ésas, las
    // trayectorias de battle 3979/3980 (2723/2724) se retiraron a propósito
    // (checkpoint Linear de MON-16), conservando `battles`/`battle_turns` de
    // esas dos -- por eso las 16 batallas siguen contando en el dataset aunque
    // no todas aporten trayectoria. Sólo battle 3978 (trayectoria 2722) y
    // battle 3981 (trayectoria 2725) son elegibles para `training` bajo D44:
    // íntegramente `state_schema_version=2`, terminadas, `source <> 'test'`.
    // 82 pasos en total, cero violaciones. Este test fija el estado medido de
    // HOY: si el conteo cambia, hay que repinearlo a conciencia con los
    // números reales (ver `.claude/verification/SKILL.md`), nunca aflojarlo a
    // `> 0` ni volver a la aserción de corpus vacío.
    const result = run(["audit", "--scope", "training"]);
    expect(result.status).toBe(0);
    expect(result.stderr).toBe("");
    expect(result.stdout).toContain(
      "Dataset: 16 batallas · 2 trayectorias · 82 pasos · scope training",
    );
    for (const invariant of INVARIANTS) {
      expect(result.stdout).toContain(`PASS ${invariant}: 0`);
    }
    for (const field of OPPONENT_FIELDS) {
      expect(result.stdout).toContain(`PASS hidden_information/${field}: 0`);
    }
    expect(result.stdout).toMatch(/Queries: 6 \(esperadas 6\)/);
    expect(result.stdout).toMatch(/pasos auditados: 82\b/);
    // El aviso de corpus vacío (D44) es condicional a `trayectorias.length
    // === 0` en `cli.ts`; con 2 trayectorias elegibles no debe imprimirse.
    expect(result.stdout).not.toContain("corpus de entrenamiento VACÍO");
    expect(result.stdout).not.toContain("no certifican un corpus limpio");
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
