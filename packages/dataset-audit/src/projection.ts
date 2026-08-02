/** Réplica del estado observable que poke-env 0.15.0 construye del protocolo.
 *
 * El índice de "revelado alguna vez" respondía si un dato fue público; una
 * ventana por campo respondía si el valor existió en algún momento del rango.
 * Ninguna de las dos responde la pregunta que importa: **¿este snapshot
 * corresponde a un instante real de la batalla?**. Con una ventana por campo,
 * un HP del turno 3, un status del turno 4 y unos boosts del turno 5 pasaban
 * juntos aunque nunca hubieran coexistido.
 *
 * Acá el protocolo se REPRODUCE: cada línea muta un modelo con el mismo ciclo
 * de vida que `Pokemon` de poke-env, y una fila se valida contra el estado
 * COMPLETO en un instante —un cursor— y no campo por campo. Si ningún cursor
 * de su ventana explica la fila entera, la fila no describe ningún punto de la
 * batalla.
 *
 * Regla de oro heredada del SKILL: ante una diferencia con poke-env gana
 * poke-env. Por eso cada handler cita la línea de la librería que reproduce, y
 * las rarezas se replican en vez de "arreglarse" (por ejemplo: `switch_out` NO
 * restaura los tipos de una Mega, y `_update_from_details` corta temprano si el
 * `details` repite, así que una Mega que vuelve al campo conserva sus tipos).
 *
 * Lo que el protocolo público NO permite derivar se marca `unresolved` y el
 * auditor se abstiene de ese campo: afirmar de más sería inventar, y afirmar
 * de menos —aceptar cualquier cosa en todos los campos— era el defecto.
 */

import {
  canonicalIdentity,
  identKey,
  normalizeProtocolText,
  parseDetails,
  parseHpToken,
  parseIdent,
  retrieveMoveId,
  SPECIAL_MOVES,
  type ProtocolIdent,
  type SpeciesIndex,
} from "./protocol.js";
import type { DexMove, DexPokemon } from "./types.js";

export const BOOST_STATS = [
  "accuracy", "atk", "def", "evasion", "spa", "spd", "spe",
] as const;

export type Boosts = Record<string, number>;

/** Centinela de poke-env para "el item todavía no se reveló"
 * (`GenData.UNKNOWN_ITEM`, sembrado en `Pokemon.__init__`). Es distinto de
 * `null`, que significa "no tiene item" tras un `-enditem`. */
export const UNKNOWN_ITEM = "unknown_item";

export function blankBoosts(): Boosts {
  return { accuracy: 0, atk: 0, def: 0, evasion: 0, spa: 0, spd: 0, spe: 0 };
}

/** Campos observables cuyo valor puede quedar sin derivar. */
export type UnresolvedField = "ability" | "types" | "moves" | "boosts" | "hp" | "item";

export interface MoveState {
  id: string;
  /** `pp * 8 // 5`; `undefined` si el dex local no trae el movimiento. */
  maxPp: number | undefined;
  /** El PP es un rango sólo cuando Pressure pudo aplicar y no es decidible.
   * En todo el resto de los casos `ppMin === ppMax` y el chequeo es exacto. */
  ppMin: number | undefined;
  ppMax: number | undefined;
  fromTransform: boolean;
}

export interface MonState {
  ident: ProtocolIdent;
  /** `Pokemon._species`. NO cambia con `detailschange`/`-formechange`
   * (`forme_change` usa `store_species=False`, `pokemon.py:431-433`). */
  species: string;
  /** La forma vigente, que es lo que manda en tipos y ability de forma. */
  formeId: string;
  /** El dex local no conoce `formeId` (formas cosméticas). */
  formeUnknown: boolean;
  level: number;
  active: boolean;
  hp: number | undefined;
  status: string | null;
  boosts: Boosts;
  item: string | null;
  baseAbility: string | null;
  temporaryAbility: string | null;
  formeChangeAbility: string | null;
  /** `_temporary_types`: typechange y Transform. Se limpia al salir del campo. */
  temporaryTypes: string[] | undefined;
  baseMoves: Map<string, MoveState>;
  transformMoves: Map<string, MoveState> | undefined;
  mimicMove: MoveState | undefined;
  /** Identidad copiada por el Transform vigente, si es de nuestro lado. */
  transformTarget: ProtocolIdent | undefined;
  dancing: boolean;
  lastDetails: string;
  unresolved: Set<UnresolvedField>;
  /** Abilities que una línea prueba que este pokémon tiene pero que poke-env
   * NO le escribe. El caso es Trace: `|-ability|p1a: Gardevoir|Hustle|[from]
   * ability: Trace|[of] p2a: Durant` demuestra que Durant tiene Hustle, y la
   * librería sólo escribe sobre Gardevoir. Exigirla sería exigir más de lo que
   * poke-env produce; prohibirla sería llamar fuga a un dato público. Se acepta
   * y no se exige. */
  admissibleAbilities: Set<string>;
}

/** Lo que la proyección necesita del dex local. Nunca se consulta internet. */
export interface DexView {
  gen: number;
  forme(speciesId: string): DexPokemon | undefined;
  /** `Move.max_pp` = `pp * 8 // 5` (`move.py:471-478`). */
  maxPp(moveId: string): number | undefined;
  knowsMove(moveId: string): boolean;
  /** La segunda mitad de `_pressure_on` (`battle.py:1209-1221`): la categoría
   * de objetivo del movimiento habilita el descuento de Pressure. */
  pressureApplies(moveId: string): boolean;
}

