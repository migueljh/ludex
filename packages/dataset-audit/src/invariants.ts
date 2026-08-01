/** Las invariantes del dataset, auditadas sobre TODO el corpus.
 *
 * Un test que verifica propiedades del dataset corre sobre todo el dataset, no
 * sobre las filas de la corrida en curso: ya se escapó un defecto por filtrar
 * `WHERE battle_tag = ANY(:tags)`. Por eso el filtrado admitido es sólo el de
 * scope y generación, y se hace en el SQL, nunca escondiendo violaciones.
 */

import { buildDexIndex, auditOpponentPokemon, type DexIndex } from "./opponent.js";
import {
  buildProtocolIndex,
  buildSpeciesResolver,
  normalizeProtocolText,
  opponentSideOf,
} from "./protocol.js";
import type {
  AuditResult,
  Dataset,
  InvariantName,
  OpponentField,
  TrajectoryRecord,
  TrajectoryStepRecord,
  Violation,
} from "./types.js";
import { INVARIANTS, OPPONENT_FIELDS, SUPPORTED_STATE_VERSIONS } from "./types.js";

export { normalizeProtocolText };

/** Flags donde `false` y la ausencia son semánticamente equivalentes.
 *
 * `action_from_order` los agrega SÓLO cuando valen `true`, y `legal_actions`
 * sólo genera la variante `mega`. Ésta es la única normalización permitida:
 * cualquier otra diferencia entre la acción y su máscara es una violación
 * real, no ruido de formato. */
export const OPTIONAL_ACTION_FLAGS = ["mega", "z_move", "dynamax", "terastallize"] as const;

function stableKey(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(stableKey).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableKey(item)}`).join(",")}}`;
}

/** Quita los flags especiales que valen `false`, y nada más. */
export function normalizeAction(action: unknown): unknown {
  if (action === null || typeof action !== "object" || Array.isArray(action)) return action;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(action as Record<string, unknown>)) {
    if ((OPTIONAL_ACTION_FLAGS as readonly string[]).includes(key) && value === false) continue;
    out[key] = value;
  }
  return out;
}

export function actionKey(action: unknown): string {
  return stableKey(normalizeAction(action));
}

function stepLocation(
  invariant: InvariantName,
  detail: string,
  trajectory: TrajectoryRecord,
  step: TrajectoryStepRecord,
  battleTag: string,
  field?: OpponentField,
): Violation {
  return {
    invariant,
    ...(field === undefined ? {} : { field }),
    detail,
    battleTag,
    playerSide: trajectory.playerSide,
    turnNumber: step.turnNumber,
    decisionIndex: step.decisionIndex,
  };
}

/** El reward que el recorder escribe para cada resultado
 * (`cli.py::_battle_outcome` + `repository.finalize`). `null` no es "empate":
 * es "la batalla no terminó", y ahí `finalize()` nunca corre, así que el
 * reward tiene que quedar en `null`. */
export const REWARD_BY_RESULT = { win: 1, loss: -1, tie: 0 } as const;

