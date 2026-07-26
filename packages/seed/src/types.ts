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

export interface ItemRow {
  showdownId: string;
  name: string;
  description: string | null;
  flags: Record<string, unknown>;
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
