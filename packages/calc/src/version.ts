import { createRequire } from "node:module";

// Pineada en package.json (D16); /health la expone para auditar bumps.
const require = createRequire(import.meta.url);
export const CALC_VERSION = require("@smogon/calc/package.json").version as string;
