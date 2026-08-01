/** Índice acumulativo del protocolo crudo.
 *
 * El protocolo es la fuente de verdad del estado (D17). Este módulo lo lee
 * UNA sola vez por corrida y deja, para cada `(battle_id, player_side)`, el
 * PRIMER turno en que cada hecho público quedó revelado. Preguntar "¿esto era
 * público en el turno T?" pasa a ser `primerTurno <= T`, O(1), en vez de
 * rebobinar el prefijo de protocolo una vez por paso.
 *
 * Reglas del SKILL que este módulo respeta a propósito:
 *
 *  - Se compara LÍNEA POR LÍNEA, nunca sobre el protocolo concatenado: en un
 *    blob un nombre puede "aparecer" a caballo entre dos tokens sin relación
 *    y una fuga real pasaría como revelada.
 *  - Se compara por TOKEN, no por substring de la línea entera: sin eso,
 *    `|move|p1a: Gengar|Shadow Ball|p2a: Mr. Mime` "revelaría" a Mr. Mime como
 *    especie sólo por ser el objetivo.
 *  - La identidad de un miembro es su `base_species` (`Pokemon.identifies_as`),
 *    no su `species`: una Mega que sale del campo y vuelve NO es un miembro
 *    nuevo.
 *  - La normalización saca TODA la puntuación y los diacríticos: "Mr. Mime"
 *    tiene un punto y "Farfetch'd" un apóstrofo.
 */

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

/** `Yanmega, L82, F` -> especie normalizada y nivel narrado (Showdown omite
 * `L100`, por eso `level` puede venir `undefined` legítimamente). */
export function parseDetails(raw: string | undefined): {
  species: string;
  level?: number;
} | undefined {
  if (raw === undefined) return undefined;
  const parts = raw.split(",").map((part) => part.trim());
  const species = normalizeProtocolText(parts[0] ?? "");
  if (species.length === 0) return undefined;
  let level: number | undefined;
  for (const part of parts.slice(1)) {
    const match = /^L(\d+)$/i.exec(part);
    if (match) level = Number(match[1]);
  }
  return { species, level };
}

const REVEAL_TAGS = new Set(["switch", "drag", "replace", "detailschange"]);
/** Etiquetas que cambian UN stat nombrado en `parts[3]`. */
const BOOST_TAGS_WITH_STAT = new Set(["-boost", "-unboost", "-setboost"]);
/** Etiquetas que cambian VARIOS stats sin nombrarlos: se registran con el
 * comodín `*`, porque cualquiera de los siete pudo moverse. */
const BOOST_TAGS_ANY_STAT = new Set([
  "-swapboost", "-copyboost", "-clearboost",
  "-clearnegativeboost", "-clearpositiveboost", "-invertboost",
]);
/** Comodín de stat para los boosts que el protocolo no desglosa. */
export const ANY_BOOST_STAT = "*";
const TYPE_CHANGE_TAGS = new Set(["-transform", "detailschange", "-formechange", "replace"]);

/** Evidencia pública acumulada de UN lado de UNA batalla. Cada mapa guarda el
 * PRIMER turno en que el hecho quedó revelado. */
export interface SideEvidence {
  species: Map<string, number>;
  level: Map<string, number>;
  move: Map<string, number>;
  item: Map<string, number>;
  ability: Map<string, number>;
  status: Map<string, number>;
  faint: Map<string, number>;
  boost: Map<string, number>;
  typeChange: Map<string, number>;
  /** Transform/Imposter: a partir de acá el moveset, los tipos y la ability
   * son copia de otro pokémon y no se pueden contrastar contra el dex. */
  transform: Map<string, number>;
  /** `|rule|HP Percentage Mod`: con la regla activa el HP rival se narra en
   * centésimos, así que `hp_fraction` tiene que caer en la grilla de 1/100. */
  hpPercentageMod: boolean;
  /** Turnos con al menos una línea de protocolo grabada. */
  turnsWithLines: Set<number>;
}