export function auditDataset(dataset: Dataset): AuditResult {
  const violations: Violation[] = [];
  const battles = new Map(dataset.battles.map((battle) => [battle.id, battle]));
  const trajectories = new Map(
    dataset.trajectories.map((trajectory) => [trajectory.id, trajectory]),
  );

  const dexIndexByGen = new Map<number, DexIndex>();
  const dexFor = (gen: number): DexIndex => {
    let index = dexIndexByGen.get(gen);
    if (index === undefined) {
      index = buildDexIndex(dataset.dexPokemon, dataset.dexMoves, gen);
      dexIndexByGen.set(gen, index);
    }
    return index;
  };

  // `base_species` es lo único que el índice necesita del dex, y es estable
  // entre generaciones: un solo mapa fusionado alcanza para un corpus
  // multi-gen sin volver a recorrer el protocolo por generación.
  // El protocolo se recorre UNA vez, acá. Nada dentro del loop de pasos vuelve
  // a tocar `dataset.turns`: ése era el N+1 (92 277 173 normalizaciones sobre
  // un corpus de 447 428 líneas).
  const protocolIndex = buildProtocolIndex(
    dataset.turns,
    buildSpeciesResolver(dataset.dexPokemon),
  );

  const stats = {
    stepsAudited: 0,
    opponentEntriesAudited: 0,
    opponentFieldChecksRun: 0,
    protocolLinesScanned: protocolIndex.linesScanned,
  };

  const stepsByTrajectory = new Map<number, TrajectoryStepRecord[]>();
  for (const step of dataset.steps) {
    const bucket = stepsByTrajectory.get(step.trajectoryId);
    if (bucket === undefined) stepsByTrajectory.set(step.trajectoryId, [step]);
    else bucket.push(step);
  }

  for (const step of dataset.steps) {
    const trajectory = trajectories.get(step.trajectoryId);
    if (!trajectory) continue;
    const battle = battles.get(trajectory.battleId);
    if (!battle) continue;
    stats.stepsAudited += 1;
    const at = (invariant: InvariantName, detail: string, field?: OpponentField) =>
      violations.push(
        stepLocation(invariant, detail, trajectory, step, battle.battleTag, field),
      );

    // --- 1. cero fuga de información oculta -------------------------------
    const opponent = step.state.opponent?.pokemon;
    if (!Array.isArray(opponent)) {
      at("hidden_information", "state.opponent.pokemon ausente o no es una lista");
    } else {
      const evidence = protocolIndex.get(
        trajectory.battleId,
        trajectory.playerSide,
        opponentSideOf(trajectory.playerSide),
      );
      const context = {
        evidence,
        dex: dexFor(trajectory.gen),
        gen: trajectory.gen,
        turn: step.turnNumber,
      };
      let actives = 0;
      for (const pokemon of opponent) {
        stats.opponentEntriesAudited += 1;
        stats.opponentFieldChecksRun += auditOpponentPokemon(
          pokemon,
          context,
          (field, detail) => at("hidden_information", detail, field),
          (detail) => at("hidden_information", detail),
        );
        if (pokemon.active === true) actives += 1;
      }
      if (actives > 1) {
        at("hidden_information", `${actives} rivales marcados active a la vez`, "active");
      }
      if (opponent.length > 6) {
        at("hidden_information", `el rival tiene ${opponent.length} miembros; el juego permite 6`);
      }
    }

    // --- 2. la acción está dentro de su propia máscara ---------------------
    if (!Array.isArray(step.legalActions)) {
      at("action_in_mask", "legal_actions no es una lista");
    } else if (step.actionTaken === null) {
      // Un paso sin acción no le enseña nada a la política: `save_step` sólo
      // escribe `NULL` si la decisión nunca se materializó.
      at("action_in_mask", "action_taken es NULL: la fila no representa una decisión");
    } else {
      const mask = new Set(step.legalActions.map(actionKey));
      if (!mask.has(actionKey(step.actionTaken))) {
        at(
          "action_in_mask",
          `action_taken=${JSON.stringify(step.actionTaken)} no está en legal_actions (${step.legalActions.length} opciones)`,
        );
      }
    }

    // --- 3. la fila pertenece al turno en que su decisión se resolvió ------
    const stateTurn = step.state.turn;
    if (typeof stateTurn !== "number" || !Number.isInteger(stateTurn) || stateTurn < 0) {
      at("action_turn", `state.turn=${JSON.stringify(stateTurn)} no es un turno válido`);
    } else if (stateTurn > step.turnNumber) {
      // `battle.turn` capturado dentro de choose_move es SIEMPRE <= el turno
      // real (`_correct_step_turns` sólo puede subirlo, nunca bajarlo). Un
      // state.turn mayor sería un techo falso.
      at(
        "action_turn",
        `state.turn=${stateTurn} es posterior a turn_number=${step.turnNumber}`,
      );
    }

    // --- 5. rederivabilidad: protocolo crudo + fila autoconsistente -------
    const evidence = protocolIndex.get(
      trajectory.battleId,
      trajectory.playerSide,
      opponentSideOf(trajectory.playerSide),
    );
    if (evidence === undefined || !evidence.turnsWithLines.has(step.turnNumber)) {
      at("state_rederivable", "el paso no tiene protocol_lines crudas para su battle/player_side/turn");
    }
    if (stableKey(step.state.legal_actions) !== stableKey(step.legalActions)) {
      at(
        "state_rederivable",
        "state.legal_actions y la columna legal_actions no coinciden: la fila se contradice a sí misma",
      );
    }
    // `null` es omisión, no contradicción: poke-env todavía no parseó `|tier|`
    // cuando llega el primer `|request|`, así que `state.format` viene `null`
    // en la decisión 0 (medido: 431 filas, todas con decision_index=0, y
    // ninguna con un valor distinto del de la trayectoria). Omitir es
    // aceptable; afirmar otra cosa no.
    for (const [key, expectedMeta] of [
      ["gen", trajectory.gen],
      ["format", trajectory.format],
      ["player_role", trajectory.playerSide],
    ] as const) {
      const actual = step.state[key];
      if (actual !== null && actual !== undefined && actual !== expectedMeta) {
        at(
          "state_rederivable",
          `state.${key}=${JSON.stringify(actual)} contradice a la trayectoria (${String(expectedMeta)})`,
        );
      }
    }

    // --- 6. reward propagado con la semántica del recorder -----------------
    const expected = trajectory.finalResult === null
      ? null
      : REWARD_BY_RESULT[trajectory.finalResult];
    if (step.reward !== expected) {
      at(
        "reward_propagation",
        `reward=${String(step.reward)}; esperado ${String(expected)} para final_result=${String(trajectory.finalResult)}`,
      );
    }

    // --- 7. versión de esquema --------------------------------------------
    if (step.state.schema_version !== step.stateSchemaVersion) {
      at(
        "schema_version",
        `state_schema_version=${step.stateSchemaVersion} pero JSON=${String(step.state.schema_version)}`,
      );
    }
    if (!SUPPORTED_STATE_VERSIONS.includes(step.stateSchemaVersion)) {
      at(
        "schema_version",
        `state_schema_version=${step.stateSchemaVersion} no está entre las soportadas (${SUPPORTED_STATE_VERSIONS.join(", ")})`,
      );
    }
  }

  // --- 4. una decisión por decision_index --------------------------------
  for (const [trajectoryId, steps] of stepsByTrajectory) {
    const trajectory = trajectories.get(trajectoryId);
    if (!trajectory) continue;
    const battle = battles.get(trajectory.battleId);
    const battleTag = battle?.battleTag;
    const ordered = [...steps].sort((a, b) => a.decisionIndex - b.decisionIndex);
    const seen = new Set<number>();
    let previousTurn: number | undefined;
    for (const [position, step] of ordered.entries()) {
      const location = {
        invariant: "decision_index" as const,
        battleTag,
        playerSide: trajectory.playerSide,
        turnNumber: step.turnNumber,
        decisionIndex: step.decisionIndex,
      };
      if (seen.has(step.decisionIndex)) {
        violations.push({ ...location, detail: `decision_index ${step.decisionIndex} duplicado` });
      }
      seen.add(step.decisionIndex);
      if (step.decisionIndex !== position) {
        // decision_index cuenta decisiones y arranca en 0 por trayectoria. Un
        // hueco es una decisión perdida del dataset (el modo de falla de C2),
        // y se pierde en silencio si nadie lo mira.
        violations.push({
          ...location,
          detail: `decision_index ${step.decisionIndex} en la posición ${position}: la secuencia no arranca en 0 o tiene huecos`,
        });
      }
      if (previousTurn !== undefined && step.turnNumber < previousTurn) {
        // Dos decisiones pueden COMPARTIR turno (cambio forzado tras un
        // debilitamiento), pero una decisión posterior no puede resolverse en
        // un turno anterior.
        violations.push({
          ...location,
          detail: `turn_number=${step.turnNumber} retrocede respecto de la decisión anterior (turno ${previousTurn})`,
        });
      }
      previousTurn = step.turnNumber;
    }
  }

  // --- 8. huérfanos -------------------------------------------------------
  for (const trajectory of dataset.trajectories) {
    if (!battles.has(trajectory.battleId)) {
      violations.push({
        invariant: "orphans",
        detail: `trayectoria ${trajectory.id} referencia batalla inexistente ${trajectory.battleId}`,
      });
    }
  }
  for (const step of dataset.steps) {
    if (!trajectories.has(step.trajectoryId)) {
      violations.push({
        invariant: "orphans",
        detail: `paso de trayectoria ${step.trajectoryId} no tiene trayectoria`,
        turnNumber: step.turnNumber,
        decisionIndex: step.decisionIndex,
      });
    }
  }
  for (const turn of dataset.turns) {
    if (!battles.has(turn.battleId)) {
      violations.push({
        invariant: "orphans",
        detail: `turno sin batalla: battle_id=${turn.battleId}, side=${turn.playerSide}, turn=${turn.turnNumber}`,
        playerSide: turn.playerSide,
        turnNumber: turn.turnNumber,
      });
    }
  }

  const byInvariant = new Map<InvariantName, number>();
  const byField = new Map<OpponentField, number>();
  for (const violation of violations) {
    byInvariant.set(violation.invariant, (byInvariant.get(violation.invariant) ?? 0) + 1);
    if (violation.field !== undefined) {
      byField.set(violation.field, (byField.get(violation.field) ?? 0) + 1);
    }
  }

  return {
    checks: INVARIANTS.map((name) => ({ name, violations: byInvariant.get(name) ?? 0 })),
    opponentFields: OPPONENT_FIELDS.map((name) => ({
      name,
      violations: byField.get(name) ?? 0,
    })),
    violations,
    stats,
  };
}