/** Categorías de objetivo sobre las que Pressure descuenta el segundo PP. */
const PRESSURE_TARGETS = new Set([
  "all", "allAdjacent", "allAdjacentFoes", "any", "normal", "randomNormal", "scripted",
]);

export function buildDexView(
  pokemon: readonly DexPokemon[],
  moves: readonly DexMove[],
  gen: number,
): DexView {
  const moveIndex = new Map<string, DexMove>();
  for (const move of moves) {
    if (move.gen !== gen) continue;
    moveIndex.set(normalizeProtocolText(move.showdownId), move);
  }
  const byId = new Map<string, DexPokemon>();
  for (const entry of pokemon) {
    if (entry.gen !== gen) continue;
    byId.set(normalizeProtocolText(entry.showdownId), entry);
  }
  return {
    gen,
    forme: (speciesId) => byId.get(normalizeProtocolText(speciesId)),
    maxPp: (moveId) => {
      const entry = moveIndex.get(retrieveMoveId(moveId));
      return entry?.pp === null || entry?.pp === undefined
        ? undefined
        : Math.floor(entry.pp * 8 / 5);
    },
    knowsMove: (moveId) => moveIndex.has(retrieveMoveId(moveId)),
    pressureApplies: (moveId) => {
      const entry = moveIndex.get(retrieveMoveId(moveId));
      if (entry === undefined) return false;
      return PRESSURE_TARGETS.has(entry.target)
        || entry.flags.some((flag) => flag.toLowerCase() === "mustpressure");
    },
  };
}

/** Mismo predicado que `_update_from_pokedex` (`pokemon.py:650-655`): una forma
 * Mega/Primal/Stellar/Terastal impone su propia ability. */
export function isFormeChangeForme(entry: DexPokemon): boolean {
  const forme = entry.forme ?? "";
  if (forme.length === 0) return false;
  return forme.startsWith("Mega")
    || forme === "Primal"
    || forme === "Stellar"
    || forme === "Terastal"
    || forme.endsWith("-Tera");
}

/** `Pokemon.ability` (`pokemon.py:860-871`). */
export function abilityOf(mon: MonState): string | null {
  if (mon.temporaryAbility !== null) return mon.temporaryAbility;
  if (mon.formeChangeAbility !== null) return mon.formeChangeAbility;
  return mon.baseAbility;
}

/** `Pokemon.types` (`pokemon.py:1396-1408`). `undefined` = no derivable. */
export function typesOf(mon: MonState, dex: DexView): string[] | undefined {
  if (mon.temporaryTypes !== undefined) return mon.temporaryTypes;
  const entry = dex.forme(mon.formeId);
  return entry === undefined ? undefined : entry.types.map((type) => type.toUpperCase());
}

/** `MoveSet.moves` (`move.py:998-1013`): el Transform tapa el moveset base y
 * Mimic sustituye su propia entrada. */
export function movesOf(mon: MonState): Map<string, MoveState> {
  const base = mon.transformMoves ?? mon.baseMoves;
  if (mon.mimicMove === undefined) return base;
  const out = new Map<string, MoveState>();
  for (const [id, move] of base) {
    if (id === "mimic") out.set(mon.mimicMove.id, mon.mimicMove);
    else out.set(id, move);
  }
  return out;
}

export function isFainted(mon: MonState): boolean {
  return mon.status === "fnt";
}

const HP_TAGS = new Set(["-damage", "-heal", "-sethp"]);
const SUFFIX_OF = /\[of\]\s*(p[1-9][a-z]?:[^|]+)/i;
/** Etiquetas cuyo `parts[2]` es un pokémon para poke-env. */
const MON_TAGS = new Set([
  "-damage", "-heal", "-sethp", "faint", "-status", "-curestatus", "-cureteam",
  "-boost", "-unboost", "-setboost", "-clearboost", "-clearnegativeboost",
  "-clearpositiveboost", "-invertboost", "-ability", "-endability", "-item",
  "-enditem", "detailschange", "-formechange", "-mega", "-primal", "-transform",
  "-start", "-end", "-activate", "cant",
]);

/** Reproducción del estado observable de UNA batalla, los dos lados.
 *
 * Los dos lados, sí: `-copyboost`, `-swapboost` y `-transform` cruzan el campo,
 * y sin el otro lado habría que marcar DESCONOCIDO justamente donde el
 * protocolo alcanza perfectamente para derivar el valor. */
export class BattleProjection {
  readonly mons = new Map<string, MonState>();
  private readonly activeBySide = new Map<string, string>();
  /** Cambia cada vez que muta algo observable: sirve para no evaluar dos veces
   * el mismo cursor. */
  revision = 0;

  /** `own` es NUESTRO lado: poke-env conoce su ability y su item por el
   * `|request|` privado, que no está en el protocolo. Los necesita para dos
   * cosas concretas: saber si Pressure descuenta dos PP, y saber QUÉ item
   * recibe el rival cuando un Trick los intercambia. Es información propia,
   * no del rival. */
  constructor(
    private readonly dex: DexView,
    private readonly species: SpeciesIndex,
    private readonly own?: {
      side: string;
      abilities: Map<string, string>;
      items: Map<string, string | null>;
    },
  ) {}

  activeOf(side: string): MonState | undefined {
    const key = this.activeBySide.get(side);
    return key === undefined ? undefined : this.mons.get(key);
  }

  /** Los miembros de un lado, indexados por su identidad canónica
   * (`base_species`, que es el criterio de `Pokemon.identifies_as`). */
  teamOf(side: string): MonState[] {
    const out: MonState[] = [];
    for (const mon of this.mons.values()) {
      if (mon.ident.side === side) out.push(mon);
    }
    return out;
  }

