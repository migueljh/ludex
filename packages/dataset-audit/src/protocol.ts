/** Lectura del protocolo crudo: evidencia de revelación + línea de tiempo.
 *
 * El protocolo es la fuente de verdad del estado (D17). Este módulo lo lee UNA
 * sola vez por corrida y produce dos cosas distintas, porque responden
 * preguntas distintas:
 *
 *  - `SideEvidence` — "¿esto fue público alguna vez, hasta el turno T?".
 *    Sirve para lo que el protocolo revela pero no vuelve a mencionar: item,
 *    ability, especie, existencia de un movimiento.
 *  - `SideTimeline` — "¿cuál era el VALOR en el turno T?". Sirve para todo lo
 *    que tiene ciclo de vida: HP, activo, status, debilitado, boosts, PP,
 *    Transform. Sin esto, "revelado alguna vez" deja pasar un valor falso.
 *
 * Reglas del SKILL que este módulo respeta a propósito:
 *
 *  - Se compara LÍNEA POR LÍNEA, nunca sobre el protocolo concatenado.
 *  - Se compara por TOKEN, no por substring de la línea entera.
 *  - La identidad de un miembro es su `base_species` (`Pokemon.identifies_as`).
 *  - La normalización saca TODA la puntuación y los diacríticos.
 */

import {
  blankBoosts,
  emptyLog,
  emptySideTimeline,
  monOf,
  record,
  UNKNOWN,
  type Boosts,
  type EventLog,
  type SideTimeline,
  type Unknown,
} from "./timeline.js";
import type { BattleTurnRecord, DexPokemon } from "./types.js";

export function normalizeProtocolText(value: string): string {
  return value
    .normalize("NFD")
    .replace(/\p{Mark}/gu, "")
    .replace(/[\p{Punctuation}\p{Separator}\p{Symbol}]/gu, "")
    .toLocaleLowerCase("en-US");
}

/** Hidden Power: poke-env nombra la acción con el tipo (`hiddenpowerice`)
 * pero Showdown narra sólo `Hidden Power`. El recorte es específico —hay 17
 * Hidden Power que comparten el id base—, no una regla genérica de prefijos. */
export function moveEvidenceKeys(moveId: string): string[] {
  const normalized = normalizeProtocolText(moveId);
  return normalized.startsWith("hiddenpower") && normalized !== "hiddenpower"
    ? [normalized, "hiddenpower"]
    : [normalized];
}

export interface ProtocolIdent {
  side: string;
  name: string;
}

/** `p2a: Yanmega` -> `{ side: "p2", name: "yanmega" }`. */
export function parseIdent(raw: string | undefined): ProtocolIdent | undefined {
  if (raw === undefined) return undefined;
  const match = /^(p[1-9])[a-z]?:\s*(.+)$/i.exec(raw.trim());
  if (!match) return undefined;
  const name = normalizeProtocolText(match[2]);
  return name.length === 0 ? undefined : { side: match[1].toLowerCase(), name };
}

/** `Yanmega, L82, F` -> especie y nivel.
 *
 * El nivel NUNCA queda indefinido para un `details` no vacío: Showdown omite
 * `L100` y `_level_from_details` del recorder devuelve 100 en ese caso. Por
 * eso el auditor puede exigir que `level` sea uno de los niveles narrados, sin
 * ninguna excepción "implícita" que un valor falso pueda aprovechar.
 */
export function parseDetails(raw: string | undefined): {
  species: string;
  level: number;
} | undefined {
  if (raw === undefined) return undefined;
  const parts = raw.split(",").map((part) => part.trim());
  const species = normalizeProtocolText(parts[0] ?? "");
  if (species.length === 0) return undefined;
  for (const part of parts.slice(1)) {
    const match = /^L(\d+)$/i.exec(part);
    if (match) return { species, level: Number(match[1]) };
  }
  return { species, level: 100 };
}

