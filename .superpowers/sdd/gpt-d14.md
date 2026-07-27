# D14 — Herencia aditiva de learnsets

## Status

Completa. `inheritanceChain` une, en orden y sin duplicados, la rama evolutiva
propia de una forma y la rama de su especie base. No usa la alternativa
excluyente `own.prevo || own.baseSpecies`.

Antes del re-seed se creó `/tmp/antes-d14.dump` con `pg_dump` en formato custom
y compresión 9. El archivo quedó no vacío (3,3 MB) y `pg_restore -l` pudo listar
sus 117 entradas.

## Conteos antes y después

| Generación | Antes | Después | Delta |
|---|---:|---:|---:|
| 6 (XY/ORAS) | 62.198 | 62.198 | 0 |
| 9 (SV) | 65.624 | 65.642 | +18 |

El re-seed de ambas generaciones se ejecutó dos veces sobre la base poblada.
La segunda pasada conservó exactamente 62.198 y 65.642 filas, por lo que el
resultado no depende de tablas vacías y respeta el pipeline upsert-only de D13.

## Cinco puntos de verificación

1. **Monotonía por generación y especie.** Un test compara, para cada especie
   disponible de gen 6 y gen 9, el conjunto producido por la cadena anterior
   con el nuevo: no se perdió ningún movimiento. Gen 6 quedó idéntica y gen 9
   solo sumó.
2. **Quince faltantes.** Los 15 pares detectados por el auditor están presentes
   en PostgreSQL, incluido `ninetalesalola/moonblast`, con `learn_methods`
   completo y `sourceSpecies` de su preevolución regional.
3. **Gourgeist.** `gourgeist`, `gourgeistsmall`, `gourgeistlarge` y
   `gourgeistsuper` conservan 66 movimientos cada uno.
4. **Canario gen 6.** Permanecen 834 Pokémon, 618 movimientos y 62.198
   learnsets. La prueba del extractor fija 62.198 como conteo exacto.
5. **Consumidor teams.** Los 15 tests pasan contra la data nueva. El auditor
   queda en 0 `db_missing`; su test ya no exige que Moonblast falte y conserva
   `ninetalesalola/ember` como canario `db_extra`.

## Auditoría antes y después

| Estado | Gen 6 db_extra | Gen 9 db_extra | Total db_extra | db_missing |
|---|---:|---:|---:|---:|
| Antes | 342 | 3.205 | 3.547 | 15 |
| Después | 342 | 3.208 | 3.550 | 0 |

Los 18 pares nuevos se dividen en 15 que el oráculo acepta y tres eventos que
el oráculo rechaza:

- `ninetalesalola/celebrate`: evento gen 7 de `vulpixalola`; no transferible a
  gen 9.
- `lycanrocdusk/happyhour`: evento gen 7 de `rockruffdusk`; no transferible a
  gen 9.
- `polteageistantique/celebrate`: evento gen 8 de `sinisteaantique`; el
  oráculo dice que Polteageist-Antique no aprende Celebrate.

Se probó la regla general de no heredar eventos de generaciones anteriores.
Falló antes de llegar a la base: gen 6 bajó de 62.198 a 61.918 y el chequeo
especie por especie detectó pérdidas (por ejemplo, cinco movimientos en
Charizard-Mega-X). Se revirtió de inmediato, conforme al criterio acordado.

## Verificación ejecutada

- `packages/seed`: 18/18 tests focalizados y `tsc --noEmit`.
- `packages/teams`: 15/15 tests y `tsc --noEmit`.
- Auditor real: gen 6, 342 discrepancias (0 missing); gen 9, 3.208
  discrepancias (0 missing).
- Dos re-seeds consecutivos sobre PostgreSQL por `127.0.0.1:15432`.

## Concerns

Los tres eventos quedan como límite conocido sin relevancia competitiva. Los
dos primeros requieren semántica de transferencia entre generaciones y el
tercero una regla de compatibilidad de evento/evolución más fuerte. Filtrar
solo por `method === "event"` y generación de origen no es seguro: elimina 280
filas legítimas de gen 6. D14 documenta la prueba fallida para evitar que esa
regla incompleta se reintroduzca.