  private touch(): void {
    this.revision += 1;
  }

  private get(identRaw: string | undefined, details?: string): MonState | undefined {
    const ident = parseIdent(identRaw);
    if (ident === undefined) return undefined;
    const key = identKey(ident);
    let mon = this.mons.get(key);
    if (mon === undefined) {
      mon = {
        ident,
        species: "",
        formeId: "",
        formeUnknown: true,
        level: 100,
        active: false,
        hp: undefined,
        status: null,
        boosts: blankBoosts(),
        item: UNKNOWN_ITEM,
        baseAbility: null,
        temporaryAbility: null,
        formeChangeAbility: null,
        temporaryTypes: undefined,
        baseMoves: new Map(),
        transformMoves: undefined,
        mimicMove: undefined,
        transformTarget: undefined,
        dancing: false,
        lastDetails: "",
        unresolved: new Set(),
        admissibleAbilities: new Set(),
      };
      this.mons.set(key, mon);
      this.touch();
      if (details !== undefined) this.updateFromDetails(mon, details);
    }
    return mon;
  }

  // --- espejo de Pokemon ---------------------------------------------------

  /** `_update_from_pokedex` (`pokemon.py:638-667`). */
  private updateFromPokedex(mon: MonState, speciesId: string, storeSpecies: boolean): void {
    const normalized = normalizeProtocolText(speciesId);
    if (storeSpecies) mon.species = normalized;
    mon.formeId = normalized;
    const entry = this.dex.forme(normalized);
    mon.formeUnknown = entry === undefined;
    if (entry === undefined) {
      // Sin entrada de dex no hay tipos ni abilities derivables para esta
      // forma. Es un límite del dex local, no una licencia para aceptar todo:
      // sólo estos dos campos quedan sin afirmar.
      mon.unresolved.add("types");
      mon.unresolved.add("ability");
      this.touch();
      return;
    }
    if (isFormeChangeForme(entry)) {
      mon.formeChangeAbility = normalizeProtocolText(entry.abilities[0] ?? "") || null;
    } else if (mon.formeChangeAbility === null) {
      if (entry.abilities.length === 1 && this.dex.gen >= 3) {
        mon.baseAbility = normalizeProtocolText(entry.abilities[0]);
      }
    } else {
      mon.formeChangeAbility = null;
    }
    // `update_from_request` (`pokemon.py:716-720`): la ability propia entra por
    // el request, no por el protocolo.
    if (storeSpecies && this.own !== undefined && mon.ident.side === this.own.side) {
      if (abilityOf(mon) === null) {
        const known = this.own.abilities.get(normalized);
        if (known !== undefined) mon.baseAbility = known;
      }
      if (mon.item === UNKNOWN_ITEM && this.own.items.has(normalized)) {
        mon.item = this.own.items.get(normalized) ?? null;
      }
    }
    this.touch();
  }

  /** `_update_from_details` (`pokemon.py:669-714`), corte temprano incluido. */
  private updateFromDetails(mon: MonState, details: string): void {
    if (details === mon.lastDetails) return;
    mon.lastDetails = details;
    const parsed = parseDetails(details);
    if (parsed === undefined) return;
    mon.level = parsed.level;
    if (parsed.species !== mon.species) this.updateFromPokedex(mon, parsed.species, true);
    this.touch();
  }

  /** `set_hp_status` (`pokemon.py:534-555`).
   *
   * `0 fnt` es el único token que deriva a `faint()`. En el resto, la ausencia
   * de token de status BORRA el status: no es "sin novedad". */
  private setHpStatus(mon: MonState, token: string | undefined): void {
    if (token !== undefined && token.trim() === "0 fnt") {
      this.faint(mon);
      return;
    }
    const hp = parseHpToken(token);
    if (hp === undefined) return;
    mon.status = hp.status;
    mon.hp = hp.fraction;
    this.touch();
  }

  /** `faint` (`pokemon.py:422-430`). No limpia boosts ni desactiva. */
  private faint(mon: MonState): void {
    mon.hp = 0;
    mon.status = "fnt";
    mon.temporaryAbility = null;
    mon.transformMoves = undefined;
    mon.transformTarget = undefined;
    mon.mimicMove = undefined;
    this.touch();
  }

  /** `switch_out` (`pokemon.py:589-618`). */
  private switchOut(mon: MonState): void {
    mon.active = false;
    mon.boosts = blankBoosts();
    mon.temporaryAbility = null;
    mon.transformMoves = undefined;
    mon.transformTarget = undefined;
    mon.mimicMove = undefined;
    // `_temporary_types` sí se limpia; `_type_1`/`_type_2` NO: una Mega que
    // sale del campo sigue con los tipos de su forma.
    mon.temporaryTypes = undefined;
    mon.unresolved.delete("moves");
    this.touch();
  }

  /** `Battle.switch` (`battle.py:142-155`). */
  private switchIn(identRaw: string, details: string, hpToken: string | undefined): void {
    const ident = parseIdent(identRaw);
    if (ident === undefined) return;
    const previous = this.activeOf(ident.side);
    if (previous !== undefined) this.switchOut(previous);
    const mon = this.get(identRaw, details);
    if (mon === undefined) return;
    mon.active = true;
    this.updateFromDetails(mon, details);
    this.activeBySide.set(ident.side, identKey(ident));
    this.setHpStatus(mon, hpToken);
    this.touch();
  }