export interface ProtocolIndex {
  /** Clave `${battleId}:${playerSide}:${opponentSide}`. */
  get(battleId: number, playerSide: string, opponentSide: string): SideEvidence | undefined;
  linesScanned: number;
}

function emptyEvidence(): SideEvidence {
  return {
    species: new Map(),
    level: new Map(),
    move: new Map(),
    item: new Map(),
    ability: new Map(),
    status: new Map(),
    faint: new Map(),
    boost: new Map(),
    typeChange: new Map(),
    transform: new Map(),
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
 * nombra y una búsqueda exacta las deja sin dex: 492 falsos positivos de
 * `ability` sobre el corpus real, todos Furfrou.
 *
 * La resolución de respaldo es el prefijo MÁS LARGO del dex que sea prefijo
 * del id buscado, que es exactamente cómo Showdown forma el id de una forma
 * cosmética (`furfrou` + `pharaoh`). Sigue anclada al dex: no hay ninguna
 * lista de especies escrita a mano.
 */
export function buildSpeciesResolver(entries: Iterable<DexPokemon>): SpeciesResolver {
  const exact = new Map<string, DexPokemon>();
  for (const entry of entries) exact.set(normalizeProtocolText(entry.showdownId), entry);
  const byLengthDesc = [...exact.keys()].sort((a, b) => b.length - a.length);
  const cache = new Map<string, DexPokemon | undefined>();
  return (speciesId: string) => {
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
  };
}

/** Identidad canónica de una especie según el dex local: su `base_species`.
 * Es el criterio de `Pokemon.identifies_as`, no una comparación por `species`
 * (que rompe con toda forma alternativa: Arceus-Poison, Rotom-Wash). */
export function identityKeys(species: string, resolve: SpeciesResolver): string[] {
  const normalized = normalizeProtocolText(species);
  const entry = resolve(normalized);
  const base = entry ? normalizeProtocolText(entry.baseSpecies) : normalized;
  return base === normalized ? [normalized] : [normalized, base];
}

/** Un evento sobre `ident` se atribuye a TODAS las identidades que ese ident
 * nombró hasta ahora (el apodo del protocolo puede ser la forma base mientras
 * `species` es una forma alternativa: `p2a: Rotom` para `Rotom-Wash`). */
function identityAliases(
  ident: ProtocolIdent,
  identSpecies: Map<string, Set<string>>,
): string[] {
  const aliases = new Set<string>([ident.name]);
  for (const species of identSpecies.get(ident.name) ?? []) aliases.add(species);
  return [...aliases];
}

// El sufijo aparece con y sin `[from]`: `|-immune|p2a: X|[from] ability: Levitate`
// pero `|-activate|p2a: X|ability: Sturdy`.
const SUFFIX_ITEM = /(?:\[from\]\s*)?item:\s*([^|]+)/i;
const SUFFIX_ABILITY = /(?:\[from\]\s*)?ability:\s*([^|]+)/i;
const SUFFIX_OF = /\[of\]\s*(p[1-9][a-z]?:[^|]+)/i;
/** Causas que INTERCAMBIAN o roban un item entre dos pokémon. */
const SWAP_CAUSE = /(?:move:\s*(?:Trick|Switcheroo|Thief|Covet)|ability:\s*(?:Magician|Pickpocket))/i;

/** Quién es el dueño real del item/ability que revela un sufijo.
 *
 * Reproduce la semántica del `[of]` que poke-env 0.15.0 codifica en sus cuatro
 * helpers de `-damage`/`-heal` (`abstract_battle.py:333-403`), que el proyector
 * del recorder ya replica en `apply_damage_or_heal_ownership`:
 *
 *  - `-damage` con `[of] Y`: el item/ability es de Y (Rocky Helmet, Rough Skin).
 *  - `-heal`: el `[of]` es ENGAÑOSO y el dueño es el ident de la línea
 *    (Water Absorb cura a quien la tiene y nombra a quien lanzó el agua);
 *    la única excepción es Hospitality, que sí viene de otro.
 *  - `-ability`: el `[of]` es la FUENTE copiada (Trace), no el dueño.
 *  - el resto de las etiquetas: el ident de la línea.
 *
 * Atribuirle el dato al `[of]` en los dos primeros casos dejaba al dueño real
 * sin evidencia: 1655 falsos positivos de `ability` sobre el corpus real.
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
  // `-item`: poke-env (`abstract_battle.py:949-989`) distingue tres causas.
  // Frisk la tiene el `[of]` (el que espía); Pickpocket y Magician las tiene
  // el ident (el que roba). Darle Pickpocket al `[of]` dejaba al ladrón sin
  // evidencia: 14 falsos positivos sobre el corpus real.
  if (tag === "-item" && kind === "ability") {
    return value === "pickpocket" || value === "magician" ? ident : of;
  }
  // Resto: el `[of]` es la CAUSA, y la causa es quien tiene el item/ability.
  // `|-item|p1a: Nuestro|Leftovers|[from] ability: Frisk|[of] p2a: Rival` es
  // prueba pública de que el Frisk es del rival.
  return of;
}

/** Construye el índice. Recorre el corpus UNA vez: el costo es proporcional a
 * las líneas de protocolo, no a la cantidad de pasos. */
export function buildProtocolIndex(
  turns: readonly BattleTurnRecord[],
  resolve: SpeciesResolver,
): ProtocolIndex {
  const byBattleSide = new Map<string, BattleTurnRecord[]>();
  for (const turn of turns) {
    const key = `${turn.battleId}:${turn.playerSide}`;
    const bucket = byBattleSide.get(key);
    if (bucket === undefined) byBattleSide.set(key, [turn]);
    else bucket.push(turn);
  }

  const evidences = new Map<string, SideEvidence>();
  let linesScanned = 0;

  for (const [key, bucket] of byBattleSide) {
    bucket.sort((a, b) => a.turnNumber - b.turnNumber);
    // Una evidencia por lado observado. `player_side` es el dueño del stream;
    // el lado auditado es el otro, pero el índice se arma para los dos por si
    // una trayectoria futura audita p2.
    const perSide = new Map<string, SideEvidence>();
    const identSpecies = new Map<string, Map<string, Set<string>>>();
    const ensure = (side: string): SideEvidence => {
      let evidence = perSide.get(side);
      if (evidence === undefined) {
        evidence = emptyEvidence();
        perSide.set(side, evidence);
        identSpecies.set(side, new Map());
      }
      return evidence;
    };
    // Los dos lados existen desde el arranque: crearlos perezosamente dejaba
    // sin `turnsWithLines` los turnos anteriores a la primera línea con ident.
    for (const side of ["p1", "p2"]) ensure(side);

    let lastSwap: [ProtocolIdent, ProtocolIdent] | undefined;
    for (const turn of bucket) {
      const turnNumber = turn.turnNumber;
      if (turn.protocolLines.length > 0) {
        for (const evidence of perSide.values()) evidence.turnsWithLines.add(turnNumber);
      }
      for (const line of turn.protocolLines) {
        linesScanned += 1;
        if (line.length === 0 || line.charCodeAt(0) !== 124 /* | */) continue;
        const parts = line.split("|");
        const tag = parts[1] ?? "";

        if (tag === "rule") {
          const rule = parts[2] ?? "";
          if (/^HP Percentage Mod\b/i.test(rule)) {
            for (const side of ["p1", "p2"]) ensure(side).hpPercentageMod = true;
          }
          continue;
        }
        // `-clearallboost` no lleva ident: afecta a los dos lados. Va antes
        // del filtro por ident, que si no lo descartaría en silencio.
        if (tag === "-clearallboost") {
          for (const [side, evidence] of perSide) {
            for (const alias of identSpecies.get(side)?.keys() ?? []) {
              remember(evidence.boost, `${alias}|${ANY_BOOST_STAT}`, turnNumber);
            }
            for (const aliases of identSpecies.get(side)?.values() ?? []) {
              for (const alias of aliases) {
                remember(evidence.boost, `${alias}|${ANY_BOOST_STAT}`, turnNumber);
              }
            }
          }
          continue;
        }

        const ident = parseIdent(parts[2]);
        if (ident === undefined) continue;
        const evidence = ensure(ident.side);
        const aliasesBySide = identSpecies.get(ident.side)!;

        if (REVEAL_TAGS.has(tag) || tag === "-formechange") {
          // `-formechange` trae sólo la especie donde los otros traen el
          // `details` completo; `parseDetails` cubre las dos formas porque
          // toma el token anterior a la primera coma.
          const details = parseDetails(parts[3]);
          if (details !== undefined) {
            for (const identityKey of identityKeys(details.species, resolve)) {
              remember(evidence.species, identityKey, turnNumber);
              const bucketAliases = aliasesBySide.get(ident.name) ?? new Set<string>();
              bucketAliases.add(identityKey);
              aliasesBySide.set(ident.name, bucketAliases);
              if (details.level !== undefined) {
                remember(evidence.level, `${identityKey}|${details.level}`, turnNumber);
              }
            }
            if (details.level !== undefined) {
              remember(evidence.level, `${ident.name}|${details.level}`, turnNumber);
            }
          }
          // El token de HP puede traer el estado: `100/100 par`, `0 fnt`.
          const hpToken = parts[4] ?? "";
          const statusInHp = /\b(brn|par|slp|frz|psn|tox|fnt)\b/i.exec(hpToken);
          if (statusInHp) {
            for (const alias of identityAliases(ident, aliasesBySide)) {
              remember(evidence.status, `${alias}|${statusInHp[1].toLowerCase()}`, turnNumber);
            }
          }
        }

        const aliases = identityAliases(ident, aliasesBySide);

        if (TYPE_CHANGE_TAGS.has(tag)) {
          for (const alias of aliases) remember(evidence.typeChange, alias, turnNumber);
        }
        if (tag === "-start" && /^typechange$/i.test(parts[3] ?? "")) {
          for (const alias of aliases) remember(evidence.typeChange, alias, turnNumber);
        }
        if (tag === "move") {
          for (const moveKey of moveEvidenceKeys(parts[3] ?? "")) {
            for (const alias of aliases) remember(evidence.move, `${alias}|${moveKey}`, turnNumber);
          }
        }
        // Trick/Switcheroo/Thief/Covet/Magician/Pickpocket INTERCAMBIAN el
        // item: la línea `|-activate|A|move: Trick|[of] B` nombra a los dos
        // socios y el `|-item|` que sigue narra el item que cambió de dueño.
        // Ese item es público para AMBOS —uno lo tenía, el otro lo tiene—, y
        // acreditárselo sólo al ident dejaba al socio sin evidencia (medido:
        // 70 falsos positivos de `item` sobre filas v2).
        if (tag === "-activate" && SWAP_CAUSE.test(parts[3] ?? "")) {
          const other = parseIdent(SUFFIX_OF.exec(line)?.[1]);
          lastSwap = other === undefined ? undefined : [ident, other];
        }
        if (tag === "-item" || tag === "-enditem") {
          const item = normalizeProtocolText(parts[3] ?? "");
          if (item.length > 0) {
            for (const alias of aliases) remember(evidence.item, `${alias}|${item}`, turnNumber);
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
              const partnerEvidence = ensure(partner.side);
              for (const alias of identityAliases(
                partner, identSpecies.get(partner.side)!,
              )) {
                remember(partnerEvidence.item, `${alias}|${item}`, turnNumber);
              }
            }
          }
        }
        if (tag === "-ability") {
          const ability = normalizeProtocolText(parts[3] ?? "");
          if (ability.length > 0) {
            for (const alias of aliases) {
              remember(evidence.ability, `${alias}|${ability}`, turnNumber);
            }
            // Trace: `|-ability|p1a: Gardevoir|Intimidate|[from] ability: Trace|
            // [of] p2a: Luxray` dice que Gardevoir tiene Trace y que copió
            // Intimidate DE Luxray. poke-env sólo escribe sobre el ident
            // (`abstract_battle.py:781-793`), pero la línea es prueba PÚBLICA
            // de que Luxray tiene esa ability: el auditor pregunta "¿esto era
            // público?", no "¿poke-env lo anotó?".
            if (/\[from\]\s*ability:\s*Trace/i.test(line)) {
              const traced = parseIdent(SUFFIX_OF.exec(line)?.[1]);
              if (traced !== undefined) {
                const tracedEvidence = ensure(traced.side);
                for (const alias of identityAliases(
                  traced, identSpecies.get(traced.side)!,
                )) {
                  remember(tracedEvidence.ability, `${alias}|${ability}`, turnNumber);
                }
              }
            }
          }
        }
        if (tag === "-status") {
          const status = normalizeProtocolText(parts[3] ?? "");
          if (status.length > 0) {
            for (const alias of aliases) remember(evidence.status, `${alias}|${status}`, turnNumber);
          }
        }
        if (tag === "faint") {
          for (const alias of aliases) {
            remember(evidence.faint, alias, turnNumber);
            remember(evidence.status, `${alias}|fnt`, turnNumber);
          }
        }
        if (BOOST_TAGS_WITH_STAT.has(tag)) {
          const stat = normalizeProtocolText(parts[3] ?? "");
          if (stat.length > 0) {
            for (const alias of aliases) remember(evidence.boost, `${alias}|${stat}`, turnNumber);
          }
        }
        if (BOOST_TAGS_ANY_STAT.has(tag)) {
          for (const alias of aliases) {
            remember(evidence.boost, `${alias}|${ANY_BOOST_STAT}`, turnNumber);
          }
        }

        // Un Transform copia el moveset, los tipos y la ability de OTRO
        // pokémon —normalmente uno nuestro— y eso es una inferencia legítima,
        // no una fuga: los datos copiados salen de nuestro propio lado.
        if (tag === "-transform" || /\[from\]\s*ability:\s*Imposter/i.test(line)) {
          for (const alias of aliases) remember(evidence.transform, alias, turnNumber);
        }

        const itemSuffix = SUFFIX_ITEM.exec(line);
        if (itemSuffix) {
          const item = normalizeProtocolText(itemSuffix[1]);
          if (item.length > 0) {
            const owner = suffixOwner(tag, ident, line, "item", item);
            const ownerEvidence = ensure(owner.side);
            for (const alias of identityAliases(owner, identSpecies.get(owner.side)!)) {
              remember(ownerEvidence.item, `${alias}|${item}`, turnNumber);
            }
          }
        }
        const abilitySuffix = SUFFIX_ABILITY.exec(line);
        if (abilitySuffix) {
          const ability = normalizeProtocolText(abilitySuffix[1]);
          if (ability.length > 0) {
            const owner = suffixOwner(tag, ident, line, "ability", ability);
            const ownerEvidence = ensure(owner.side);
            for (const alias of identityAliases(owner, identSpecies.get(owner.side)!)) {
              remember(ownerEvidence.ability, `${alias}|${ability}`, turnNumber);
            }
          }
        }
      }
    }

    for (const [side, evidence] of perSide) evidences.set(`${key}:${side}`, evidence);
  }

  return {
    get: (battleId, playerSide, opponentSide) =>
      evidences.get(`${battleId}:${playerSide}:${opponentSide}`),
    linesScanned,
  };
}

/** El lado observado desde `playerSide`. `p1` observa a `p2` y viceversa. */
export function opponentSideOf(playerSide: string): string {
  return playerSide === "p1" ? "p2" : "p1";
}
