/** Las invariantes del dataset, auditadas sobre TODO el corpus.
 *
 * Un test que verifica propiedades del dataset corre sobre todo el dataset, no
 * sobre las filas de la corrida en curso: ya se escapó un defecto por filtrar
 * `WHERE battle_tag = ANY(:tags)`. Por eso el filtrado admitido es sólo el de
 * scope y generación, y se hace en el SQL, nunca escondiendo violaciones.
 */

import { matchOpponentTeam, type FieldMismatch, type OpponentContext } from "./opponent.js";
import {
  buildSpeciesIndex,
  normalizeProtocolText,
  opponentSideOf,
  type SpeciesIndex,
} from "./protocol.js";
import { BattleProjection, buildDexView, type DexView } from "./projection.js";
import type {
  AuditResult,
  BattleTurnRecord,
  Dataset,
  InvariantName,
  OpponentField,
  OpponentPokemonState,
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
    schemaVersion: step.stateSchemaVersion,
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

  // `base_species` es lo único que el índice de especies necesita del dex, y
  // es estable entre generaciones: un solo resolvedor fusionado alcanza para
  // un corpus multi-gen sin volver a recorrer el protocolo por generación.
  // Un índice POR GENERACIÓN: el dex de gen 9 no puede resolver una especie
  // dentro de una batalla de gen 6, y `gholdengo` es justamente ese caso.
  const speciesByGen = new Map<number, SpeciesIndex>();
  const speciesFor = (gen: number): SpeciesIndex => {
    let index = speciesByGen.get(gen);
    if (index === undefined) {
      index = buildSpeciesIndex(dataset.dexPokemon, gen, dataset.cosmeticFormes ?? []);
      speciesByGen.set(gen, index);
    }
    return index;
  };
  const dexViewByGen = new Map<number, DexView>();
  const dexFor = (gen: number): DexView => {
    let view = dexViewByGen.get(gen);
    if (view === undefined) {
      view = buildDexView(dataset.dexMoves, gen);
      dexViewByGen.set(gen, view);
    }
    return view;
  };

  // El protocolo se agrupa UNA vez por (batalla, lado). Nada dentro del loop de
  // pasos vuelve a recorrer `dataset.turns`: ése era el N+1 (92 277 173
  // normalizaciones sobre un corpus de 447 428 líneas).
  const turnsByBattleSide = new Map<string, BattleTurnRecord[]>();
  for (const turn of dataset.turns) {
    const key = `${turn.battleId}:${turn.playerSide}`;
    const bucket = turnsByBattleSide.get(key);
    if (bucket === undefined) turnsByBattleSide.set(key, [turn]);
    else bucket.push(turn);
  }
  const turnsWithLines = new Map<string, Set<number>>();
  for (const [key, bucket] of turnsByBattleSide) {
    bucket.sort((a, b) => a.turnNumber - b.turnNumber);
    const present = new Set<number>();
    for (const turn of bucket) {
      if (turn.protocolLines.length > 0) present.add(turn.turnNumber);
    }
    turnsWithLines.set(key, present);
  }

  const stats = {
    stepsAudited: 0,
    opponentEntriesAudited: 0,
    opponentFieldChecksRun: 0,
    protocolLinesScanned: 0,
    cursorsEvaluated: 0,
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
    // Los 11 campos rivales NO se auditan acá: una fila tiene que corresponder
    // a un instante coherente de la batalla, y eso se decide reproduciendo el
    // protocolo (`auditOpponentRows`, más abajo), no fila por fila.
    const opponent = step.state.opponent?.pokemon;
    if (!Array.isArray(opponent)) {
      at("hidden_information", "state.opponent.pokemon ausente o no es una lista");
    } else {
      let actives = 0;
      for (const pokemon of opponent) if (pokemon.active === true) actives += 1;
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
    const present = turnsWithLines.get(`${trajectory.battleId}:${trajectory.playerSide}`);
    if (present === undefined || !present.has(step.turnNumber)) {
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

  // --- 1bis. la fila describe UN instante real de la batalla ---------------
  //
  // El protocolo se reproduce una vez por (batalla, lado) y las filas se van
  // contrastando contra el estado vigente. Una fila es admisible si ALGÚN
  // cursor de su ventana `[state.turn, turn_number]` explica su equipo rival
  // entero: no se le permite a cada campo elegir un instante distinto.
  interface PendingRow {
    step: TrajectoryStepRecord;
    trajectory: TrajectoryRecord;
    battleTag: string;
    entries: OpponentPokemonState[];
    ownTeam: OpponentPokemonState[];
    from: number;
    to: number;
    resolved: boolean;
    /** Los desajustes del cursor que MEJOR explica la fila. Reportar contra el
     * último cursor de la ventana mezclaba el defecto real con el arrastre de
     * todo lo que cambió después: una fila correcta salvo el HP aparecía con
     * cinco campos rotos. */
    best?: FieldMismatch[];
    bestScore?: number;
  }
  const rowsByBattleSide = new Map<string, PendingRow[]>();
  for (const step of dataset.steps) {
    const trajectory = trajectories.get(step.trajectoryId);
    if (!trajectory) continue;
    const battle = battles.get(trajectory.battleId);
    if (!battle) continue;
    const entries = step.state.opponent?.pokemon;
    if (!Array.isArray(entries)) continue;
    const stateTurn = step.state.turn;
    // `state.turn` es el `battle.turn` capturado DENTRO de `choose_move` y
    // `turn_number` el turno en que la decisión se resolvió; `_correct_step_turns`
    // sólo puede subirlo (D20/D22/D23). El snapshot está en algún punto de ese
    // rango, y un `state.turn` inválido no puede ensanchar la ventana.
    const from = typeof stateTurn === "number" && Number.isInteger(stateTurn)
      && stateTurn >= 0 && stateTurn <= step.turnNumber
      ? stateTurn
      : step.turnNumber;
    const ownTeam = step.state.me?.pokemon;
    const row: PendingRow = {
      step,
      trajectory,
      battleTag: battle.battleTag,
      entries,
      ownTeam: Array.isArray(ownTeam) ? ownTeam : [],
      from,
      to: step.turnNumber,
      resolved: false,
    };
    const key = `${trajectory.battleId}:${trajectory.playerSide}`;
    const bucket = rowsByBattleSide.get(key);
    if (bucket === undefined) rowsByBattleSide.set(key, [row]);
    else bucket.push(row);
  }

  for (const [key, rows] of rowsByBattleSide) {
    const blocks = turnsByBattleSide.get(key);
    // Sin protocolo crudo no hay con qué contrastar: lo reporta `state_rederivable`.
    if (blocks === undefined || blocks.length === 0) continue;
    const playerSide = rows[0].trajectory.playerSide;
    const opponentSide = opponentSideOf(playerSide);
    // Las abilities de NUESTRO equipo las conoce poke-env por el request
    // privado y el protocolo no las trae. Sin ellas, un Pressure nuestro que
    // nadie narró haría que el PP esperado del rival se fuera por uno.
    const ownAbilities = new Map<string, string>();
    const ownItems = new Map<string, string | null>();
    for (const row of [...rows].sort((a, b) => a.step.decisionIndex - b.step.decisionIndex)) {
      for (const mine of row.ownTeam) {
        if (typeof mine.species !== "string") continue;
        const species = normalizeProtocolText(mine.species);
        if (typeof mine.ability === "string" && !ownAbilities.has(species)) {
          ownAbilities.set(species, normalizeProtocolText(mine.ability));
        }
        // El item propio es lo que un Trick le ENTREGA al rival: sin él, el
        // auditor no puede decir qué item tiene el rival después del canje.
        if (!ownItems.has(species) && (typeof mine.item === "string" || mine.item === null)) {
          ownItems.set(
            species,
            typeof mine.item === "string" ? normalizeProtocolText(mine.item) : null,
          );
        }
      }
    }
    const projection = new BattleProjection(
      dexFor(rows[0].trajectory.gen),
      speciesFor(rows[0].trajectory.gen),
      { side: playerSide, abilities: ownAbilities, items: ownItems },
    );
    const contextOf = (row: PendingRow): OpponentContext => ({
      projection,
      dex: dexFor(row.trajectory.gen),
      species: speciesFor(row.trajectory.gen),
      gen: row.trajectory.gen,
      opponentSide,
      playerSide: row.trajectory.playerSide,
      ownTeam: row.ownTeam,
    });
    const reportRow = (row: PendingRow, cursorTurn: number | undefined): void => {
      const mismatches = row.best
        ?? matchOpponentTeam(row.entries, contextOf(row), { limit: Infinity, collect: true }).mismatches;
      if (mismatches.length === 0) {
        row.resolved = true;
        return;
      }
      const window = row.from === row.to ? `turno ${row.to}` : `ventana ${row.from}-${row.to}`;
      const suffix = cursorTurn === undefined
        ? " (la ventana no tiene protocolo)"
        : "";
      for (const mismatch of mismatches) {
        violations.push(stepLocation(
          "hidden_information",
          `ningún instante de la ${window} explica la fila${suffix}; en el que más se le acerca: ${mismatch.detail}`,
          row.trajectory,
          row.step,
          row.battleTag,
          mismatch.field,
        ));
      }
    };

    rows.sort((a, b) => a.from - b.from || a.to - b.to);
    let next = 0;
    let active: PendingRow[] = [];
    let lastRevision = -1;
    const test = (): void => {
      if (active.length === 0 || projection.revision === lastRevision) return;
      lastRevision = projection.revision;
      stats.cursorsEvaluated += 1;
      for (const row of active) {
        if (row.resolved) continue;
        // Primero se CUENTA sin construir mensajes, y cortando apenas el
        // cursor deja de poder ser mejor que el mejor visto. Los mensajes se
        // arman una sola vez por fila, sobre el cursor que más se le acerca.
        const limit = row.bestScore ?? Infinity;
        const scored = matchOpponentTeam(row.entries, contextOf(row), { limit, collect: false });
        if (scored.count === 0) {
          row.resolved = true;
        } else if (scored.count < limit) {
          row.bestScore = scored.count;
          row.best = matchOpponentTeam(
            row.entries,
            contextOf(row),
            { limit: Infinity, collect: true },
          ).mismatches;
        }
      }
    };

    const ownIdent = `${playerSide}a:`;
    const opponentIdent = `${opponentSide}a:`;
    let lastTurn: number | undefined;
    for (const block of blocks) {
      const turn = block.turnNumber;
      lastTurn = turn;
      while (next < rows.length && rows[next].from <= turn) active.push(rows[next]!), next += 1;
      // El estado con el que ARRANCA el turno también es un cursor válido: una
      // decisión puede haberse tomado antes de que se narrara nada del turno.
      lastRevision = -1;
      test();
      for (const line of block.protocolLines) {
        stats.protocolLinesScanned += 1;
        projection.apply(line);
        // Una línea que sólo nombra a NUESTRO lado no puede haber movido el
        // estado rival: no hay cursor nuevo que probar. Las que no nombran a
        // nadie (`-clearallboost`, `|turn|`) sí se prueban.
        if (!(line.includes(ownIdent) && !line.includes(opponentIdent))) test();
      }
      const remaining: PendingRow[] = [];
      for (const row of active) {
        if (row.resolved) continue;
        if (row.to <= turn) reportRow(row, turn);
        else remaining.push(row);
      }
      active = remaining;
    }
    while (next < rows.length) active.push(rows[next]!), next += 1;
    for (const row of active) if (!row.resolved) reportRow(row, lastTurn);
    for (const row of rows) {
      stats.opponentEntriesAudited += row.entries.length;
      stats.opponentFieldChecksRun += row.entries.length * OPPONENT_FIELDS.length;
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
        schemaVersion: step.stateSchemaVersion,
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