/** `55/100 par`, `0 fnt`, `100/100`. */
export function parseHpToken(raw: string | undefined): {
  fraction: number;
  status: string | null;
  fainted: boolean;
} | undefined {
  if (raw === undefined) return undefined;
  const token = raw.trim();
  const match = /^(\d+)(?:\/(\d+))?(?:\s+([a-z]+))?/i.exec(token);
  if (!match) return undefined;
  const current = Number(match[1]);
  const total = match[2] === undefined ? undefined : Number(match[2]);
  if (total !== undefined && total === 0) return undefined;
  const fraction = total === undefined ? (current === 0 ? 0 : 1) : current / total;
  const rawStatus = match[3]?.toLowerCase() ?? null;
  const fainted = rawStatus === "fnt" || current === 0;
  return { fraction, status: fainted ? "fnt" : rawStatus, fainted };
}

const REVEAL_TAGS = new Set(["switch", "drag", "replace", "detailschange"]);
/** Sólo estas ENTRAN al campo. `detailschange` es un cambio de forma sobre el
 * pokémon que ya está adentro (una Mega): no limpia boosts ni termina un
 * Transform, y confundirlo con una entrada borraba boosts públicos reales
 * (medido: 32 filas v2, todas Mega). `replace` (Illusion) revela quién estaba
 * realmente en el campo y hereda su HP y su status, así que tampoco reinicia
 * los boosts de la posición. */
const SWITCH_IN_TAGS = new Set(["switch", "drag"]);
const HP_TAGS = new Set(["-damage", "-heal", "-sethp"]);
/** Etiquetas que cambian UN stat nombrado en `parts[3]`. */
const BOOST_TAGS_WITH_STAT = new Set(["-boost", "-unboost", "-setboost"]);
const TYPE_CHANGE_TAGS = new Set(["-transform", "detailschange", "-formechange", "replace"]);

/** Evidencia de revelación: primer turno en que cada hecho se hizo público. */
export interface SideEvidence {
  /** Especie narrada VERBATIM en un `details`. */
  speciesExact: Map<string, number>;
  /** Especie o su base, para la identidad canónica. */
  species: Map<string, number>;
  move: Map<string, number>;
  item: Map<string, number>;
  ability: Map<string, number>;
  status: Map<string, number>;
  typeChange: Map<string, number>;
  /** `|rule|HP Percentage Mod`: el HP rival se narra en centésimos. */
  hpPercentageMod: boolean;
  turnsWithLines: Set<number>;
}

export interface SideView {
  evidence: SideEvidence;
  timeline: SideTimeline;
}

export interface ProtocolIndex {
  get(battleId: number, playerSide: string, opponentSide: string): SideView | undefined;
  linesScanned: number;
}

function emptyEvidence(): SideEvidence {
  return {
    speciesExact: new Map(),
    species: new Map(),
    move: new Map(),
    item: new Map(),
    ability: new Map(),
    status: new Map(),
    typeChange: new Map(),
    hpPercentageMod: false,
    turnsWithLines: new Set(),
  };
}

function remember(map: Map<string, number>, key: string, turn: number): void {
  const previous = map.get(key);
  if (previous === undefined || turn < previous) map.set(key, turn);
}

/** ¿Este hecho ya era público en el turno `turn`? */
export function revealedBy(
  map: Map<string, number>,
  keys: readonly string[],
  turn: number,
): boolean {
  for (const key of keys) {
    const first = map.get(key);
    if (first !== undefined && first <= turn) return true;
  }
  return false;
}

/** Resuelve un id de especie contra el dex local. */
export type SpeciesResolver = (speciesId: string) => DexPokemon | undefined;

/** Construye el resolvedor.
 *
 * LÍMITE MEDIDO del dex local: la tabla `pokemon` no trae las formas
 * *cosméticas* de Showdown (Furfrou-Pharaoh, Florges-Blue, Sawsbuck-Autumn,
 * Gastrodon-East, las 28 Unown), que en el dex oficial comparten la entrada de
 * su especie base. poke-env sí las resuelve, así que el estado grabado las
 * nombra y una búsqueda exacta las deja sin dex.
 *
 * La resolución de respaldo es el prefijo MÁS LARGO del dex que sea prefijo
 * del id buscado, que es exactamente cómo Showdown forma el id de una forma
 * cosmética (`furfrou` + `pharaoh`). Sigue anclada al dex: no hay ninguna
 * lista de especies escrita a mano.
 *
 * Es resolución, NO validación: `resolvedExactly` distingue las dos, porque un
 * sufijo inventado (`furfroubanana`) también resuelve a `furfrou`. Quien
 * valide una especie tiene que exigir, además, que el protocolo la haya
 * narrado verbatim.
 */
