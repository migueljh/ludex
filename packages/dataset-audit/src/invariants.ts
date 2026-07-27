import type {
  AuditResult,
  Dataset,
  InvariantName,
  TrajectoryRecord,
  TrajectoryStepRecord,
  Violation,
} from "./types.js";

const CHECKS: InvariantName[] = [
  "hidden_information",
  "action_turn",
  "state_rederivable",
  "reward_propagation",
  "schema_version",
  "orphans",
];

export function normalizeProtocolText(value: string): string {
  return value
    .normalize("NFD")
    .replace(/\p{Mark}/gu, "")
    .replace(/[\p{Punctuation}\p{Separator}\p{Symbol}]/gu, "")
    .toLocaleLowerCase("en-US");
}

function stepLocation(
  invariant: InvariantName,
  detail: string,
  trajectory: TrajectoryRecord,
  step: TrajectoryStepRecord,
  battleTag: string,
): Violation {
  return {
    invariant,
    detail,
    battleTag,
    playerSide: trajectory.playerSide,
    turnNumber: step.turnNumber,
    decisionIndex: step.decisionIndex,
  };
}

export function auditDataset(dataset: Dataset): AuditResult {
  const violations: Violation[] = [];
  const battles = new Map(dataset.battles.map((battle) => [battle.id, battle]));
  const trajectories = new Map(
    dataset.trajectories.map((trajectory) => [trajectory.id, trajectory]),
  );
  const turnsByBattleSide = new Map<string, typeof dataset.turns>();

  for (const turn of dataset.turns) {
    const key = `${turn.battleId}:${turn.playerSide}`;
    const existing = turnsByBattleSide.get(key) ?? [];
    existing.push(turn);
    turnsByBattleSide.set(key, existing);
  }

  for (const step of dataset.steps) {
    const trajectory = trajectories.get(step.trajectoryId);
    if (!trajectory) continue;
    const battle = battles.get(trajectory.battleId);
    if (!battle) continue;
    const location = (invariant: InvariantName, detail: string) =>
      stepLocation(invariant, detail, trajectory, step, battle.battleTag);
    const turns = turnsByBattleSide.get(
      `${trajectory.battleId}:${trajectory.playerSide}`,
    ) ?? [];
    const ownTurn = turns.find((turn) => turn.turnNumber === step.turnNumber);

    const opponent = step.state.opponent?.pokemon;
    if (Array.isArray(opponent)) {
      const linesThroughTurn = turns
        .filter((turn) => turn.turnNumber <= step.turnNumber)
        .flatMap((turn) => turn.protocolLines);
      for (const pokemon of opponent) {
        if (typeof pokemon.species !== "string" || pokemon.species.length === 0) {
          violations.push(location(
            "hidden_information",
            "opponent.pokemon contiene species ausente o no textual",
          ));
          continue;
        }
        const normalizedSpecies = normalizeProtocolText(pokemon.species);
        const revealed = linesThroughTurn.some((line) =>
          normalizeProtocolText(line).includes(normalizedSpecies)
        );
        if (!revealed) {
          violations.push(location(
            "hidden_information",
            `oponente '${pokemon.species}' aparece en state antes de ser revelado por el protocolo`,
          ));
        }
      }
    }

    if (step.state.turn !== step.turnNumber) {
      violations.push(location(
        "action_turn",
        `fila turn_number=${step.turnNumber} pero state.turn=${String(step.state.turn)}`,
      ));
    }

    if (!ownTurn || ownTurn.protocolLines.length === 0) {
      violations.push(location(
        "state_rederivable",
        "el paso no tiene protocol_lines crudas para su battle/player_side/turn",
      ));
    }

    if (trajectory.finalResult !== null) {
      const expected = trajectory.finalResult === "win" ? 1 : -1;
      if (step.reward !== expected) {
        violations.push(location(
          "reward_propagation",
          `reward=${String(step.reward)}; esperado ${expected} para final_result=${trajectory.finalResult}`,
        ));
      }
    }

    if (step.state.schema_version !== step.stateSchemaVersion) {
      violations.push(location(
        "schema_version",
        `state_schema_version=${step.stateSchemaVersion} pero JSON=${String(step.state.schema_version)}`,
      ));
    }
  }

  const versions = [...new Set(dataset.steps.map((step) => step.stateSchemaVersion))]
    .sort((a, b) => a - b);
  if (versions.length > 1) {
    violations.push({
      invariant: "schema_version",
      detail: `hay versiones de esquema mezcladas: ${versions.join(", ")}`,
    });
  }

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

  return {
    checks: CHECKS.map((name) => ({
      name,
      violations: violations.filter((violation) => violation.invariant === name).length,
    })),
    violations,
  };
}
