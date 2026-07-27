---
name: showdown-data
description: Trampas conocidas del paquete npm pokemon-showdown y de @smogon/calc en Ludex. Usar al tocar packages/seed, packages/calc, cualquier extractor de data de juego, learnsets, movimientos, especies, type chart, o al leer/escribir las tablas pokemon, moves, learnsets, items, abilities, type_chart. También al agregar una generación nueva.
---

# Data de Pokémon Showdown en Ludex

Conocimiento verificado contra `pokemon-showdown@0.11.10` y `@smogon/calc@0.11.0`.
Nada de esto se deduce leyendo la API: todo costó una sesión de debugging.
Las decisiones formales viven en `docs/DECISIONS.md` (D2, D3, D4, D6, D10, D12, D14, D15, D16).

## Regla cero

Si un comportamiento de estos paquetes te sorprende, **verificalo inspeccionando el
objeto, no recordando la API**. Los dos paquetes fallan en silencio con mucha más
frecuencia de lo que lanzan errores.

## Import del paquete

```ts
import showdown from "pokemon-showdown";
const { Dex } = showdown;
```

`import { Dex } from "pokemon-showdown"` **falla** bajo el loader ESM nativo de Node:
esbuild deja los exports como getters no configurables y `cjs-module-lexer` reporta
cero exports nombrados. Vitest no lo muestra porque usa su propio interop, así que el
test pasa y el runtime revienta. Usar siempre default + desestructuración.

## Los mods NO filtran por generación

`Dex.mod('gen6')` devuelve el dex **completo**, con el contenido posterior marcado
`isNonstandard: 'Future'`. Sin filtro se cargan 523 especies de generaciones futuras
en un seed de gen 6.

El único filtro es:

```ts
entry.gen <= dex.gen && !entry.isNonstandard
```

Está en `packages/seed/src/extract/dex.ts` como `isAvailable`. Cualquier extractor
nuevo tiene que usarlo. Esto excluye además CAP (`isNonstandard: 'CAP'`) y cosas
inobtenibles (`'Unobtainable'`), que es lo correcto para un torneo.

## Learnsets: la parte más delicada del repo

**Códigos.** Formato `<gen><letra><resto>`: `6L47` (nivel 47 en gen 6), `6M` (MT),
`5T` (tutor gen 5), `6S0` (evento). Letras conocidas: L level, M machine, T tutor,
E egg, S event, D dream, V transfer, C tradeback, R reminder. Si aparece una letra
nueva en un bump de versión, **mapearla antes de seedear**, no ignorarla.

**Herencia: unión de dos ramas, nunca excluyente.** `inheritanceChain` recorre la
unión ordenada y deduplicada de (a) la forma y sus preevoluciones propias, y (b) la
especie base y las suyas.

La alternativa `own.prevo || own.baseSpecies` **está expresamente descartada** (D14).
Ya se intentó y regresionó gen 6: Gourgeist-Small y Gourgeist-Large quedaron con cero
movimientos. Las formas de tamaño necesitan ambas ramas; las formas regionales
necesitan que el consumidor filtre. No reintroducir.

**El id propio va primero en la cadena.** Un bug anterior hacía
`current = dex.species.get(current.baseSpecies)` en vez de empujar el id propio antes.
Resultado: toda forma con learnset propio (Rotom-Wash, Kyurem-Black, Meowstic hembra,
Wormadam-Sandy, Pikachu-Cosplay) perdía sus movimientos directos, y Meowth-Galar
aparecía con `sourceSpecies: "meowth"`. La cadena correcta es
`[idPropio, baseSpecies, ...prevos]`.

**No aplanar los métodos.** `learn_methods` guarda cada método por separado con su
`gen` de origen y su `sourceSpecies`. La legalidad se decide en query time, nunca en
seed time (D3). Un booleano "lo aprende" pierde información que no se recupera sin
reseedear, y el filtro por level cap del torneo la necesita.

**No filtrar eventos al heredar.** Se probó excluir métodos `event` de generaciones
anteriores a la objetivo: gen 6 bajó de 62.198 a 61.918 filas y perdió movimientos
legacy, entre ellos cinco de Charizard-Mega-X. Se revirtió. No reintroducir sin una
regla de compatibilidad de eventos completa que preserve la monotonía.