export interface SpeciesIndex {
  resolve: SpeciesResolver;
  /** ¿El dex conoce este id exactamente, sin caer al prefijo? */
  resolvedExactly: (speciesId: string) => boolean;
}

export function buildSpeciesIndex(entries: Iterable<DexPokemon>): SpeciesIndex {
  const exact = new Map<string, DexPokemon>();
  for (const entry of entries) exact.set(normalizeProtocolText(entry.showdownId), entry);
  const byLengthDesc = [...exact.keys()].sort((a, b) => b.length - a.length);
  const cache = new Map<string, DexPokemon | undefined>();
  return {
    resolve: (speciesId: string) => {
      const normalized = normalizeProtocolText(speciesId);
      const hit = exact.get(normalized);
      if (hit !== undefined) return hit;
      if (cache.has(normalized)) return cache.get(normalized);
      let resolved: DexPokemon | undefined;
      for (const candidate of byLengthDesc) {
        if (candidate.length < normalized.length && normalized.startsWith(candidate)) {
          resolved = exact.get(candidate);
          break;
        }
      }
      cache.set(normalized, resolved);
      return resolved;
    },
    resolvedExactly: (speciesId: string) => exact.has(normalizeProtocolText(speciesId)),
  };
}

export function buildSpeciesResolver(entries: Iterable<DexPokemon>): SpeciesResolver {
  return buildSpeciesIndex(entries).resolve;
}

/** Identidad canónica de una especie: su `base_species` según el dex local.
 * Es el criterio de `Pokemon.identifies_as`, no una comparación por `species`
 * (que rompe con toda forma alternativa: Arceus-Poison, Rotom-Wash). */
export function canonicalIdentity(species: string, resolve: SpeciesResolver): string {
  const normalized = normalizeProtocolText(species);
  const entry = resolve(normalized);
  return entry ? normalizeProtocolText(entry.baseSpecies) : normalized;
}

/** Claves con las que buscar evidencia de revelación: la especie tal cual y su
 * identidad canónica. */
export function identityKeys(species: string, resolve: SpeciesResolver): string[] {
  const normalized = normalizeProtocolText(species);
  const base = canonicalIdentity(normalized, resolve);
  return base === normalized ? [normalized] : [normalized, base];
}

const SUFFIX_ITEM = /(?:\[from\]\s*)?item:\s*([^|]+)/i;
const SUFFIX_ABILITY = /(?:\[from\]\s*)?ability:\s*([^|]+)/i;
const SUFFIX_OF = /\[of\]\s*(p[1-9][a-z]?:[^|]+)/i;
/** Causas que INTERCAMBIAN o roban un item entre dos pokémon. */
const SWAP_CAUSE = /(?:move:\s*(?:Trick|Switcheroo|Thief|Covet)|ability:\s*(?:Magician|Pickpocket))/i;

/** Quién es el dueño real del item/ability que revela un sufijo.
 *
 * Reproduce la semántica del `[of]` que poke-env 0.15.0 codifica en sus cuatro
 * helpers de `-damage`/`-heal` (`abstract_battle.py:333-403`) y en su rama
 * `-item` (`:949-989`), que el proyector del recorder ya replica.
 */
function suffixOwner(
  tag: string,
  ident: ProtocolIdent,
  line: string,
  kind: "item" | "ability",
  value: string,
): ProtocolIdent {
  const of = parseIdent(SUFFIX_OF.exec(line)?.[1]);
  if (of === undefined) return ident;
  // `-ability`: el `[of]` es el pokémon TRAZADO, no el dueño de Trace.
  if (tag === "-ability") return ident;
  // `-heal` por ability: el `[of]` nombra a quien lanzó el ataque absorbido,
  // no a quien tiene la ability. Hospitality es la única que sí viene de otro.
  if (tag === "-heal" && kind === "ability") return value === "hospitality" ? of : ident;
  // `-item`: Frisk la tiene el `[of]` (el que espía); Pickpocket y Magician,
  // el ident (el que roba).
  if (tag === "-item" && kind === "ability") {
    return value === "pickpocket" || value === "magician" ? ident : of;
  }
  // Resto: el `[of]` es la CAUSA, y la causa es quien tiene el item/ability.
  return of;
}

