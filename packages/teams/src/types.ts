import type showdown from "pokemon-showdown";

// Tipos derivados del paquete sin importar runtime (el import de runtime se
// hace por default + desestructuracion; ver validate.ts).
export type ModdedDex = ReturnType<(typeof showdown)["Dex"]["mod"]>;
export type PokemonSet = ReturnType<(typeof showdown)["Teams"]["import"]> extends (infer T)[] | null ? T : never;