**Límite conocido y aceptado:** tres eventos cuelgan de ramas propias
(`ninetalesalola/celebrate`, `lycanrocdusk/happyhour`, `polteageistantique/celebrate`)
que un oráculo estricto rechazaría. Sin relevancia competitiva. Documentado, no es un
bug a arreglar.

## Movimientos

**`basePower: 0` significa dos cosas distintas.** Movimiento de estado, o movimiento
de poder variable: Seismic Toss, Gyro Ball, Return, Low Kick, Grass Knot.
`category` desambigua el primero pero no el segundo. **Antes de mostrarle un
movimiento a un LLM en un prompt, no reportes "power 0" a secas** o el modelo lo va a
descartar por inútil. `@smogon/calc` sí lo resuelve bien por su cuenta.

**`accuracy: true` significa "nunca falla"** (Swift, Aerial Ace). En la base se guarda
como `NULL`, y es el único valor especial de la columna (D15). Al armar features o
prompts, `NULL` se traduce como **"nunca falla"**, jamás como "faltante" o
"desconocido": son los movimientos más confiables del juego, y tratarlos como dato
imputable es tomar la decisión exactamente opuesta a la correcta.

**Hidden Power.** En gen 6 las 17 variantes comparten id `hiddenpower`. El dedupe
conserva la entrada cuyo nombre normaliza a su propio id (la base, Normal, 60), que es
determinista sin depender del orden de iteración. El tipo real lo fijan los IVs, y el
protocolo de batalla siempre reporta `Hidden Power`.

## Especies

`baseSpecies` y `prevo` vienen como **nombre legible** (`"Charizard"`, `"Rotom"`), no
como id. La clave natural del proyecto es el id normalizado (D2), así que normalizá con
`dex.species.get(x).id` antes de guardar cualquier referencia entre especies. Showdown
usa `""` en vez de `null` para `forme` y `prevo`.

`tier` es el **único dato volátil** del seed: refleja el tiering de Smogon vigente, no
el histórico de la generación. Es la explicación por defecto si los conteos cambian tras
un bump.

## @smogon/calc

**No valida nada.** Un movimiento inexistente devuelve daño 0 sin error.
`item`, `ability` y `nature` inválidos se ignoran en silencio. La validación de
existencia por generación (vía `toID`) es responsabilidad de `packages/calc`.

**Strings exactos o silencio.** `weather: 'Harsh Sun'` no lanza error: se ignora
(el valor correcto es `'Harsh Sunshine'`). Peor todavía, **el allowlist de clima y
terreno está gateado por generación**: `'Hail'` en gen 9 se ignora en silencio (pasó a
llamarse `'Snow'` y da +50% de Defensa al tipo Hielo), igual que los climas primordiales
y los terrenos antes de gen 5. Aceptar un valor fuera de la mecánica de la generación
devuelve "sin clima" disfrazado de "con clima".

No reimplementar la fórmula de daño en Python (D16). Una reimplementación no falla
ruidosamente: devuelve números sutilmente equivocados y el agente decide "esto no lo
mata" cuando sí lo mataba.

## Conteos de referencia (`pokemon-showdown@0.11.10`)

| tabla      | gen 6 | gen 9 |
|------------|------:|------:|
| pokemon    |   834 |   874 |
| moves      |   618 |   685 |
| items      |   283 |   248 |
| abilities  |   191 |   310 |
| type_chart |   324 |   361 |
| learnsets  | 62198 | 65642 |

Verificaciones independientes que confirman que el filtro por generación anda:
`abilities` gen 6 = 191 es exactamente el número de habilidades hasta Delta Stream, y
`type_chart` es 18² en gen 6 y 19² en gen 9 (entra el tipo Astral).

**Los 618 movimientos de gen 6 contra los 621 del dex nacional ya están explicados**
(D12): tres inobtenibles (`thousandarrows`, `thousandwaves`, `lightofruin`) y dos CAP
(`paleowave`, `shadowstrike`). **No volver a investigar esta diferencia.**

## Al agregar una generación nueva

1. Correr el seed con `--gen N` y comparar los conteos contra la tabla de D12.
2. Verificar una frontera de generación real, no que el paquete existe: que un tipo
   nuevo aparezca y uno viejo cambie de efectividad.
3. Revisar las formas regionales, que son donde la herencia de learnsets tiene el
   límite conocido de D14.
4. Actualizar la tabla de conteos en `DECISIONS.md` con la versión del paquete al lado.