/** Línea de tiempo observable del lado rival, turno a turno.
 *
 * El índice de "primer turno revelado" demostraba que un dato fue público
 * alguna vez; no demostraba que el valor del snapshot fuera el correcto en ESE
 * turno. Un HP de 42 cuando el protocolo narró 55, un status ausente cuando el
 * protocolo lo aplicó, un boost de +2 cuando el protocolo narró +1: los tres
 * pasaban.
 *
 * Acá el protocolo se proyecta a un modelo con ciclo de vida —entra, se
 * daña, se cura, se debilita, sale y vuelve— y cada campo del snapshot se
 * compara contra el valor que ese modelo predice.
 *
 * Costo: `O(líneas)` para construir y `O(log n)` por consulta. Cada campo
 * guarda un LOG DE EVENTOS `(turno, valor)` en vez de una foto por turno, así
 * que la memoria es proporcional a los cambios reales, no a turnos × equipo.
 *
 * Regla de oro heredada del SKILL: ante una diferencia con poke-env gana
 * poke-env. Donde el protocolo no alcanza para derivar un valor exacto
 * —`-copyboost`, `-swapboost`— el modelo marca DESCONOCIDO y el auditor se
 * abstiene, en vez de afirmar otra cosa.
 */

export const BOOST_STATS = [
  "accuracy", "atk", "def", "evasion", "spa", "spd", "spe",
] as const;

export type Boosts = Record<string, number>;

/** Valor no derivable del protocolo público. Distinto de "ausente". */
export const UNKNOWN = Symbol("unknown");
export type Unknown = typeof UNKNOWN;

export function blankBoosts(): Boosts {
  return { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 };
}

/** Log de eventos ordenado por turno. Los turnos llegan en orden ascendente y
 * un mismo turno puede traer VARIOS valores: un turno narra una secuencia
 * (entra a 100/100, recibe 92/100, se cura a 98/100) y una decisión de
 * reemplazo forzado observa el estado A MITAD de esa secuencia. Colapsar el
 * turno a su último valor descartaba justamente los estados intermedios que
 * esas filas tienen que poder declarar. */
export interface EventLog<T> {
  turns: number[];
  values: T[];
}

export function emptyLog<T>(): EventLog<T> {
  return { turns: [], values: [] };
}

export function record<T>(log: EventLog<T>, turn: number, value: T): void {
  log.turns.push(turn);
  log.values.push(value);
}

/** Estado observable de UN miembro rival, proyectado desde el protocolo. */
export interface MonTimeline {
  hp: EventLog<number>;
  status: EventLog<string | null>;
  boosts: EventLog<Boosts | Unknown>;
  /** Turno del `|faint|`. Un debilitado no vuelve. */
  faintTurn?: number;
  /** Conteo acumulado de usos narrados, por movimiento. */
  moveUses: Map<string, EventLog<number>>;
  /** Identidad copiada por Transform (`lado:nombre`), o `null` si terminó. */
  transform: EventLog<string | null>;
  /** Niveles narrados en un `details`. */
  levels: Set<number>;
}

export function emptyMon(): MonTimeline {
  return {
    hp: emptyLog(),
    status: emptyLog(),
    boosts: emptyLog(),
    moveUses: new Map(),
    transform: emptyLog(),
    levels: new Set(),
  };
}

/** Línea de tiempo de UN lado de UNA batalla. */
export interface SideTimeline {
  /** Identidad canónica activa, por turno. */
  active: EventLog<string>;
  mons: Map<string, MonTimeline>;
}

export function emptySideTimeline(): SideTimeline {
  return { active: emptyLog(), mons: new Map() };
}

export function monOf(timeline: SideTimeline, identity: string): MonTimeline {
  let mon = timeline.mons.get(identity);
  if (mon === undefined) {
    mon = emptyMon();
    timeline.mons.set(identity, mon);
  }
  return mon;
}

/** Índice del primer evento con turno >= `turn`. */
function lowerBound(turns: readonly number[], turn: number): number {
  let low = 0;
  let high = turns.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (turns[mid] < turn) low = mid + 1;
    else high = mid;
  }
  return low;
}

/** El último valor con turno <= `turn`, o `undefined` si no hubo ninguno. */
export function valueAt<T>(log: EventLog<T>, turn: number): T | undefined {
  const index = lowerBound(log.turns, turn + 1);
  return index === 0 ? undefined : log.values[index - 1];
}

/** La ventana de turnos en la que una fila pudo observar su estado.
 *
 * `state.turn` es el `battle.turn` capturado DENTRO de `choose_move` y
 * `turn_number` es el turno en que la decisión se RESOLVIÓ; la corrección de
 * `_correct_step_turns` sólo puede subirlo (D20/D22/D23). El snapshot, por lo
 * tanto, se tomó en algún punto entre el final del turno `from - 1` y el final
 * del turno `to`. Cualquier valor que el protocolo narró dentro de esa ventana
 * es consistente; uno que no narró nunca, no.
 */
export interface TurnWindow {
  from: number;
  to: number;
}

export function turnWindow(stateTurn: unknown, turnNumber: number): TurnWindow {
  const from = typeof stateTurn === "number"
    && Number.isInteger(stateTurn)
    && stateTurn >= 0
    && stateTurn <= turnNumber
    ? stateTurn
    : turnNumber;
  return { from, to: turnNumber };
}

/** Los valores admisibles en la ventana: el que venía de antes más todos los
 * que el protocolo narró dentro. `undefined` = el protocolo no dice nada y el
 * auditor no puede afirmar nada. */
export function admissibleValues<T>(log: EventLog<T>, window: TurnWindow): T[] | undefined {
  const start = lowerBound(log.turns, window.from);
  const end = lowerBound(log.turns, window.to + 1);
  const values: T[] = [];
  if (start > 0) values.push(log.values[start - 1]);
  for (let index = start; index < end; index += 1) values.push(log.values[index]);
  return values.length === 0 ? undefined : values;
}

export function matchesWindow<T>(
  log: EventLog<T>,
  window: TurnWindow,
  observed: T,
  equals: (a: T, b: T) => boolean = Object.is,
): boolean {
  const values = admissibleValues(log, window);
  if (values === undefined) return true;
  return values.some((value) => equals(value, observed));
}

export function describeWindow<T>(
  log: EventLog<T>,
  window: TurnWindow,
  render: (value: T) => string = (value) => JSON.stringify(value),
): string {
  const values = admissibleValues(log, window);
  if (values === undefined) return "—";
  return [...new Set(values.map(render))].join(" / ");
}

export function boostsEqual(a: Boosts | Unknown, b: Boosts | Unknown): boolean {
  if (a === UNKNOWN || b === UNKNOWN) return true;
  for (const stat of BOOST_STATS) {
    if ((a[stat] ?? 0) !== (b[stat] ?? 0)) return false;
  }
  return true;
}

/** Usos narrados de un movimiento hasta el final de la ventana. Es el conteo
 * MÁS ALTO admisible, o sea el piso de PP más permisivo. */
export function usesAt(mon: MonTimeline, moveKey: string, turn: number): number {
  const log = mon.moveUses.get(moveKey);
  if (log === undefined) return 0;
  return valueAt(log, turn) ?? 0;
}
