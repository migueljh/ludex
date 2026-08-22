import type { AuthorshipMix, Dataset, TrajectoryStepRecord } from "./types.js";

function actionLabel(action: TrajectoryStepRecord["actionTaken"]): string {
  if (!action) return "(sin acción)";
  const kind = typeof action.kind === "string" ? action.kind.toUpperCase() : "UNKNOWN";
  const target = typeof action.id === "string"
    ? action.id
    : typeof action.species === "string"
      ? action.species
      : JSON.stringify(action);
  const flags = ["mega", "z_move", "dynamax", "terastallize"]
    .filter((flag) => action[flag] === true)
    .map((flag) => ` +${flag}`)
    .join("");
  return `${kind} ${target}${flags}`;
}

function visibleProtocol(lines: string[]): string[] {
  return lines.filter((line) =>
    line.length > 1
    && !line.startsWith(">")
    && !line.startsWith("|request|")
    && !line.startsWith("|t:|")
  );
}

function opponentKnowledge(step: TrajectoryStepRecord): string {
  const pokemon = step.state.opponent?.pokemon;
  if (!Array.isArray(pokemon) || pokemon.length === 0) return "(ninguno)";
  return pokemon.map((entry) => {
    const species = typeof entry.species === "string" ? entry.species : "(species inválida)";
    const active = entry.active === true ? " activo" : "";
    const hp = typeof entry.hp_fraction === "number"
      ? ` HP=${Math.round(entry.hp_fraction * 100)}%`
      : "";
    const status = typeof entry.status === "string" ? ` ${entry.status}` : "";
    return `${species}${active}${hp}${status}`;
  }).join(", ");
}

export function renderBattle(dataset: Dataset, selector: string | number): string {
  const battle = dataset.battles.find((candidate) =>
    typeof selector === "number"
      ? candidate.id === selector
      : candidate.battleTag === selector
  );
  if (!battle) throw new Error(`batalla '${selector}' no encontrada`);

  // Índices armados una vez: buscar el turno con `find` dentro del loop de
  // pasos era el mismo N+1 que el chequeo de fuga.
  const turnsByKey = new Map(
    dataset.turns.map((turn) => [`${turn.battleId}:${turn.playerSide}:${turn.turnNumber}`, turn]),
  );
  const stepsByTrajectory = new Map<number, TrajectoryStepRecord[]>();
  for (const step of dataset.steps) {
    const bucket = stepsByTrajectory.get(step.trajectoryId);
    if (bucket === undefined) stepsByTrajectory.set(step.trajectoryId, [step]);
    else bucket.push(step);
  }

  const trajectories = dataset.trajectories
    .filter((trajectory) => trajectory.battleId === battle.id)
    .sort((a, b) => a.playerSide.localeCompare(b.playerSide));
  const lines = [
    `Batalla ${battle.battleTag} (#${battle.id})`,
    `${battle.p1} vs ${battle.p2} · formato ${battle.format} · source ${battle.source} · ganador ${battle.winner ?? "(sin terminar)"}`,
  ];

  if (trajectories.length === 0) {
    lines.push("", "(sin trayectoria)");
    return lines.join("\n");
  }

  for (const trajectory of trajectories) {
    const steps = [...(stepsByTrajectory.get(trajectory.id) ?? [])]
      .sort((a, b) => a.decisionIndex - b.decisionIndex);
    lines.push(
      "",
      `Trayectoria ${trajectory.id} · ${trajectory.playerSide} · gen ${trajectory.gen} · resultado ${trajectory.finalResult ?? "(pendiente)"}`,
    );
    for (const step of steps) {
      const stateTurn = turnsByKey.get(
        `${battle.id}:${trajectory.playerSide}:${step.turnNumber}`,
      );
      // Showdown entrega la primera request antes de emitir |turn|1: el
      // estado observado es turn 0, pero el resultado de esa acción queda en
      // battle_turns 1. Las decisiones posteriores usan su mismo número.
      const initialOutcome = step.turnNumber === 0
        ? turnsByKey.get(`${battle.id}:${trajectory.playerSide}:1`)
        : undefined;
      const outcomeTurn = initialOutcome ?? stateTurn;
      lines.push(
        "",
        `Turno ${step.turnNumber} · decisión ${step.decisionIndex} · ${trajectory.playerSide}`,
        `Acción: ${actionLabel(step.actionTaken)} (${step.actionSource})`,
        `Rival conocido: ${opponentKnowledge(step)}`,
        initialOutcome ? "Pasó (protocolo turno 1):" : "Pasó:",
      );
      const protocol = visibleProtocol(outcomeTurn?.protocolLines ?? []);
      if (protocol.length === 0) {
        lines.push("  (sin protocolo)");
      } else {
        lines.push(...protocol.map((line) => `  ${line}`));
      }
    }
  }
  return lines.join("\n");
}

const NO_APPROVAL_OUTCOME_LABEL = "(sin outcome)";

/** D65 (MON-31/Fase 3 S2): mezcla de autoría sobre `dataset.steps` completo.
 *
 * `dataset.steps` ya salió de la query global de `loadDataset` (scope +
 * generación aplicados en el SQL, nunca filtrado por `battle_tag` -- ver
 * AGENTS.md), así que esta función es en sí misma la consulta "sobre todo
 * el dataset" que D65 exige: no vuelve a filtrar por lote ni por corrida.
 */
export function computeAuthorshipMix(dataset: Dataset): AuthorshipMix {
  const bySource: Record<string, number> = {};
  const byApprovalOutcome: Record<string, number> = {};
  for (const step of dataset.steps) {
    bySource[step.actionSource] = (bySource[step.actionSource] ?? 0) + 1;
    const outcomeKey = step.approvalOutcome ?? NO_APPROVAL_OUTCOME_LABEL;
    byApprovalOutcome[outcomeKey] = (byApprovalOutcome[outcomeKey] ?? 0) + 1;
  }
  return { bySource, byApprovalOutcome, total: dataset.steps.length };
}

function formatCounts(counts: Record<string, number>): string[] {
  return Object.entries(counts)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, count]) => `  ${key}: ${count}`);
}

/** Reporte legible de la mezcla `agent`/`human`/`opponent` y de los tres
 * outcomes de aprobación, sobre TODO el dataset cargado (D65 5.3: "el
 * auditor reporta mezcla agent/human y los tres outcomes"). */
export function renderAuthorshipReport(dataset: Dataset): string {
  const mix = computeAuthorshipMix(dataset);
  const lines = [
    `Autoría de ${mix.total} decisiones`,
    "",
    "Por action_source:",
    ...formatCounts(mix.bySource),
    "",
    "Por approval_outcome:",
    ...formatCounts(mix.byApprovalOutcome),
  ];
  return lines.join("\n");
}