  /** `_end_illusion_on` (`abstract_battle.py:409-427`). */
  private replace(identRaw: string, details: string): void {
    const ident = parseIdent(identRaw);
    if (ident === undefined) return;
    const illusioned = this.activeOf(ident.side);
    const illusionist = this.get(identRaw, details);
    if (illusionist === undefined) return;
    if (illusioned !== undefined && illusioned !== illusionist) {
      illusionist.active = true;
      this.updateFromDetails(illusionist, details);
      illusionist.status = illusioned.status;
      illusionist.hp = illusioned.hp;
      illusioned.hp = undefined;
      illusioned.status = null;
      this.switchOut(illusioned);
    } else {
      illusionist.active = true;
      this.updateFromDetails(illusionist, details);
    }
    this.activeBySide.set(ident.side, identKey(ident));
    this.touch();
  }

  /** El setter de `Pokemon.ability` (`pokemon.py:873-878`): la PRIMERA
   * revelación fija la ability persistente; toda revelación posterior es un
   * override temporal, que `-endability` y `switch_out` revierten. */
  private revealAbility(mon: MonState, raw: string): void {
    const ability = normalizeProtocolText(raw);
    if (ability.length === 0) return;
    if (mon.baseAbility === null) mon.baseAbility = ability;
    else mon.temporaryAbility = ability;
    mon.unresolved.delete("ability");
    this.touch();
  }

  /** `_add_move` (`pokemon.py:165-180`). Con Transform vigente el movimiento
   * nuevo entra al moveset COPIADO, y nace con el PP de un copiado. */
  private addMove(mon: MonState, rawId: string): MoveState | undefined {
    const id = retrieveMoveId(rawId);
    const resolved = movesOf(mon);
    const existing = resolved.get(id);
    if (existing !== undefined) return existing;
    if (SPECIAL_MOVES.has(id) || !this.dex.knowsMove(id)) return undefined;
    const target = mon.transformMoves ?? mon.baseMoves;
    const fromTransform = mon.transformMoves !== undefined;
    const dexMax = this.dex.maxPp(id);
    const maxPp = dexMax === undefined
      ? undefined
      : fromTransform && this.dex.gen >= 5 ? Math.min(5, dexMax) : dexMax;
    const move: MoveState = {
      id,
      maxPp,
      ppMin: maxPp,
      ppMax: maxPp,
      fromTransform,
    };
    target.set(id, move);
    this.touch();
    return move;
  }

  /** `Move.use` (`move.py:123-130`). `pressure` es `undefined` cuando el
   * protocolo no permite decidirlo: ahí el PP queda en un rango de uno. */
  private useMove(move: MoveState, pressure: boolean | undefined, overridden = false): void {
    const base = overridden ? 0 : 1;
    const minDecrement = base + (pressure === true ? 1 : 0);
    const maxDecrement = base + (pressure === false ? 0 : 1);
    if (move.ppMax !== undefined) move.ppMax = Math.max(move.ppMax - minDecrement, 0);
    if (move.ppMin !== undefined) move.ppMin = Math.max(move.ppMin - maxDecrement, 0);
    this.touch();
  }

  /** `Pokemon.moved` (`pokemon.py:457-503`), reducido a lo observable. */
  private moved(
    mon: MonState,
    rawId: string,
    options: { use: boolean; reveal: boolean; pressure: boolean | undefined },
  ): void {
    const move = options.reveal ? this.addMove(mon, rawId) : undefined;
    if (options.use && move !== undefined) this.useMove(move, options.pressure);
  }

  /** `_pressure_on` (`battle.py:1196-1221`).
   *
   * Es EXACTO: la tabla `moves` trae `target` y `flags`, que son las dos
   * condiciones que la librería evalúa además de la ability del defensor. El
   * único caso sin respuesta es un movimiento que el dex local no conoce. */
  private pressureOn(
    attacker: ProtocolIdent,
    moveId: string,
    targetRaw: string | undefined,
  ): boolean | undefined {
    const explicit = parseIdent(targetRaw);
    const target = explicit !== undefined
      ? this.mons.get(identKey(explicit))
      : this.activeOf(attacker.side === "p1" ? "p2" : "p1");
    if (target === undefined || isFainted(target)) return false;
    if (abilityOf(target) !== "pressure") return false;
    if (!this.dex.knowsMove(moveId)) return undefined;
    return this.dex.pressureApplies(moveId);
  }

  private boostsOf(mon: MonState): Boosts {
    return mon.boosts;
  }

  // --- despacho por etiqueta ----------------------------------------------