function currentBoosts(log: EventLog<Boosts | Unknown>): Boosts | Unknown {
  return log.values.length === 0 ? blankBoosts() : log.values[log.values.length - 1];
}

/** Construye evidencia y línea de tiempo en UNA pasada sobre el corpus. */
export function buildProtocolIndex(
  turns: readonly BattleTurnRecord[],
  species: SpeciesIndex | SpeciesResolver,
): ProtocolIndex {
  const resolve: SpeciesResolver = typeof species === "function" ? species : species.resolve;
  const byBattleSide = new Map<string, BattleTurnRecord[]>();
  for (const turn of turns) {
    const key = `${turn.battleId}:${turn.playerSide}`;
    const bucket = byBattleSide.get(key);
    if (bucket === undefined) byBattleSide.set(key, [turn]);
    else bucket.push(turn);
  }

  const views = new Map<string, SideView>();
  let linesScanned = 0;

  for (const [key, bucket] of byBattleSide) {
    bucket.sort((a, b) => a.turnNumber - b.turnNumber);
    const perSide = new Map<string, SideView>();
    /** ident del protocolo -> identidad canónica conocida hasta ahora. */
    const identCanon = new Map<string, Map<string, string>>();
    const ensure = (side: string): SideView => {
      let view = perSide.get(side);
      if (view === undefined) {
        view = { evidence: emptyEvidence(), timeline: emptySideTimeline() };
        perSide.set(side, view);
        identCanon.set(side, new Map());
      }
      return view;
    };
    for (const side of ["p1", "p2"]) ensure(side);

    /** La identidad canónica que este ident nombra hoy. En random battles el
     * apodo ES el nombre base, así que el fallback coincide. */
    const canonOf = (ident: ProtocolIdent): string =>
      identCanon.get(ident.side)?.get(ident.name) ?? ident.name;

    let lastSwap: [ProtocolIdent, ProtocolIdent] | undefined;

    for (const turn of bucket) {
      const turnNumber = turn.turnNumber;
      if (turn.protocolLines.length > 0) {
        for (const view of perSide.values()) view.evidence.turnsWithLines.add(turnNumber);
      }
      for (const line of turn.protocolLines) {
        linesScanned += 1;
        if (line.length === 0 || line.charCodeAt(0) !== 124 /* | */) continue;
        const parts = line.split("|");
        const tag = parts[1] ?? "";

        if (tag === "rule") {
          if (/^HP Percentage Mod\b/i.test(parts[2] ?? "")) {
            for (const side of ["p1", "p2"]) ensure(side).evidence.hpPercentageMod = true;
          }
          continue;
        }
        // `-clearallboost` no lleva ident: afecta a los dos lados. Va antes
        // del filtro por ident, que si no lo descartaría en silencio.
        if (tag === "-clearallboost") {
          for (const view of perSide.values()) {
            for (const mon of view.timeline.mons.values()) {
              record(mon.boosts, turnNumber, blankBoosts());
            }
          }
          continue;
        }

        const ident = parseIdent(parts[2]);
        if (ident === undefined) continue;
        const view = ensure(ident.side);
        const { evidence, timeline } = view;
        const canonBySide = identCanon.get(ident.side)!;

        if (REVEAL_TAGS.has(tag) || tag === "-formechange") {
          // `-formechange` trae sólo la especie donde los otros traen el
          // `details` completo; `parseDetails` cubre las dos formas.
          const details = parseDetails(parts[3]);
          if (details !== undefined) {
            const identity = canonicalIdentity(details.species, resolve);
            canonBySide.set(ident.name, identity);
            remember(evidence.speciesExact, details.species, turnNumber);
            remember(evidence.species, details.species, turnNumber);
            remember(evidence.species, identity, turnNumber);
            const mon = monOf(timeline, identity);
            mon.levels.add(details.level);

            if (tag === "replace") record(timeline.active, turnNumber, identity);
            if (SWITCH_IN_TAGS.has(tag)) {
              // Entra al campo: el anterior sale, y salir del campo LIMPIA
              // boosts y termina el Transform (`Pokemon.switch_out`).
              const previous = timeline.active.values[timeline.active.values.length - 1];
              if (previous !== undefined && previous !== identity) {
                const leaving = monOf(timeline, previous);
                record(leaving.boosts, turnNumber, blankBoosts());
                record(leaving.transform, turnNumber, null);
              }
              record(timeline.active, turnNumber, identity);
              record(mon.boosts, turnNumber, blankBoosts());
              record(mon.transform, turnNumber, null);
            }
            const hp = parseHpToken(parts[4]);
            if (hp !== undefined) {
              record(mon.hp, turnNumber, hp.fraction);
              record(mon.status, turnNumber, hp.status);
              if (hp.status !== null) {
                remember(evidence.status, `${identity}|${hp.status}`, turnNumber);
              }
              if (hp.fainted) mon.faintTurn ??= turnNumber;
            }
          }
        }

        const identity = canonOf(ident);
        const mon = monOf(timeline, identity);

        if (TYPE_CHANGE_TAGS.has(tag)) remember(evidence.typeChange, identity, turnNumber);
        if (tag === "-start" && /^typechange$/i.test(parts[3] ?? "")) {
          remember(evidence.typeChange, identity, turnNumber);
        }

        if (HP_TAGS.has(tag)) {
          const hp = parseHpToken(parts[3]);
          if (hp !== undefined) {
            record(mon.hp, turnNumber, hp.fraction);
            record(mon.status, turnNumber, hp.status);
            if (hp.status !== null) {
              remember(evidence.status, `${identity}|${hp.status}`, turnNumber);
            }
            if (hp.fainted) mon.faintTurn ??= turnNumber;
          }
        }
        if (tag === "faint") {
          mon.faintTurn ??= turnNumber;
          record(mon.hp, turnNumber, 0);
          record(mon.status, turnNumber, "fnt");
          remember(evidence.status, `${identity}|fnt`, turnNumber);
        }
        if (tag === "-status") {
          const status = normalizeProtocolText(parts[3] ?? "");
          if (status.length > 0) {
            record(mon.status, turnNumber, status);
            remember(evidence.status, `${identity}|${status}`, turnNumber);
          }
        }
        if (tag === "-curestatus") {
          record(mon.status, turnNumber, null);
        }
        if (tag === "-cureteam") {
          for (const teammate of timeline.mons.values()) {
            if (teammate.faintTurn === undefined) record(teammate.status, turnNumber, null);
          }
        }

        if (BOOST_TAGS_WITH_STAT.has(tag)) {
          const stat = normalizeProtocolText(parts[3] ?? "");
          const amount = Number(parts[4]);
          const current = currentBoosts(mon.boosts);
          if (stat.length > 0 && Number.isFinite(amount) && current !== UNKNOWN) {
            const next = { ...current };
            if (tag === "-setboost") next[stat] = amount;
            else {
              const delta = tag === "-boost" ? amount : -amount;
              next[stat] = Math.max(-6, Math.min(6, (next[stat] ?? 0) + delta));
            }
            record(mon.boosts, turnNumber, next);
          }
        }
        if (tag === "-clearboost") record(mon.boosts, turnNumber, blankBoosts());
        if (tag === "-clearnegativeboost" || tag === "-clearpositiveboost" || tag === "-invertboost") {
          const current = currentBoosts(mon.boosts);
          if (current !== UNKNOWN) {
            const next = { ...current };
            for (const [stat, value] of Object.entries(next)) {
              if (tag === "-invertboost") next[stat] = -value;
              else if (tag === "-clearnegativeboost" && value < 0) next[stat] = 0;
              else if (tag === "-clearpositiveboost" && value > 0) next[stat] = 0;
            }
            record(mon.boosts, turnNumber, next);
          }
        }
        if (tag === "-copyboost" || tag === "-swapboost") {
          // Necesitan los boosts del OTRO lado, que esta proyección no tiene.
          // Marcar DESCONOCIDO y abstenerse es lo correcto: el recorder mismo
          // falla cerrado en `-swapboost`.
          record(mon.boosts, turnNumber, UNKNOWN);
        }

        if (tag === "move") {
          for (const moveKey of moveEvidenceKeys(parts[3] ?? "")) {
            remember(evidence.move, `${identity}|${moveKey}`, turnNumber);
            let uses = mon.moveUses.get(moveKey);
            if (uses === undefined) {
              uses = emptyLog<number>();
              mon.moveUses.set(moveKey, uses);
            }
            const previous = uses.values.length === 0 ? 0 : uses.values[uses.values.length - 1];
            record(uses, turnNumber, previous + 1);
          }
        }

        if (tag === "-transform") {
          const target = parseIdent(parts[3]);
          record(mon.transform, turnNumber, target === undefined ? "" : `${target.side}:${target.name}`);
        }

        if (tag === "-item" || tag === "-enditem") {
          const item = normalizeProtocolText(parts[3] ?? "");
          if (item.length > 0) {
            remember(evidence.item, `${identity}|${item}`, turnNumber);
            // Trick/Switcheroo/Thief/Covet/Magician/Pickpocket INTERCAMBIAN el
            // item: es público para AMBOS socios, uno lo tenía y el otro lo
            // tiene.
            const swapped = SWAP_CAUSE.test(line);
            const partners: ProtocolIdent[] = [];
            if (swapped && lastSwap !== undefined) {
              for (const candidate of lastSwap) {
                if (candidate.side !== ident.side || candidate.name !== ident.name) {
                  partners.push(candidate);
                }
              }
            }
            const ofIdent = swapped ? parseIdent(SUFFIX_OF.exec(line)?.[1]) : undefined;
            if (ofIdent !== undefined) partners.push(ofIdent);
            for (const partner of partners) {
              const partnerView = ensure(partner.side);
              remember(
                partnerView.evidence.item,
                `${identCanon.get(partner.side)?.get(partner.name) ?? partner.name}|${item}`,
                turnNumber,
              );
            }
          }
        }
        if (tag === "-activate" && SWAP_CAUSE.test(parts[3] ?? "")) {
          const other = parseIdent(SUFFIX_OF.exec(line)?.[1]);
          lastSwap = other === undefined ? undefined : [ident, other];
        }
        if (tag === "-ability") {
          const ability = normalizeProtocolText(parts[3] ?? "");
          if (ability.length > 0) {
            remember(evidence.ability, `${identity}|${ability}`, turnNumber);
            // Trace: la línea es prueba PÚBLICA de que el pokémon TRAZADO
            // (`[of]`) tiene esa ability, aunque poke-env sólo escriba sobre
            // el ident (`abstract_battle.py:781-793`).
            if (/\[from\]\s*ability:\s*Trace/i.test(line)) {
              const traced = parseIdent(SUFFIX_OF.exec(line)?.[1]);
              if (traced !== undefined) {
                const tracedView = ensure(traced.side);
                remember(
                  tracedView.evidence.ability,
                  `${identCanon.get(traced.side)?.get(traced.name) ?? traced.name}|${ability}`,
                  turnNumber,
                );
              }
            }
          }
        }

        const itemSuffix = SUFFIX_ITEM.exec(line);
        if (itemSuffix) {
          const item = normalizeProtocolText(itemSuffix[1]);
          if (item.length > 0) {
            const owner = suffixOwner(tag, ident, line, "item", item);
            remember(
              ensure(owner.side).evidence.item,
              `${identCanon.get(owner.side)?.get(owner.name) ?? owner.name}|${item}`,
              turnNumber,
            );
          }
        }
        const abilitySuffix = SUFFIX_ABILITY.exec(line);
        if (abilitySuffix) {
          const ability = normalizeProtocolText(abilitySuffix[1]);
          if (ability.length > 0) {
            const owner = suffixOwner(tag, ident, line, "ability", ability);
            remember(
              ensure(owner.side).evidence.ability,
              `${identCanon.get(owner.side)?.get(owner.name) ?? owner.name}|${ability}`,
              turnNumber,
            );
          }
        }
      }
    }

    for (const [side, view] of perSide) views.set(`${key}:${side}`, view);
  }

  return {
    get: (battleId, playerSide, opponentSide) =>
      views.get(`${battleId}:${playerSide}:${opponentSide}`),
    linesScanned,
  };
}

/** El lado observado desde `playerSide`. `p1` observa a `p2` y viceversa. */
export function opponentSideOf(playerSide: string): string {
  return playerSide === "p1" ? "p2" : "p1";
}
