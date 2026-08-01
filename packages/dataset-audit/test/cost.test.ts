/** Canarios de costo.
 *
 * El defecto que cierra F2-04 no era un crash: era que el chequeo de fuga
 * rebobinaba el prefijo de protocolo UNA VEZ POR PASO. Medido sobre el corpus
 * real: 92 277 173 normalizaciones de línea sobre un corpus de 447 428 líneas,
 * y 15 927 268 referencias de línea materializadas por `filter`+`flatMap`.
 *
 * Estos canarios fallan si alguien lo reintroduce, aunque el resultado de la
 * auditoría siga siendo correcto.
 */

import { describe, expect, it } from "vitest";
import { auditDataset } from "../src/invariants.js";
import type { Dataset } from "../src/types.js";
import { auditedStep, baseDataset } from "./fixtures.js";

/** Repite el paso auditado `n` veces SIN tocar el protocolo. */
function withSteps(count: number): Dataset {
  const dataset = baseDataset();
  const template = auditedStep(dataset);
  for (let index = dataset.steps.length; index < count; index += 1) {
    dataset.steps.push({
      ...template,
      decisionIndex: index,
      state: { ...template.state },
    });
  }
  return dataset;
}

describe("el costo de leer el protocolo no crece con trajectory_steps", () => {
  it("indexa exactamente una vez cada línea del corpus", () => {
    const dataset = baseDataset();
    const corpusLines = dataset.turns
      .reduce((total, turn) => total + turn.protocolLines.length, 0);
    expect(auditDataset(dataset).stats.protocolLinesScanned).toBe(corpusLines);
  });

  it("el mismo protocolo con 2 y con 2000 pasos se lee la MISMA cantidad de veces", () => {
    const small = auditDataset(withSteps(2));
    const large = auditDataset(withSteps(2000));

    expect(large.stats.stepsAudited).toBe(2000);
    expect(small.stats.stepsAudited).toBe(2);
    // Relación conceptual: el protocolo se lee una vez, no una por paso.
    expect(large.stats.protocolLinesScanned).toBe(small.stats.protocolLinesScanned);
    // Valor exacto: cualquier deriva en cómo se recorre el corpus se nota.
    expect(large.stats.protocolLinesScanned).toBe(13);
  });

  it("el trabajo POR PASO sí crece: si no, el dataset grande no se auditó", () => {
    const large = auditDataset(withSteps(2000));
    expect(large.stats.opponentFieldChecksRun).toBe(
      large.stats.opponentEntriesAudited * 11,
    );
    expect(large.stats.opponentEntriesAudited).toBeGreaterThan(2000);
  });
});
