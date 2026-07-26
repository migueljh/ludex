export interface SpeciesRow {
  showdownId: string;
  dexNum: number;
  name: string;
  baseSpecies: string;
  forme: string | null;
  isDefault: boolean;
  types: string[];
  baseStats: { hp: number; atk: number; def: number; spa: number; spd: number; spe: number };
  abilities: Record<string, string>;
  weightKg: number | null;
  evolvesFrom: string | null;
  tier: string | null;
}

export interface MoveRow {
  showdownId: string;
  name: string;
  type: string;
  category: string;
  power: number;
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