  /** `AbstractBattle.parse_message` (`abstract_battle.py:565-1190`). */
  apply(line: string): void {
    if (line.length === 0 || line.charCodeAt(0) !== 124 /* | */) return;
    const parts = line.split("|");
    const tag = parts[1] ?? "";

    if (tag === "switch" || tag === "drag") {
      if (parts.length > 4) this.switchIn(parts[2], parts[3], parts[4]);
      return;
    }
    if (tag === "replace") {
      if (parts.length > 3) this.replace(parts[2], parts[3]);
      return;
    }
    if (tag === "move") {
      this.applyMove(parts);
      return;
    }
    // `-clearallboost` (Haze) no lleva ident y sólo alcanza a los DOS activos
    // (`battle.py:32-36`), no a los equipos completos.
    if (tag === "-clearallboost") {
      for (const side of ["p1", "p2"]) {
        const active = this.activeOf(side);
        if (active !== undefined) active.boosts = blankBoosts();
      }
      this.touch();
      return;
    }
    // `-copyboost|SOURCE|TARGET` escribe en el SEGUNDO ident los boosts del
    // primero (`abstract_battle.py:912-914`): la fuente NO cambia.
    if (tag === "-copyboost" && parts.length > 3) {
      const source = this.get(parts[2]);
      const target = this.get(parts[3]);
      if (source !== undefined && target !== undefined) {
        target.boosts = { ...this.boostsOf(source) };
        if (source.unresolved.has("boosts")) target.unresolved.add("boosts");
        else target.unresolved.delete("boosts");
        this.touch();
      }
      return;
    }
    if (tag === "-swapboost" && parts.length > 4) {
      const source = this.get(parts[2]);
      const target = this.get(parts[3]);
      if (source !== undefined && target !== undefined) {
        const stats = parts[4].includes("[from]")
          ? [...BOOST_STATS]
          : parts[4].split(",").map((stat) => normalizeProtocolText(stat));
        for (const stat of stats) {
          if (!(BOOST_STATS as readonly string[]).includes(stat)) continue;
          const swap = source.boosts[stat] ?? 0;
          source.boosts[stat] = target.boosts[stat] ?? 0;
          target.boosts[stat] = swap;
        }
        this.touch();
      }
      return;
    }

    // Sólo estas etiquetas resuelven un pokémon en poke-env. Sin esta lista,
    // un `|-sidestart|p2: Miguel|Spikes` creaba un miembro fantasma llamado
    // como el JUGADOR y el equipo rival pasaba a tener siete.
    if (!MON_TAGS.has(tag)) return;
    const mon = this.get(parts[2]);
    if (mon === undefined) return;

    if (HP_TAGS.has(tag)) {
      this.setHpStatus(mon, parts[3]);
      if (tag === "-damage") {
        this.damageOwnership(parts, line);
      } else if (tag === "-heal") {
        this.healOwnership(parts, line);
      }
      return;
    }
    if (tag === "faint") {
      this.faint(mon);
      return;
    }
    if (tag === "-status") {
      const status = normalizeProtocolText(parts[3] ?? "");
      if (status.length > 0) {
        mon.status = status;
        this.touch();
      }
      return;
    }
    if (tag === "-curestatus") {
      // `cure_status(status)` sólo borra si coincide, y nunca sobre un FNT.
      const status = normalizeProtocolText(parts[3] ?? "");
      if (status.length === 0) {
        if (!isFainted(mon)) mon.status = null;
      } else if (mon.status === status) {
        mon.status = null;
      }
      this.touch();
      return;
    }
    if (tag === "-cureteam") {
      for (const teammate of this.teamOf(mon.ident.side)) {
        if (!isFainted(teammate)) teammate.status = null;
      }
      this.touch();
      return;
    }
    if (tag === "-boost" || tag === "-unboost" || tag === "-setboost") {
      const stat = normalizeProtocolText(parts[3] ?? "");
      const amount = Number(parts[4]);
      if (stat.length > 0 && Number.isFinite(amount)
        && (BOOST_STATS as readonly string[]).includes(stat)) {
        if (tag === "-setboost") mon.boosts[stat] = amount;
        else {
          const delta = tag === "-boost" ? amount : -amount;
          mon.boosts[stat] = Math.max(-6, Math.min(6, (mon.boosts[stat] ?? 0) + delta));
        }
        this.touch();
      }
      return;
    }
    if (tag === "-clearboost") {
      mon.boosts = blankBoosts();
      this.touch();
      return;
    }
    if (tag === "-clearnegativeboost" || tag === "-clearpositiveboost" || tag === "-invertboost") {
      for (const stat of BOOST_STATS) {
        const value = mon.boosts[stat] ?? 0;
        if (tag === "-invertboost") mon.boosts[stat] = -value;
        else if (tag === "-clearnegativeboost" && value < 0) mon.boosts[stat] = 0;
        else if (tag === "-clearpositiveboost" && value > 0) mon.boosts[stat] = 0;
      }
      this.touch();
      return;
    }
    if (tag === "-ability") {
      this.applyAbility(mon, parts, line);
      return;
    }
    if (tag === "-endability") {
      mon.temporaryAbility = null;
      this.touch();
      return;
    }
    if (tag === "-item") {
      this.applyItem(parts);
      return;
    }
    if (tag === "-enditem") {
      mon.item = null;
      this.touch();
      return;
    }
    if (tag === "detailschange" || tag === "-formechange") {
      // `forme_change` (`pokemon.py:431-433`): cambia la FORMA, no la especie
      // ni el nivel. Un `-formechange` no trae `details`, así que leerlo como
      // uno fabricaba un `level=100` que la línea nunca dijo.
      const species = (parts[3] ?? "").split(",")[0];
      if (species.length > 0) this.updateFromPokedex(mon, species, false);
      return;
    }
    if (tag === "-mega" || tag === "-primal") {
      this.applyMega(mon, tag, parts[3]);
      return;
    }
    if (tag === "-transform") {
      this.applyTransform(mon, parts);
      return;
    }
    if (tag === "-start") {
      this.applyStart(mon, parts);
      return;
    }
    if (tag === "-end") {
      const effect = normalizeProtocolText(parts[3] ?? "");
      if (effect === "typechange") {
        mon.temporaryTypes = undefined;
        mon.unresolved.delete("types");
        this.touch();
      } else if (effect === "skillswap") {
        mon.temporaryAbility = null;
        this.touch();
      }
      return;
    }
    if (tag === "-activate") {
      this.applyActivate(mon, parts, line);
      return;
    }
    if (tag === "cant") {
      mon.dancing = false;
      return;
    }
  }

