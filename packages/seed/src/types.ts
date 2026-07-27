export interface SpeciesRow {
  showdownId: string;
  dexNum: number;
  name: string;
  /** Id normalizado de la especie base (D2), ej. 'charizard'. Clave de búsqueda. */
  baseSpecies: string;
  /** Nombre legible de la especie base, ej. 'Charizard'. Solo para mostrar. */
  baseSpeciesName: string;
  forme: string | null;
  isDefault: boolean;
  types: string[];
  baseStats: { hp: number; atk: number; def: number; spa: number; spd: number; spe: number };
  abilities: Record<string, string>;
  weightKg: number | null;
  evolvesFrom: string | null;
  tier: string | null;
}

/**
 * Showdown pone basePower 0 en cuatro situaciones distintas; power_kind las
 * distingue sin parsear `description`:
 * - status:       category === 'Status'
 * - variable:     tiene basePowerCallback (gyroball, lowkick, return...)
 * - fixed_damage: tiene damage numerico o 'level' (dragonrage, seismictoss...)
 * - special:      basePower 0 y ninguna de las anteriores (superfang, counter,
 *                 los OHKO...); el mecanismo solo esta en la prosa de description
 * - standard:     basePower > 0
 */
export type PowerKind = "status" | "variable" | "fixed_damage" | "special" | "standard";

export interface MoveRow {
  showdownId: string;
  name: string;
  type: string;
  category: string;
  power: number;
  powerKind: PowerKind;
  /** NULL = "nunca falla" (Showdown: accuracy === true, ej. Swift). Ver D15. */
  accuracy: number | null;
  pp: number;
  priority: number;
  target: string;
  flags: Record<string, number>;
  description: string | null;
}

/**
 * El tipo Item del paquete NO expone `flags`. Estas son las propiedades reales
 * que sirven al agente y al filtro de disponibilidad por ronda.
 */
export interface ItemProperties {
  isBerry: boolean;
  isGem: boolean;
  isChoice: boolean;
  isPokeball: boolean;
  megaStone: string | null;    // especie mega que produce, ej. 'Charizard-Mega-X'
  megaEvolves: string | null;  // especie base que evoluciona, ej. 'Charizard'
  naturalGift: unknown | null;
  fling: unknown | null;
}

export interface ItemRow {
  showdownId: string;
  name: string;
  description: string | null;
  properties: ItemProperties;
}

export interface AbilityRow {
  showdownId: string;
  name: string;
  description: string | null;
}

export interface TypeChartRow {
  attackingType: string;
  defendingType: string;
  multiplier: number;
}

export type LearnMethodName =
  | "level" | "machine" | "tutor" | "egg" | "event"
  | "dream" | "transfer" | "tradeback" | "reminder";

export interface LearnMethod {
  gen: number;
  method: LearnMethodName;
  level?: number;
  sourceSpecies: string;
}

export interface LearnsetRow {
  speciesId: string;
  moveId: string;
  methods: LearnMethod[];
}
