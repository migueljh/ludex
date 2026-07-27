/**
 * Auditoria de learnsets (familia D14).
 *
 * Para cada forma no-default de cada gen seedeada, compara dos respuestas a
 * "¿esta especie puede aprender este movimiento en esta gen?":
 *
 *   oraculo: TeamValidator.validateMoves del paquete pineado (la legalidad
 *            real del juego, con toda la cadena evolutiva resuelta por
 *            Showdown).
 *   base:    existe la fila (especie, movimiento) en learnsets para esa gen
 *            (lo que el seed escribio con su propia resolucion de herencia).
 *
 * Discrepancias:
 *   db_missing: el oraculo dice legal y la base no tiene la fila (agujero).
 *   db_extra:   la base tiene la fila y el oraculo dice ilegal (falso
 *               positivo, p. ej. heredado de la linea evolutiva equivocada).
 *
 * Es SOLO LECTURA. No arregla nada: reporta.
 */
import { existsSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import showdown from "pokemon-showdown";
import { createPool } from "./db.js";

const { Dex, TeamValidator } = showdown;

const envPath = fileURLToPath(new URL("../../../.env", import.meta.url));
if (existsSync(envPath)) process.loadEnvFile(envPath);

interface Mismatch {
  gen: number;
  species: string;
  forme: string | null;
  move: string;
  direction: "db_missing" | "db_extra" | "vs_base_missing" | "vs_base_extra" | "oracle_error";
  /** El problema que dio el oraculo (para distinguir "no la aprende" de "esta baneada en el formato"). */
  oracle?: string;
}

/**
 * Megas y primales no se obtienen como forma: su movepool ES el de la base y
 * el oraculo no puede juzgarlos (lanza 'Bad sources passed to checkCanLearn'
 * al intentar sourcear la forma directamente). Para ellas la auditoria es
 * interna: las filas de la forma en la base deben ser exactamente las de su
 * especie base (D10: las megas quedan con el conteo de su base).
 */
function isBattleOnlyForm(species: { isMega?: boolean; isPrimal?: boolean }): boolean {
  return Boolean(species.isMega || species.isPrimal);
}

async function auditGen(pool: ReturnType<typeof createPool>, gen: number): Promise<Mismatch[]> {
  const genId = (await pool.query<{ id: number }>(
    "SELECT id FROM generations WHERE gen_number = $1", [gen])).rows[0]?.id;
  if (!genId) return [];

  const forms = (await pool.query<{ showdown_id: string; forme: string | null }>(
    "SELECT showdown_id, forme FROM pokemon WHERE gen_id = $1 AND NOT is_default ORDER BY showdown_id",
    [genId])).rows;
  const moveIds = (await pool.query<{ showdown_id: string }>(
    "SELECT showdown_id FROM moves WHERE gen_id = $1 ORDER BY showdown_id", [genId])).rows
    .map((r) => r.showdown_id);
  const learnRows = (await pool.query<{ species: string; move: string }>(
    `SELECT p.showdown_id AS species, m.showdown_id AS move
     FROM learnsets l
     JOIN pokemon p ON p.id = l.pokemon_id
     JOIN moves m ON m.id = l.move_id
     WHERE p.gen_id = $1`, [genId])).rows;
  const db = new Map<string, Set<string>>();
  for (const r of learnRows) {
    if (!db.has(r.species)) db.set(r.species, new Set());
    db.get(r.species)!.add(r.move);
  }

  // Sin dex explicito: con un modded dex el constructor explota ("This must
  // be called on the base Dex"); el formato ya lleva el mod implicito y da
  // los mismos veredictos (verificado con ninetalesalola+moonblast/ember).
  const dex = Dex.mod(`gen${gen}`);
  const validator = new TeamValidator(`gen${gen}ou`);
  const mismatches: Mismatch[] = [];

  for (const [i, form] of forms.entries()) {
    if (i % 25 === 0) console.log(`  gen ${gen}: forma ${i}/${forms.length}...`);
    const species = dex.species.get(form.showdown_id);
    if (!species.exists) continue;
    const dbMoves = db.get(form.showdown_id) ?? new Set<string>();

    if (isBattleOnlyForm(species)) {
      const baseMoves = db.get(species.baseSpecies ? dex.species.get(species.baseSpecies).id : form.showdown_id) ?? new Set<string>();
      for (const moveId of new Set([...dbMoves, ...baseMoves])) {
        if (dbMoves.has(moveId) && !baseMoves.has(moveId)) {
          mismatches.push({ gen, species: form.showdown_id, forme: form.forme, move: moveId, direction: "vs_base_extra" });
        } else if (!dbMoves.has(moveId) && baseMoves.has(moveId)) {
          mismatches.push({ gen, species: form.showdown_id, forme: form.forme, move: moveId, direction: "vs_base_missing" });
        }
      }
      continue;
    }

    let oracleAvailable = true;
    try {
      validator.validateMoves(species, ["tackle"], validator.allSources(species));
    } catch {
      oracleAvailable = false;
    }
    if (!oracleAvailable) {
      // Forma de batalla que el oraculo no puede sourcear (p. ej.
      // zygardecomplete): se compara contra su base en la propia DB.
      const baseMoves = db.get(dex.species.get(species.baseSpecies).id) ?? new Set<string>();
      for (const moveId of new Set([...dbMoves, ...baseMoves])) {
        if (dbMoves.has(moveId) && !baseMoves.has(moveId)) {
          mismatches.push({ gen, species: form.showdown_id, forme: form.forme, move: moveId, direction: "vs_base_extra", oracle: "oracle no disponible (forma de batalla)" });
        } else if (!dbMoves.has(moveId) && baseMoves.has(moveId)) {
          mismatches.push({ gen, species: form.showdown_id, forme: form.forme, move: moveId, direction: "vs_base_missing", oracle: "oracle no disponible (forma de batalla)" });
        }
      }
      continue;
    }

    for (const moveId of moveIds) {
      let problems: string[];
      try {
        // validateMoves muta PokemonSources al aplicar incompatibilidades.
        // Cada par (forma, movimiento) necesita una fuente nueva o el
        // movimiento anterior contamina todos los veredictos siguientes.
        problems = validator.validateMoves(
          species,
          [moveId],
          validator.allSources(species),
        );
      } catch (e) {
        // zoroarkhisui: la cadena interna del oraculo se rompe con su linea
        // de formas; se reporta como no juzgable, no como discrepancia.
        mismatches.push({
          gen, species: form.showdown_id, forme: form.forme, move: moveId,
          direction: "oracle_error", oracle: (e as Error).message,
        });
        continue;
      }
      const oracleLegal = problems.length === 0;
      const dbHas = dbMoves.has(moveId);
      if (oracleLegal && !dbHas) {
        mismatches.push({ gen, species: form.showdown_id, forme: form.forme, move: moveId, direction: "db_missing" });
      } else if (!oracleLegal && dbHas) {
        mismatches.push({ gen, species: form.showdown_id, forme: form.forme, move: moveId, direction: "db_extra", oracle: problems.join(" | ") });
      }
    }
  }
  return mismatches;
}

async function main(): Promise<void> {
  const pool = createPool();
  try {
    const gens = (await pool.query<{ gen_number: number }>(
      "SELECT gen_number FROM generations ORDER BY gen_number")).rows.map((r) => r.gen_number);
    console.log(`gens seedeadas: ${gens.join(", ")}`);

    const all: Mismatch[] = [];
    for (const gen of gens) {
      const m = await auditGen(pool, gen);
      const missing = m.filter((x) => x.direction === "db_missing");
      const extra = m.filter((x) => x.direction === "db_extra");
      console.log(`gen ${gen}: ${m.length} discrepancias (${missing.length} db_missing, ${extra.length} db_extra)`);
      all.push(...m);
    }

    const bySpecies = new Map<string, Mismatch[]>();
    for (const m of all) {
      const k = `${m.gen}:${m.species}`;
      if (!bySpecies.has(k)) bySpecies.set(k, []);
      bySpecies.get(k)!.push(m);
    }
    console.log("\n=== discrepancias por especie ===");
    for (const [k, list] of [...bySpecies.entries()].sort()) {
      const first = list[0];
      console.log(`${k} (${first.forme}): ${list.length}`);
      for (const m of list.slice(0, 12)) {
        console.log(`    ${m.direction}: ${m.move}${m.oracle ? `  <- ${m.oracle}` : ""}`);
      }
      if (list.length > 12) console.log(`    ... y ${list.length - 12} mas`);
    }
    if (bySpecies.size === 0) console.log("(ninguna)");

    // Artefacto crudo para analisis posterior (el texto de arriba es el resumen).
    const outPath = process.argv[2];
    if (outPath) {
      writeFileSync(outPath, JSON.stringify(all, null, 1));
      console.log(`\nJSON crudo: ${outPath} (${all.length} filas)`);
    }
  } finally {
    await pool.end();
  }
}

main().catch((err) => { console.error(err); process.exit(1); });