  /** `-mega` (`abstract_battle.py:1001-1008`) -> `mega_evolve`
   * (`pokemon.py:443-455`). */
  private applyMega(mon: MonState, tag: string, stone: string | undefined): void {
    const suffix = tag === "-mega" ? "mega" : "primal";
    const base = normalizeProtocolText(mon.species);
    let candidate = base.endsWith(suffix) ? base : `${base}${suffix}`;
    mon.temporaryAbility = null;
    if (this.dex.forme(candidate) === undefined && tag === "-mega" && stone !== undefined) {
      const letter = stone.trim().slice(-1).toLowerCase();
      if (letter === "x" || letter === "y") candidate = `${candidate}${letter}`;
    }
    this.updateFromPokedex(mon, candidate, false);
  }

  /** `-start` (`abstract_battle.py:798-826`). */
  private applyStart(mon: MonState, parts: string[]): void {
    const effect = parts[3] ?? "";
    if (effect === "typechange") {
      const of = parseIdent(parts[5]?.startsWith("[of] ") ? parts[5].slice(5) : undefined);
      if (of !== undefined) {
        // Reflect Type: los tipos son los del pokémon citado.
        const source = this.mons.get(identKey(of));
        const types = source === undefined ? undefined : typesOf(source, this.dex);
        if (types === undefined) mon.unresolved.add("types");
        else {
          mon.temporaryTypes = [...types];
          mon.unresolved.delete("types");
        }
      } else {
        const narrated = (parts[4] ?? "").split("/")
          .map((chunk) => normalizeProtocolText(chunk).toUpperCase())
          .filter((chunk) => chunk.length > 0);
        if (narrated.length === 0) mon.unresolved.add("types");
        else {
          mon.temporaryTypes = narrated;
          mon.unresolved.delete("types");
        }
      }
      this.touch();
      return;
    }
    if (effect === "Mimic") {
      const copied = retrieveMoveId(parts[4] ?? "");
      if (copied.length > 0) {
        const dexMax = this.dex.maxPp(copied);
        mon.mimicMove = { id: copied, maxPp: dexMax, ppMin: dexMax, ppMax: dexMax, fromTransform: false };
        this.touch();
      }
    }
  }

  /** `-activate` (`abstract_battle.py:827-894`). */
  private applyActivate(mon: MonState, parts: string[], line: string): void {
    const effect = parts[3] ?? "";
    const of = parseIdent(SUFFIX_OF.exec(line)?.[1]);
    if (effect.replace("move: ", "") === "Skill Swap") {
      const abilities = parts.slice(4)
        .map((part) => part.replace("[ability] ", "").replace("[ability2] ", "").trim())
        .filter((part) => part.length > 0 && !part.startsWith("[of]"));
      const actor = of === undefined ? undefined : this.mons.get(identKey(of));
      if (abilities.length >= 2) {
        // gen 5+: la línea revela las DOS abilities y las intercambia.
        if (mon.baseAbility === null) mon.baseAbility = normalizeProtocolText(abilities[1]);
        mon.temporaryAbility = normalizeProtocolText(abilities[0]);
        mon.unresolved.delete("ability");
        if (actor !== undefined) {
          actor.temporaryAbility = normalizeProtocolText(abilities[1]);
          actor.unresolved.delete("ability");
        }
      } else if (actor !== undefined) {
        const target = abilityOf(mon);
        const source = abilityOf(actor);
        if (target !== null && source !== null) {
          mon.temporaryAbility = source;
          actor.temporaryAbility = target;
        } else {
          mon.unresolved.add("ability");
          actor.unresolved.add("ability");
        }
      }
      this.touch();
      return;
    }
    if (effect === "ability: Dancer") {
      mon.dancing = true;
      return;
    }
    if (effect === "ability: Mummy") {
      const victim = of ?? parseIdent(parts[4]);
      const target = victim === undefined ? undefined : this.mons.get(identKey(victim));
      if (target !== undefined) {
        target.temporaryAbility = "mummy";
        this.touch();
      }
      return;
    }
    if (effect === "ability: Wandering Spirit") {
      const victim = of === undefined ? undefined : this.mons.get(identKey(of));
      mon.temporaryAbility = normalizeProtocolText(parts[4] ?? "") || mon.temporaryAbility;
      if (victim !== undefined) victim.temporaryAbility = "wanderingspirit";
      this.touch();
      return;
    }
    if (effect === "ability: Symbiosis") {
      const receiver = of === undefined ? undefined : this.mons.get(identKey(of));
      if (receiver !== undefined) {
        receiver.item = normalizeProtocolText((parts[4] ?? "").replace("[item] ", ""));
      }
      mon.item = null;
      this.touch();
      return;
    }
    if (effect === "item: Leppa Berry") {
      const move = movesOf(mon).get(retrieveMoveId(parts[4] ?? ""));
      if (move !== undefined) {
        if (move.ppMin !== undefined && move.maxPp !== undefined) {
          move.ppMin = Math.min(move.ppMin + 10, move.maxPp);
        }
        if (move.ppMax !== undefined && move.maxPp !== undefined) {
          move.ppMax = Math.min(move.ppMax + 10, move.maxPp);
        }
        this.touch();
      }
      return;
    }
    if (effect === "move: Mimic") {
      const copied = retrieveMoveId(parts[4] ?? "");
      if (copied.length > 0) {
        const dexMax = this.dex.maxPp(copied);
        mon.mimicMove = { id: copied, maxPp: dexMax, ppMin: dexMax, ppMax: dexMax, fromTransform: false };
        this.touch();
      }
      return;
    }
    if (effect === "move: Trick") {
      // La línea de `-activate` es la que intercambia; los `-item` que la
      // siguen los ignora poke-env (`abstract_battle.py:995-1000`).
      const other = of ?? parseIdent(parts[4]);
      const partner = other === undefined ? undefined : this.mons.get(identKey(other));
      if (partner !== undefined) {
        const swap = mon.item;
        mon.item = partner.item;
        partner.item = swap;
        this.touch();
      }
    }
  }

  /** `-ability` (`abstract_battle.py:770-797`). El `[of]` de un Trace nombra al
   * pokémon TRAZADO, no al dueño de la línea. */
  private applyAbility(mon: MonState, parts: string[], line: string): void {
    const cause = parts[3] ?? "";
    if (cause.length === 0) return;
    if (/\[from\]\s*ability:\s*Trace/i.test(line)) {
      if (abilityOf(mon) !== "trace") {
        if (mon.temporaryAbility !== null) mon.temporaryAbility = null;
        else if (mon.baseAbility !== null) mon.baseAbility = null;
        this.revealAbility(mon, "trace");
      }
      this.revealAbility(mon, cause);
      // La línea PRUEBA que el trazado tiene esa ability, aunque poke-env sólo
      // escriba sobre el ident: no afirmarlo dejaría pasar un dato que sí es
      // público.
      const traced = parseIdent(SUFFIX_OF.exec(line)?.[1]);
      const other = traced === undefined ? undefined : this.mons.get(identKey(traced));
      if (other !== undefined) other.admissibleAbilities.add(normalizeProtocolText(cause));
      return;
    }
    if (normalizeProtocolText(cause) === "neutralizinggas") return;
    this.revealAbility(mon, cause);
  }

  /** `-item` (`abstract_battle.py:949-1000`). */
  private applyItem(parts: string[]): void {
    const mon = this.get(parts[2]);
    if (mon === undefined) return;
    const item = normalizeProtocolText(parts[3] ?? "");
    const cause = parts[4] ?? "";
    const of = parseIdent(parts[5]?.replace("[of] ", ""));
    const other = of === undefined ? undefined : this.mons.get(identKey(of));
    if (parts.length === 6) {
      if (cause === "[from] ability: Frisk") {
        // Frisk: el `[of]` es quien ESPÍA; el item es del OTRO activo.
        if (other !== undefined) this.revealAbility(other, "frisk");
        const owner = other === undefined
          ? mon
          : this.activeOf(other.ident.side === "p1" ? "p2" : "p1") ?? mon;
        owner.item = item;
      } else if (cause === "[from] ability: Pickpocket"
        || cause === "[from] ability: Magician"
        || cause === "[from] move: Thief"
        || cause === "[from] move: Covet") {
        mon.item = item;
        if (cause.includes("Pickpocket")) this.revealAbility(mon, "pickpocket");
        if (cause.includes("Magician")) this.revealAbility(mon, "magician");
        if (other !== undefined) other.item = null;
      } else {
        // poke-env levanta `ValueError` acá: si una línea así existiera, la
        // grabación habría fallado. Se marca y no se afirma nada.
        mon.unresolved.add("item");
      }
      this.touch();
      return;
    }
    if (parts.length > 4 && (cause === "[from] ability: Magician"
      || cause === "[from] move: Switcheroo"
      || cause === "[from] move: Trick")) {
      // Los consume el `-activate` que las precede.
      return;
    }
    mon.item = item;
    this.touch();
  }

  /** `_check_damage_message_for_item` / `_for_ability`
   * (`abstract_battle.py:333-368`). */
  private damageOwnership(parts: string[], line: string): void {
    const of = parseIdent(SUFFIX_OF.exec(line)?.[1]);
    const item = /\[from\]\s*item:\s*([^|]+)/i.exec(line)?.[1];
    if (item !== undefined) {
      const owner = of === undefined ? this.get(parts[2]) : this.mons.get(identKey(of));
      if (owner !== undefined) {
        owner.item = normalizeProtocolText(item);
        this.touch();
      }
    }
    const ability = /\[from\]\s*ability:\s*([^|]+)/i.exec(line)?.[1];
    // Sólo con `[of]`: el daño por ability propia no lo atribuye la librería.
    if (ability !== undefined && of !== undefined) {
      const owner = this.mons.get(identKey(of));
      if (owner !== undefined) this.revealAbility(owner, ability);
    }
  }

  /** `_check_heal_message_for_item` / `_for_ability`
   * (`abstract_battle.py:370-403`). El `[of]` de un `-heal` es engañoso. */
  private healOwnership(parts: string[], line: string): void {
    const of = parseIdent(SUFFIX_OF.exec(line)?.[1]);
    const mon = this.get(parts[2]);
    const ability = /\[from\]\s*ability:\s*([^|]+)/i.exec(line)?.[1];
    if (ability !== undefined && of !== undefined) {
      const normalized = normalizeProtocolText(ability);
      const owner = normalized === "hospitality" ? this.mons.get(identKey(of)) : mon;
      if (owner !== undefined) this.revealAbility(owner, ability);
    }
    const item = /\[from\]\s*item:\s*([^|]+)/i.exec(line)?.[1];
    if (item !== undefined && of === undefined && mon !== undefined) {
      const normalized = normalizeProtocolText(item);
      // La baya ya fue consumida cuando llega el mensaje: escribirla afirmaría
      // un item que el pokémon ya no tiene.
      if (mon.item !== null && !normalized.includes("berry") && !normalized.includes("herb")) {
        mon.item = normalized;
        this.touch();
      }
    }
  }

  /** `-transform` (`abstract_battle.py:1059-1065`) -> `Pokemon.transform`
   * (`pokemon.py:625-636`). */
  private applyTransform(mon: MonState, parts: string[]): void {
    const target = parseIdent(parts[3]);
    const source = target === undefined ? undefined : this.get(parts[3]);
    if (source === undefined || source.species.length === 0) {
      // `Pokemon.transform` entra al pokédex con `into.species`: con un objetivo
      // sin especie, poke-env levanta `KeyError` y la grabación no existe. La
      // proyección NO aplica nada y tampoco marca nada como no derivable:
      // abstenerse acá era exactamente la excusa que dejaba pasar cualquier
      // ability y cualquier moveset detrás de un `-transform` inventado.
      return;
    }
    const imposter = parts.slice(4).some((part) => part.trim() === "[from] ability: Imposter");
    if (imposter) {
      this.addMove(mon, "transform");
      this.revealAbility(mon, "imposter");
    }
    mon.transformTarget = target;
    mon.transformMoves = new Map();
    const entry = this.dex.forme(source.species);
    if (entry === undefined) mon.unresolved.add("types");
    else {
      mon.temporaryTypes = entry.types.map((type) => type.toUpperCase());
      mon.unresolved.delete("types");
    }
    mon.boosts = { ...source.boosts };
    const sourceAbility = abilityOf(source);
    if (sourceAbility !== null) this.revealAbility(mon, sourceAbility);
    // El moveset copiado es el del objetivo. Del lado propio poke-env conoce el
    // moveset COMPLETO por el `|request|` privado, que no está en el protocolo:
    // ahí la fila trae el equipo propio y el auditor lo usa como objetivo real.
    for (const [id, move] of movesOf(source)) {
      const capped = move.maxPp === undefined
        ? undefined
        : this.dex.gen >= 5 ? Math.min(5, move.maxPp) : move.maxPp;
      mon.transformMoves.set(id, {
        id,
        maxPp: capped,
        ppMin: capped,
        ppMax: capped,
        fromTransform: true,
      });
    }
    mon.unresolved.add("moves");
    this.touch();
  }

  /** `move` (`abstract_battle.py:582-741`). La pertenencia de un movimiento no
   * es "salió en un `|move|`": Magic Bounce, Dancer, Magic Coat, Mirror Move,
   * lockedmove y los llamadores (Copycat, Metronome, Nature Power, Round) NO
   * revelan el movimiento del actor. */
  private applyMove(parts: string[]): void {
    const event = [...parts];
    const ident = parseIdent(event[2]);
    if (ident === undefined) return;
    const mon = this.get(event[2]);
    if (mon === undefined) return;
    let use = !mon.dancing;
    let reveal = !mon.dancing;
    let failed = false;
    let spread = false;
    let overridden: string | undefined;
    mon.dancing = false;

    const last = (): string => event[event.length - 1] ?? "";
    for (const suffix of ["[miss]", "[still]", "[notarget]"]) {
      if (last() === suffix) {
        event.pop();
        failed = true;
      }
    }
    if (last() === "[notarget]") event.pop();
    while (last().startsWith("[spread]")) {
      spread = true;
      event.pop();
    }
    if (["[from] lockedmove", "[from]lockedmove", "[from] Sky Attack"].includes(last())) {
      use = false;
      reveal = false;
      event.pop();
    }
    if (["[from] Pursuit", "[from]Pursuit", "[zeffect]"].includes(last())) event.pop();
    if (last() === "[from] Sleep Talk") event[event.length - 1] = "[from] move: Sleep Talk";
    if (last().startsWith("[anim]")) event.pop();
    if (last().startsWith("[from] move: ") || last().startsWith("[from]move: ")) {
      const raw = event.pop() ?? "";
      overridden = raw.split(": ").pop();
      if (overridden === "Sleep Talk") {
        // Sleep Talk llama a un movimiento PROPIO: revelarlo es correcto.
      } else if (["Copycat", "Metronome", "Nature Power", "Round"].includes(overridden ?? "")) {
        reveal = false;
      } else if (["Grass Pledge", "Water Pledge", "Fire Pledge"].includes(overridden ?? "")) {
        overridden = undefined;
      }
    }
    if (last() === "null") event.pop();
    if (last().startsWith("[from] ability: ") || last().startsWith("[from]ability: ")) {
      const revealed = (event.pop() ?? "").split(": ").pop() ?? "";
      this.revealAbility(mon, revealed);
      if (revealed === "Magic Bounce") {
        use = false;
        reveal = false;
      } else if (revealed === "Dancer") {
        return;
      }
    }
    if (last() === "[from] Magic Coat" || last() === "[from] Mirror Move") {
      use = false;
      reveal = false;
      event.pop();
    }
    while (last() === "[still]") event.pop();

    const move = event[3];
    if (move === undefined || move.length === 0) return;
    let presumedTarget = event.length > 4 ? event[4] : undefined;
    if (spread || presumedTarget === "") presumedTarget = undefined;
    const pressure = this.pressureOn(ident, move, presumedTarget);

    if (overridden !== undefined) {
      this.moved(mon, move, { use: false, reveal, pressure: false });
      const caller = movesOf(mon).get(retrieveMoveId(overridden));
      if (caller !== undefined) this.useMove(caller, pressure, true);
      return;
    }
    if (!failed && ["Sleep Talk", "Copycat", "Metronome", "Nature Power"].includes(move)) {
      // Descuento preventivo: poke-env no pasa `pressure` en esta rama.
      this.moved(mon, move, { use, reveal, pressure: false });
      return;
    }
    this.moved(mon, move, { use, reveal, pressure });
  }

  /** Identidad canónica de un miembro proyectado, para cruzarlo con la fila. */
  identityOf(mon: MonState): string {
    return canonicalIdentity(mon.species, this.species.resolve);
  }
}
