# Decisiones — Ludex

Registro de decisiones no triviales del proyecto. Cada entrada documenta la
decisión y el motivo; no se reescriben, se agregan nuevas si algo cambia.

## D1 — Migraciones: SQL plano con dbmate

La fuente de verdad del esquema son archivos `.sql` versionados en
`db/migrations/`, corridos por [dbmate](https://github.com/amacneil/dbmate).

Motivo: el esquema lo van a consumir dos lenguajes (Python/SQLAlchemy en el
agente, Node en el seed y quizá en la web). Si un ORM es la fuente de verdad, el
otro lenguaje queda subordinado y necesita tipos generados igual. SQL plano deja
a los dos en pie de igualdad, y `pgvector`, los enums y las PKs compuestas se
escriben directo sin pelear con abstracciones.

Costo aceptado: los modelos SQLAlchemy de la fase 2 se escriben a mano.

## D2 — Clave natural: el id normalizado de Showdown

Cada entidad de data de juego se identifica por `(gen_id, showdown_id)`, donde
`showdown_id` es el id normalizado del paquete (`charizardmegax`, `thunderbolt`,
`leftovers`), no el nombre legible.

Motivo: es lo que aparece en el protocolo de batalla en runtime, es estable
entre versiones y evita ambigüedades de acentos, guiones y mayúsculas. El nombre
legible se guarda aparte, solo para mostrar.

## D3 — Herencia de learnsets: resuelta en el seed, sin aplanar

El seed camina la cadena de preevoluciones y escribe una fila por
`(pokemon, move)` que incluye los movimientos heredados. **`learn_methods`
conserva cada método por separado, con su generación de origen.** No es un
booleano.

Motivo: el torneo es por gimnasios con level cap, así que la diferencia entre
"por nivel 42", "por MT" y "por tutor" es exactamente el filtro que necesita
`round_availability`. Y Showdown codifica la generación de origen del método
(`6L45`, `5T`, `6M`): en gen6ou un movimiento transferido de una generación
anterior es legal, pero en un torneo con pokémon atrapados in-game no lo es. Si
el seed aplana, esa distinción se pierde y no se recupera sin reseedear.

**La regla de legalidad se aplica en query time, no en seed time.** El seed
guarda todo lo que el paquete sabe; quien consulta decide qué acepta.

Regla por defecto del torneo: se consideran legales solo los métodos con
`gen == generación del torneo`. Los métodos de generaciones anteriores quedan
almacenados y disponibles, marcados como transferidos, para que la UI pueda
mostrarlos como "legal en ladder, no en el torneo".

## D4 — Versiones pineadas y registradas en la base

`pokemon-showdown` se instala con versión exacta (`--save-exact`), y cada
corrida del seed escribe una fila en `seed_runs` con la versión del paquete, el
timestamp y los conteos por tabla.

Motivo: el paquete se actualiza seguido y el campo `tier` refleja el tiering de
Smogon vigente, no el histórico. Es el único dato volátil de todo el seed.
Cuando dentro de seis meses un reseed cambie los conteos, `seed_runs` responde
por qué en treinta segundos.

La imagen del server local de Showdown también se pinea a un tag concreto.

## D5 — El seed corre en el host

Postgres expone 5433 al host (5432 adentro del contenedor; ver D8: 5432 lo
ocupa otro proyecto del usuario) y `pnpm seed` corre fuera de Docker contra
`localhost`. Menos fricción para iterar y debuggear el volcado.

## D6 — Los mods de Showdown no filtran por generación

`Dex.mod('genN')` devuelve el dex completo, con contenido posterior marcado
`isNonstandard: 'Future'`. El filtro `entry.gen <= dex.gen && !entry.isNonstandard`
es responsabilidad del seed. Sin él se cargarían 523 especies de gens futuras en
un seed de gen 6.

## D7 — Se elimina `pokemon.is_nonstandard`

El filtro de D6 excluye todo lo nonstandard, así que la columna sería siempre
`NULL`.

## D8 — Puertos del host remapeados para convivir con `jets`

El puerto 5432 del host ya está ocupado por un contenedor de otro proyecto del
usuario (`jets-api-db-1`), que se queda prendido. En vez de detener un
contenedor ajeno, Ludex remapea únicamente el mapeo al host:

- Postgres: `5433:5432` en `docker-compose.yml`. Adentro de la red de compose
  el servicio sigue siendo `postgres:5432`; `migrate` no cambia.
- Showdown: `SHOWDOWN_LOCAL_PORT=8100` en `.env.example` (antes 8000), para el
  mismo tipo de eventual colisión en la fase 2.
- `DATABASE_URL` en `.env.example` pasa a
  `postgres://ludex:ludex@localhost:5433/ludex?sslmode=disable`.

Motivo: nunca se toca infraestructura de otro proyecto para hacer lugar a
Ludex. Solo cambia el mapeo host-contenedor; todo lo que corre dentro de la
red de docker-compose (el servicio `migrate`, y eventualmente `showdown`
resolviendo contra `postgres`) sigue usando los puertos internos estándar.

## D9 — Imagen de Postgres pineada a `pgvector/pgvector:0.8.5-pg16`

`docker-compose.yml` usaba `pgvector/pgvector:pg16`, un tag flotante que se
mueve entre versiones de pgvector y patches de Postgres. Se pinea a
`pgvector/pgvector:0.8.5-pg16`.

Motivo: fija la versión de pgvector, que es la que afecta el comportamiento de
los índices vectoriales y del retrieval por similitud que usa la fase 6, y a
la vez deja que los patches de Postgres sigan fluyendo dentro de 16.x para no
quedarse sin parches de seguridad. Se eligió el tag de versión y no el digest
`sha256` para no congelar también los patches de Postgres: el digest fijaría
la imagen entera, incluida la versión exacta de Postgres 16.x, lo cual va más
allá de lo que esta decisión busca pinear.

Coherente con D1 (dbmate pineado a `2.21`) y D4 (versiones exactas de
`pokemon-showdown` y de la imagen de Showdown): todo componente cuya versión
afecta el comportamiento observable del sistema se pinea a un tag concreto.

## D10 — Volumen de referencia de `extractLearnsets` para gen 6

Con `pokemon-showdown@0.11.10`, contando pares `(especie, código)` con
`gen <= 6` directamente desde `getLearnsetData` sin resolver herencia, hay
**49321** pares. Después de que `extractLearnsets` (ver D3, `packages/seed/src/extract/learnsets.ts`)
resuelve la herencia por `baseSpecies` y por cadena `prevo`, el número de filas
`(pokemon, move)` resueltas para gen 6 es **62198**.

Motivo: la herencia solo puede sumar filas (una especie nunca pierde un
movimiento que ya tenía directo), así que 62198 > 49321 es la primera señal de
que la cadena de herencia corrió. Si un reseed futuro devuelve un número igual
o menor a 49321 para gen 6, algo rompió la resolución de `baseSpecies` o de
`prevo` (por ejemplo, las 48 megaevoluciones de gen 6 quedando sin aportar
movimientos propios de su forma base). Sirve de canario barato sin tener que
inspeccionar fila por fila.

**Este canario está enforced**, no solo documentado: el test "resuelve mas
filas de las que hay directas" en `packages/seed/test/extract/learnsets.test.ts`
afirma `rows.length` tanto `toBeGreaterThan(49321)` (la relación conceptual,
sobrevive a cambios de versión del paquete) como `toHaveLength(62198)` (el
valor exacto pineado, detecta cualquier deriva). Se verificó rompiendo a
propósito la resolución de `baseSpecies` en `inheritanceChain`: el conteo cae
a 52482 filas y el test falla, mientras que sin esta aserción exacta el chequeo
`toBeGreaterThan(49321)` solo hubiera pasado en falso (52482 > 49321 igual).

**Corrección post-review (mismo valor de este canario, actualizado):** el
valor **62157** que este documento fijaba antes no era el correcto, sino el de
un segundo bug en la misma función, distinto del que este canario prueba.
`inheritanceChain` *reemplazaba* la forma por su `baseSpecies` en vez de
*anteponerla* (`current = dex.species.get(current.baseSpecies)` en vez de
empujar primero el id propio), así que toda forma con learnset propio —no solo
las megas, que en efecto no tienen uno— perdía sus movimientos directos.
Afectaba a Rotom-Wash/Heat/etc, Kyurem-Black/White, Meowstic hembra,
Wormadam-Sandy/Trash, Pikachu-Cosplay (19 especies en gen 6) y también, en el
sentido inverso, a formas como Meowth-Galar, que aparecía con sus 84 filas
todas atribuidas a `sourceSpecies: "meowth"`. El fix antepone el id propio a la
cadena (`[idPropio, baseSpecies, ...prevos]`) en vez de saltar directo a
`baseSpecies`; el `seen` Set ya existente evita duplicar la forma base consigo
misma, y las megaevoluciones (sin entrada propia en `getLearnsetData`) quedan
con el mismo conteo que su especie base, sin cambios. Ver
`packages/seed/test/extract/learnsets.test.ts`, test "las formas con learnset
propio conservan sus movimientos ademas de heredar".

## D11 — Puerto del host movido a 15432

El Postgres de Ludex expone `15432` en el host (sigue siendo 5432 adentro del
contenedor). Reemplaza al 5433 que fijaba D8.

Motivo: esta máquina tiene un `postgresql@14` nativo, instalado por Homebrew y
levantado como servicio, que bindea `127.0.0.1:5433` y `[::1]:5433`. Docker
bindea `*:5433`. Como el bind específico gana sobre el wildcard, una conexión a
`localhost:5433` llegaba al Postgres nativo y no al del proyecto: `psql -h
localhost -p 5433 -U ludex` devolvía `FATAL: role "ludex" does not exist`.

Esto es peor que un puerto ocupado, porque no falla al levantar: el contenedor
arranca sano, las migraciones corren bien (el servicio `migrate` habla con
`postgres:5432` por la red interna de compose, sin pasar por el host), y recién
el seed —que sí corre en el host— habría escrito en la base equivocada.

Se eligió 15432 y no 5434 para salir del rango donde se instalan bases por
defecto, y no se paró el servicio de Homebrew porque es de otro proyecto del
usuario y Homebrew lo relevanta al reiniciar, con lo que el conflicto volvería
sin aviso.

## D12 — Conteos de referencia de `pokemon-showdown@0.11.10`

Valores verificados corriendo el seed contra una base vacía (ver criterios de
aceptación §9), para poder comparar tras un bump de versión del paquete:

| tabla       | gen 6 | gen 9 |
|-------------|------:|------:|
| pokemon     |   835 |   874 |
| moves       |   619 |   685 |
| items       |   283 |   248 |
| abilities   |   191 |   310 |
| type_chart  |   324 |   361 |
| learnsets   | 62253 | 65642 |

**Superseded por D47 (MON-24) para el caso puntual de Floette-Eterna/Light of
Ruin.** La fila de gen 6 de arriba (835/619/62253) ya refleja ese arreglo;
834/618/62198 eran los valores originales de esta decisión, antes de D47. El
resto de esta nota describe el estado ANTERIOR a D47 -- queda para registro
de por qué el conteo cambió, no es el criterio vigente.

**Nota histórica: por qué gen 6 daba 618 movimientos y no los 621 del dex
nacional hasta ORAS.** Los 618 eran los usables antes de D47. Los 3 que
faltaban para 621 estaban en la data del paquete pero el filtro `isAvailable`
(D6) los excluía:

- `thousandarrows`, `thousandwaves` (firma de Zygarde-Completo) y
  `lightofruin` (firma de Floette-Eterna): `gen=6` pero
  `isNonstandard: 'Unobtainable'` — existen en el código de XY/ORAS y no son
  obtenibles en ningún juego.
- `paleowave` y `shadowstrike`: `isNonstandard: 'CAP'` — son movimientos del
  proyecto CAP de Smogon, no movimientos reales.

618 + 3 unobtainables = 621 del dex nacional; los 2 CAP no cuentan en ninguna
suma contra el dex nacional.

**Lo que cambió con D47:** `lightofruin` SÍ es battle-legal en
`gen6randombattle` -- es el movimiento propio de Floette-Eterna en el
catálogo estándar random-battle del paquete pineado -- pese a su
`isNonstandard` real-world; ahora se seedea. `thousandarrows`/
`thousandwaves` siguen excluidos: ningún set estándar de gen 6 los
referencia. 619 usables + 2 unobtainables no referenciados = 621 del dex
nacional, mismo total. Nadie vuelva a investigar esta diferencia salvo que el
paquete cambie de versión.

Motivo: son los mismos conteos que `seedGeneration` imprime y que
`seed_runs` (D4) persiste por corrida. Tenerlos también acá, fijos al lado de
la versión pineada del paquete, da un punto de comparación estático sin tener
que ir a buscar una corrida vieja en la base cuando se evalúe un bump de
`pokemon-showdown`.

Fila `learnsets` corregida tras el fix de D10 (antes decía 62157/64921, los
valores con el bug de `inheritanceChain` todavía presente).

El server local de Showdown (D5, `docker/showdown/Dockerfile`) pinea
`SHOWDOWN_REF=v0.11.10`, la misma versión exacta que `pokemon-showdown` en
`packages/seed/package.json` (D4).

## D13 — `seed_runs.row_counts` guarda conteos reales de tabla, no filas escritas

`finishRun` (`packages/seed/src/load/runs.ts`) ahora corre `count(*)` por
tabla, filtrado por `gen_id` (y por `pokemon.gen_id` vía join para
`learnsets`, que no tiene `gen_id` propio), y guarda eso en
`seed_runs.row_counts`. Antes guardaba lo que devolvía `upsertBatch`: la
cantidad de filas *enviadas* en la corrida, no las que quedaron en la tabla.

Motivo: el pipeline es enteramente upsert — no hay un solo `DELETE` ni
`TRUNCATE` en `packages/seed/src/`. Si un bump futuro de `pokemon-showdown`
deja de traer una especie o un movimiento que antes existía, la fila vieja
sobrevive para siempre (nadie la borra) y un contador de filas *escritas*
reportaría el número nuevo (más chico) sin que quede registro de que la tabla
en realidad todavía tiene la fila zombie de más. Eso rompe la promesa de D4:
"`seed_runs` responde por qué en treinta segundos" — con filas zombie
respondía mal, porque el número que guardaba no era el estado real de la
tabla.

**Alcance de este arreglo, explícitamente acotado:** esto NO agrega borrado de
filas obsoletas. Sigue sin haber ningún `DELETE`/`TRUNCATE` en el pipeline, así
que una especie o movimiento retirado en un bump de versión seguiría vivo en
la tabla indefinidamente, y una fase posterior que lea legalidad de
`learnsets` lo seguiría viendo como legal. Lo único que cambia es que
`row_counts` ahora refleja el estado real de la tabla para esa generación, así
que si eso pasa, la discrepancia entre "lo que bajó `pokemon-showdown`" y "lo
que hay en la tabla" queda visible comparando corridas de `seed_runs` en vez
de quedar escondida detrás de un contador que solo mide escrituras. Decidir
si el pipeline debe borrar filas obsoletas (y cómo: soft delete, `DELETE`
directo, etc.) es una decisión de diseño más grande, para otra rebanada.

## D14 — Resuelto: herencia aditiva en formas con línea evolutiva propia

`inheritanceChain` (en `packages/seed/src/extract/learnsets.ts`) recorre la
**unión ordenada y deduplicada** de dos ramas:

1. la forma y su cadena de preevoluciones propias;
2. la especie base y su cadena de preevoluciones.

La forma conserva así su learnset propio y todo lo heredado. `sourceSpecies`
permanece en cada entrada de `learn_methods`, por lo que el seed sigue guardando
todo lo que conoce el paquete (D3) sin aplanar cómo se obtuvo cada movimiento.

La alternativa excluyente `own.prevo || own.baseSpecies` está expresamente
descartada. Ya se intentó en fase 1 y regresionó gen 6: `gourgeistsmall` y
`gourgeistlarge` quedaron con cero movimientos y `gourgeistsuper` con cuatro,
porque tomar la preevolución propia impedía caer también a la rama base. Ambas
ramas son necesarias; elegir una no es una simplificación equivalente.

La implementación se verificó especie por especie contra la resolución previa:
el conjunto anterior debe ser subconjunto del nuevo, tanto en gen 6 como en gen
9. Además, las cuatro formas de Gourgeist conservan exactamente 66 movimientos.

Resultado con `pokemon-showdown@0.11.10`:

- Gen 6: 62.198 learnsets antes y después (delta 0).
- Gen 9: 65.624 antes, 65.642 después (delta +18).
- Los 15 faltantes detectados por el auditor quedaron presentes, incluido
  `ninetalesalola/moonblast`.
- La unión reveló además tres movimientos de evento en ramas propias:
  `ninetalesalola/celebrate`, `lycanrocdusk/happyhour` y
  `polteageistantique/celebrate`. El auditor estricto rechaza los dos primeros
  porque el evento no se transfiere de gen 7 a gen 9; el tercero es un límite
  más fuerte, porque el oráculo dice que Polteageist-Antique no aprende
  Celebrate de ninguna forma. Son movimientos de regalo sin relevancia
  competitiva y quedan documentados como límite conocido de esta resolución
  aditiva.

Se probó excluir, al heredar, métodos cuyo tipo fuera `event` y cuya generación
fuera anterior a la objetivo. La regla resultó demasiado amplia: gen 6 bajó de
62.198 a 61.918 filas y perdió movimientos legacy especie por especie (entre
ellos cinco de Charizard-Mega-X). Se revirtió. No debe reintroducirse sin una
regla de compatibilidad de eventos más completa que preserve la monotonía.

El re-seed se ejecutó dos veces sobre la base poblada y mantuvo los mismos
conteos. El arreglo no depende de tablas vacías y respeta el pipeline
upsert-only de D13.

Quien necesite legalidad estricta puede caminar la línea evolutiva **real** de
la forma con un recursive CTE sobre `evolves_from` y evaluar los métodos y su
generación de origen. `sourceSpecies` permite reconstruir la procedencia, pero
los tres eventos demuestran que pertenecer a la línea correcta no alcanza por
sí solo: también hay reglas de transferencia y compatibilidad de eventos que
el consumidor deberá aplicar.

## D15 — `moves.accuracy IS NULL` significa "nunca falla", no "desconocida"

Showdown codifica los movimientos que no pueden fallar (Swift, Aerial Ace,
Thunder bajo lluvia...) con `accuracy: true` en vez de un número.
`extractMoves` (`packages/seed/src/extract/moves.ts`) convierte ese `true` en
`NULL`, y es el **único** valor especial de la columna: todo `accuracy` no
nulo es un porcentaje entero (100 = siempre acierta salvo modificadores de
precisión/evasión).

Motivo: la alternativa —inventar un centinela numérico como 0 o 101— rompería
cualquier agregado (`avg`, `min`) y cualquier comparación numérica sin que la
base lo impida. `NULL` es el valor que SQL ya reserva para "no aplica", y la
semántica "no aplica porque no hay tirada de precisión" es exactamente la del
juego.

Riesgo que esta decisión cierra: un consumidor que lea `accuracy IS NULL`
como "precisión desconocida" (por ejemplo, un extractor de features que lo
impute como dato faltante) estaría tomando la decisión exactamente opuesta a
la correcta — son los movimientos MÁS confiables del juego. Quien arme
features debe traducir `NULL` a "nunca falla", no a "faltante".

## D16 — Cálculo de daño: servicio Node con `@smogon/calc`

`@smogon/calc` es la calculadora oficial de Smogon, mantenida y parametrizada
por generación. No hay equivalente en Python.

Motivo: el cálculo contempla los 16 rolls de daño, STAB, efectividad, clima,
pantallas, habilidades que modifican daño, objetos, críticos, quemadura y
multi-golpes, con diferencias por generación. Una reimplementación en Python
no falla ruidosamente: devuelve números sutilmente equivocados, y el agente
decide "esto no lo mata" cuando sí lo mataba.

Costo aceptado: un contenedor más y una llamada HTTP local por turno,
despreciable frente a la latencia de un LLM. Ventaja: la web de la fase 4 le
pega al mismo servicio.

`packages/seed` seguirá siendo Node, pero es una herramienta de build: no hay
Node vivo durante una batalla salvo el servicio de calc.

**Implementación (2026-07-26):** `packages/calc`, servicio HTTP sin estado
(POST `/calc`, GET `/health`), pineado a **`@smogon/calc@0.11.0`** con versión
exacta en `package.json` (misma regla que D4: el paquete contiene los números
de la fórmula vía `@pkmn/data`, así que su versión afecta el comportamiento
observable). La imagen se construye con `pnpm install --frozen-lockfile`
sobre el lockfile del monorepo, que es lo que congela también las
dependencias transitivas. `GET /health` expone `calc_version` para auditar
bumps. Se bindea a `127.0.0.1` en compose (regla de D11).

Dos comportamientos del paquete que el contrato tiene que conocer, medidos en
la inspección (ver `.superpowers/sdd/kimi-calc.md`):

- El paquete **no valida nada**: un movimiento inexistente calcula daño 0 sin
  error, y `item`/`ability`/`nature` inválidos se ignoran en silencio. La
  validación de existencia (por generación, via `toID`) es del servicio.
- Strings exactos o silencio: `weather: 'Harsh Sun'` no lanza error pero se
  ignora (el tipo del paquete es `'Harsh Sunshine'`). El contrato solo admite
  los strings exactos del paquete. **Y el allowlist de clima/terreno está
  gateado por generación**: el paquete ignora en silencio valores fuera de la
  mecánica de cada gen (`'Hail'` en gen 9 — el granizo pasó a llamarse
  `'Snow'` y da +50% de Defensa al tipo Hielo—, los climas primordiales y los
  terrenos antes de gen 5). Aceptarlos sería devolver "sin clima" disfrazado
  de "con clima", la misma clase de bug que esta decisión existe para evitar.

**Límite conocido (2026-07-27, review de calc):** el servicio corre como root
dentro del contenedor. Con el bind a `127.0.0.1` y sin datos sensibles no es
una vulnerabilidad activa, pero si alguna vez se expone fuera de localhost es
lo primero a revisar, antes que rate limiting o auth.
## D17 — El protocolo crudo es la fuente de verdad; el estado derivado es una vista

Se persisten **ambos**: el stream de protocolo tal como lo recibe cada jugador,
turno por turno, y el estado normalizado que produce el serializador, con su
`state_schema_version`.

Motivo: convierte la única decisión irreversible de la fase en reversible. Si
dentro de seis meses se descubre que el serializador filtraba información oculta
o le faltaba un campo, se re-deriva todo el histórico sin perder una batalla. El
costo es texto, que es barato.

Consecuencia de diseño: el serializador debe ser una función pura del protocolo
crudo más el estado de poke-env, sin depender de nada que no quede persistido.

Esta decisión ya se cobró dos veces en la práctica, no solo en la teoría: la
review final reconstruyó 6 `winner` que un bug de idempotencia había
nulificado leyendo `battle_turns.protocol_lines` (`|win|<nombre>`), y C1/C2
(ver D20 y D21) son recuperables — el histórico mal alineado o incompleto se
puede volver a derivar en vez de descartarse — precisamente porque el
protocolo crudo de esas 57 batallas de prueba seguía intacto.

## D18 — El serializador es una lista blanca explícita, nunca una copia

El serializador nombra campo por campo lo que entra en el estado. Está prohibido
recorrer atributos del objeto `Battle` de poke-env o serializarlo genéricamente.

Motivo: poke-env expone tu equipo completo y el del rival con la misma forma,
pero del rival solo debés ver lo revelado. Una copia genérica filtra el equipo
entero del oponente en el turno 1. Con lista blanca, un campo nuevo en una
versión futura de la librería no se cuela solo.

Verificado sobre el dataset completo (16004 snapshots del rival revisados en la
review final): 0 fugas reales de especie, 0 movimientos no revelados, stats del
rival ausentes en el 100% de las filas. El test señuelo (`atributo_privado`)
falla de verdad si alguien mete un `vars(mon)`; el mismo señuelo falta todavía
en el fake de `Battle` (queda abierto, ver minor 7 de la review final).

## D19 — Se juega `gen6randombattle`, no `gen6ou`

Los equipos los genera el servidor.

Motivo: construir y validar equipos es fase 5. Y como el server local genera los
dos equipos, tenemos la verdad completa contra la cual contrastar lo que el
serializador *debería* haber visto — un banco de pruebas mejor que un formato
con equipos propios.

## D20 — C1: el desfase de un turno se corrige con materialización diferida, no con una espera antes de responder

> **Superada por [D31](#d31--el-snapshot-de-decisión-se-completa-con-el-frame-público-ya-emitido-antes-del-lock).**
> El síntoma y la causa raíz de acá siguen siendo correctos, pero la premisa de
> que la narración llega "en el mismo lote" que el `|request|` es **falsa** (son
> frames de websocket distintos, cada uno con su task), y por eso la
> materialización diferida no arreglaba nada: refrescaba al final del lote del
> request, cuando la narración todavía no había llegado. La medición: 297/762
> decisiones (39.0%) informaban al proveedor un activo rival equivocado. Leer D31
> antes de tocar este mecanismo.

**Síntoma medido** (review final de `feat/agent-conexion-estado`, sobre 3123
filas reales): mi lado del estado queda post-resolución de un turno mientras
el lado del rival queda un turno atrás, dentro de la misma fila. Causa: en
`sim/battle.ts`, Showdown manda el `|request|` de la próxima decisión (que ya
refleja mi equipo post-turno, incluida una mega evolución) **antes** de
narrar lo que pasó en el turno (`sendRequests()` antes que `sendUpdates()`).
poke-env llama `choose_move` apenas parsea ese `|request|`.

**Primer intento (descartado): esperar la narración antes de responder.**
El diagnóstico original asumía que la narración "ya venía en el cable",
independiente de nuestra respuesta, y proponía esperar dentro de
`_handle_battle_message`, antes de delegar en `super()` (que dispara
`choose_move`), a que `battle.turn < recorder._current_turn` se cerrara.
Medido con instrumentación en vivo contra el código real
(`sonda_deteccion_c1.py`, dos batallas completas, ~110 decisiones): esa
desigualdad **no se cumple ni una sola vez**, porque el lote que trae el
`|request|` nunca contiene todavía la línea `|turn|` siguiente — llega en el
lote posterior — así que ambos valores son siempre iguales en el instante en
que se los compara.

Reemplazando el detector por "esperar a que lleguen más líneas de protocolo"
(sin importar el número de turno) tampoco alcanzó: agotaba el timeout en el
100% de las decisiones de una batalla real. La causa, verificada con un
experimento decisivo (`sonda_causalidad.py`): la narración de un turno es la
**respuesta del servidor a que ambos jugadores ya eligieron para ese turno**,
no datos independientes ya en tránsito. Retrasar artificialmente la propia
respuesta 500ms retrasó la narración exactamente 500ms. Esperar antes de
responder es esperar algo que no puede existir todavía sin que primero
respondamos — con `max_concurrent_battles=1` y las dos puntas en el mismo
proceso (la topología real de `cli.py`), esto es un punto muerto garantizado,
no una carrera a veces perdida.

**Arreglo que funciona: diferir la serialización, no la respuesta.**
`choose_move` sigue devolviendo la orden de inmediato — la latencia de juego
no cambia — pero la fotografía del estado se hace en una tarea de fondo que
espera, acotada, a que lleguen líneas de protocolo nuevas para ese
`battle_tag` antes de llamar a `serialize_battle`. Como esa espera ya no
bloquea la respuesta, el servidor sigue su curso normal y la narración llega
sola: medido sobre 160 decisiones reales (`sonda_defer.py`), 0 timeouts, 0 a
~11ms de espera. El orden de los pasos se preserva reservando el índice de
`self.steps[tag]` en el momento síncrono de `choose_move`, no dejando que
cada tarea decida su propia posición.

La etiqueta `turn` de la fila **no** se corrige por decisión, en paralelo, en
cada tarea de fondo — esa fue la segunda versión de este arreglo, y también
se descartó por medición. Se corrige en una **pasada final**
(`LudexPlayer._correct_step_turns`, llamada desde `wait_for_pending_steps`,
después de que todas las tareas de fondo terminaron), que recorre
`self.steps[tag]` **en el orden real de las decisiones** con un cursor global
(`ProtocolRecorder.entries_from`) que solo avanza, buscando para cada una la
línea `|move|`/`|switch|` propia que la ejecuta:

- **Por qué una pasada final y no una corrección por tarea:** las tareas de
  fondo terminan en el orden en que a CADA UNA le llega su narración, no
  necesariamente en el orden en que se tomaron las decisiones. Corregir en
  paralelo permitía que dos decisiones que mencionan la misma especie o el
  mismo movimiento (dos turnos seguidos de Outrage por el bloqueo del
  movimiento; volver a cambiar al mismo pokémon) reusaran por accidente la
  línea de protocolo que le pertenecía a la otra.
- **Por qué un cursor global y no un rango de turnos:** un cursor que solo
  avanza garantiza que la línea consumida por la decisión N nunca puede
  reasignarse a la N+1, sin importar cuánto se parezcan sus acciones.
- **Por qué además un techo (`decision_turn + 3` turnos) y no una búsqueda
  sin límite:** sin techo, una decisión **excusada** (ver más abajo, la
  acción nunca se ejecutó) deja el cursor atrasado, y la búsqueda de la
  decisión siguiente puede escaparse muchos turnos hacia adelante y
  encontrar por accidente una repetición no relacionada del mismo nombre
  —`gen6randombattle` repite nombres de movimiento y de especie todo el
  tiempo—. `decision_turn` (`battle.turn` capturado sincrónicamente al
  decidir) es siempre `<=` el turno real, así que nunca es un techo falso.
- La etiqueta arranca en `decision_turn`, no en `battle.turn` leído al
  despertar la tarea de fondo (que ya puede estar adelantado): así, si no se
  encuentra ninguna línea, la fila queda con el mejor valor disponible en
  vez de uno adelantado de más.

**Límite conocido, cuantificado, no resuelto en esta rama — y no resoluble
por este mecanismo:** una acción elegida puede **no ejecutarse nunca** si el
propio pokémon se debilita por un ataque del rival más rápido antes de que le
toque actuar. Showdown no deja **ningún** rastro de esto: ni `|cant|`, ni
`|-fail|`, ni ninguna línea — el movimiento elegido simplemente no aparece en
ninguna parte del protocolo, porque nunca llegó a resolverse. No es un bug de
esta captura: ninguna cantidad de búsqueda contra el protocolo puede
encontrar una línea que no existe. Medido sobre corridas reales limpias
(`apps/agent`, `gen6randombattle`, este mismo código): de 6 corridas de 2
batallas cada una, 5 tuvieron al menos una fila así (sobre ~70-100 acciones
por corrida); en todos los casos verificados a mano, la fila corresponde a
una acción elegida que el propio pokémon nunca llegó a ejecutar. Es
recuperable vía D17 (el protocolo crudo sigue completo, así que se puede
volver a derivar o marcar explícitamente el día que se decida tratarlo), pero
tratarlo requiere una señal nueva —detectar que el propio pokémon se debilitó
ese turno y excusar la fila explícitamente, en vez de buscar una línea que no
existe— que excede esta ola de arreglos y queda para una decisión de
producto: ¿se excusan estas filas en el consumidor del dataset, se marcan con
un campo (`action_executed: bool`), o se aceptan tal cual (la etiqueta de
turno sigue siendo la del momento en que se decidió, que es honesta aunque no
tenga una línea de protocolo que la confirme)?

**Corrección (fix-flaky, D23): este párrafo estaba desactualizado y describe
un caso que ya NO es cierto.** Para cuando se escribió, sonaba plausible que
un debilitamiento propio no dejara rastro — pero un pokémon que se debilita
SIEMPRE narra `|faint|` (es la propia mecánica de Showdown, no algo opcional),
y ese rastro es exactamente el segundo de los tres que `_find_action_line`
reconoce (ver el resumen al principio de esta entrada). Cazando el defecto
intermitente de `test_la_accion_de_la_fila_corresponde_a_su_propio_turno`
(D23) sobre ~14 casos reales capturados en vivo, **ninguno** resultó ser
"cero rastro alcanzable": los 14 se explicaron con evidencia positiva y se
cerraron. El límite que sí sigue abierto, mucho más angosto que lo que este
párrafo describía, queda documentado en D23.

## D21 — C2: `trajectory_steps` se identifica por `(trajectory_id, decision_index)`, no por `(trajectory_id, turn_number)`

Un cambio de reemplazo forzado tras un debilitamiento **no avanza
`battle.turn`**: Showdown manda el nuevo `|request|` (`forceSwitch: true`)
dentro del mismo turno. Con la clave vieja, dos decisiones distintas caían
bajo el mismo `(trajectory_id, turn_number)` y el `ON CONFLICT` de `save_step`
pisaba la primera con la segunda. Medido sobre las 57 trayectorias de prueba:
57 de 57 tenían exactamente un paso por número de turno, contra 271
debilitamientos propios — la clase completa "elegir el reemplazo tras un
debilitamiento" (~7.5% de las decisiones) no llegaba nunca a la base.

`decision_index` cuenta **decisiones canónicas resueltas**, no turnos ni
invocaciones técnicas: arranca en 0 por trayectoria y avanza cuando Showdown
resuelve una elección de juego. Un retry causado por `[Invalid choice]` o
`[Unavailable choice]` es otro `attempt_index` interno de la **misma** decisión
y reemplaza su mismo slot; no crea otro `decision_index` (D34). En cambio, un
`forceSwitch` posterior sí es una decisión canónica nueva aunque comparta
`battle.turn`. `turn_number` queda como columna común, ya no parte de la clave:
dos decisiones pueden compartir turno y eso ahora es representable.

Sin backfill (migración `20260727000006`): las 57 batallas grabadas hasta la
review final eran de prueba, random contra random, y no sirven para entrenar.
Se truncan y se regraban con `agent play`.

## D22 — C-1 vuelve a ser síncrona; el cursor de `_correct_step_turns` reconoce
tres formas de "la acción no se ejecutó" y deja de robarle líneas a la
decisión siguiente

> **Complementada por [D31](#d31--el-snapshot-de-decisión-se-completa-con-el-frame-público-ya-emitido-antes-del-lock).**
> Que la captura sea síncrona sigue siendo obligatorio y D31 no lo relaja: la foto
> del snapshot y la máscara se siguen tomando antes del primer `await`. Lo que
> agrega D31 es de dónde sale la parte del rival — de una proyección pura sobre el
> frame público **ya emitido**, esperado en un inbox pre-lock, y nunca releyendo
> el objeto `battle`. `_finalize_pending_steps` ya no existe.

Dos correcciones de la última puerta antes del merge de
`feat/agent-conexion-estado` (review-merge.md), sobre el mismo mecanismo:

**Primera parte — por qué C-1 (D20) volvió a ser síncrona (commit
`dd62bc8`).** D20 documentó la materialización diferida (`3ea7caf`) como el
arreglo que funciona. La review de merge encontró que **no** lo era: la task
de fondo lee `battle`, un objeto mutable, y para cuando corre, el
planificador de asyncio puede haber despachado ya la decisión N+1 —
`serialize_battle(battle)` fotografía entonces el punto de decisión
SIGUIENTE, no el de la decisión N que originó la task. Medido sobre datos
reales: 7 de 6625 filas (~0.11%) con `action_taken` fuera de su propia
`legal_actions`, un piso (carrera contra el planificador) y no un techo. La
corrección: nada que tenga que ser consistente con la decisión (`legal_actions`,
`action_taken`, `decision_turn`) se vuelve a leer de `battle` después de que
la decisión pasó — se captura sincrónicamente en `choose_move`, como antes de
`3ea7caf`, y `action_taken ∈ legal_actions` vuelve a valer por construcción.
Lo que SÍ seguía haciendo falta esperar (el lado del rival) se resuelve
dentro de la MISMA llamada síncrona a `_handle_battle_message`, nunca en una
task planificada aparte (ver el docstring de `LudexPlayer` en `client.py`
para el detalle completo). Verificado: 12 batallas nuevas, 774 filas, cero
fuera de máscara.

**Segunda parte — el cursor de `_correct_step_turns` pierde líneas (fix
posterior al merge, "fix-cursor").** Con C-1 cerrado, la corrección de
`turn_number` (D20, `_find_action_line`) seguía teniendo un defecto
independiente: el cursor global, que existe para que dos decisiones no
compartan línea, podía en cambio **perder** la línea real de una decisión
posterior. Instrumentado y verificado sobre datos reales (no se dio por
buena la hipótesis original antes de medir), con dos mecanismos distintos,
ambos con el mismo síntoma:

1. **Una forma de "no se ejecutó" que el código no reconocía.**
   `battle-gen6randombattle-398`, decisión 45 (Muk elige Brick Break): el
   pokémon se autogolpea por confusión (`|-activate|p1a: Muk|confusion`,
   sin `|move|` ni `|cant|`) — un tercer rastro que Showdown sí deja y que
   `_find_action_line` no miraba. Sin nada que matchear en su propio
   bloque, la búsqueda seguía de largo y el respaldo (que solo miraba el
   LADO, `p1a`/`p2a`, nunca el pokémon) aceptaba el `|faint|p1a: Swellow`
   de un pokémon **no relacionado**, dos turnos más adelante, en una cadena
   de cambios sin ninguna conexión con la decisión de Muk. Eso adelantaba
   el cursor más allá de la línea real de la decisión SIGUIENTE (cambio a
   Ludicolo, turno 44), que quedaba entonces inalcanzable.
2. **Un match "real" posterior ganándole a un respaldo válido anterior.**
   `battle-gen6randombattle-408`: Volbeat congelado resuelve su decisión de
   "Encore" con `|cant|...|frz` en su propio turno — evidencia válida y
   completa —, pero la búsqueda seguía escaneando y encontraba, dos turnos
   después, el `|move|...|Encore||[still]` de la decisión **siguiente**
   (un segundo intento de Encore, que también falla). Una decisión se
   resuelve una sola vez: un match del mismo nombre después de un respaldo
   ya encontrado en su propio bloque no puede ser evidencia de ESA
   decisión.

**El arreglo, en `_find_action_line`/`client.py`:**

- Se reconoce el autogolpe por confusión (`|-activate|{side}a: Name|confusion`,
  sin `|move|` en el mismo bloque) como una tercera forma válida de
  respaldo, al mismo nivel que `|cant|` y `|faint|` propio.
- El respaldo (`|cant|`/`|faint|`/confusión) ahora exige que el pokémon
  nombrado en esa línea sea el mismo que iba a actuar (`actor_species`,
  capturado sincrónicamente en `choose_move` junto con `legal_actions`).
  Se compara por `base_species`, no por `species`: Showdown identifica al
  actor en `|move|`/`|switch|`/`|cant|`/`|faint|` siempre con el nombre
  BASE (`p1a: Arceus`, nunca `p1a: Arceus-Poison`; lo mismo con Rotom,
  Giratina-Origin, Wormadam, Keldeo-Resolute, Landorus-Therian,
  Thundurus-Therian, Shaymin-Sky), mientras que `mon.species` de poke-env sí
  incluye la forma (`arceuspoison`) — comparar contra `species` directamente
  rompía el chequeo para cualquier pokémon con forma alternativa.
- Una vez que la búsqueda encuentra un respaldo válido dentro de su propio
  turno, deja de escanear turnos posteriores buscando un match "real": si
  el turno avanza más allá del turno del respaldo sin que apareciera un
  match real ANTES, se corta ahí. Un match real posterior pertenece, por
  definición, a la decisión siguiente que repite el mismo nombre.

Verificación tras el arreglo: instrumentación decisión-por-decisión sobre
`battle-gen6randombattle-398` (las cinco decisiones de cambio a Ludicolo
pasan de 14/22/31/**43**/**48** a 14/22/31/**44**/48, la real) y sobre
`battle-gen6randombattle-408` (la cadena Encore/Thunder Wave deja de
robarse líneas entre sí); 12 batallas frescas (`gen6randombattle`, 809
filas con acción): 785 con match directo, 24 excusadas por
`cant`/`faint`/confusión propio, **0 residuales**; suite completa (87
tests) verde en 3 corridas consecutivas, cada una jugando batallas nuevas
contra el server local.

**No cambia lo que el cursor ya protegía:** dos decisiones que mencionan el
mismo movimiento o la misma especie (dos Outrage seguidos, dos cambios al
mismo pokémon) siguen sin poder compartir línea — los tests que cubrían eso
(`test_find_action_line_no_reusa_una_linea_ya_consumida`,
`test_correct_step_turns_corrige_en_orden_con_un_cursor_que_avanza`) pasan
sin modificación.

**Defecto 2, siete filas envenenadas de builds intermedios.** El invariante
`action_taken ∈ legal_actions` (D-1 de esta entrada) seguía fallando por 7
filas de 5 batallas de prueba (`battle-gen6randombattle-246/277/290/295/362`,
grabadas el mismo día con builds intermedios de esta rama, antes de
`dd62bc8`, random contra random). Se decidió borrarlas —no re-derivarlas—
por `battle_tag` exacto, con cascada: mantener una fila que se sabe
contradictoria es peor que perder datos sintéticos sin valor de
entrenamiento, y hay backup del día. `battles`: 150→145; `trajectories`:
149→144; `trajectory_steps`: 8939→8672. Verificado tras el borrado: 0 filas
fuera de máscara en toda la base.

**Pendiente, no resuelto por este mecanismo:** el límite documentado en D20
(acción elegida que nunca se ejecuta porque el propio pokémon muere sin
`cant`/`faint` reconocible DENTRO del margen de búsqueda) sigue sin trato —
ninguno de los dos mecanismos de esta entrada lo toca. Datos de dataset real
(`source='local'`) anteriores a este arreglo conservan 9 filas con
`turn_number` retrocedido en 7 trayectorias (Minor 9 de review-merge.md, ya
señalado como deuda de higiene, no tocado en esta tarea).

## D23 — fix-flaky: `test_la_accion_de_la_fila_corresponde_a_su_propio_turno`
fallaba ~1 de cada 10 corridas por CUATRO causas reales, ninguna "sin rastro"

**Encargo:** el test fallaba de forma intermitente (del orden de 1 en 10
corridas), y la hipótesis heredada (D20: "acción cuyo pokémon muere sin dejar
rastro alcanzable") era una explicación no medida. Se pidió cazar el defecto
en vivo, capturar casos reales de la base (las filas persisten aunque el test
falle) y NO aceptar una excusa sin diagnosticar qué la necesita — precedente
explícito: una explicación que cubría 3 de 20 casos se había presentado antes
como si cubriera todo (fix-final-report.md).

**Método:** loop de `pytest` contra el test solo (cada corrida juega 2
batallas nuevas via la fixture `jugadas`), capturando el log en cada falla.
Con el `battle_tag` del mensaje de assertion se identifica la fila exacta
(`turn_number`, `decision_index`, `action_taken`) y se vuelca el protocolo
crudo de `battle_turns` para ese turno y los siguientes. Se juntaron **14
casos reales** en dos tandas (4 + 10, sobre ~90 corridas), no solo los 3
pedidos como mínimo — la instrucción de no cerrar con una muestra chica
aplicaba en ambas direcciones.

**Resultado: CUATRO causas reales, todas con evidencia positiva en el
protocolo crudo. Cero casos de "no hay rastro".**

1. **Autogolpe por confusión (5 de 14 casos) — no era un defecto del
   grabador, era un defecto del TEST.** `_find_action_line` (`client.py`)
   reconoce el autogolpe por confusión desde D22 (fix-cursor). Pero el
   helper del test, `_propio_no_actuo`, es una reimplementación
   INDEPENDIENTE de la misma lógica — nunca se actualizó cuando D22 agregó
   ese tercer caso. El test fallaba sobre filas correctamente etiquetadas
   por el grabador; el defecto era que el propio chequeo de verificación no
   sabía reconocer una de sus tres excusas conocidas. Arreglo: se agregó el
   reconocimiento de `|-activate|{side}a: Name|confusion` a
   `_propio_no_actuo`, en paridad con producción.
2. **El rival encorea antes de que la acción elegida se ejecute (3 de 14
   casos, ej. `battle-gen6randombattle-558`, `-581`, `-624`) — defecto real,
   no reconocido ni en producción ni en el test.** Encore tiene prioridad
   (+2) y, si el rival lo usa el mismo turno en que se iba a actuar, fuerza
   la repetición del ÚLTIMO movimiento propio en vez de narrar la acción
   recién elegida — Showdown nunca deja una línea con esa acción. Arreglo:
   nuevo respaldo en `_find_action_line` que reconoce
   `|move|{opp}a: X|Encore|{side}a: Name` (nombrando al mismo actor) como
   evidencia de que la decisión se resolvió ahí. **Con un guardrail medido
   en carne propia**: Encore solo bloquea MOVIMIENTOS, nunca cambios — la
   primera versión de este arreglo, sin distinguir `action_taken.kind`,
   causó una regresión nueva (`battle-gen6randombattle-684`, decisión 22, un
   cambio real a Kangaskhan-Mega que resuelve un turno después) al cortar la
   búsqueda antes de encontrar la línea real. Detectada corriendo la suite
   completa después del primer intento del arreglo, no asumida.
3. **La batalla termina antes de que la decisión se resuelva (1 de 14 casos,
   `battle-gen6randombattle-571`) — defecto real, no reconocido.** Raikou
   elige Substitute; el Suicune rival se remata solo con el retroceso de
   Struggle en el turno siguiente y la batalla termina (`|win|`) antes de
   que le tocara jugar esa acción. Arreglo: `|win|`/`|tie|` como respaldo
   incondicional (sin chequeo de actor/lado: cierran la trayectoria entera,
   no hay decisión siguiente a la que robarle la línea).
4. **Illusion disfraza un switch-in con el nombre de otro pokémon (1 de 14
   casos, `battle-gen6randombattle-657`) — defecto real, arquitectónicamente
   distinto de los otros tres.** Se elige cambiar a Zoroark; Showdown narra
   el cambio con el nombre del ÚLTIMO compañero de equipo vivo
   (`|switch|p1a: Drapion|...`), nunca "Zoroark" — la comparación por
   substring no puede encontrarlo nunca. La única evidencia es la
   revelación posterior (`|replace|{side}a: X|X, .../-end|...|Illusion`),
   que en el caso real tardó **14 turnos**: muy por fuera de
   `ACTION_SEARCH_MARGIN_TURNS` (3). Es la única excepción de
   `_find_action_line` que no respeta `max_turn` — la revelación de Illusion
   no tiene ventana razonable, y no hay riesgo de "robo de línea" porque se
   corta apenas aparece OTRO switch propio sin revelarse antes.

**No en el alcance original, encontrado verificando la suite completa (no
solo el test que motivó la tarea):** dos defectos independientes en
`test_no_hay_fuga_de_informacion_del_rival` — Relic Song de Meloetta
(`|-formechange|`, alterna Aria/Pirouette, tipos Normal/Psíquico ↔
Normal/Lucha) y la Reversión Primal de Groudon/Kyogre (`|detailschange|`,
cambia tipos) no estaban cubiertos por `_tipos_por_cambio_dinamico` (que solo
reconocía `-start|typechange`, el patrón de Protean/Libero). Mismo tipo de
causa —un cambio de forma público y narrado, tratado como si no lo
estuviera—, mismo arreglo (nueva función espejo,
`_formas_reveladas_por_cambio_de_forma`, con lookup a dex). No se habría
encontrado sin correr la suite completa repetidamente como pedía la
verificación.

**Por qué no se declaró "límite conocido, no resoluble" para ninguno de los
cuatro:** los tres primeros dejan evidencia acotada dentro de
`ACTION_SEARCH_MARGIN_TURNS` (2-3 turnos como mucho, medido). El cuarto
(Illusion) tiene evidencia NO acotada en el tiempo, pero sigue siendo
evidencia positiva real, nunca ambigua (`|replace|.../-end|...|Illusion` es
inconfundible y específico a una especie), así que se implementó igual en
vez de excusarlo. El párrafo de D20 que hablaba de "ningún rastro" describía
una situación que, medida ahora, no ocurre: un debilitamiento SIEMPRE narra
`|faint|` (ver corrección agregada a D20).

**Verificación:** 14 casos reales documentados con protocolo crudo pegado
(fix-flaky.md); tests unitarios nuevos en `test_client.py` reproduciendo cada
causa en miniatura (incluida la regresión del guardrail de Encore/switch);
suite completa corrida repetidamente en verde tras cada arreglo, incluida la
corrida que expuso la regresión de Encore/switch y las dos causas de
Meloetta/Groudon.

**Archivos tocados:** `apps/agent/src/ludex_agent/showdown/client.py`
(`_ident_matches`, respaldo de Encore con guardrail `action_taken.kind`,
respaldo de `|win|`/`|tie|`, `_illusion_revela_a`);
`apps/agent/tests/showdown/test_client.py` (8 tests nuevos);
`apps/agent/tests/integration/test_play.py` (`_propio_no_actuo` con paridad
de las 4 formas, `_illusion_revela`, `_formas_reveladas_por_cambio_de_forma`).

## D24 — `websockets==16.0` se fuerza con un override de uv para el grafo local

`poke-env==0.15.0` exige exactamente `websockets==16.0`. `langgraph==1.2.9`
trae transitivamente `langgraph-sdk>=0.4.2`, cuyo metadata declara
`websockets>=14,<16`; no existe una resolución normal compatible entre las
últimas versiones. No se baja poke-env: el grabador y sus correctores de
turnos dependen de su comportamiento observable y fueron verificados contra
0.15.0.

El agente usa `StateGraph` local, en proceso. No usa LangGraph Platform ni el
transporte WebSocket de `langgraph-sdk`, por lo que se fijó
`[tool.uv] override-dependencies = ["websockets==16.0"]`. No es una suposición
silenciosa: un test importa poke-env y LangGraph juntos, compila y ejecuta un
grafo mínimo; otro juega una batalla real en el mismo proceso. La suite del
agente queda como canario del único consumidor real de websockets.

Si se incorpora LangGraph Platform, este override deja de considerarse
inocuo. Antes de habilitar el cliente remoto hay que retirar o reevaluar el
override, resolver la matriz de versiones soportada por `langgraph-sdk` y
ejercer sus transports —incluido streaming WebSocket— con integración real.

## D25 — autoría y camino interno de una acción son ejes separados

`trajectory_steps.action_source` responde quién decidió (`agent`, `human` u
`opponent`). El nuevo `action_path` responde cómo llegó el agente a la
decisión (`llm`, `llm_retry` o `fallback`). Para los tres caminos la autoría
sigue siendo `agent`; mezclar `fallback` dentro de `action_source` volvería
ambiguas consultas futuras sobre intervención humana.

`action_path` es `text NULL` con un `CHECK` acotado, un desvío deliberado de
los enums nativos usados en otras columnas. Este eje crecerá con la
aprobación humana y el contexto, y Postgres no permite quitar valores de un
enum con la misma facilidad. No sienta precedente general para reemplazar
otros enums. `NULL` significa “fila histórica sin camino de decisión
registrado”: las decisiones previas fueron aleatorias y asignarles un default
sería inventar su procedencia.

## D26 — el grafo separa fallos de infraestructura de fallos semánticos

La decisión por turno corre localmente como
`parse_state → calc_damage → decide`. El prompt recibe solamente la fotografía
allowlisted, la máscara legal y resultados deterministas de calc. El respaldo
prioriza KO garantizado (`min_damage >= HP restante`), acota el daño esperado
al HP restante y, en cambios forzados, elige por minimax del peor daño
esperado relativo.

Hay dos reintentos distintos y no comparten contador:

- 429 rota la clave dentro del proveedor y repite exactamente el mismo pedido.
- 5xx/timeout reintenta el mismo pedido sin consumir el reintento semántico.
- JSON inválido o acción fuera de máscara consume el único reintento semántico;
  una segunda respuesta inválida usa `fallback`.

Gemini toma primero `GEMINI_API_KEY/GEMINI_API_KEYS`, que son los nombres del
entorno real. `GOOGLE_API_KEY/GOOGLE_API_KEYS` se aceptan después como aliases
compatibles y se deduplican; las claves siguen existiendo solo en memoria.

Las excepciones de proveedor heredan de `RuntimeError`, nunca de `ValueError`.
Por eso atraviesan el bucle semántico: pool/cadena agotados y deadline vencido
abortan ruidosamente y jamás producen `action_path='fallback'`. La cadena
entre proveedores se permite al jugar, pero queda prohibida en benchmark:
proveedor y modelo permanecen fijos o la medición aborta informando cuántas
batallas terminó, sin publicar un winrate comparable.

El presupuesto interno total por decisión es 240 s. No representa un reloj
permanente de Showdown: la batalla solo tiene límite por turno cuando su timer
está activado. Cuando sí lo está, este presupuesto debe revisarse contra el
margen real de ese servidor. Fotografía, máscara y mapa acción→`BattleOrder`
se capturan sincrónicamente antes del primer `await`; el grafo nunca relee el
`Battle` mutable. El corrector de turnos continúa juzgando contra protocolo
crudo y no fue modificado.

Baseline random, `gen6randombattle`, 300 batallas por rival, concurrencia 20:
47,67% contra RandomPlayer (143–157), 11,67% contra MaxBasePowerPlayer
(35–265) y 3,00% contra SimpleHeuristicsPlayer (9–291). Se versionó completo
en `apps/agent/evals/random-baseline.json`. Las métricas LLM reales quedan
**pendientes por falta de clave**, no en cero.

## D27 — Typer 0.15.1 requiere Click 8.1.8 en este proyecto

Al agregar el subcomando `benchmark`, `--help` falló con
`Parameter.make_metavar() missing ... ctx`: el lock había resuelto
Click 8.4.2, cuya firma ya no coincide con el formateador rich de Typer
0.15.1. Se pineó `click==8.1.8`, comprobado contra el CLI real, y quedó un
test que exige que `benchmark` aparezca en el help. No es un cambio de
dominio ni una razón para actualizar Typer dentro de esta rebanada.

## D28 — los benchmarks contabilizan usage real y fijan proveedor y modelo

El costo de una corrida se calcula desde el `usage` devuelto por cada respuesta
exitosa: tokens de entrada, lectura de caché, salida y razonamiento cuando el
proveedor lo separa. No se estima un promedio por llamada. Los precios viven en
un archivo externo fechado, con fuente y tarifas por millón de tokens; cada
artefacto registra qué tabla usó. Si falta una tarifa necesaria, el costo queda
vacío en vez de inventarse.

Las rutas de modelo son explícitas. MiniMax, DeepSeek y MiMo usan
`/chat/completions`; Kimi K2.6 usa ese protocolo con `thinking=enabled`,
temperatura 1 y límite de salida medido. Qwen por OpenCode Zen usa
`/messages`, con timeout de 60 s y máximo de 1.024 tokens de salida para
acotar latencia y costo. El SDK Anthropic agrega `/v1/messages`, por lo que para esa ruta
se elimina el `/v1` final de la base configurada. Zen/Qwen rechaza tanto el
`json_schema` nativo de Claude como las tools forzadas: su contrato es JSON
textual estricto, sometido después a la misma validación, reintento semántico y
fallback que cualquier otra respuesta del modelo. Esto quedó comprobado contra
el endpoint real y protegido por canarios.

Rotar claves dentro de un proveedor no cambia la identidad de una medición.
Cambiar de proveedor o modelo sí: está prohibido durante un benchmark. Una
corrida fija ambos al empezar y, si su infraestructura se agota, aborta
ruidosamente con el progreso alcanzado. La cadena entre proveedores queda
reservada para partidas interactivas, donde terminar la batalla importa más que
la comparabilidad estadística.

Los benchmarks pagos se ejecutan secuencialmente y escriben un snapshot JSON
atómico después de cada batalla. Cada línea de progreso informa batallas,
resultado acumulado, llamadas, tokens y costo. Si la corrida se interrumpe
durante una batalla, se fuerza un último snapshot con el usage acumulado hasta
ese instante aunque el contador de batallas no haya avanzado. El ledger
Markdown recibe una fila solamente al cierre normal o aborto clasificado; un
snapshot con `status="running"` es deliberadamente recuperable y no se presenta
como resultado comparable.

poke-env despacha cada mensaje en una task propia; una excepción de decisión
puede quedar allí sin despertar el `battle_against` que espera el final de la
batalla. `LudexPlayer` publica el primer fallo de fondo mediante un
`concurrent.futures.Future`, porque el loop del cliente puede vivir en otro
hilo. El runner corre cada batalla en carrera contra esa señal: si llega el
fallo, cancela la espera, conserva el snapshot y propaga la excepción original.
Así una corrida desatendida aborta ruidosamente en vez de quedar colgada.

## D29 — el request propio activo prueba un switch oculto por Illusion

Un Zoroark propio puede entrar disfrazado, salir antes de que Illusion se
rompa y no aparecer nunca por su nombre en la narración pública. En
`battle-gen6randombattle-1799`, el agente eligió Zoroark, Showdown narró
`|switch|p1a: Barbaracle|...` y Zoroark salió antes de producir
`|replace|...|Zoroark`. El corrector no encontró la especie elegida y dejó la
decisión en el turno crudo 0 aunque se resolvió en el turno 1.

El `|request|` privado propio es una clase adicional de evidencia positiva:
después de resolver el cambio, `side.pokemon` identifica con `active: true` al
Zoroark real aunque la línea pública conserve el disfraz. Se acepta sólo para
una decisión de switch y sólo cuando `request.side.id` coincide con el lado
propio que se está corrigiendo. Nunca se consulta un request del lado
contrario: Illusion debe seguir ocultando la identidad del rival y el dataset
no puede incorporar información que ese jugador no conoce.

El request se parsea como JSON; no se busca la especie como substring. La
línea contiene los seis Pokémon propios y el nombre de un miembro en banca no
prueba que haya entrado. Sólo cuenta el objeto cuyo campo `active` es
exactamente `true`, y su `ident` debe pertenecer al mismo lado. Aunque la
evidencia se obtiene del request, el cursor global avanza después del
`|switch|` público asociado en el mismo turno, para que una decisión posterior
no pueda reutilizar esa línea.

## D30 — `KeyRotatingProvider` enfría claves con 429, no las descarta para siempre

`_first_available_key` solo avanzaba: una clave con `QuotaExceeded` quedaba
excluida el resto del proceso, incluso entre batallas de la misma corrida (el
`KeyRotatingProvider` se construye una vez por benchmark, no una vez por
batalla). Corrida real, `evals/runs/20260728-gemini25flash-5.json`: 39
llamadas, 10 rotaciones, 11 claves configuradas — unas 3,5 llamadas por
clave antes de darla por muerta. Ninguna cuota diaria se agota así de rápido;
es la firma de un límite por MINUTO, que se libera solo, y que el código
trataba como si fuera definitivo.

Es hija directa del fix anterior (D-sin-número, no registrada: "no
reintentar en cada turno una clave ya agotada", que evitaba las ~98
rotaciones de una corrida más vieja). Ese fix corrigió el síntoma
—reintentar CADA turno— pero sobrecorrigió a "nunca más".

El reemplazo es enfriamiento por clave: `_cooldown_until[key_index]` guarda
hasta cuándo una clave sigue no disponible; pasado ese momento, vuelve a
intentarse, empezando otra vez por la preferida (índice 0). Si Gemini
informa `retry_delay` (el 429 de `google.rpc.RetryInfo`, aplanado a texto
por `langchain_google_genai` — confirmado contra el propio docstring de la
librería en `_common.py`, que documenta la regex
`retry_delay\s*\{\s*seconds:\s*(\d+)` como la forma soportada de
recuperarlo), se usa ese valor. Si no, cae a un default de 60s, el mismo que
usa la documentación de `langchain_google_genai` como fallback.

No se distingue cuota diaria de límite por minuto: no hay señal parseable
para eso hoy (el `retry_delay`, cuando aparece, ya resuelve el caso
interesante — el límite por minuto — y una cuota diaria simplemente vuelve a
fallar y se reenfría). El costo de no distinguir es acotado — una llamada
perdida por default cada 60s contra una clave con cuota diaria agotada — y
muy distinto de los dos bugs anteriores (exclusión permanente, o reintentar
todo el pool en cada turno).

Cuando TODAS las claves están enfriándose, `ProviderPoolExhausted` solo se
lanza si esperar a que la primera se libere no entra en el deadline del
turno; si entra, `complete()` duerme ese tramo y reintenta. Dos canarios
(`tests/graph/test_provider.py`) fijan ambas direcciones: una clave enfriada
no se reintenta antes de tiempo, y sí vuelve después — revertido el fix,
los seis tests nuevos fallan.

## D31 — el snapshot de decisión se completa con el frame público ya emitido, antes del lock

**Contexto.** `serialize_battle` corría dentro de `choose_move`, cuando poke-env
ya había reconstruido *nuestro* lado desde el `|request|` privado pero todavía no
había parseado la narración pública del turno que ese request resolvió. El
resultado: un snapshot que mezcla nuestro lado en `t=k` con el rival en `t=k−1`
— **no corresponde a ningún punto real de la batalla**. Medido sobre el corpus
real: **297 de 762 decisiones (39.0%)** informaban al proveedor un pokémon activo
rival **equivocado**, y **270 de esas 297 (90.9%)** mostraban exactamente el
activo de la decisión anterior.

`_finalize_pending_steps` no lo arreglaba. Se apoyaba en una premisa falsa —
documentada como hecho en el docstring de `LudexPlayer` — de que la narración
llegaba "en el MISMO lote" que el request. Son **frames de websocket distintos**,
cada uno con su propia task.

**Lo que se midió** (sonda causal contra Showdown local, dos variantes de 39 y 76
decisiones; diseño y traza cruda en
`docs/superpowers/specs/2026-07-29-f2-01-prelock-snapshot-design.md`):

- `NARR(k)`, la narración que resuelve la decisión **anterior**, llega al socket
  **0.022–5.557 ms** después del `|request|` y **antes** de que enviemos la
  elección. Demorar la elección 500 ms **no la mueve** (76/76). No depende de
  nuestra respuesta.
- `NARR(k+1)`, la que resuelve la decisión **actual**, sí depende: llega 4–45 ms
  **después** del envío.
- Lo que mantiene a `NARR(k)` fuera de `Battle` no es el cable sino el **lock por
  batalla** de poke-env (`ps_client.py:171-176`): su task ya arrancó pero queda
  encolada esperando el mismo lock que la decisión mantiene abierto (medido:
  bloqueada 501 ms con un hold de 500 ms).

**La distinción importa y corrige a `.claude/agent-recording/SKILL.md` y a
D20/D22:** "esperar la narración es un punto muerto garantizado" vale para
`NARR(k+1)`, no para `NARR(k)`.

**Decisión.** Un observador envuelve `PSClient._handle_message` —el único punto de
poke-env 0.15.0 que corre dentro de la task del frame y **antes** del lock— y
publica el frame crudo en un inbox por `battle_tag`. La decisión espera esa señal
y aplica una **proyección pura** sobre el snapshot inmutable.

Contrato del snapshot:

- `graph_input["raw_state"]` y `step["state"]` son **el mismo objeto**: no pueden
  representar puntos distintos de la batalla.
- Máscara, `action_taken` y mapa acción→`BattleOrder` se siguen capturando
  **síncronos antes del primer `await`**. La proyección nunca escribe
  `legal_actions`, `me` ni `player_role`.
- El proyector vive en `showdown/protocol.py`, que **no importa poke-env**: se
  testea sin levantar nada. Las traducciones que necesitan el dex o los enums de
  la librería entran inyectadas (`ObservableVocabulary`), de modo que las
  inferencias legítimas quedan ancladas al dex y **nunca a una lista a mano**.
- Nunca se consume una línea `|request|`, ni se relee `Battle` después de decidir.
- Se espera **únicamente** al inbox. Esperar a `Battle`, al `ProtocolRecorder` o a
  `_handle_battle_message` desde una decisión que tiene el lock sigue siendo un
  punto muerto y sigue prohibido.
- El `ProtocolRecorder` no cambia: se sigue grabando bajo el lock y sigue siendo
  la fuente de verdad persistida (D17). El inbox es solo el canal de señal.
- **El chat de batalla nunca entra al prompt.** La completitud de la espera es
  lista **blanca** (`RESOLUTION_TAGS`): `c`/`c:`, `inactive`, `j`/`l`/`n`, `t:`
  solo, `request`, `error`, `popup`, `raw`, `html` e `init` no pueden completarla
  ni llegar al estado. Con lista negra, un `|c:|` habría bastado para decidir sin
  la narración y el desfase volvía.
- `|turn|` **no** es requisito de completitud: el frame de un cambio forzado
  cierra en el `|faint|` y nunca lo trae (medido: 7/76 decisiones con
  `forceSwitch`, todas con `turn=None`). Exigirlo colgaba cada cambio forzado.

**Fallo cerrado, no fila degradada.** Si la narración no llega dentro del
presupuesto (`projection_timeout_seconds`, default 1.0 s, acotado además por el
presupuesto de la decisión): se incrementa `projection_timeout_count`, se lanza
`ProjectionTimeoutError`, se propaga por `_background_failure` y **se retira el
paso reservado**. No se invoca al proveedor con estado stale ni se persiste una
fila marcada. **Si una fila existe, su proyección es válida por construcción** —
una fila degradada contaminaría el corpus antes de que el auditor pudiera
excluirla.

**El cursor es un `ContextVar`, no `last_seq`.** poke-env crea una task por frame
y todas publican al arrancar, pero solo una entra al lock por vez. Bajo carga,
para cuando la decisión del frame N llega a `choose_move`, los frames N+1, N+2…
ya están publicados: `last_seq` devolvería uno de **ellos** y la decisión
esperaría una narración que no es la suya. Cazado por el test de frames reales,
no teorizado.

**`detailschange` no cambia `species`.** `Pokemon.forme_change()` llama a
`_update_from_pokedex(..., store_species=False)` (`pokemon.py:431-433`): tras una
Mega evolución poke-env conserva la forma base. La proyección hace lo mismo —
cambia los **tipos**, no la especie. Escribir `slowbromega` habría hecho que la
proyección contradiga al resto del dataset dentro de la misma batalla (medido en
`battle-gen6randombattle-1896`).

Corolario de medición: la prevalencia oficial del defecto es **297/762 = 39.0%**,
con firma de retraso-en-uno **270/297 = 90.9%**. Una versión intermedia de la
consulta contaba `detailschange` como cambio de identidad y daba 309/762 = 40.6%;
eso sobrecontaba 12 filas por Mega. Toda la documentación de F2-01 cita 39.0%.

**La identidad de un miembro del equipo es su `base_species`, no su `species`.**
Es el criterio de `Pokemon.identifies_as` (`pokemon.py:435-438`). Comparar
`species` a secas contaba `camerupt` y `cameruptmega` como dos miembros: una Mega
que sale del campo y vuelve producía un **equipo rival de siete**, imposible por
las reglas del juego (medido en `battle-gen6randombattle-1917`, decisión 32).
Cuál de las dos formas nombra cada lado depende de la línea: `switch` escribe la
especie (`store_species=True`), `detailschange` no.

**`replace` son dos entradas del equipo, no un renombre.** Renombrar la entrada
activa —lo primero que se implementó— borraba al imitado del equipo rival, aunque
su `|switch|` es evidencia pública de que el rival lo tiene, y le regalaba al
imitador el item, la ability y los movimientos que se le habían atribuido al
imitado. Paridad con `AbstractBattle._end_illusion_on`
(`abstract_battle.py:409-427`): el imitador entra con especie, nivel y tipos del
`details` del `|replace|` y hereda HP, status y `fainted`, porque el que estaba en
el campo era él; el imitado queda inactivo, con boosts limpios, `status=None` y
`hp_fraction=0.0` —lo que devuelve `current_hp_fraction` cuando `_current_hp` es
`None` (`pokemon.py:988-995`)—; item y movimientos no viajan.

**La ability sale del dex cuando el dex la determina.** Si una especie tiene
exactamente una ability posible, saberla no es información oculta: Zoroark solo
puede tener Illusion, Weezing solo Levitate. Es la misma inferencia que hace
`_update_from_pokedex` (`pokemon.py:658-661`), con el mismo corte de `gen >= 3`, y
resuelve el `illusion` público sin ninguna lista de especies a mano; Camerupt tiene
tres abilities y queda en `None`. Una forma Mega/Primal reporta la suya, porque
poke-env la guarda en `forme_change_ability` y la property la prefiere
(`pokemon.py:650-655`, `861-871`). `|-end|{side}a: X|Illusion` se procesa además
por sí misma, para cubrir la ventana de frames que arranca después del `|replace|`.

**Todo el estado temporal de Transform se limpia al salir del campo.** `switch_out`
borra en poke-env `_temporary_types`, `temporary_ability`, `_transform_moves` y los
boosts (`pokemon.py:600-612`). Lo persistente sobrevive: la ability que reveló
Imposter y el movimiento `transform` que poke-env agrega al moveset base, con PP
completo. El proyector guarda el estado base **antes** de que Transform lo tape, en
un registro por identidad canónica dentro de la propia proyección, en vez de
intentar adivinarlo después.

**La rama `move` no puede inventar pertenencia ni PP.** Tratar toda línea `|move|`
como "este movimiento es del actor" era falso: el corpus tiene 39 líneas `|move|`
con `[from] ability:`, casi todas **Magic Bounce**, donde el movimiento reflejado
era del rival de ese actor. Se reproducen las excepciones que poke-env ya codifica
(`abstract_battle.py:582-700`), ancladas a los sufijos públicos: Magic Bounce,
Magic Coat, Mirror Move, `lockedmove` y Sky Attack no revelan; Copycat, Metronome,
Nature Power y Round no revelan el eco; Sleep Talk sí, porque llama movimientos
propios, y con PP completo porque el PP lo paga Sleep Talk; Dancer descarta la línea
entera. El PP se **descuenta** desde el valor que ya trae el snapshot, y va en
`null` cuando no es derivable con exactitud —`max_pp` desconocido, o Pressure de
nuestro lado, que descuenta 2 con una regla dependiente del objetivo—. Conservar el
número anterior afirmaba un PP stale.

**`typechange` y Transform/Imposter también se proyectan**, ancladas a la
librería y no a listas a mano: `|-start|…|typechange|Water/Flying` vía
`PokemonType.from_name`, con la forma `[of]` de Reflect Type resuelta igual que
`abstract_battle.py:802-809`; y `|-transform|…|[from] ability: Imposter`, que copia
de un pokémon **nuestro** —información que ya tenemos, no fuga— con tipos del dex
de la especie copiada, boosts, moveset y ability del objetivo y la especie
intacta, igual que `Pokemon.transform()` (`pokemon.py:625-636`). El PP de un
movimiento copiado es `min(5, max_pp)` desde gen 5 (`move.py:114`, `move.py:
477-478`): regla fija de la generación, derivable. Un `switch` posterior borra los
tipos temporales, igual que `switch_out` limpia `_temporary_types`.

**La exclusión de cambios forzados en la verificación de integración exige firma
demostrable.** Excluir por `turn_number` repetido a secas era demasiado ancho:
tapaba cualquier defecto futuro que duplicara el turno por otro motivo, que es la
misma cobertura silenciosa que costó 265 pasos cuando la PK estaba sobre
`turn_number` (D21). Se piden dos hechos públicos independientes: la máscara
persistida sin **ni un** movimiento (un `forceSwitch` llega sin `moves`) y un
`|faint|{rol}a:` en el protocolo de ese mismo turno. Una decisión que comparte
turno y no cumple las dos **hace fallar el test**.

**La retención del `RawFrameInbox` está acotada**: `MAX_RETAINED_FRAMES = 128` por
tag durante la batalla y `close()` la libera al terminarla. El tope no puede
volverse una respuesta equivocada: si el frame que seguía al cursor se desalojó,
`wait_for_resolution` falla **cerrado**, porque el primer frame retenido ya no es
demostrablemente el de esa decisión. El mayor `seq` desalojado se rastrea **por
tag**, ya que `_seq` es global y los `seq` de un tag no son contiguos.

**Esquema v2.** Un movimiento rival revelado desde `|move|` entra como
`{"id": ..., "pp": null, "max_pp": null}`: `null` significa **"no derivable de
esa evidencia pública"**, no cero ni PP faltante por error. Las filas históricas
v1 no se reescriben ni se les inventa metadata. El invariante global deja de ser
"una sola versión" y pasa a exigir que columna y JSON coincidan por fila, que
solo aparezcan versiones soportadas `{1, 2}`, y que las filas nuevas sean v2. No
hace falta migración: la columna ya versiona el payload.

**Turno.** La proyección fija `turn` desde el `|turn|N` de la narración previa, y
conserva el `decision_turn` síncrono cuando no lo hay (cambio forzado). Para la
ruta del grafo, `_correct_step_turns` **verifica** que coincida con la línea que
resolvió la acción y **falla ruidosamente** si no, en vez de mutar a posteriori el
dict que ya vio el proveedor. La ruta random conserva su corrección histórica.

**Reintentos por elección rechazada.** F2-01 se ocupa solo de frescura y de no
trabarse: se marca el próximo `choose_move` como retry **antes** de delegar en
`super()` —única forma de cubrir las dos rutas, porque `[Invalid choice]`
reintenta dentro del mismo frame y `[Unavailable choice]` en uno posterior—, se
salta la espera (no hay turno que resolver) y se reutiliza **solo** `opponent`,
`field` y `turn` de la última proyección válida sobre el snapshot propio
**nuevo**. Nunca el dict completo: la máscara pudo cambiar al descubrirse
`trapped`. Sin proyección previa válida, falla ruidosamente. La identidad canónica
de la decisión, el descarte del intento rechazado y `decision_index` siguen
siendo de F2-02.

**Memoria pública entre decisiones, por tag e identidad canónica.**
`project_observable_state` se llama UNA VEZ por decisión, siempre con un
snapshot fresco de `serialize_battle(battle)`. Confundir "tapado
temporalmente" con "hay que recalcular del dex" —lo que hacía la versión
anterior— perdía los tipos de una Mega o el ability/moveset propios de un
Transform tan pronto la decisión que los aplicó terminaba: `switch_out`
(`pokemon.py:600-612`) en poke-env **nunca** resetea `_type_1`/`_type_2`, solo
limpia `_temporary_types`/`temporary_ability`/`_transform_moves`, que son
campos DISTINTOS. La corrección agrega un parámetro explícito
`persistent_state: dict[str, dict]` (mutado in-place, no un caché oculto): un
typechange o un Transform siembran ahí, con `setdefault` (nunca pisan un
registro ya sembrado), el valor de tipos/ability/moves de ANTES del override;
`switch_out` restaura desde ahí si hay registro, y si NO lo hay **no toca
nada** —ni tipos, ni ability, ni moves—, porque ya son los persistentes
correctos. `client.py` le pasa a cada decisión el mismo dict por `battle_tag`,
vivo mientras dura la batalla y liberado en `win`/`tie`.

**Item y ability revelados por el sufijo de un `-damage`/`-heal`.** Se
reproducen los cuatro helpers de poke-env (`abstract_battle.py:333-403`): daño
por item/ability propios (sin `[of]`), daño por item/ability ajenos (`[of] X`,
donde X puede ser CUALQUIERA de los dos lados), heal por item propio (con el
guard de poke-env: no reescribe si el item ya es `None` —consumido— o si el
nombre es una berry/herb), y heal por ability propia (el `[of]` de esa línea es
engañoso y NO indica el dueño, salvo el caso especial Hospitality). Se procesa
**antes** del filtro por `ident` de la línea, porque el mon dañado puede ser
nuestro propio activo mientras el item/ability revelado es del rival vía
`[of]`: filtrar por ident perdería esa revelación entera.

**`-clearallboost` no trae `ident`.** Limpia los dos activos a la vez
(`abstract_battle.py:901-902`); el guard genérico `len(parts) < 3` lo volvía
inalcanzable (94 líneas reales en el corpus de test, cero ejercidas). Se
procesa antes de ese guard. `-clearnegativeboost`/`-clearpositiveboost`/
`-invertboost`/`-copyboost` sí se proyectan (el objetivo de `-copyboost` puede
ser el rival aunque la fuente seamos nosotros). **`-swapboost` falla CERRADO**
(`ProjectionAmbiguityError`, ver más abajo): documentarlo como límite y
conservar el boost stale del rival —lo que hacía la ronda anterior— quedó
rechazado explícitamente. Sigue en `RESOLUTION_TAGS` para no colgar la espera,
pero la decisión entera se aborta antes que persistir un boost del rival
sabidamente incorrecto.

**Dancer revela su propia ability, no el movimiento.** Orden exacto de
poke-env (`abstract_battle.py:650-656`): la ability se asigna PRIMERO,
incondicionalmente, y recién después viene el `return` que omite
`register_move`. La versión anterior invertía el orden y dejaba `ability=None`.

**La fuente propia se resuelve por el NOMBRE del evento, no por "quien está
activo ahora".** `snapshot["me"]` viene fresco del `|request|` propio, ya
post-resolución de TODO el turno. Si un evento (Transform, Reflect Type,
`-copyboost`) nombra a un pokémon propio que DESPUÉS salió del campo dentro de
la MISMA narración, "el activo ahora" es el que entró después, no el nombrado
— medido en `battle-gen6randombattle-1929`: un `-transform` que copiaba a
Spinda terminaba copiando a Tentacruel. `own_mon_named()` resuelve por
identidad canónica (`base_species`, igual que D22) contra el equipo COMPLETO
en `snapshot["me"]["pokemon"]` (poke-env conoce los seis desde el team
preview), y **falla cerrado** (`ProjectionAmbiguityError`) si el nombre no
corresponde a ningún miembro conocido, en vez de sustituir por el activo
"por las dudas" — que sería repetir el mismo bug. `own_active()` queda
restringido a lo que de verdad depende de "ahora mismo" (Pressure sobre un
movimiento rival).

**La ability tiene una base persistente y un override temporal, igual que el
setter de poke-env.** `Pokemon.ability` (setter, `pokemon.py:873-878`): si
`_ability` es `None`, el valor se vuelve persistente; si no, es un override
temporal. La versión anterior conflacionaba las dos en un solo campo. Ahora
`reveal_ability()` implementa la misma regla (usada por `-ability`, Magic
Bounce/Dancer y la ability copiada por Transform): la primera revelación es
persistente y no siembra nada; una revelación posterior es temporal y siembra,
con `setdefault`, el valor anterior en `persistent_state[canon]["ability"]`.
`switch_out` restaura desde ahí — y a diferencia de `types`/`moves` (que se
consumen, `pop`, porque un Transform es puntual), **la ability NO se
descarta**: sobrevive para el próximo override, igual que `_ability` nunca se
olvida en poke-env aunque el pokémon salga del campo. Trace es el caso
especial (`abstract_battle.py:781-792`, "correcting for bad PS ordering of
logs"): borra la base anterior y fija `"trace"` como la nueva, ANTES de
aplicar el override con la ability copiada — 170 líneas reales en el corpus.
`-endability` restaura ya, sin esperar un switch, y solo si hay un override
activo (si no, es un no-op, igual que en poke-env).

**Hallazgo incidental, fuera de los cuatro pedidos: `_find_action_line` matcheaba
de más contra un sufijo `[from] move: X`.** No es parte de la proyección del
rival —es la atribución de turno de NUESTRAS propias decisiones (D20/D22/D23)—,
pero la verificación de integración estrechada de esta ronda lo destapó: Sleep
Talk llamando a Rest se narra `|move|p1a: Spiritomb|Rest||[from] move: Sleep
Talk|[still]`, y buscar `"sleeptalk" in _normalize(line)` sobre la línea
**entera** matcheaba esa línea de Rest aunque la decisión elegida fuera un
**segundo** Sleep Talk real, más adelante. Medido en
`battle-gen6randombattle-1925`: la decisión 32 se apropiaba de la línea que ya
había resuelto la decisión 31 y quedaba con `turn_number` equivocado. El match
ahora se ancla a `parts[3]` (el token del movimiento o la especie), no a la
línea completa. Cambio mínimo, con su propio test (`test_find_action_line_no_
matchea_un_sufijo_from_move_de_otro_movimiento`) y su propia rotura deliberada;
no toca el camino pre-lock ni ninguno de los cuatro findings de esta ronda.

**Límite conocido.** El seam `_handle_message` es privado y poke-env 0.15.0 no
expone un hook pre-lock. `tests/showdown/test_pokeenv_contract.py` lo protege; su
aserción central es que el inbox se puebla **con el lock del tag tomado** mientras
`_on_battle_message` todavía no fue invocado, así que si poke-env moviera el lock
más arriba el test cae antes de que el dataset se degrade en silencio. La salida
de fondo es un hook `on_raw_frame` upstream.

## D32 — retrieve_context: contexto rico y prompt compacto, generation-scoped, sin fuga

**Contexto.** El grafo de decisión (`parse_state → retrieve_context →
calc_damage → decide`) no consultaba datos de juego: el proveedor decidía con el
estado observable únicamente. F2-06 introduce el nodo `retrieve_context`, que
lee especies, movimientos y learnsets de la base local de Postgres y produce
**dos** contextos: uno **rico** (`GraphState.context`) para
`calc_damage`/F2-07 y consumidores deterministas, y uno **compacto**
(`GraphState.prompt_context`) que es lo único que recibe `decide`/provider.

**Fuente: exclusivamente game data local, generation-scoped.** No hay internet,
no hay fetch runtime de Pokémon Showdown, no hay perfiles, embeddings,
playbooks, round availability ni tablas futuras. Toda consulta resuelve
`gen_number → gen_id` y filtra pokémon y movimientos por `gen_id`; los
learnsets respetan la generación del pokémon y del movimiento. Nunca se fija
Gen 6 directamente en producción (`grep -ri "gen6" src/` no devuelve nada fuera
de configuración y fixtures). La frontera Gen 6/Gen 9 se verifica con hechos
del juego: `gholdengo` existe en 9 y no en 6, y `tackle` baja de poder 50
(gen 6) a 40 (gen 9).

**ContextRepository obligatorio y fallo ruidoso.** `build_decision_graph` no
admite `repository=None`: el parámetro es posicional required, no opcional. No
existe fallback de contexto vacío. CLI, tests y todos los callers inyectan un
repositorio explícito. Los errores del repositorio se **propagan**; no se
ocultan. Un `ContextRepository` omitido produce `TypeError` en la
construcción del grafo (`test_grafo_exige_context_repository`).

**El engine vive en el loop del listener (corrección posterior a la
integración con MON-6).** poke-env ejecuta `choose_move` (y por ende el
grafo) en el loop del listener, que puede ser otro hilo distinto del loop
que orquesta la batalla o el test. Un `AsyncEngine` creado por el caller
bindea su pool de asyncpg a ESE loop y al usarlo desde el listener cruza
loops ("Future attached to a different loop"). `PostgresContextRepository`
recibe una `database_url: str` y crea su engine **perezosamente** en el
primer `await`, garantizando que el pool se bindea al loop correcto; `aclose()`
lo dispone. Esto no acopla al `BattleRepository` (que persiste en el loop
principal) y repara el defecto latente que Galileo dejó tanto en el CLI de
producción como en el test de integración.

**Lookup de movimientos observados, independiente del learnset.** Los
movimientos propios conocidos y rivales revelados se resuelven directamente
por `(gen_id, showdown_id)`, sin requerir que pertenezcan al learnset de la
especie visible. Es obligatorio para Illusion, Transform y Mimic: un movimiento
observado puede venir de una especie que el snapshot no muestra como tal. Un
ID observado inexistente produce `LookupError` (**fallo ruidoso**, no
descriptor silencioso). Un tuple vacío evita queries inválidas y devuelve
catálogo vacío.

**Especies rivales limitadas a evidencia pública.** Las especies rivales
consultadas son **exclusivamente** las reveladas en `battle_state`
(allowlisted). Nunca se consulta `raw_state`, equipos privados ni especies
hipotéticas. `extract_species_ids` lee solo `battle_state.me` y
`battle_state.opponent`, deduplica en orden de aparición y descarta entradas
inválidas. El test `test_retrieve_context_excluye_rival_no_revelado`
verifica que `raw_state.opponent.pokemon` (con mewtwo) no se consulta
mientras `battle_state.opponent` (con garchomp) sí.

**`possible_moves` expresa posibilidades, no información real oculta.** Para
el rival, `possible_moves` es el learnset **completo** de la especie visible,
presentado claramente como posibilidades del learnset, no como el moveset
real. Un movimiento observado prevalece sobre `possible-only` para el mismo
ID: el catálogo de movimientos del `prompt_context` está deduplicado por
`showdown_id`. **Ursaring/Sludge Bomb** es el caso testigo: solo se consulta
`ursaring`, no aparece ni se consulta `zoroark`, `sludgebomb` no entra en
`possible_moves` de Ursaring (no está en su learnset), sí entra como
`observed_move` con descriptor enriquecido en `prompt_context`.

**Preservación de `learn_methods`/`sourceSpecies` en el contexto rico.** El
contexto rico conserva por movimiento: `learn_methods` (lista de métodos con
`gen`, `method`, `level` y `sourceSpecies`), `flags`, `description`,
`accuracy`/`never_misses`, `power_kind` y la información completa necesaria
para F2-07 (`calc_damage`). El test `test_learn_methods_conserva_source_species_
y_campos` verifica Charizard-Mega-X: `flamethrower` hereda de `charizard`
(machine), `charmeleon` (level 43) y `charmander` (level 37).

**Semántica de `accuracy NULL` y `power_kind`.** `moves.accuracy IS NULL`
significa **"nunca falla"** (D15), no "desconocida": el repositorio lo
expone como `never_misses: true`. El test lo verifica con `swift` sobre
pikachu. `power_kind` distingue: `status` (movimiento de estado, p.ej.
`splash`), `variable` (poder variable, p.ej. `gyroball`), `fixed_damage`
(daño fijo, p.ej. `seismictoss`), `special` y `standard`. En el
`prompt_context`, `accuracy` se proyecta como `"never_misses"` (string)
cuando es `NULL`, y como el valor numérico en caso contrario.

**Proyección compacta (`prompt_context`).** `project_prompt_context` produce:
- `own`: solo movimientos **realmente conocidos** en `battle_state`
  (`known_moves`), no el learnset propio completo.
- `opponent`: solo especies **reveladas**; `revealed_moves` con igualdad
  exacta con evidencia pública; `possible_moves` = learnset completo de la
  especie visible.
- `moves`: catálogo deduplicado por `showdown_id`. Movimientos
  conocidos/revelados: descriptor base **+** `description` **+** `flags`.
  Movimientos únicamente posibles: descriptor **base compacto** (sin
  `description` ni `flags`).
- Una observación prevalece sobre `possible-only` para el mismo ID.
- No existe truncado runtime.

**El provider recibe únicamente `prompt_context`.** `decide_node` construye un
`decision_state` donde `battle_state.context` = `state["prompt_context"]`, no
el contexto rico. Los tests usan sentinels exclusivos del objeto rico
(`"rich-only-sentinel-source-species"`, `"learn_methods"`, `"sourceSpecies"`,
`"observed_moves"`) para probar que **no llegan** al prompt. `battle_state`,
`context` y `prompt_context` no se mutan durante `decide` (verificado con
`deepcopy` antes/después en `test_prompt_context_separa_observados_
enriquecidos_de_posibles_compactos`).

**`calc_damage`/F2-07 recibe el contexto rico.** El test
`test_calc_damage_recibe_contexto_rico_no_prompt_context` confirma que
`calc_damage` ve `state["context"]` (con `learn_methods` y sentinels ricos),
no `prompt_context`.

**Presupuesto.** Baseline histórico aceptado, recalculado desde el código
final contra `battle-gen6randombattle-397` (decisiones 1 y 25):

| caso | prompt_context (bytes) | bajo 64 KiB |
|------|-----------------------:|:-----------:|
| 6+1  | 19,846                 | ✓           |
| 6+6  | 44,740                 | ✓           |

Son mediciones reproducibles (`len(json.dumps(..., separators=(",",":")).
encode())`), no golden exacto eterno. El techo de 64 KiB (65,536 bytes)
funciona como **canario**, no como límite operacional: los canarios de
completitud (`own_known`, `opponent_candidates`, `catalog`,
`learn_methods`) comprueban que el límite no se cumple eliminando
candidatos o semántica. Nunca se cortan datos silenciosamente para cumplir
el techo.

**Casos vinculantes verificados.** Frontera Gen 6/Gen 9 para especies
(`gholdengo`) y en `load_moves` (`tackle`); `accuracy NULL` → `never_misses`;
`power_kind` distingue `status`/`variable`/`fixed_damage`/`special`/
`standard`; Ursaring/Sludge Bomb (sin inferir zoroark); ID observado ausente
→ `LookupError`; IDs vacíos → resultado vacío sin query inválida;
`ContextRepository` omitido → `TypeError`; error del repositorio →
propagación; F2-07 recibe contexto rico; provider recibe solo
`prompt_context`; `battle_state` y ambos contextos inmutables; no entran
especies rivales no reveladas.

**Formas cosméticas y frontera ruidosa (corrección de Changes Requested).**
El repositorio no podía silenciosamente descartar especies visibles sin fila
directa en `pokemon`: las formas cosméticas (Vivillon-Tundra,
Florges-Yellow/Orange, Sawsbuck-Summer, Unown-O) son especies que aparecen
en el protocolo con su showdown_id visible, no se seedean por separado, y
antes quedaban fuera del catálogo y del prompt sin ruido.

La primera corrección usó igualdad de `baseStats` para clasificar cosméticas,
pero era incorrecta: Arceus-Poison (120/120/120/120/120/120 idénticos a
Arceus Normal) y Castform-Sunny se degradaban a su base perdiendo tipo;
Pikachu-World/Partner (gen 8/7) se aceptaban en gen 6 porque
`GenData.from_gen(6).pokedex` contiene formas de generaciones futuras sin
filtrar (ver `.claude/showdown-data/SKILL.md`: "los mods NO filtran por
generación").

La corrección final:

- **Una fila directa `(gen_id, visible_showdown_id)` SIEMPRE gana.** Se
  consulta la unión de IDs visibles y bases candidatas en una sola query
  (sin N+1). Para cada visible_id: si hay fila directa se usa; si no, se
  busca entre las bases candidatas.
- **`cosmeticFormes` explícito del dex es el único criterio.** Una forma es
  cosmética sólo si su entrada en el dex tiene `cosmeticFormes` (lista no
  vacía) y `baseSpecies` distinto al propio. Las formas mecánicas (Arceus
  tipos, Castform climas, Mega, Gmax, Ogerpon te, Pikachu Cosplay/World/
  Partner) tienen `cosmeticFormes=None` y NO se resuelven.
- **Postgres decide disponibilidad, no GenData.** Estar en el pokedex no
  significa estar disponible en la generación. La query es generation-scoped
  (`WHERE p.gen_id = :gen_id`): una forma de generación posterior sin fila
  en `pokemon` no produce resultado y se raises `LookupError`.
- **Fallo ruidoso, nunca descarte silencioso.** Si no hay fila directa y la
  forma no es miembro explícito de `cosmeticFormes`, se lanza `LookupError`.
  Canarios (vigentes): `pikachupartner` gen6, `pikachuworld` gen6,
  `charizardgmax` gen6, `gholdengo` gen6. `floetteeternal` gen6 **dejó de
  ser canario de este fallo tras D47** (MON-24): sigue sin ser cosmética
  (el criterio de arriba no cambió), pero ahora tiene fila directa en
  Postgres, así que resuelve en vez de fallar. `floetteeternal` bajo
  generaciones cuyo catálogo random-battle no la referencia (p.ej. gen 9)
  sigue fallando ruidoso, mismo mecanismo que siempre.
- **El `showdown_id` visible se conserva** sólo tras una resolución
  cosmética válida, para que la proyección correlacione por especie
  revelada.

Medido: 332 apariciones afectadas (276 propias + 56 rivales) bajo
`state_schema_version=2`. Las 5 formas cosméticas (vivillontundra,
florgesyellow, florgesorange, sawsbucksummer, unowno) se resuelven a su base
vía `cosmeticFormes`. Las formas de gen posterior (`pikachupartner`,
`pikachuworld`, `charizardgmax`, `gholdengo`) quedan como canarios del fallo
ruidoso. `floetteeternal` (38 apariciones) quedó como canario del fallo
ruidoso hasta D47 (MON-24); desde D47 tiene fila directa vía la excepción
tipada de `packages/seed/src/extract/dex.ts::isAvailableForExtraction` y ya
no cae en esta rama.

**Exclusiones diferidas.** Quedan explícitamente fuera de F2-06: round
availability (la ronda activa se agrega en una rebanada posterior), perfiles
del rival, lecciones de análisis previos, playbook activo, retrieval por
pgvector (embeddings) e internet. El grafo actual no depende de ninguna de
estas fuentes; cualquier referencia futura las introducirá como capas
adicionales, no reemplazando la game data local.

## D33 — el auditor de dataset es la compuerta del corpus: un instante coherente, frontera cerrada y `pp: null` con dueño

**Contexto.** `packages/dataset-audit` tenía que pasar de sonda a compuerta.
Tres rondas de revisión mostraron que el problema no era la cobertura sino la
*pregunta*: primero preguntaba "¿esto fue público alguna vez?", después "¿este
valor fue público en algún momento de la ventana?". Las dos son preguntas **por
campo**, y con cualquiera de ellas un snapshot podía tomar el HP del turno 3, el
status del turno 4 y los boosts del turno 5 y pasar, aunque esos valores nunca
hubieran coexistido.

**Una fila vale si ALGÚN instante la explica entera.** El auditor reproduce el
protocolo crudo (D17) sobre un modelo con el mismo ciclo de vida que `Pokemon`
de poke-env 0.15.0 y compara el equipo rival **completo** contra un único
cursor dentro de `[state.turn, turn_number]`. Ningún campo elige su propio
instante. La ventana es esa y no otra porque `state.turn` es el `battle.turn`
capturado dentro de `choose_move` y `_correct_step_turns` sólo puede subir el
turno (D20/D22/D23).

**El oráculo es poke-env, no el recorder.** El auditor no reimplementa al
proyector de D31: reimplementa a la librería, que es lo que el recorder debía
producir. Por eso replica sus rarezas en vez de "arreglarlas" —`switch_out` no
restaura los tipos de una Mega, `_update_from_details` corta temprano si el
`details` repite, `faint()` no libera la ranura, `-clearallboost` alcanza sólo a
los dos activos, `-copyboost|FUENTE|OBJETIVO` escribe en el segundo— y por eso
un `|-mega|` cuyo `event[3]` trae la especie en vez de la piedra **no cambia
nada**, igual que en `mega_evolve`.

**Pertenencia de movimientos con la lista de la librería.** Magic Bounce, Magic
Coat y Mirror Move no atribuyen el movimiento rebotado; Dancer descarta el
evento entero; `lockedmove` y Sky Attack no revelan ni descuentan; Copycat,
Metronome, Nature Power y Round llaman a un movimiento que **no** es del actor;
Sleep Talk sí revela, porque sólo puede llamar a uno propio.

**La frontera del dex falla CERRADA, generation-scoped y con el criterio de
D32.** La fila directa de esa generación gana; si no hay fila, sólo un miembro
real de `cosmeticFormes` cae a su base, y sólo si la base tiene fila en esa
generación. Todo lo demás es especie desconocida y **se reporta**: no se
"resuelve por prefijo" ni se degradan `types`/`ability` a no auditables. Sin
esto, una fila con `Furfrou-Banana`, tipos DRAGON y Wonder Guard —narrada por un
protocolo igual de falso— pasaba con cero violaciones, y un `gholdengo` de gen 9
resolvía dentro de una batalla de gen 6. Medido sobre el corpus: el único id que
cae por esta frontera es `floetteeternal` (393 filas, todas v1), que es
exactamente el canario de fallo ruidoso que fija D32.

**`pp: null` es "no derivable con exactitud" (D31), no "omitido".** El proyector
del recorder escribe `null` cuando `pressure_on_us()` —nuestro activo con
Pressure—, porque el descuento pudo ser de uno o de dos y la regla exacta
depende de la categoría de objetivo; y una vez en `null` la cuenta no vuelve
sola. El replay del auditor modela ese ciclo de vida: acepta `null` **sólo**
sobre un movimiento con esa indeterminación vigente o heredada, y lo rechaza
cuando el valor sí era derivable. Fuera de ese caso el PP es **exacto**, no un
piso: la tabla `moves` trae `target` y `flags`, que son las dos condiciones que
`_pressure_on` evalúa además de la ability del defensor.

**Lo propio no es fuga.** La ability y el item de nuestro equipo salen de
`state.me` de la misma fila: poke-env los conoce por el `|request|` privado, que
el protocolo no contiene. Sin ellos no se puede decidir Pressure ni saber qué
item recibe el rival en un Trick. Un Transform sólo acepta lo efectivamente
copiado —tipos del dex de la especie copiada, ability copiada, moveset con el PP
en `min(5, max_pp)`— y si su objetivo no es resoluble no excusa nada.

**Scopes y versiones.** `--scope all` no excluye nada, incluidas las filas
`source='test'`; `--scope training` excluye `battles.source='test'` y
`trajectories.final_result IS NULL`; `--gen N` es el mismo contrato filtrado.
Los dos son read-only (`default_transaction_read_only=on`) y salen con código 1
ante cualquier violación. `state_schema_version` 1 y 2 conviven porque comparten
forma; una versión fuera del conjunto se rechaza. `win→1`, `loss→-1`, `tie→0`, y
`final_result IS NULL ⇒ reward IS NULL`: `NULL` no es empate, es "no terminó".

**Costo, escrito como es.** Seis `SELECT` parametrizados, siempre, con scope y
generación dentro del SQL; el protocolo se recorre una vez por `(batalla, lado)`
y no hay N+1. La tabla de alias cosméticos **no** es una query: sale del mismo
dex local empaquetado que usa `PokeEnvSpeciesVocabulary`, sin internet. La
complejidad real es `O(L + Σ_filas C_ventana · E · F)` —líneas del corpus, más
cursores de la ventana por entradas rivales por campos—, **no** `O(L + pasos)`.
Lo constante es el conteo de queries, y eso es lo que el canario mide.

**La cita a D19 de la migración es incorrecta.** `20260727000007_battle_source_test.sql`
remite a "D19" para el contrato `source <> 'test'`, pero D19 es *"se juega
`gen6randombattle`, no `gen6ou`"*. La migración histórica **no se edita**; esta
decisión es la canónica del scope y deja constancia del error de referencia.
## D34 — Los retries rechazados comparten una decisión canónica y sólo la acción resuelta entra al dataset

**Contexto.** Showdown rechaza elecciones por dos rutas distintas de poke-env
0.15.0. `[Invalid choice]` llama otra vez a `choose_move` dentro del handler del
mismo frame; `[Unavailable choice]` espera un request posterior que puede
revelar, por ejemplo, que el activo está atrapado. El código anterior buscaba
el prefijo, hacía `pop()` del último step y luego lo volvía a agregar. Eso
compactaba el índice por accidente en el caso feliz, pero no correlacionaba el
error con un intento enviado. El mismo canal también emite errores auxiliares:
un `/undo` sin nada que cancelar produce `[Invalid choice] There's nothing to
cancel` y borraba una decisión válida. El protocolo real y `battle_turns`
demuestran que estos errores **sí** llegan por el canal de batalla.

**Decisión canónica e intentos.** Una decisión canónica es el request de juego
que Showdown finalmente resuelve. Tiene un solo slot y `decision_index`; cada
reintento técnico incrementa un `attempt_index` sólo en memoria. El estado
separado `PendingChoice` transiciona `reserved/retried → rejected → retried` o
`reserved/retried → resolved`. Un rechazo invalida el slot sin eliminarlo. El
retry instala un dict nuevo en el mismo índice con snapshot, máscara, mapa de
órdenes, acción, `action_path` y reasoning propios. De D31 sólo reutiliza copias
de `opponent`, `field` y `turn` de la última proyección pública válida; nunca el
dict completo ni la máscara anterior.

**Correlación outbound atómica.** El wrapper de `PSClient.send_message` crea una
secuencia por `battle_tag` y enlaza el intento **antes del primer `await`**. La
fase es `sending`, `sent` o `failed`; si el websocket falla, el intento no queda
ficticiamente enviado. El original se delega exactamente una vez y los comandos
ajenos mantienen su comportamiento. Un rechazo sólo se acepta si coinciden el
tag, el request (`rqid` + frame pre-lock), el pending actual y el último comando
`/choose` de ese intento. La correlación se libera al cerrar la batalla.

**Taxonomía fail-closed.** Hay tres clases, determinadas por texto y por comando
outbound asociado:

- rechazo correlacionado de `/choose`: invalida el slot y habilita el retry;
- error auxiliar probado de `/undo` (`nothing/too late to cancel`): conserva el
  pending, no reintenta, incrementa un contador consultable y deja log
  estructurado; la línea cruda ya quedó en el recorder;
- `nothing to choose`, `too late to make`, `The battle crashed` o cualquier
  Invalid/Unavailable no clasificable: `ChoiceProtocolError` por el canal de
  background/runner, sin retry ni mutación del step.

La línea auxiliar se quita sólo del lote delegado a la rama demasiado amplia
de poke-env; D17 conserva siempre el frame crudo completo.

**Resolución.** Un request ordinario siguiente, incluido un `forceSwitch`,
resuelve el intento aceptado anterior antes de reservar el próximo índice. Un
request `wait:true` también confirma resolución aunque poke-env no llame a
`choose_move`. `win`/`tie` resuelve un intento enviado y aceptado. `win`/`tie`
con fase `rejected`, o `deinit` con cualquier pending, fallan cerrado. El cierre
libera inbox, request head, outbound, retry y proyección temporal del tag.

**Gate del dataset.** Después de `wait_for_pending_steps` y antes de crear la
`trajectory`, `_persist_one` valida todos los slots y el pending. Un slot
ausente, sin estado/acción, rechazado o terminalmente incoherente incrementa
`lost_step_count` y lanza `IncompleteTrajectoryError` con tag, índice y fase.
La batalla y sus `battle_turns` conservan el protocolo crudo como evidencia,
pero no queda una trayectoria vacía ni se escriben `trajectory_steps`
parciales, no se llama `finalize` y no se reparte reward a una trayectoria
incompleta. Sólo la acción finalmente resuelta entra en `trajectory_steps`; el
intento rechazado queda auditable en el protocolo crudo y en
`rejected_choice_count`.

**Lifetime del runner.** `play` compite `battle_against` con el future de fallo
background mediante dos tareas hijas que pertenecen a
`_battle_against_or_failure`. El helper las cancela y espera en `finally` ante
éxito, fallo o cancelación externa; cancelar el vigilante no cancela el future
compartido, que está shielded en `LudexPlayer`. El `TimeoutError` se interpreta
como deadline silencioso sólo cuando `asyncio.Timeout.expired()` confirma que
ese contexto inició la cancelación. Un `TimeoutError` recibido por el canal
background —por ejemplo, del websocket— se propaga sin convertirse en una
batalla omitida.

**Consecuencias.** D21 queda precisada: `decision_index` cuenta decisiones
canónicas resueltas y los retries comparten índice. No se agrega migración ni
se cambia `BattleRepository`; la PK `(trajectory_id, decision_index)` y el
`finalize` existente son correctos porque ningún intento rechazado llega a esa
tabla. Los tests controlados de Shadow Tag y `move 99` verifican protocolo,
índices contiguos y reward sólo sobre filas resueltas.

## D35 — el adaptador calc distingue observado/asumido, acota possible_moves y clasifica errores

**Contexto.** F2-07 lleva el contexto observable de la batalla a `packages/calc`
(servicio Node con `@smogon/calc@0.11.0`, D16). La primera entrega mapeó clima,
terreno, pantallas, boosts, status, HP, Mega y hazards en la dirección general
correcta, pero la revisión encontró cuatro huecos: (1) los defaults que calc
aplica a campos omitidos se presentaban como datos ciertos; (2) los
`possible_moves` del learnset del rival se calculaban todos secuencialmente sin
medir costo ni reducir; (3) el error 400 se capturaba con un `except Exception`
que absorbía JSON/shape inválido como si fuera semántico; y (4) los oráculos de
integración construían `_request` a mano y reenviaban el mismo request, en vez
de atravesar `calc_damage` con valores exactos.

**Defaults expuestos, no ocultos.** `@smogon/calc` resuelve con defaults todo
lo que el request omita (inspeccionado en el constructor de `Pokemon` del
paquete): `level→100`, `nature→Serious`, `ability→abilities[0]` de la especie,
`evs→0` (gen ≥ 3, 252 antes), `ivs→31`, `boosts→0`, `gender→M`, `status→""`,
`item→null`, `curHP→maxHP`. El servicio ahora devuelve `effective.attacker` y
`effective.defender` con esos valores efectivos; Python los compara contra lo
que envió y clasifica cada matchup en `observed` (lo que la batalla expuso),
`unknown` (lo que no expuso) y `assumed` (los defaults efectivos de calc). El
fallback de movimiento no declara "KO garantizado" (`min_damage >= remaining`)
si el resultado depende de ability/item/nature/EVs/IVs asumidos: bajo
supuestos rankea por valor esperado, nunca como certeza. El fallback de switch
es un minimax relativo sobre fracciones esperadas y expone las mismas
`assumptions` por entry; no afirma certeza.

**`possible_moves` acotado y medido.** El learnset del rival es un candidato,
no evidencia (D31/D32). La unión revelados+posibles deduplica con revealed
ganando y conserva procedencia (`revealed`/`possible`). Entre los posibles se
excluye únicamente `category=status` (no calculan daño); el descriptor completo
de cada posible —categoría incluida— viaja en el entry. Los matchups se
calculan con concurrencia acotada (`asyncio.Semaphore(8)`) y orden
determinista (`asyncio.gather` preserva el orden; los learnsets llegan
ordenados por `showdown_id` desde la base). Después del cálculo se reduce por
candidato a los top-3 posibles por daño máximo, preservando todos los
revelados y los diagnósticos. `damage_metrics` reporta calls, bytes y latencia
en mediana/p90/p99/máximo. `damage_metrics` se declara en el contrato
`GraphState` y sobrevive al workflow productivo
(`build_decision_graph(...).ainvoke` la conserva en la salida; StateGraph
descarta los canales no declarados). No amplía persistencia. El
`Semaphore(8)` tiene regresión propia: un fake bloqueante con 12 requests
demuestra `max_in_flight == 8`, y con latencias invertidas (el que empieza
tarde termina primero) se demuestra que el orden de salida sigue siendo el de
entrada. Canario real sobre la base: Blastoise Gen 6 trae
102 movimientos (29 status) → 73 requests no-status, y el máximo de latencia
medido cabe holgado en el presupuesto de decisión de 240 s (D26): no se
necesitó endpoint batch.

**Taxonomía de errores.** El servicio responde `{"error":{"code":string,
"message":string}}` (schema medido). Sólo un HTTP 400 con JSON y shape válidos
se captura por acción (`CalcSemanticError` con `kind/code/status/message`).
JSON inválido, shape inválido, 5xx, timeout, `RequestError` y errores de
programación propagan (como `CalcProtocolError` u original). No queda
`except Exception` amplio. El camino se prueba con `CalcClient` contra el
servidor real y contra un stub HTTP real para los 400 malformados.

**El HTTP 200 también se valida contra el shape completo.** El 200 no se
acepta sólo por parsear JSON: `CalcClient` valida contra `CalcResponse`
(calc.ts) los campos productivos (`damage_rolls`, `min/max_damage`,
`min/max_percent`, `ko_chance`, `description`, `defender_hp`) y
`effective.attacker/defender` con sus sub-campos. Un 200 con JSON válido pero
shape ausente, parcial o con tipos inválidos propaga `CalcProtocolError` y
nunca llega a `_attach_assumptions` (una respuesta parcial podía fabricar
assumptions vacías y volver a habilitar certeza falsa). El contrato se
rechaza en la frontera pública `CalcClient.calculate`, no en helpers
internos. Precisión final medida contra `EffectivePokemon` (calc.ts):
`nature`, `ability` e `item` aceptan `string|null` (el server emite `|| null`);
`status` y `gender` son `string` obligatorios (el server emite `|| ""` y
`|| "M"`), y `null` produce `CalcProtocolError`. `ko_chance.chance` es
opcional —el server serializa `chance: number | undefined` y omite
`undefined`— pero presente debe ser numérico: `chance=null` produce
`CalcProtocolError`. Las únicas claves válidas de `evs`/`ivs`/`boosts` son
`hp, atk, def, spa, spd, spe` (`STATS` de calc.ts); una clave ajena produce
`CalcProtocolError`, conservando la validación de valores numéricos. Stubs
HTTP de regresión cubren `effective` ausente y malformado, `status`/`gender`
null, `chance` null, claves de stats ajenas, campos productivos faltantes y
tipos inválidos, para attacker y defender.

**Mega por el camino completo.** `retrieve_context` recopila los items visibles
y `ContextRepository.load_mega_forms` resuelve en batch `items.megaStone`/
`megaEvolves` → fila `pokemon` de la forma Mega, generation-scoped (D2/D32).
`calc_damage` usa el item del activo; si `megaEvolves` no corresponde a la
especie, el item no es megastone o la forma no existe para esa gen →
`LookupError` ruidoso, nunca degradación a la forma base. Charizardite X/Y,
Venusaur, piedra equivocada, item no-Mega y frontera de generación (gen 9 no
tiene megas) están cubiertos por tests que atraviesan
`retrieve_context → load_mega_forms → calc_damage`.

**Hazards solo en switch-in.** Los hazards de entrada (`STEALTH_ROCK`, `SPIKES`)
van en `defenderSide` únicamente para el candidato que entra (incoming); contra
el rival ya activo (outgoing) se omiten, porque el activo ya los recibió al
entrar y el paquete los descuenta del `ko_chance`. Verificado con el oráculo:
Surf vs Charizard a 0.8 HP pasa de "guaranteed 2HKO" a "guaranteed OHKO after
Stealth Rock" sólo en el switch-in.

**Alcance y límites.** No se reimplementa la fórmula de daño en Python (D16).
La taxonomía de errores distingue el 400 semántico del fallo de protocolo pero
no subclasifica los 5xx (son infraestructura, punto). `possible_moves` reporta
bytes del request, no de la respuesta. Los oráculos de integración dependen de
que `packages/calc@0.11.0` no cambie los valores pineados (el canario los
refija). D33/D34 quedan integrados por merge aditivo de
`integration/phase-2-accepted`; D36/MON-10 no se toca.

## D36 — `identity_key` (hash de apertura pública) reemplaza `battle_tag` como identidad persistida; unicidad por `(source, identity_key)`

**Contexto.** `battle_tag` (`battle-<formato>-<N>`) no es un identificador
global: `N` sale del contador del server de Showdown, que vive en
`logs/lastbattle.txt` **dentro** del contenedor sin volumen. Un rebuild lo
reinicia en 1 y reusa tags viejos para batallas completamente distintas. Con
`UNIQUE(battle_tag)` como identidad, dos batallas genuinamente distintas que
comparten tag, p1, p2 y format después de un restart se fusionaban en
silencio: `battle_turns` y `trajectory_steps` de una sobreescribían a la
otra, mientras `battles` seguía luciendo internamente consistente
(reproducido en una transacción con rollback contra datos reales durante el
diagnóstico).

**`identity_key` sale de lo que el servidor narró, no de su contador.**
`compute_opening_identity` (`showdown/protocol.py`) calcula
`ps-open-v1:sha256:<64hex>` sobre el bloque de apertura **público** del
turno 0 (las líneas allowlisted `t:`, `gametype`, `gen`, `tier`, `rule`,
`teamsize`, `player`, `start`, `switch`; cualquier otra línea —`>tag`,
`|init|`, `|title|`, `|j|`, `|request|` privado— se descarta por no figurar
en el allowlist), normalizado línea por línea, en orden canónico, sin
deduplicar y sin comparar por substring ni por concatenación. La única
asimetría real entre p1 y p2 en ese bloque es el HP del `|switch|` inicial
(Showdown manda el valor exacto al dueño y el porcentual al rival); ambos
representan siempre el 100% al arrancar, así que normalizar ese token al
sentinel `FULL` es lo que le da paridad a la clave entre los dos lados. Un
switch inicial que no esté al 100% falla cerrado (`OpeningIdentityError`):
en el turno de apertura eso nunca debería pasar, y forzar el sentinel de
todos modos violaría D17.

**La completitud se parametriza por gametype, no se fija a Singles.** El
rol/slot esperado sale del conjunto canónico de roles que cada gametype
exige (`singles`/`doubles`/`triples`: `{p1,p2}`; `multi`: `{p1,p2,p3,p4}`) y
de la fórmula real de `Pokemon.getSlot()` del simulador vendorizado
(`pokemon-showdown@0.11.10`, `sim/pokemon.ts:504-507`, versión pineada por
D4): `positionOffset = floor(side.n / 2) * side.active.length`, letra =
`'abcdef'[posición + positionOffset]`. Para singles/doubles/triples esto
coincide con "cada rol usa sus propias letras", pero para `multi` no: p1/p2
caen en `'a'` y p3/p4 en `'b'`, no una letra uniforme por rol. Dos rondas de
revisión encontraron aperturas que producían una clave válida sin
representar una apertura real: conteos de línea que cuadraban
aritméticamente con un solo lado presente (dos `player|p1`, cero `p2`), y
topologías con roles ajenos al gametype declarado (singles con p1+p3, multi
sin p3/p4). Ambos casos ahora fallan cerrado porque `player`/`teamsize`/
`switch` se validan como **conjuntos** exactos contra el rol canónico del
gametype, no por conteo.

**Unicidad por `(source, identity_key)`, no global.** Identidad y
procedencia quedan separadas a propósito: un import y una grabación local no
se fusionan mientras `source` siga gobernando qué entra a training.
`battle_tag` deja de ser identidad y vuelve a ser la etiqueta de sala; sigue
indexado como `(source, battle_tag)` porque se sigue consultando por tag
(p.ej. re-persistencia desde el CLI).

**Conflicto resuelto atómicamente en una sola sentencia, no con un SELECT
previo.** La primera entrega comprobaba compatibilidad de
p1/p2/format/played_by/winner con un `SELECT` separado antes del
`INSERT ... ON CONFLICT`; una revisión (`LINEAR_VERDICT` L-01, CRÍTICO)
reprodujo con dos conexiones reales forzadas a interlevar que ambas llamadas
pasaban ese precheck antes de que cualquiera escribiera, dejando metadata y
winners incompatibles sin excepción. La compatibilidad ahora vive
enteramente en el `WHERE` de `_SAVE_BATTLE_SQL`: si metadata no coincide o
hay dos winners conocidos distintos, el `WHERE` da falso, ni el `UPDATE` ni
el `INSERT` ocurren, y `RETURNING` vuelve vacío — señal única e inequívoca
para `BattleIdentityConflictError`. Postgres toma un lock exclusivo sobre la
fila en conflicto desde que detecta la colisión de `(source, identity_key)`
hasta el commit/rollback de esa transacción, serializando a cualquier
escritor concurrente contra la misma `identity_key`. `winner` sólo avanza de
`NULL` a conocido o repite el mismo valor; dos winners conocidos distintos
nunca se pisan.

**Migración y backfill.** `identity_key` se agrega como columna, se
backfillea a `legacy:<battle_tag>` para las 552 filas históricas (triviales
de no repetir porque `battle_tag` ya era global bajo el régimen viejo; sus
`ProtocolRecorder` murieron con el proceso que las grabó, así que no pueden
re-persistirse) y queda `NOT NULL`. Se elimina `UNIQUE(battle_tag)` y se
agrega `UNIQUE(source, identity_key)`. Documentado explícitamente: las filas
legacy **no** se deduplican contra una futura reingesta de la misma batalla.
`migrate:down` hace preflight de `battle_tag` duplicados y aborta ruidoso
con el esquema intacto si reintroducir `UNIQUE(battle_tag)` global sería
inseguro, en vez de fallar a mitad de camino o elegir a mano qué fila
borrar.

**Verificación.** `test_protocol.py` cubre paridad p1/p2 con HP exacto vs.
porcentual invertidos, orden de llegada irrelevante, no comparación por
substring/concatenado, no deduplicación, aperturas incompletas y switches no
íntegros al 100%, y las topologías por gametype (`test_singles_con_p1_y_p3_
falla_cerrado`, `test_multi_sin_p3_p4_falla_cerrado`, `test_multi_con_
topologia_incorrecta_falla_cerrado`, `test_multi_real_completo_pasa`).
`test_repository.py` cubre conflicto de metadata y de winner
(`test_conflicto_de_metadata_con_misma_identidad_revienta`,
`test_conflicto_de_winner_conocido_distinto_revienta`), avance de winner
NULL→conocido y repetición legítima, y la atomicidad real con dos conexiones
bloqueantes (`test_dos_conexiones_reales_se_serializan_por_winner_
incompatible`): se afirma positivamente que la segunda conexión queda
bloqueada (`wait_for` con timeout real, no inferencia) antes de comprobar
`RETURNING`. `test_models.py` compara constraints e índices reales contra
`pg_indexes`/`information_schema`
(`test_battles_constraints_e_indices_espejan_el_ddl`), no sólo tipos de
columna. Cada arreglo se probó primero rojo contra el código pre-fix con la
reproducción exacta de la revisión, y se restauró y reverificó verde
después. Aplicado a la base real de Ludex tras un `pg_dump` con nombre;
verificado además contra 2 batallas reales jugadas contra el Showdown local
(sin restart).

**Alcance y límites.** No se agrega migración para separar `battle_turns`/
`trajectory_steps` de esta identidad: su clave sigue siendo
`(trajectory_id, decision_index)` y ningún intento rechazado llega a esas
tablas. Re-persistir la misma batalla usa la misma `identity_key` y es
idempotente por diseño. Las filas legacy backfilleadas no se pueden
re-vincular a una reingesta futura de la misma batalla porque no hay forma
de recalcular su fingerprint de apertura post-hoc. D33/D34/D35 quedan
integrados por merge aditivo de `integration/phase-2-accepted`, sin
reabrirse.

## D37 — el PP indeterminable por Pressure se marca en `persistent_state`, no en el snapshot de una sola llamada

**Contexto.** MON-18 diagnosticó por eliminación (`FINAL ROOT-CAUSE
CHECKPOINT`, MON-18) que el PP de un movimiento rival marcado `null` por
Pressure (D-existente, `register_move`/`pressure_on_us`) reaparecía como
número en la decisión siguiente sin que el rival volviera a usar ese
movimiento. Causa: `client.py` construye el `snapshot` de **cada** llamada
a `project_observable_state` fresco desde `serialize_battle(battle)`
(nunca encadena la proyección anterior — `client.py:1185/1390`), y
`serializer.py::_moves` lee `mv.current_pp` directo del objeto `Move` de
poke-env, que cuenta su propio PP sin saber de Pressure. El `None` de
`register_move` sólo vivía dentro de la llamada que lo produjo.

**Solución.** `persistent_state[identidad]` gana dos claves nuevas,
paralelas al patrón ya existente de `types`/`ability`/`moves`:

- **`unknown_pp_moves`** (`set[str]` de `move_id`): permanente, como
  `ability`. `register_move` la puebla cuando `pressure_on_us()` es
  verdadero **y el pokémon no está transformado en ese instante**. Sobrevive
  cualquier cantidad de switches ordinarios: `switch_out` nunca la toca.
- **`transform_unknown_pp_moves`**: temporal, como `types`/`moves` de un
  Transform. Si el pokémon está transformado al marcar (detectado por
  `"moves" in entry`, la misma señal que ya usa `switch_out` para saber si
  hay un Transform activo), la marca va acá en vez de en
  `unknown_pp_moves`, y `switch_out` la descarta junto con el moveset
  copiado — para que un Transform posterior sobre un objetivo distinto no
  herede una marca que no le corresponde.

Al principio de **cada** llamada a `project_observable_state` (antes de
procesar cualquier línea nueva del frame), se reaplica `pp = None` sobre
los movimientos marcados, sin importar qué número traiga el snapshot
fresco.

**Corrección R1 (LINEAR_VERDICT MON-18, blocker L-01): las dos marcas son
MUTUAMENTE EXCLUYENTES en la reaplicación, nunca se unen.** La entrega
original unía `unknown_pp_moves | transform_unknown_pp_moves` sin condición.
Tasos/Latwan reprodujeron el defecto: mientras `"moves" in entry` indica
Transform activo, el moveset **visible** en `mon["moves"]` es el COPIADO,
no el base — una marca permanente del base (de antes de transformarse)
nombra un `move_id` que puede coincidir por casualidad con el copiado
(mismo movimiento, p.ej. Scald, en el pokémon base y en el objetivo
copiado), pero son instancias de PP **distintas**. Unir las dos marcas
forzaba `pp=None` en un Scald recién copiado con PP real `5/5`. La
reaplicación ahora es exclusiva: con `"moves" in entry` (Transform activo)
sólo gobierna `transform_unknown_pp_moves`; sin Transform activo, sólo
`unknown_pp_moves`. Al terminar el Transform (`switch_out` restaura el
moveset base y descarta la marca temporal), la marca permanente vuelve a
gobernar sobre el moveset base restaurado.

**Aislamiento.** Por construcción: `persistent_state` ya está indexado por
identidad canónica (`base_species`), así que dos rivales distintos con el
mismo `move_id` nunca comparten marca. Dentro de una identidad, la marca es
un conjunto de `move_id`, no una bandera global del pokémon.

**Lo que NO cambia.** `max_pp` no participa: `Move.max_pp` en poke-env es
una property que siempre deriva del dex (`move.py:471-481`), nunca vuelve
`None` por sí sola, así que no hay nada que reaplicar ahí. `client.py` no
se tocó — el fix es enteramente interno a `project_observable_state`
(reaplicación) y `register_move` (marca), ambos en `protocol.py`.

**Verificado.** 7 tests en `test_protocol.py` — todos con un snapshot
fresco e independiente construido a mano por decisión (nunca la salida de
la llamada anterior encadenada; R1 blocker L-02 encontró que la entrega
original sí encadenaba en el test de Transform, contra lo que afirmaba el
REVIEW PACKET, y fue reescrito): reaplicación con snapshot fresco entre dos
llamadas; persistencia a través de switch-out y switch-in ordinarios;
aislamiento entre dos rivales con el mismo `move_id`; marca temporal de un
Transform descartada al terminar y sin fuga a un Transform posterior
distinto; el contrapeso negativo — sin compartir el mismo
`persistent_state` por `battle_tag`, la marca se pierde; la regresión
exacta de L-01 — un Scald base marcado permanentemente no contamina un
Scald copiado por Transform, y el Scald base vuelve a `null` al terminar el
Transform; y el canario de orden (L-02) — un Transform en la misma llamada
que trae un snapshot fresco numérico guarda en `persistent_state` el
moveset base **ya corregido** a `null`, no el número crudo (mover la
reaplicación después del loop de líneas lo pone rojo). Más 1 test en
`test_client.py` que atraviesa dos resoluciones reales del mismo
`battle_tag` vía `choose_move` y espía `project_observable_state` (sin
tocar `client.py` productivo) para confirmar identidad (`is`) del mismo
objeto `persistent_state` en ambas — falla si el caller dejara de reusar
`self._temporary_state.setdefault(tag, {})`.

Mutaciones dirigidas a los tres blockers de R1: unir las marcas sin
condición (L-01), mover la reaplicación después del loop de líneas (L-02) y
reemplazar `persistent_state` por un dict nuevo en `client.py` (L-03) — las
tres, aplicadas y restauradas por separado, ponen rojo exactamente el test
que cada una debía romper. Restauradas: 206/206 en
`test_protocol.py`/`test_client.py`.
## D38 — la metadata de decisión se persiste en `trajectory_steps` por envelope inmutable (F2-08/MON-13)

**Contexto.** El dataset de entrenamiento necesitaba el contrato de decisión
completo por `decision_index` —action, target, rationale breve, confidence,
alternatives— más metadata de calidad ML: provider/model efectivos, latencia
y usage. Nada de eso se persistía: `reasoning` se calculaba en el grafo y se
descartaba, y provider/model vivían solo en los artefactos del benchmark.

**Decisión.** 11 columnas nuevas en `trajectory_steps`, todas NULL en filas
históricas y de ruta random (sin backfill, nunca se inventa provider/model):
`rationale`, `target` jsonb, `confidence` double precision, `alternatives`
jsonb, `provider`, `model`, `decision_latency_ms` y los cuatro tokens planos
(`input/output/cached_input/reasoning_tokens`, el contrato fijo de
`CompletionUsage`). Constraints: confidence NULL o [0,1]; co-ocurrencia
`provider/model`; los cuatro tokens todos NULL o todos no-NULL, `>= 0`,
`cached <= input`; latencia NULL o `>= 0`; `alternatives` array y `target`
object cuando no-NULL.

**Envelope inmutable por llamada, no `last_*`.** `DecisionProvider.complete`
devuelve `CompletionEnvelope` (payload, provider efectivo, model efectivo,
usage, latencia de esa llamada). El patrón `last_completion_info` quedó
rechazado por el design verdict: es estado mutable compartido y cruza
metadata entre decisiones concurrentes — reproducido en la demo
`/tmp/demo_last_style.py`: la decisión lenta terminaba reportando el payload
de una decisión vecina. Con el envelope, la metadata viaja en el valor de
retorno y el cruce es imposible por construcción.

**Semántica por decisión canónica.** provider/model son únicamente los de la
respuesta LLM aceptada; en `fallback` quedan NULL (no se atribuye la acción
determinista a un modelo) con `alternatives=[]` y un rationale determinista.
`rationale` es el campo CANÓNICO del schema productivo y del prompt (L-01 de
la revisión R1): el payload del proveedor se rechaza si emite `reasoning` en
su lugar (missing rationale + extra_forbidden). El alias interno `reasoning`
en el resultado de `decide` solo existe para consumidores que ya lo leían
(run_graph) y se deriva del rationale ya validado; nunca forma parte del
schema enviado al proveedor. El usage de la decisión suma las respuestas
facturables del camino (retries
semánticos incluidos; los de infraestructura no producen usage en el backend
actual). `decision_latency_ms` va desde el primer intento LLM hasta la
respuesta aceptada o el fallback. `target` es NULL válido y esperado en
singles; un target no-NULL solo se acepta si la misma mascara legal expone
targets, y mientras no los exponga se rechaza la respuesta (reintento
semántico). Las alternatives atraviesan el mismo `normalize_action` +
`validate_action` que la principal, deben ser únicas tras normalizar y
distintas de la principal; `[]` es válido; cualquier violación consume el
reintento semántico (D26). D34 sigue vinculante: solo persiste la metadata
de la acción finalmente resuelta; una elección rechazada por Showdown no
conserva metadata ejecutada.

**Persistencia con EXCLUDED puro, como grupo.** `save_step` reemplaza las 11
columnas de metadata con `EXCLUDED` puro en el DO UPDATE (corrección del
TECH LEAD PARTIAL CHECKPOINT VERDICT: COALESCE podía actualizar
`action_taken` pero retener rationale/provider/model de una acción anterior,
creando una fila incoherente). Re-persistir la misma decisión es idempotente
(misma metadata -> mismos valores); una re-persistencia explícita sin
metadata deja el grupo completo en NULL. `rationale` es el nombre canónico;
`battle_turns.agent_reasoning` no se usa.

**Cableado del caller (integración MON-18).** `run_graph` en `client.py`
copia la metadata del resultado del grafo al `step` canónico y
`_persist_one` (cli.py) la pasa a `save_step`. La ruta random y la historia
quedan NULL. La forma exacta de los targets de la mascara legal se definirá
cuando una mascara los exponga (hoy la rechaza). Migración
`20260803000001_trajectory_decision_metadata.sql` verificada up/down en una
DB descartable; la DB compartida no fue migrada.

## D39 — la selección de provider/model se resuelve por decisión; la DB gobierna y el env solo bootstrap (F2-09/MON-14)

**Contexto.** El switch de modelos del plan ("cambiar el modelo desde la UI, incluso entre turnos") no existía: el provider se inyectaba al compilar el grafo y no se podía cambiar sin recompilar. El plan define `providers`/`models`/`settings` desde el principio; nada de eso estaba en el esquema.

**Decisión.** Tres tablas nuevas (`providers`, `models`, `settings`; migración `20260804000001`): `providers` guarda `name`, `base_url` opcional, `api_key_env` (el NOMBRE de la variable de entorno, NUNCA el valor — las claves no existen en la DB, en logs ni en snapshots) y `enabled`; `models` referencia al provider con `model_id`, `label`, `is_default`, `enabled`; `settings` es key/value jsonb con la selección activa (`active_model` = `{"provider": ..., "model": ...}`, sin secretos).

**Resolución POR DECISIÓN, no por compilación ni por arranque de batalla.** El grafo gana el nodo `resolve_provider` al inicio (`START → resolve_provider → parse_state → retrieve_context → calc_damage → decide → END`): cada invocación consulta la selección activa en la DB (`ModelRepository.active_selection`, con fallback al modelo default habilitado y, si la DB está vacía, al bootstrap de env `LUDEX_PROVIDER`/`LUDEX_MODEL`). Cambiar el modelo activo entre dos invocaciones del mismo grafo surte efecto sin recompilar ni reiniciar la batalla (test bloqueante: `test_el_mismo_grafo_cambia_de_modelo_entre_invocaciones`; la mutación que cachea la selección al arranque lo pone rojo).

**Instancias cacheadas por `(provider, model)`, selección nunca cacheada.** `ProviderResolver` reutiliza la INSTANCIA del provider para el mismo par: el cooldown de claves (D30) y los reintentos de infraestructura viven en la instancia y sobreviven entre turnos del mismo modelo. La selección activa se consulta en cada `resolve()`. El envelope inmutable de MON-13 (D38) sigue siendo la única vía de metadata efectiva: la selección resuelta por turno llega al `CompletionEnvelope` y de ahí a la metadata persistida de `trajectory_steps`.

**Fail-closed de selecciones stale/disabled (R2/L-01).** `ModelRepository.validate_selection` es la ÚNICA frontera de consulta/validación de la selección activa: comprueba en una consulta que el provider existe y está `enabled` y que el model existe para ESE provider y está `enabled`; cualquier violación lanza `ModelSelectionError`. La usan `agent model-set` (rechaza sin tocar settings) y `ProviderResolver.resolve`: una selección de la DB (settings o default) que apunta a un modelo inexistente o deshabilitado lanza `ProviderSelectionError` y NUNCA cae silenciosamente al bootstrap ni al default — la decisión no se atribuye a un modelo que la DB deshabilitó, y `provider_factory` no se invoca (canarios en tests). El bootstrap de env no pasa por la frontera: es el último recurso, no depende de la DB, y la factory ya valida claves/rutas. El fallback de `active_selection` sin fila en settings sigue exigiendo provider y model habilitados (mismo query de default).

**Benchmark pinneado y auditado.** El benchmark fija provider/model al inicio (D28) y usa `PinnedResolver(..., enforce_pin=True)`: un auditor envuelve el provider y verifica que cada envelope efectivo coincida con el pin; cualquier mezcla lanza `ProviderMixError` y aborta la corrida. Env bootstrap: el benchmark NO consulta la DB.

**Adapter de ejecución explícito y cableado real.** `execute_action` en
`graph/execute.py` (módulo PURO, sin poke-env) traduce el `action` del grafo
al `BattleOrder` del mapa capturado. NO es un nodo LangGraph y no puede
serlo: el mapa accion→`BattleOrder` se captura síncrono antes del primer
await (D31/D22 — la disciplina que garantiza `action_taken in
legal_actions` por construcción) y poke-env exige el `BattleOrder` como
retorno de `choose_move`; el grafo corre después de awaits. La
correspondencia grafo→poke-env queda cerrada por el adapter fuera del
grafo: `run_graph` en `showdown/client.py` llama `execute_action(action,
action_orders)` sobre el resultado del grafo y convierte `None` (fuera de
la máscara capturada) en `RuntimeError`. El cableado quedó estacionado
durante la ventana en que `client.py` era territorio de MON-18 y se
completó en la integración final (MON-18 liberado, ver D40): la
equivalencia end-to-end se prueba en `test_el_caller_ejecuta_despues_de_
decide_en_orden` (orden `resolve_provider → parse_state →
retrieve_context → calc_damage → decide → execute`); el test que rompe el
cableado (quitar la llamada a `execute_action` de `run_graph`) se pone en
rojo y se restaura.

**Metadata por decisión desde el envelope del grafo.** Las 11 claves de
metadata de la decisión (`rationale`, `confidence`, `alternatives`,
`target`, `provider`, `model`, `decision_latency_ms`, `input_tokens`,
`output_tokens`, `cached_input_tokens`, `reasoning_tokens`) llegan al
`step` canónico SOLO desde el resultado de `decide` (que sale del
`CompletionEnvelope` inmutable de D38, nunca de estado compartido
`last_*`), en los DOS caminos: éxito LLM y fallback determinista usan la
MISMA frontera (`run_graph` copia el resultado del grafo al step; la ruta
random queda NULL). El test que reutiliza metadata de una decisión
anterior (patrón `last_completion_info` de D38) cruza las 11 claves entre
dos decisiones del mismo tag y se pone en rojo.

**Catálogo OpenCode Zen no hardcodeado.** `agent provider-init` puebla `providers` desde el catálogo de config (bootstrap) y, para open_code_zen con clave y base URL presentes, sincroniza `models` resolviendo el endpoint `/models` (shape OpenAI-compatible asumido, `{"data": [{"id": ...}]}`; un shape inválido falla ruidoso — límite documentado: se verificó el formato del gateway contra el contrato OpenAI-compatible, no contra una key real en esta rebanada). `agent model-set` fija la selección activa. Sin PATCH endpoint ni UI (fase 3/4).

**Excepción de cliente documentada.** No se usa `init_chat_model` de LangChain: el contrato real necesita opciones por proveedor (timeout, `thinking`/`max_tokens`, structured output) que esa API no expone sin ramas; se conservan los clientes especializados (`_LangChainBackend` por kind) construidos por `default_provider_factory` desde la fila de la DB.

**Alcance y límites.** El camino LLM vivo de esta fase es el benchmark (pinneado); el resolver por turno queda probado a nivel workflow/caller y listo para fase 3 (HITL), donde el grafo jugará batallas largas con cambio de modelo entre turnos. `ProviderChain` queda como API para el caso interactivo multi-proveedor (hoy el benchmark la usa con un solo provider). D37 (MON-18) y D38 (MON-13) se conservan íntegras.

## D40 — el item del rival vive en `persistent_state`; el auditor reevalúa el dex en cada switch-in (MON-18 R3)

**Contexto.** El ROOT-CAUSE CHECKPOINT R3 confirmó, en vivo
(`battle-gen6randombattle-2746`, y previamente medido en
`battles.id=2782/2787`), que `poke-env` 0.15.0 corrompe
`battle.opponent_team[...].item` entre una decisión y la siguiente después
de un intercambio por Trick: el valor corrupto es exactamente el item que
NUESTRO propio activo recibió en el mismo canje. Como `client.py` arma el
`snapshot` de cada decisión fresco desde `serialize_battle(battle)` (nunca
encadena la proyección anterior, D31), ese valor corrupto pisa evidencia
pública ya establecida sin que ninguna línea nueva la pida. El item se
autocorrige por casualidad cuando el ítem verdadero genera una línea pasiva
propia (Life Orb, Leftovers) en la MISMA llamada, pero queda corrupto para
siempre cuando no la genera (Choice Scarf/Band/Specs) — exactamente el
patrón medido en `battles.id=2782` (Purugly) y `2787` (Alakazam).

Por separado, el mismo CHECKPOINT clasificó el hallazgo de `types` sobre
Meloetta en `battles.id=2787` como un **falso positivo del auditor**, no un
defecto del recorder: Relic Song revierte la forma de Meloetta al salir del
campo (a diferencia de Mega, que persiste), y la línea de switch-in que
revierte narra el MISMO `details` que la primera vez que entró. La fila
persistida por `apps/agent` ya era correcta.

**Decisión — item (`apps/agent/src/ludex_agent/showdown/protocol.py`).**
Mismo patrón arquitectónico que D37 para el PP bajo Pressure, pero
permanente (Transform no copia item, así que no hace falta la variante
temporal): `remember_item(mon, value)` fija el item Y lo memoriza en
`persistent_state[identidad]["item"]` desde las cuatro rutas públicas ya
existentes que lo revelan (`-item`, `-enditem`, daño/heal propio por item vía
`apply_damage_or_heal_ownership`), y esa memoria se reaplica al principio de
CADA llamada, antes de procesar cualquier línea nueva del frame — igual que
`unknown_pp_moves`. `value=None` (item consumido/removido) es tan
significativo como cualquier item real: la clave `"item"` queda presente con
ese valor, nunca ausente, y nunca se escribe con el sentinel inicial
`unknown_item` (eso no es evidencia, es su ausencia). No hace falta código
específico de Trick: toda revelación de item, sea cual sea su origen, ya
pasaba por esas cuatro rutas, y las cuatro ya estaban correctamente
delimitadas por lado (nunca escriben sobre el equipo propio).

**Decisión — types/Meloetta (`packages/dataset-audit/src/projection.ts`).**
`updateFromDetails` cortaba en `details === mon.lastDetails` antes de
reevaluar el dex — el mismo corte que trae `Pokemon._update_from_details`
de poke-env (`pokemon.py:669-714`), que el auditor reproduce a propósito.
La condición se angosta, no se elimina: además de comparar `details`, ahora
exige `mon.formeId === mon.species` (ninguna forma temporal activa) para
saltarse la reevaluación. Un Mega que sale y vuelve nunca pasaba por este
camino para empezar — su switch-in narra un `details` DISTINTO
("Charizard-Mega-X..." vs "Charizard...") porque la forma persiste — así que
la corrección no lo afecta.

**Tests.** `apps/agent/tests/showdown/test_protocol.py`: reproducción
determinista de `battles.id=2782` con las líneas reales de sus turnos 1-2 y
un snapshot fresco que trae el valor corrupto medido en vivo (canario de
boundary: cruza dos llamadas de `project_observable_state` con sólo
`persistent_state` en común, igual que D37); contrapeso Life Orb (la
revelación pasiva también escribe en memoria y también sobrevive un
snapshot fresco corrupto); `-enditem` persiste `None` y sobrevive un
snapshot fresco; evidencia nueva reemplaza memoria anterior (`None` o item
distinto); sobrevive un switch ordinario; dos identidades no se contaminan;
una línea del lado propio no contamina memoria rival (ya pasaba antes del
fix, sirve de sentinela — mismo patrón que el contrapeso negativo de D37).
`packages/dataset-audit/test/projection.test.ts`: switch-in con el mismo
`details` que uno anterior revierte una forma temporal; contrapeso Mega
(persiste, ya pasaba y sigue pasando). Tres mutaciones dirigidas sobre el
item (retirar la reaplicación, retirar la persistencia de `-enditem`,
permitir que una línea propia escriba memoria rival) y una sobre Meloetta
(restaurar el corte defectuoso), cada una puesta en rojo exactamente el test
esperado y restaurada.

**Alcance.** Cero cambios en `showdown/client.py` ni `cli.py` (confirmado
por `git diff --stat` vacío en ambos rangos). D37 y D38 quedan intactos. No
se tocó ninguna fila de la DB compartida.

### R4 — transferencia de item y provenance bajo Illusion

La revisión de R3 encontró dos blockers sobre el mismo mecanismo de D40,
ambos exclusivos de `apps/agent/src/ludex_agent/showdown/protocol.py`
(`packages/dataset-audit` no se tocó esta ronda).

**T-01 — transferencia de item hacia nuestro lado dejaba al rival con
memoria stale.** `-item|p1a: X|Item|[from] move: Thief|[of] p2a: Y` narra
la transferencia en UNA sola línea: el `ident` (parts[2]) es quien RECIBE
el item -- nuestro propio activo -- y `[of]` nombra a quien lo pierde.
Showdown nunca manda una línea separada para el que pierde. El filtro
genérico de `ident` descartaba la línea completa antes de que ningún
handler notara que el rival se quedó sin item, y su entrada en
`persistent_state` seguía diciendo el item VIEJO para siempre.
`apply_item_transfer_ownership` corre antes del filtro genérico -- mismo
patrón que `apply_damage_or_heal_ownership` -- y limpia (`remember_item(...,
None)`) al rival nombrado por `[of]` cuando la causa es Thief/Covet
(movimientos) o Pickpocket/Magician (abilities). Cuando el receptor es el
RIVAL (nos robó a nosotros), la función es un no-op: el `[of]` nombra a
nuestro lado, que `_owner_of` nunca resuelve, y el handler normal de
`-item` (ya alcanzable porque el `ident` es del rival) cubre esa dirección
sin cambios. Symbiosis no se implementa: exige un aliado del mismo lado,
estructuralmente inalcanzable en singles -- el único gametype que este
proyector modela (`active_prefix` asume una sola ranura activa por lado) y
el único que juega `apps/agent` en la práctica.

**T-02 — un item revelado durante Illusion quedaba pegado al imitado para
siempre.** La memoria de D40 no distinguía "evidencia sobre esta
identidad" de "evidencia observada mientras Zoroark la usaba de disfraz".
`remember_item` ahora guarda, en la PRIMERA mutación de `item` desde el
último switch-in de una identidad (marcada por la ausencia de la clave
`item_backup`), el valor ANTERIOR -- ausente (sentinel `_NO_PRIOR_ITEM`),
`None`, o un item -- antes de pisarlo. Dos desenlaces posibles para esa
estancia:

- **Switch-out ordinario** (`switch_out`): descarta el backup SIN
  restaurar nada. Un switch normal confirma que la identidad aparente era
  real, así que el item nuevo queda permanente.
- **`|replace|` (Illusion se rompe)** (`end_illusion`): restaura la memoria
  de item que el imitado tenía ANTES de la primera mutación de la
  estancia, justo antes de delegar en `switch_out` -- si no había memoria
  previa, la clave `item` vuelve a quedar AUSENTE, nunca `None` (son
  estados distintos: "sin evidencia" vs. "confirmado sin item"). Zoroark
  nunca se siembra a partir del imitado, política fail-closed ya existente
  desde antes de D40: sigue `unknown_item` salvo evidencia independiente.
  Sólo las claves `item`/`item_backup` se tocan; `ability`, `types`,
  `moves` y las marcas de PP sobreviven intactas.

**Tests.** Parametrizado sobre Thief/Covet/Pickpocket/Magician (con un
item rival previamente memorizado, la línea de adquisición hacia p1, y un
snapshot fresco posterior que sigue mostrando `None`); contrapeso de la
dirección donde el rival adquiere; aislamiento entre identidades. Para
Illusion: flujo completo con item previo conocido (revelación durante el
disfraz, una llamada fresca intermedia, `|replace|`, recuperación del item
previo, Zoroark sin heredarlo, un switch-in posterior del imitado que
tampoco hereda el item de Zoroark, y otras claves de `persistent_state`
intactas); memoria previa ausente vuelve a clave ausente, no a `None`;
switch ordinario confirma el item nuevo y descarta el backup. Cuatro
mutaciones dirigidas (retirar el manejo pre-filtro de T-01; omitir la
restauración en `replace`; tratar "ausente" como `None`; limpiar todo el
`entry` y perder `ability`), cada una puesta en rojo exactamente el test
esperado y restaurada.

**Alcance (R4).** Cero cambios en `showdown/client.py`, `cli.py` y
`packages/dataset-audit` (código ya aceptado, sin tocar esta ronda). D37 y
D38 siguen intactos. No se tocó ninguna fila de la DB compartida.

## D41 — los tipos base del rival viven en `canonical_types`; `-formechange` es temporal, `detailschange` es permanente (MON-19)

**Contexto.** El ROOT-CAUSE CHECKPOINT de MON-19 reprodujo en vivo, cruzando
la frontera real (`serialize_battle` → `project_observable_state`), el
defecto detrás de `battles.id=2787` (`battle-gen6randombattle-2719`):
`Pokemon._update_from_details` de poke-env (`pokemon.py:669-671`) corta en
seco si `details` no cambió desde la última vez. Relic Song narra un
`-formechange` a Pirouette sin cambiar `details` (a diferencia de Mega, cuyo
`details` SÍ cambia), así que el switch-in que revierte Pirouette al volver
a entrar puede seguir leyendo el `types` corrupto del propio poke-env. Es
exactamente el mismo mecanismo que D40 ya documentó para `item` tras Trick
-- la diferencia es que D40 clasificó el hallazgo de `types` sobre Meloetta
como un falso positivo del AUDITOR (`packages/dataset-audit/src/
projection.ts`, ya corregido ahí); esta ronda confirmó, con snapshots
frescos e independientes cruzando el boundary real, que además hay un
defecto GENUINO en el RECORDER (`protocol.py`) para la misma criatura, no
cubierto por esa corrección del auditor.

**Decisión.** Mismo patrón arquitectónico que D37/D40: una clave nueva y
permanente en `persistent_state`, `canonical_types`, reaplicada al
principio de cada llamada, antes de procesar cualquier línea del frame. La
clave preexistente `"types"` conserva exactamente su significado actual --
backup de un override TEMPORAL activo (typechange, Transform) -- y ahora
también cubre `-formechange`; las dos claves son mutuamente excluyentes en
la reaplicación (mismo patrón que D37 exige entre `unknown_pp_moves` y
`transform_unknown_pp_moves`): mientras `"types"` esté presente, el override
es lo que se ve, y reaplicar `canonical_types` por encima lo pisaría mal.

- **`switch_in`** es evidencia pública DIRECTA del tipo canónico: recalcula
  `types` del dex sin condición (ya lo hacía) y ahora también escribe
  `canonical_types`, además de descartar cualquier `"types"` colgado de la
  misma identidad -- un override temporal no puede sobrevivir a un
  switch-in de la propia identidad.
- **`detailschange`** (Mega/Primal) es PERMANENTE -- `details` cambia, así
  que poke-env nunca corta acá, y estos tipos SON el nuevo canónico:
  actualiza `canonical_types` directo, sin pasar por el backup temporal.
- **`-formechange`** (Relic Song y demás formas que sí revierten al salir)
  es TEMPORAL: entra al mismo ciclo backup/restauración que ya usan
  `apply_typechange`/`apply_transform`, y NUNCA toca `canonical_types` --
  si lo hiciera, `canonical_types` quedaría con la forma temporal en vez de
  la base, y la reaplicación la sostendría stale para siempre.
- **`switch_out`** no requiere cambios: ya sólo restaura el backup temporal
  de `"types"` (típechange/Transform/`-formechange`), dejando
  `canonical_types` intacto y disponible para la próxima llamada fresca --
  exactamente lo que pide el contrato.

**Tests.** `apps/agent/tests/showdown/test_protocol.py`: reproducción
determinista de `battles.id=2787` con snapshots frescos e independientes en
cada etapa (switch-in inicial, Relic Song, una llamada fresca intermedia
SIN evidencia nueva mientras Pirouette sigue activa, switch-out, switch-in
con los mismos `details` base, y una llamada fresca posterior que confirma
NORMAL/PSYCHIC -- el bug real la dejaba en NORMAL/FIGHTING en ese último
paso); contrapeso Mega con cuatro snapshots frescos independientes
(persiste permanentemente, incluso tras salir y volver); contrapeso
typechange y contrapeso Transform (ambos siguen activos con
`canonical_types` presente de fondo, sin que la reaplicación los pise);
Meloetta sin `-formechange` no cambia; canario de orden (la reaplicación
corre ANTES del loop de líneas, mismo patrón que D37/D40); switch_in
descarta un override temporal colgado de la misma identidad. Cuatro
mutaciones dirigidas (retirar la reaplicación; tratar `detailschange` como
temporal; promover `-formechange` a `canonical_types`; reaplicar
`canonical_types` aun con un override temporal activo), cada una puesta en
rojo exactamente los tests esperados y restaurada.

**Alcance.** Cero cambios en `showdown/client.py`, `cli.py`,
`packages/dataset-audit`, migraciones o datos históricos (confirmado por
`git diff --stat` vacío en esos rangos). D37 y D40 quedan intactos. El
auditor (`dataset-audit --scope all` y `--scope training`, Node 22) se
corrió completo tras el fix: los conteos de violaciones -- incluido
`hidden_information/types` -- quedan IDÉNTICOS a la línea base previa, como
corresponde: este fix sólo afecta grabaciones FUTURAS a través de
`client.py` (fuera de alcance de MON-19), nunca las 701 batallas ya
persistidas en la DB compartida.

## D42 — reloj inyectable en el grafo de decisión y métricas de latencia (F2-10/MON-15)

Todas las mediciones de tiempo en el camino crítico de decisión usan un
reloj inyectable (`Callable[[], float]` que devuelve segundos monotónicos),
en vez de llamar directamente a `time.monotonic()`. `KeyRotatingProvider`,
`decide()`, `FakeDecisionProvider` y `LudexPlayer` reciben el reloj por
constructor o argumento; si el llamador no lo pasa, se usa `time.monotonic`
como default.

Motivo: las métricas de latencia (p50/p95/máximo por batalla y total) deben
ser deterministas en tests y reproducibles en diagnóstico. Usar el reloj real
hace imposible afirmar que un cambio no empeora la latencia sin correr
batallas reales, y también hace imposible simular deadlines y cooldowns sin
esperar. Con el reloj inyectado el test avanza el tiempo explícitamente y
verifica que el provider enfríe claves, cumpla deadlines y reporte latencias
esperadas.

Regla de implementación: si una función necesita medir intervalos o comparar
un instante contra un deadline, recibe `clock`. Nunca se mezcla `clock()` con
`time.monotonic()` en la misma rutina: eso rompe los tests con reloj falso y
puede hacer que un deadline nunca se dispare.

**L-01 (R2): DOS poblaciones de latencia, nunca mezcladas.** La latencia de
cada completion (una llamada a `provider.complete()`) y la latencia
end-to-end de cada decisión (retries incluidos) son muestras de
poblaciones distintas, con contratos con nombre explícito:

- `completion_latency_ms_count/total/p50/p95/max` — una muestra por cada
  llamada exitosa al provider, registrada por `KeyRotatingProvider`.
- `decision_latency_ms_count/total/p50/p95/max` — una muestra por decisión,
  desde el primer intento LLM hasta la respuesta aceptada o el fallback,
  registrada por `decide()`.

Una decisión con una completion aporta exactamente una muestra a cada
población (nunca dos al mismo contador); una decisión con dos intentos
semánticos aporta 2 completions y 1 decisión; el fallback tras dos
respuestas inválidas aporta 2 completions y 1 decisión. El
`CompletionEnvelope.latency_ms` sigue siendo por llamada. El test cruzado
`test_cruzado_*` en `test_decision.py` une `KeyRotatingProvider` con
`decide()` sobre el MISMO `DecisionMetrics` y falla si alguien vuelve a
mezclar las poblaciones en cualquiera de las dos direcciones.

**Política de redondeo (L-01):** los agregados usan entero más cercano con
`round()`; truncar con `int()` está prohibido (un 99.999... debe quedar en
100, nunca en 99). Sin muestras, `total/p50/p95/max` son `None`: null en el
artefacto JSON y blanco en el ledger, nunca 0/0/0 comparable. El
`BenchmarkRecord` y el ledger distinguen ambas poblaciones por nombre
(Completion vs Decision), y cada celda vacía significa "sin muestras o no
comparable", nunca cero implícito.

**Tests.** `test_provider.py` verifica deadline, cooldown de claves con 429,
mezcla de modelos con conteos correctos usando `SequenceClock`, redondeo sin
truncamiento y percentiles nulos sin muestras. `test_decision.py` verifica
que `decide()` registra latencia 125 ms con reloj inyectado y los cuatro
canarios cruzados del doble conteo (1 completion/1 decisión, retry
semántico, fallback, y disyunción de poblaciones). Cada test falla si se
revierte el uso de `self._clock()`, si se ignora el `clock` inyectado, si se
vuelve a mezclar una población de latencia, o si se reintroduce el
truncamiento por `int()`.

**R3 — evidencia durable y sanitizada del error original.** El artefacto
JSON de una corrida fallida conserva tres campos separados: `failure`
(mensaje público sanitizado, el de siempre), `failure_type` (clase del
error clasificado, p.ej. `TransientProviderError`) y `failure_cause_type`
(clase de la causa original vía `__cause__`, p.ej. `APITimeoutError`). La
derivación vive en `failure_classification` (`benchmark.py`), que devuelve
SOLO nombres de clase — nunca mensajes crudos, URLs, módulos, tracebacks ni
secretos. Un error sin `__cause__` (p.ej. `BenchmarkDeadlineExceeded`
sintético o `ProviderSelectionError` del path not-run) deja
`failure_cause_type=None`; jamás se inventa una causa. El camino real
funciona porque `KeyRotatingProvider.complete` re-lanza el error clasificado
con `raise error from raw`, preservando el original en `__cause__`. Los
tests sintéticos atraviesan la cadena completa raw → clasificado → resultado
→ record → JSON y fallan si se retira la causa, si se clona el error sin
causa, o si se intenta persistir el mensaje crudo con cualquier key señuelo.

**R3 — contrato tipado de métricas.** `DecisionMetrics.snapshot()` mezcla
contadores `int` con percentiles de latencia `int | None`. Todos los
consumidores lo declaran así: `_benchmark_command` devuelve
`dict[str, int | None]`, los callbacks de progreso y
`build_benchmark_record` reciben `Mapping[str, int | None]`.
`calculate_cost` consume ÚNICAMENTE los campos de tokens
(`input_tokens`, `cached_input_tokens`, `output_tokens`) y rechaza con
`ValueError` si alguno es `None` — un conteo de tokens ausente no es
calculable y nunca se confunde con los percentiles nullable, que no se leen
en el costo.

**R3 — identidad del run.** El screen pinneado de OpenCode se identifica por
el modelo que efectivamente ejecutó: `20260808-opencode-claude-haiku-4-5-screen`
(antes `20260808-opencode-mimo-screen`), en archivo, `run_id`, ledger y
notas. Un artefacto pinneado jamás se identifica por un modelo distinto del
efectivo.

## D43 — matriz de compatibilidad de proveedores con protocolos declarativos y presupuesto por provider (F2-10B/MON-20)

**Contexto.** MON-20 exige probar en batallas aisladas cada modelo accesible
de Gemini, Kimi y OpenCode Zen. La documentación oficial de Zen (2026-08-07)
asigna familias a endpoints distintos: GPT y Grok → `/responses`, Claude y
Qwen → `/messages`, familias chinas (DeepSeek, MiniMax, GLM, Kimi, MiMo,
libres) → `/chat/completions`, y Gemini → su endpoint nativo por modelo
(`/models/<id>`). El repo reconocía `responses` en el schema de rutas pero lo
rechazaba en runtime, y no había deadline por batalla configurable.

**Decisiones:**

1. **Protocolo por ruta, no por ensayo.** `model-routes.json` declara por
   provider/model: protocolo, structured output, temperature/thinking,
   max_tokens, timeout y endpoint opcional. `build_route_provider` es el
   ÚNICO punto de despacho (responses → backend OpenAI Responses,
   messages → Anthropic, google → Gemini nativo con base_url del gateway,
   chat_completions → OpenAI-compatible). Un modelo sin ruta se clasifica
   `missing-route` en la matriz: NUNCA se prueba "chat_completions y si
   falla messages" pagando. `responses` usa JSON textual estricto
   (`text_json`) sometido a la misma validación semántica de D26; el
   método puede sobreescribirse por ruta (`structured_output`).

2. **Cuarentena de credenciales distinta del cooldown de cuota.** Un
   401/403 es específico de UNA clave (vencida/revocada): nueva clase
   `CredentialRejected`; `KeyRotatingProvider` pone esa clave en
   cuarentena PERMANENTE para el proceso y sigue con la siguiente, con
   contador propio `keys_quarantined` (no confundir con `key_rotations`,
   que es de 429/cooldown). Un error model-wide (404, 400, fatal)
   detiene la corrida en la primera clave: el pool de 11 no se quema en
   vano (canario). Pool completo rechazado → `ProviderPoolExhausted`
   ("quarantined"), clasificado `credential/model unavailable` — nunca
   un falso incompatible.

3. **Fix real: la rotación de Gemini jamas rotaba credenciales.** Los
   campos reales de `ChatGoogleGenerativeAI` son `google_api_key`,
   `max_retries`, `timeout` y `base_url`; el código pasaba
   `api_key`/`retries`/`request_timeout`, que pydantic ignora en silencio
   (extra=ignore): cada llamada usaba la clave del entorno y las 11 del
   pool nunca se rotaban de verdad. Se corrigió con TDD y mutación
   (revertir `google_api_key` rompe el test). `base_url` además habilita
   el protocolo nativo de Gemini detrás del gateway Zen.

4. **Deadline por batalla configurable.** `LUDEX_BATTLE_TIMEOUT_SECONDS`
   (default productivo 180, positivo, `--battle-timeout` en CLI), se
   propaga hasta `run_benchmark` y se persiste en TODOS los artefactos
   (`battle_timeout_seconds`), sin tocar el deadline compartido de cada
   decisión (D26). La matriz usa 1800. Mutaciones verificadas: ignorar el
   valor configurado, persistir otro valor o volver a una constante fija
   rompen tests.

5. **Matriz dinámica con presupuesto.** `agent matrix-plan` refresca
   /models (metadata, sin cuota) antes de cada ronda, publica altas/bajas
   contra el inventario commiteado, y escribe un manifiesto con una fila
   por provider/model: pin estricto, concurrency=1, persist=false, dos
   batallas solo si el smoke pasa, tier/precio/costo estimado por modelo.
   Presupuesto por provider (addendum): orden ascendente por costo,
   reserva smoke + dos batallas antes de iniciar, hard-stop antes del cap
   (Zen cap 10 USD dejando 1; Kimi 5.50 dejando 0.50; Gemini solo free
   tier confirmado). Si el saldo no alcanza → `pending-budget`/`not-run`,
   NUNCA unsupported/incompatible/externally-limited, preservando
   protocolo/ruta/costo y sin publicar winrate. Gemini NO se asume gratis:
   tier por modelo con fuente; sin prueba de costo cero → pending-budget.

**Límite documentado.** El free tier de Gemini no pudo verificarse contra
la página oficial (no accesible desde esta máquina); las 11 claves son free
tier según el usuario, pero el tier por modelo queda marcado `unknown`
(excepto Gemma 4, $0.00 con pesos abiertos) hasta confirmarse en la cuenta
al ejecutar. `moonshot-v1-auto` y `claude-sonnet-4` (deprecado) no tienen
precio publicable → pending-budget. `ling-3.0-flash-free` no figura en la
documentación de Zen → `missing-route` hasta verificación. Los modelos
deprecados (docs Zen) siguen listados en /models y en scope, marcados como
deprecados.

## D44 — `training` exige trayectoria ÍNTEGRAMENTE `state_schema_version=2`, con al menos un paso; una mezcla v1/v2 se excluye completa (MON-11 R3, corregido R4)

**Nota de numeración.** D42 pertenece a MON-15 y D43 a MON-20 -- no se
documentan acá; esta entrada continúa la numeración sin llenar ese hueco.
No se renumera en R4.

**Contexto.** El CHECKPOINT R2 de MON-11 clasificó las violaciones en las
12 batallas `local` (`scope=training`, 774 pasos): **1 429 en total**, de
las cuales **1 424 son `hidden_information`** y **5 son `decision_index`**
(turno que retrocede respecto de la decisión anterior) -- dos invariantes
distintos, no una sola cifra de `hidden_information`. Las 774 filas de ese
corpus son 100% `state_schema_version=1` -- un esquema que D31 ya
reemplazó. Antes de D44, `training` sólo exigía `battles.source <> 'test'`
y `trajectories.final_result IS NOT NULL` -- el contrato canónico del
scope, fijado por **D33** (`## D33 — el auditor de dataset es la compuerta
del corpus...`), no por F2-09/D39 (que es la resolución de provider/model
por decisión, MON-14, un tema sin relación). D33 documenta además que la
migración `20260727000007_battle_source_test.sql` cita mal a "D19" para
este mismo contrato; D44 hereda D33 como su antecedente correcto, no la
cita incorrecta. Antes de D44, ese contrato dejaba pasar trayectorias v1
con el mismo defecto de proyección que D31/D40/D41 documentan, y --el
riesgo real que motiva esta decisión-- una trayectoria MIXTA (algunos pasos
v1, algunos v2, producto de un cambio de esquema a mitad de una corrida
larga) filtraría igual, mezclando pasos con contratos de estado distintos
bajo el mismo `trajectory_id`.

**Decisión.** `training` exige, ADEMÁS de `source <> 'test'` y
`final_result IS NOT NULL`, que la trayectoria tenga **al menos un**
`trajectory_step` (`EXISTS`) y que TODOS sus pasos tengan
`state_schema_version = 2` (`NOT EXISTS` de cualquier otra versión). Las
dos condiciones se evalúan sobre la trayectoria completa -- nunca filtrando
`trajectory_steps` por versión dentro del `SELECT` de pasos. Una
trayectoria con un solo paso v1 se excluye ENTERA, no parcialmente: el
defecto exacto que esto evita es un filtro por-paso que dejara pasar los
pasos v2 de una trayectoria mixta y escondiera sólo los v1, produciendo una
trayectoria "recortada" que nunca existió así en el corpus real.

`all` sigue auditando v1 y v2 sin exclusión -- D44 sólo estrecha `training`.

**R4 (BLOCKER 1) -- el `EXISTS` explícito.** La versión original de D44
(R3) sólo tenía el `NOT EXISTS` de arriba. Latwan reprodujo
`ZERO_STEP_SELECTED_BY_D44 count=1`: una trayectoria `local`, con
`final_result` no nulo, y **cero** `trajectory_steps`, pasaba `training`
por VACUIDAD -- "no existe ningún paso con otra versión" es trivialmente
cierto cuando no existe ningún paso, así que el `NOT EXISTS` solo no
verificaba nada sobre esa trayectoria. `training` la aceptaba sin haber
comprobado schema alguno. El `EXISTS (SELECT 1 FROM trajectory_steps ...)`
agregado cierra ese caso: una trayectoria sin pasos nunca es "toda v2", es
indeterminada, y `training` no puede tratar lo indeterminado como
aprobado. La exclusión de trayectorias mixtas (el `NOT EXISTS`) queda
intacta -- R4 sólo agrega la condición que faltaba, no reemplaza nada.

**Consecuencia medida, hoy.** Aplicado contra el corpus real: las 12
batallas `local` (774 pasos, 100% v1) quedan TODAS fuera de `training` --
`scope=training` es hoy un corpus de **cero trayectorias elegibles**. Esto
no es una regresión ni oculta nada: es la frontera funcionando como se
diseñó sobre un corpus que, medido, no tiene todavía ninguna trayectoria
`local` en el esquema vigente. La CLI lo reporta explícito (`⚠ corpus de
entrenamiento VACÍO bajo D44`) precisamente para que un “0 violaciones” en
cada invariante no se lea como “corpus limpio” -- es “no hay nada que
auditar”, una distinción que D33 ya exigía para el caso general
(`stepsAudited === 0` sobre un dataset con filas falla ruidoso) y que acá se
extiende al caso legítimo de un scope vacío por diseño.

**Verificación.** `test/d44.test.ts` (8 tests, base Postgres descartable con
fixtures sintéticas -- la base compartida no tiene hoy ninguna trayectoria
`local`/v2, mixta, ni de cero pasos con las que probar esto contra datos
reales): local/v1 presente en `all` y ausente en `training`; local/v2
terminada presente en ambos; test/v2 ausente de `training`; local/v2 sin
terminar ausente; una trayectoria mixta v1/v2 ausente COMPLETA (incluidos
sus pasos v2, que un filtro por-paso dejaría pasar); una mezcla realista de
las cuatro categorías anteriores en un solo corpus, donde sólo la
trayectoria local/v2 terminada entra a `training`; **una trayectoria local,
finalizada, con CERO pasos, ausente de `training` (R4, el caso exacto de
`ZERO_STEP_SELECTED_BY_D44`)**; y el corpus vacío reportado como tal
(`toHaveLength(0)`, no una comparación vacua). Tres mutaciones dirigidas,
cada una roja y restaurada: retirar el predicado `NOT EXISTS` completo
(vuelve al comportamiento pre-D44, 4/8 tests rojos), reemplazarlo por un
filtro `state_schema_version = 2` dentro del `SELECT` de `steps` en vez de
sobre la trayectoria (dejaba pasar los pasos v2 de la trayectoria mixta,
exactamente el test que ese escenario existe para atrapar), y retirar SÓLO
el `EXISTS` de R4 (el test de cero pasos, y únicamente ese, se pone rojo).

`test/db.test.ts` (contra la base compartida real, sólo lectura) y
`test/cli.test.ts` (extremo a extremo) se actualizaron para reflejar el
corpus vacío real de hoy en vez de asumir `training` no vacío -- incluyendo
un test que fija a propósito que si algún día deja de ser cero hay que
revisar esa aserción, no relajarla en silencio.

**D43 R3 — 403 ambiguo y contrato de endpoint responses (MON-20, post-merge
de la integración MON-11/D44).** Dos correcciones sobre D43:

1. **403 NO es siempre credencial.** Latwan reprodujo contra 5af25c7 un 403
   model-wide con pool de 11 claves que terminó en calls=11,
   keys_quarantined=11 y `ProviderPoolExhausted`: todo 403 clasificaba
   `CredentialRejected`. Ahora: **401 → `CredentialRejected` siempre**
   (por definición es rechazo de credencial); **403 → `CredentialRejected`
   SOLO si una señal estructurada demuestra rechazo de credencial**:
   - Google: `details[].reason` en la whitelist de razones de clave
     (`API_KEY_*`, `KEY_*`, `CONSUMER_*`, `USER_*`, `MISSING_API_KEY`,
     `DOMAIN_BLOCKED`); `ACCESS_DENIED`, `PERMISSION_DENIED`,
     `SERVICE_DISABLED` y ausencia de señal quedan fuera → model-wide.
   - OpenAI-compatible: `error.code` en la whitelist (`invalid_api_key`,
     `api_key_expired`, `api_key_revoked`, `api_key_not_found`,
     `authentication_error`, `account_deactivated`, `account_disabled`);
     `insufficient_quota`, `model_not_accessible`, `access_denied` → model-wide.
   - Anthropic: 403 (`permission_error`) nunca rota (sin señal de credencial).
   El cuerpo estructurado se lee caminando la cadena de causas
   (`__cause__`): `langchain_google_genai` envuelve el `APIError` de Google
   en `ChatGoogleGenerativeAIError`, y el wrapper no conserva
   status/details — la señal vive en la causa. Sin señal estructurada, un
   403 es `FatalProviderError` y **se detiene en la primera clave** (el
   pool de 11 hace exactamente 1 llamada; canario). Nunca se decide por
   texto libre: la sanitización de claves en logs/artefactos sigue siendo
   la de siempre.

2. **`ModelRoute.endpoint` = URL COMPLETA del endpoint** (p.ej.
   `https://opencode.ai/zen/v1/responses`, tal como la publica la doc de
   Zen). El backend `_ResponsesBackend` postea con httpx EXACTAMENTE a esa
   URL y NO usa el SDK de openai (que volvería a agregar `/responses` y
   produciría `/responses/responses`). Sin `endpoint` en la ruta, se
   deriva `{base_url}/responses`. Test en la frontera HTTP: el POST va a
   `https://opencode.ai/zen/v1/responses`, nunca a `/responses/responses`.

3. **`matrix-run` fail-closed (R1).** Ejecutor versionado y probado:
   `--tier` obligatorio (free/paid), selección SOLO de filas `ready` del
   tier pedido con revalidación del tier antes del primer provider
   (mutación verificada: quitar el filtro pone rojo antes de llamar),
   refresh de `/models` antes de la ronda (`removed-from-catalog` para
   modelos que ya no están), 1 smoke → exactamente 2 batallas pinneadas
   (enforce_pin=True, sin chains, concurrency=1, persist=false,
   opponent=simple_heuristics, formato configurado, battle timeout 1800),
   artefacto atómico por modelo + estado de reanudación (un modelo
   finalizado no se repite; uno sin clasificar se reejecuta), parcial/
   abortado nunca publica winrate, `ProviderMixError` → internal-defect.
   Fase que toca open_code_zen exige `--zen-auto-reload-confirmed`
   (auto-reload desactivado); sin confirmación, no hay requests.
   L-02: la disponibilidad por provider es `min(cap_usd,
   balance_usd - leave_usd)` — el margen de saldo mínimo NO se resta dos
   veces. Con la autorización real (Zen 11/10/1 y Kimi 6/5.50/0.50) los
   límites efectivos son exactamente 10.00 y 5.50 (no 9.00/5.00).

## D45 — el respaldo de Encore rival en `_find_action_line` confirma con la repetición forzada, no con su propio anuncio (MON-21, hallazgo de MON-11 R4)

**Síntoma verificado en vivo** (`battle-gen6randombattle-3349`, capturado
durante MON-11 R4, fuera del alcance de esa rama): Wobbuffet rival (Shadow
Tag) atrapa a nuestro activo mientras lo tiene encoreado. Dos decisiones
consecutivas, forzadas cada turno al ÚNICO movimiento legal (Encore no deja
otro; el trapping saca el cambio), terminaban con el **mismo**
`turn_number` que la decisión anterior -- decisión 26/turno 23
(Mamoswine/Stealth Rock) y decisión 38/turno 35 (Cresselia/Moonblast) --
sin que `_firma_de_reemplazo_forzado` (D21, `test_play.py`) las reconociera
como reemplazo forzado legítimo, correctamente: no lo son, la máscara
persistida es un solo MOVIMIENTO, no una de puros cambios.

**Causa raíz, aislada de forma inequívoca (sin ambigüedad, ver
docs/DECISIONS.md D22/D23 para el mecanismo base).** El respaldo de Encore
del rival en `_find_action_line` (`client.py`, agregado en D23 para
`battle-gen6randombattle-558`/`-581`) devolvía el cursor **apenas
encontraba** la línea `|move|{opp}a: Caster|Encore|{side}a: Nombre`, sin
consumir la línea `|move|{side}a:...|` que Showdown narra a continuación,
en el MISMO turno: la repetición forzada en sí. Bajo Encore ordinario (sin
trapping) eso no se notaba -- la decisión siguiente casi siempre elige otra
cosa (puede cambiarse, D23 ya lo protege) y esa línea sobrante nunca
matchea a nadie. Bajo Encore + trapping, la decisión siguiente está
forzada al MISMO movimiento -- la única opción legal -- y su búsqueda
matchea esa línea sobrante como si fuera su propia resolución, heredando el
`turn_number` de la decisión ANTERIOR en vez de encontrar su propia
repetición, uno o más turnos más adelante. Verificado de forma
independiente contra las DOS capturas reales de R4 (trazado a mano línea
por línea antes de tocar código, luego confirmado con los tests):

- **Mamoswine (decisión 26/turno 23):** el anuncio de Encore aparece en el
  MISMO turno que la decisión ANTERIOR ya resolvió por un `|move|` real
  (Stealth Rock, elegida libremente entre 4 opciones) -- no hay ninguna
  repetición propia en ese turno que lo confirme (la repetición forzada
  recién ocurre, con `[still]`, un turno después). El respaldo viejo
  igual anclaba ahí.
- **Cresselia (decisión 38/turno 35):** el Encore SÍ intercepta a la
  decisión 37 dentro de su propio turno (caso legítimo de D23: eligió
  Moonlight, Encore fuerza Moonblast en su lugar) -- pero el cursor viejo
  quedaba apuntando ANTES de esa línea de Moonblast, no después,
  dejándola disponible para que la decisión 38 (forzada también a
  Moonblast por Encore+trapping) la robara.

**Arreglo, en `_find_action_line` (`client.py`).** El anuncio de Encore del
rival ya no fija un respaldo terminal: queda pendiente
(`encore_rival_turno`). Sólo se confirma -- devolviendo el cursor DESPUÉS
de la línea de repetición, no antes -- si esa línea `|move|{side}a:...|`
propia aparece en el MISMO turno; si el turno cambia sin ella (caso
Mamoswine), la búsqueda sigue de largo sin anclarse ahí, hasta encontrar la
repetición real más adelante (dentro de `ACTION_SEARCH_MARGIN_TURNS`, sin
tocar ese techo). No se relajó ni se robusteció `_firma_de_reemplazo_forzado`
(sigue exigiendo máscara TODA de switches + firma de protocolo, D21): esta
clase de decisión sigue, correctamente, sin ser un reemplazo forzado.

**Verificación.** Reproducción determinista que atraviesa el llamador real
(`LudexPlayer._correct_step_turns`, no una batalla en vivo dependiente de
que el servidor genere Wobbuffet+Shadow Tag+Encore de nuevo):
`test_correct_step_turns_encore_mas_trapping_no_hereda_el_turno_de_la_decision_anterior`
(patrón Mamoswine) y
`test_correct_step_turns_encore_intercepta_y_la_decision_siguiente_no_hereda_su_linea`
(patrón Cresselia), ambas con las líneas de protocolo reales de R4.
Contrapesos exigidos por la aceptación de MON-21, los tres verdes sin
cambios: Encore ordinario con cambio disponible
(`test_correct_step_turns_encore_ordinario_con_cambio_disponible_no_se_ve_afectado`),
trapping sin Encore
(`test_correct_step_turns_trapped_sin_encore_no_dispara_el_mecanismo_nuevo`),
y reemplazo forzado real tras debilitamiento (D21), que sigue compartiendo
turno legítimamente
(`test_correct_step_turns_reemplazo_forzado_real_tras_debilitamiento_sigue_compartiendo_turno`).
Mutación dirigida: revertir el fix en `client.py` (dejando solo los tests
nuevos) pone en rojo exactamente 4 tests -- las 2 reproducciones de arriba
más 2 tests unitarios de `_find_action_line` cuyo valor de cursor pineado
cambia con el fix -- y ninguno de los 4 contrapesos; restaurado, suite
completa de `test_client.py` verde (86/86).

**Limitación conocida, documentada, no resuelta por este arreglo:** el
resto de los hallazgos de MON-11 R4 (Issues 2-4: carrera de timing en
`client.py:1509`, ciclo de vida de `CalcClient` bajo reintentos forzados,
`floetteeternal` no resuelta como alias cosmético) permanecen fuera de
alcance -- son MON-22/23/24, issues independientes.

## D46 — `LudexPlayer` posee el ciclo de vida de sus decisiones; una barrera terminal las drena antes de cerrar `CalcClient` (MON-23)

**Síntoma verificado** (issue original): `test_respuesta_ilegal_dos_veces_juega_y_persiste_fallback`
producía, en corridas en vivo, `RuntimeError: Cannot send a request, as the
client has been closed` y `httpx.ReadError` durante `calc_damage`. Observado
en 2 de 7 corridas completas de `test_graph_play.py`.

**Causa raíz, demostrada por inspección del poke-env real instalado
(`.venv/.../poke_env/`), no por hipótesis.** `choose_move` corre como task
fire-and-forget de `PSClient.listen()` (`asyncio.create_task` por frame de
websocket, `ps_client.py:260`), agendada en `POKE_LOOP` -- el event loop
único del proceso que corre en su propio thread daemon
(`poke_env/concurrency.py`). `battle_against()` es OTRA task, agendada por
separado vía `handle_threaded_coroutines`/`run_coroutine_threadsafe` sobre
el MISMO `POKE_LOOP`: **hermana, no madre**, de la task que corre
`choose_move -> run_graph -> calc_damage`. Cuando el `asyncio.timeout(45)`
del test cancela `battle_against`, esa cancelación llega hasta la task de
`_battle_against` y se detiene ahí: la task de `listen()` que puede seguir
ejecutando `calc_damage` en ese mismo instante no es tocada. El test entra
a `finally: await calculator.aclose()` mientras esa decisión huérfana sigue
usando el mismo `CalcClient`.

Por qué no puede pasar sin el timeout externo en la MISMA batalla: el lock
por batalla de `PSClient` (`ps_client.py:171-174`) serializa todos los
mensajes de un mismo `battle_tag` -- el mensaje que resuelve el fin de la
batalla no puede procesarse hasta que la task de la decisión anterior
(que sostiene el lock durante todo `calc_damage`) libere el lock. El único
punto de fuga es la cancelación externa de la task hermana, que nunca
alcanza a la decisión.

Evidencia que descarta timeout pressure como causa primaria:
`httpx.ReadError` hereda de `NetworkError`/`TransportError`, no de
`TimeoutException` (`httpx.ReadTimeout` sí). Si la causa fuera
`CalcClient.timeout_seconds` insuficiente, la excepción esperable sería
`ReadTimeout`, no `ReadError`. Reproducido de forma determinística con las
primitivas reales de poke-env (`POKE_LOOP`, `run_coroutine_threadsafe`)
contra un servidor HTTP controlado: request en vuelo -> `httpx.ReadError`;
request enviada después de `aclose()` -> el `RuntimeError` textual exacto
del síntoma reportado.

**Dirección rechazada:** aumentar `timeout_seconds` (no ataca la causa,
ver evidencia de arriba); `asyncio.shield` de la request huérfana (deja
trabajo desperdiciado corriendo indefinidamente sin resolver el cierre);
un `httpx.AsyncClient` por decisión (cambia el perfil de performance sin
necesidad); leer/tocar `PSClient._active_tasks` (API interna de poke-env,
no expuesta, no pensada para esto).

**Arreglo:** `LudexPlayer` posee su propio registro de decisiones en
vuelo (`_decision_tasks: set[asyncio.Task]`), independiente de
`PSClient._active_tasks`. `_admit_decision()` es la primera línea
síncrona del cuerpo de la coroutine que `choose_move` devuelve
(`run_random`/`run_graph`, vía `asyncio.current_task()`): si
`_decisions_closed` ya es `True`, levanta `DecisionsClosedError` antes de
tocar proyección/grafo/calc -- nunca hay una decisión tardía que alcance
calc. `drain_inflight_decisions()` es la barrera pública, terminal e
idempotente: agenda `_drain_on_poke_loop()` en `ps_client.loop` vía
`run_coroutine_threadsafe` (cross-loop-safe, mismo patrón que
`_background_failure`/`wait_for_background_failure` ya existente), que
cierra la admisión y captura el conjunto de tasks en vuelo como UN bloque
síncrono sin `await` entre medio (atómico respecto de `_admit_decision`:
en un loop de un solo hilo, dos bloques síncronos nunca se intercalan),
cancela cada task y espera su finalización con
`asyncio.gather(..., return_exceptions=True)` ANTES de retornar. Los
callers que comparten `CalcClient`/`ContextRepository` con el grafo
(`cli.py::_benchmark_command`, el test en vivo de `test_graph_play.py`)
invocan la barrera en su `finally`, antes de `calculator.aclose()` y de
cerrar el context repository.

**Evidencia de regresión (mutación dirigida en
`apps/agent/tests/showdown/test_client.py` y `test_cli.py`):** quitar
`task.cancel()` en `_drain_on_poke_loop` pone en rojo las dos
reproducciones de topología real (una por timing, otra porque la
decisión huérfana termina con `CalcProtocolError` en vez de cancelación
limpia); quitar el `await asyncio.gather(...)` pone en rojo la
reproducción de la carrera real de `CalcClient` (la barrera retorna antes
de que la decisión termine); permitir admisión tardía (saltear el chequeo
de `_decisions_closed`) pone en rojo el test de admisión tardía
(`ProjectionTimeoutError` en vez de `DecisionsClosedError`, prueba que la
decisión intentó avanzar); reordenar `aclose()` antes de la barrera en
`_benchmark_command` pone en rojo el test de orden del caller productivo.
Las cuatro mutaciones se revirtieron después de confirmar rojo; suite
focal completa verde (277 passed, 37 skipped) tras cada reversión.

**Gate en vivo:** `test_respuesta_ilegal_dos_veces_juega_y_persiste_fallback`
corrido dos veces contra Postgres/Showdown/calc reales. Una corrida
efectivamente excedió los 45s (falla pre-existente y fuera de alcance de
MON-23: la duración de la batalla bajo `AlwaysIllegalProvider`, no el
ciclo de vida de `CalcClient`) pero el log mostró `CancelledError
intercepted` -- la decisión huérfana se canceló limpio, sin
`RuntimeError`/`ReadError` -- confirmando el arreglo bajo la condición
real que dispara el defecto. La segunda corrida completó normal en 37.8s,
verde.

**Limitación conocida, no resuelta por este arreglo:** la duración
variable de la batalla bajo `AlwaysIllegalProvider` (a veces excede los
45s del test) es una falla pre-existente, independiente del ciclo de vida
de `CalcClient`, y queda fuera de alcance -- aumentar ese timeout externo
no fue autorizado y no habría atacado la causa raíz de MON-23. El mismo
patrón estructural (`asyncio.timeout` envolviendo `battle_against`/
`run_benchmark` con `aclose()` en el `finally`) existe también en
`cli.py::play()` (`BATTLE_TIMEOUT_SECONDS=180`, vía
`_battle_against_or_failure`), que no construye `CalcClient` y por lo
tanto no está expuesto a este defecto -- no requirió cambios.

## D47 — `isNonstandard:"Unobtainable"` referenciado por el catálogo random-battle de la misma generación es battle-legal (MON-24, supersede el caso Floette-Eterna de D12/D32)

**Síntoma verificado.** Una batalla `gen6randombattle` con Floette-Eterna
podía fallar en `context_repository.py:329` con `LookupError`: "especie
visible no seedeada ni forma cosmética: `floetteeternal`" (MON-11 R4, 2 de
7 corridas en vivo).

**Causa raíz, verificada contra las dos fuentes pineadas, no inferida.**
`isNonstandard:"Unobtainable"` describe obtenibilidad real-world (una
distribución de evento ya terminada), no legalidad de batalla del
simulador. Inspeccionando `data/random-battles/gen6/sets.json` del paquete
`pokemon-showdown@0.11.10` pineado (`packages/seed/node_modules`, el mismo
`dist/` que corre en el contenedor `showdown`, tag `v0.11.10`):

- `floetteeternal` es la **única** clave de la línea Floette en ese
  catálogo: no existe una entrada `"floette"` (base) en absoluto. La única
  forma en que la línea de Floette aparece en `gen6randombattle` es como
  Floette-Eterna.
- Su set trae `movepool` con `lightofruin`, su movimiento de firma.
- Floette-Eterna NO es cosmética: `baseStats` distintos de la base
  (BST 551 contra 371) y una sola ability (`Flower Veil`, sin la oculta
  `Symbiosis` de la base). D32 tenía razón en rechazarla como alias
  cosmético; el problema nunca fue el criterio de `cosmeticFormes`, fue que
  la especie no tenía NINGUNA fila en `pokemon`.
- Barrido completo de las 483 especies del catálogo de gen 6 contra
  `isNonstandard`: `floetteeternal` es la única marcada `Unobtainable`. De
  los 273 movimientos únicos referenciados en los movepools, `lightofruin`
  es el único `Unobtainable`. `thousandarrows`/`thousandwaves` comparten el
  mismo `isNonstandard` que `lightofruin` pero **ningún** set de gen 6 los
  usa; `paleowave`/`shadowstrike` son `CAP`, no `Unobtainable`.

**Arreglo.** `packages/seed/src/extract/dex.ts::isAvailableForExtraction`
agrega una excepción tipada y declarativa sobre `isAvailable` (que NO
cambió): admite una entrada `isNonstandard:"Unobtainable"` sólo si las
CUATRO condiciones se cumplen a la vez -- `entry.gen <= dex.gen`,
`isNonstandard === "Unobtainable"` exacto, el id está en el catálogo
random-battle estándar de esa MISMA generación, y el tipo de entidad
coincide (`species`/`move`, conjuntos independientes: un movimiento nunca
entra por aparecer como clave de especie del catálogo, ni al revés).
`packages/seed/src/extract/random-battle-catalog.ts` carga ese catálogo
desde el paquete pineado (nunca cwd, vía
`require.resolve("pokemon-showdown/package.json")`), soporta el shape
`sets.json` (gen 2-7, 9: `{especie: {sets: [{movepool}]}}`) y el shape más
viejo `data.json` (gen 1, 8: `{especie: {...listas de movimientos
reconocidas}}` -- la unión validada de TODAS las listas presentes en la
entrada, nunca sólo `moves`; ver la corrección L-01 más abajo), y falla
ruidosamente ante archivo ausente o shape no reconocido -- nunca amplía
disponibilidad por defecto. Aplicado en `extractSpecies`, `extractMoves`, y
los dos conjuntos (`species`/`legalMoveIds`) que usa `extractLearnsets`.
`extractItems`/`extractAbilities` (`simple.ts`) conservan la frontera
`isAvailable` sin cambios: no hay evidencia de que el mismo patrón les
aplique.

**Corrección L-01 (LINEAR_VERDICT CHANGES_REQUESTED sobre `76c630a`).** La
primera versión de `movesFromDataShape` asumía que el shape `data.json`
era únicamente `{especie: {moves: string[]}}`. Es falso: gen 1 tiene
además `comboMoves`/`essentialMoves`/`exclusiveMoves`, y gen 8 tiene
`doublesMoves`/`noDynamaxMoves` -- y 12 entradas Gmax de gen 8
(`venusaurgmax` entre ellas) **no tienen `moves` en absoluto**, sólo
`doublesMoves`, así que `loadRandomBattleCatalog(8)` reventaba
directamente. Medido contra el paquete real: gen 1 sólo-`moves` = 47,
unión completa = 69; gen 8 sólo-`moves` = 294, unión completa = 351.
`movesFromDataShape` ahora une todas las listas reconocidas presentes
(`moves`, `comboMoves`, `essentialMoves`, `exclusiveMoves`,
`doublesMoves`, `noDynamaxMoves` -- la unión completa entre generaciones,
sin ramificar por generación en el flujo productivo) y exige al menos una;
un campo que no es ni lista reconocida ni escalar conocido (`level`,
`doublesLevel`), o una lista reconocida con tipo inválido, sigue fallando
ruidoso. `parseCatalogData` quedó exportado como función pura para poder
probar shapes sintéticos inválidos sin tocar el filesystem. Mutación
dirigida: restaurar el comportamiento previo (`new Set(entry.moves)`, sin
unión ni validación de campos) pone en rojo la colección completa de
`random-battle-catalog.test.ts` (revienta al cargar `loadRandomBattleCatalog(8)`
por `venusaurgmax` sin `moves`); revertida, suite de extracción completa
verde (113/113).

**No se tocó `context_repository.py` ni la lógica de D32.** La resolución
cosmética (`cosmeticFormes` explícito) sigue exactamente igual; lo único
que cambió es que ahora existe una fila directa para `floetteeternal` en
`pokemon`, así que el camino de "fila directa siempre gana" (ya existente)
la resuelve sola.

**Evidencia de regresión, mutación dirigida
(`packages/seed/test/extract/dex.test.ts`,`species.test.ts`,`moves.test.ts`,
`learnsets.test.ts`,`random-battle-catalog.test.ts`):** quitar la excepción
por completo pone en rojo 10 tests (conteos y casos positivos); admitir
todo `Unobtainable` sin mirar el catálogo pone en rojo la distribución de
`power_kind` y el contrapeso de `thousandarrows`/`thousandwaves` (mismo
`isNonstandard` que `lightofruin`, pero no referenciados); cruzar las
allowlists de especies/movimientos pone en rojo 11 tests; omitir
`lightofruin` del conjunto legal de `extractLearnsets` pone en rojo el
canario de las 55 filas de herencia de Floette-Eterna; usar el catálogo de
otra generación (gen 9) pone en rojo los mismos 10 tests que quitar la
excepción, porque gen 9 no referencia a Floette. Las cinco mutaciones se
revirtieron después de confirmar rojo; suite de extracción completa verde
(95/95) tras cada reversión, sin marcadores de mutación residuales
(`git diff` limpio).

**Conteos verificados por SQL contra una base descartable** (creada,
verificada por `current_database()`, sembrada y eliminada; la base
compartida nunca se tocó -- `SELECT count(*) FROM pokemon WHERE gen_id=1`
contra `ludex` siguió dando 834 después de esta ronda): gen 6 pasa de
834/618/62198 a **835/619/62253** (pokemon/moves/learnsets); gen 9 no
cambia (874/685/65642) porque su catálogo random-battle no referencia a
Floette. `floetteeternal` aporta exactamente 55 filas de learnset,
incluida `lightofruin`. `ContextRepository.load_battle_context` contra esa
misma base resuelve Floette-Eterna con sus stats/ability reales (no los de
`floette`) y `lightofruin` en su learnset; bajo gen 9 sigue fallando
ruidoso (`LookupError`), igual que `pikachupartner`/`pikachuworld`/
`charizardgmax` bajo gen 6 -- ninguno de esos canarios de D32 se movió.

**Documentación actualizada.** D12: tabla de gen 6 y nota de los 618
movimientos, con la nota histórica preservada y marcada como estado previo
a este arreglo. D32: el canario `floetteeternal` se retira de la lista de
`LookupError` esperados con una nota explícita de por qué (D47), sin tocar
el criterio de `cosmeticFormes` ni los demás canarios.
`.claude/showdown-data/SKILL.md` documenta la excepción tipada. D33
(dataset-audit) no se tocó: audita filas ya grabadas del corpus histórico,
una pregunta distinta de si el simulador puede generar la especie hoy, y
`floetteeternal` sigue siendo -correctamente- el canario de esa frontera
para los datos ya existentes.

**Limitación conocida y explícita.** Este arreglo cambia el código de
extracción y lo verifica contra una base descartable; **no reseedea la
base compartida `ludex`**.
`test_floetteeternal_no_es_cosmetica_pero_tiene_fila_directa_por_d47`
(`apps/agent/tests/db/test_context_repository.py`) va a fallar contra la
base compartida hasta que alguien corra `pnpm seed --gen 6` ahí -- es la
señal correcta de que falta ese paso operativo, no una regresión de este
cambio. No se investigó si el mismo
patrón (`Unobtainable` referenciado por un catálogo random-battle)
aplica a `items`/`abilities` en alguna generación; `simple.ts` queda sin
tocar por falta de evidencia (alcance explícito del DESIGN VERDICT).

## D48 — LATWAN R1A + OFFLINE REVIEW (MON-20): métricas reales en fallos de smoke, 401 por señal estructurada, evidencia durable sanitizada y adaptación declarativa `text_json` de las tres rutas libres

> Numeración: D46 pertenece a MON-23 y D47 a MON-24 (ambas arriba).
> La decisión de MON-20 es D48.

**Contexto.** La revisión R1A de Latwan sobre la evidencia R1A (10 artefactos
+ state file) encontró tres blockers internos: (1) `_smoke_failed` fijaba
`retries=0/rotations=0/quarantined=0`, por eso el artefacto de
`north-mini-code-free` decía `quarantined=0` mientras el checkpoint afirmaba
cuarentena + `ProviderPoolExhausted`, y el de `ling-3.0-tiny-free` no podía
demostrar los retries del 503; (2) `_classified` convertía todo HTTP 401 en
`CredentialRejected`, pero R1A demostró que Zen puede devolver 401 originado
en el provider upstream de un modelo mientras la misma credencial funciona en
modelos vecinos (cuarentenarlo es incorrecto y convierte indisponibilidad de
modelo en `ProviderPoolExhausted`); (3) la evidencia durable persistida
(`failure_type`/`failure_cause_type`) no alcanzaba: faltaba etapa, status HTTP
y código estructurado. Además autorizó offline la adaptación declarativa de
los tres 400 (`big-pickle`, `deepseek-v4-flash-free`, `laguna-s-2.1-free`).
La revisión OFFLINE posterior (LATWAN) corrigió dos fugas de la primera
implementación: `provider_error_code` podía filtrar datos (que el valor
provenga de un campo estructurado no lo vuelve seguro) y el delta de métricas
restaba percentiles (matemáticamente inválido).

**Decisiones:**

1. **Métricas reales en fallos de smoke (L-01/L-06).** `KeyRotatingProvider`
   y `ProviderChain` exponen `metrics_snapshot()`; `_run_one` toma un
   snapshot ANTES del smoke y persiste el DELTA en el artefacto, tanto en
   éxito como en fallo. `_smoke_failed` jamas vuelve a fijar
   `retries/rotations/quarantined` en cero por omisión. Nuevo contador
   `transient_retries_executed`: un retry REAL ejecutado por reintento de
   infraestructura (el dedupe `turns_transient_affected` solo dice si el
   turno se vio afectado, no cuántas veces se reintentó). El `retries` del
   artefacto sale de ese contador. Canarios: North (401 credential →
   quarantined=1) y Ling (503 ×3 → retries=2).
   **Delta matemáticamente válido (L-06):** `max/p50/p95` NO se restan
   nunca. El delta de latencias exige un provider/`DecisionMetrics` fresco
   por modelo; si el snapshot inicial ya tiene muestras de latencia
   (`completion_latency_ms_count`/`decision_latency_ms_count` > 0) el delta
   falla cerrado (`None`): no se publican percentiles inventados ni se
   devuelven gauges comparables falsos. Contadores, tokens, total y count se
   calculan por diferencia; `max/p50/p95` se copian del snapshot posterior
   (válido únicamente porque el baseline fresco tiene count=0, así que la
   población posterior ES la única).

2. **401 por señal estructurada (L-02).** Un 401 solo es
   `CredentialRejected` (cuarentena) cuando una señal ESTRUCTURADA
   allowlisted demuestra rechazo de NUESTRA credencial (`error.code` de
   openai/anthropic en `_OPENAI_CREDENTIAL_CODES`, `error.details[].reason`
   de google en `_GOOGLE_CREDENTIAL_REASONS`). 401 sin esa señal o marcado
   como upstream/model-wide → `FatalProviderError` en la primera clave, sin
   rotación ni cuarentena. Nunca se decide por texto libre. Se reemplazó el
   test que esperaba `ProviderPoolExhausted` para un 401 sin señal y se
   agregaron counterweights (`invalid_api_key` estructurado vs. upstream).
   El `ProviderPoolExhausted` por pool totalmente en cuarentena ahora se
   lanza `from` el último rechazo de credencial, conservando la causa en
   vivo.

3. **Evidencia durable sanitizada (L-03/L-05).** `MatrixModelResult` agrega
   `failure_stage` (`smoke`/`battle`), `http_status` (cuando existe) y
   `provider_error_code`; `BenchmarkResult` agrega SOLO `http_status` y
   `provider_error_code` (que la alimenta en la fase de batalla; el
   `failure_stage` de la fase de batalla lo fija la matriz, no el
   BenchmarkResult). `provider_error_code` se acepta únicamente como
   identificador acotado `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` y SOLO desde
   campos estructurados permitidos (`error.code`, `error.details[].reason`):
   que el valor provenga de un campo estructurado no lo vuelve seguro. URL,
   slash, whitespace, query, valores largos, patrones de secreto
   (`sk-…`/`AIza…`/`key=…`) o nombres de variables de entorno del proyecto →
   `None` completo, nunca truncado. Prohibido persistir mensajes, URLs,
   headers, bodies completos, nombres/valores de variables o secretos
   (canarios con URL+clave en el campo estructurado y en el mensaje crudo).
   Los campos tienen default `None` para que los state files viejos sigan
   cargando.

4. **Adaptación declarativa `text_json` (autorizada offline).** Las tres
   rutas libres de 400 permanecen en `chat_completions` y cambian SOLO su
   capacidad declarativa `structured_output` a `text_json`: el backend NO
   construye `response_format=json_schema` (nunca llama a
   `with_structured_output`), agrega la instrucción JSON textual y el
   payload sigue pasando por la MISMA validación estricta de `DecisionResponse`
   (D26). Sin `messages`, sin condicionales por `model_id` (D43: protocolo
   por ruta). Mutar cualquiera de las tres de vuelta a `json_schema` pone
   rojo su canario.

**Verificación.** TDD rojo→verde en `test_provider.py` y `test_matrix.py`:
**16 rojos** (medidos en la última corrida roja legítima, antes de la
implementación; el primer conteo de 19 incluía 3 fallos por un error del
propio helper de test, no del código bajo prueba) → 101 verdes en la suite
focal; 633 verdes + 68 skipped en `apps/agent` completo, los skipped son
integración con DB/Showdown — ronda offline. Mutaciones dirigidas
verificadas: revertir los zeros fijos de `_smoke_failed` (rojo North + 503),
revertir el 401 incondicional (rojo 401-fatal), extraer `provider_error_code`
de texto libre (rojo sanitización), revertir cada ruta a `json_schema` (rojo
canario de rutas), quitar la validación de identificador acotado de
`_structured_provider_error_code` (rojo canarios L-05) y restar todos los
ints del delta incluyendo percentiles (rojo canario L-06). Secret scan
compartido (L-03 previo) sigue en verde sobre `evals/**`.

**Límites.** La fase de batalla obtiene `http_status`/`provider_error_code`
vía `BenchmarkResult`; un `BenchmarkResult` construido por otros llamadores
sin esos campos los deja en `None`. Los artefactos R1A ya commiteados no se
regeneran (son evidencia histórica aceptada); los campos nuevos aparecen en
las corridas siguientes. `ProviderPoolExhausted` por cooldown de cuota sigue
sin causa enganchada (no es rechazo de credencial). El delta de latencias
solo es válido con un `DecisionMetrics` fresco por modelo (la matriz lo
garantiza al construir un provider nuevo por fila); un baseline sucio queda
`None` (fail-closed).

**Addendum post-R1B (LATWAN DESIGN VERDICT) — corrección offline implementada:**

5. **Lifecycle completo del benchmark (L-01).** `_benchmark_command` ahora
   cierra AMBOS PSClient (agent y rival) via la API real
   `ps_client.stop_listening()` (async, cross-loop sobre el POKE_LOOP de
   poke-env; `Player` no ofrece `close()`), después del drain D46 y ANTES de
   cerrar calc/repository/engine. El cierre intenta ambos players aunque uno
   falle y nunca oculta la excepción primaria ni impide el cierre de los
   recursos. Motivo: cada benchmark abortado fugaba 2 websockets + sus
   listener tasks en el POKE_LOOP global, acumulándose entre modelos de una
   misma ronda de la matriz. Canarios: orden drain → ambos players → recursos;
   un solo `stop_listening` por player; fallo de un player no impide el resto;
   reproducción con el POKE_LOOP real (websockets CLOSED + listeners done).

6. **Separación entre fallo de infraestructura y compatibilidad del modelo
   (L-03).** `ConnectionClosedError` de Showdown durante batalla y la
   indisponibilidad local comprobada (`ShowdownUnavailableError`, un
   `RuntimeError from OSError` del preflight `_check_showdown_reachable`)
   se clasifican `externally-limited` con stage=battle, nunca
   `internal-defect` ni incompatibilidad del modelo. Clase/causa preservadas
   sanitizadas; sin retry automático ni ajuste de ping.

7. **Preservación de fase post-smoke (L-02).** Todo fallo después de un
   smoke verde conserva `smoke_ok=True`, `failure_stage="battle"`,
   `battles_requested=2` (objetivo, no batallas iniciadas — finding #4 del
   TECH LEAD), `battles_completed=0`, las métricas del smoke disponibles
   (delta del provider) y la clase/causa sanitizadas. Una excepción interna
   genuina sigue siendo `internal-defect` pero con la fase preservada; el
   `except Exception` exterior de `run_matrix_round` queda solo para fallos
   pre-smoke.

8. **Clasificación 401/403 model-wide (L-04).** `FatalProviderError` con
   HTTP 401/403 upstream/model-wide (sin señal key-specific; el provider ya
   no rota ni cuarentena) → `credential/model unavailable`;
   `unsupported-protocol` queda reservado para rechazo de
   protocolo/structured output (HTTP 400 de response_format). Solo por
   status HTTP estructurado, nunca texto libre.

9. **not-run hermético (L-05).** La selección/validación local de
   credenciales precede al chequeo de Showdown en `_benchmark_command`: sin
   credenciales el comando emite artefacto `not-run`
   (`failure_type=ProviderSelectionError`, exit 2) sin tocar Showdown. El
   chequeo de Showdown no se debilita cuando las credenciales existen.

10. **Presupuesto R1C (L-06).** El manifiesto unitario no ejecutado corrige
    su `cumulative_cost_usd` stale (0.66056 → 0.45976 = smoke 0.00616 + 2
    batallas 0.4536, hard cap de ronda 0.60); `plan_budget` falla cerrado
    ante una reserva que exceda el cap. R1C sigue NO AUTORIZADA; no existe
    artefacto de resultado.

**Addendum post-corrección LATWAN (OFFLINE, 2026-08-12) — blockers L-01 y L-02:**

11. **Frontera estructurada del cleanup (L-01).** El `finally` lineal de
    `_benchmark_command` (cli.py) se reemplaza por `_structured_cleanup`:
    intenta SIEMPRE y EN ORDEN drain (D46) → PSClient del agent → PSClient
    del rival → CalcClient → context repository → engine, aunque cualquier
    tramo falle; los errores de los pasos se recogen y devuelven, jamas se
    tragan. Reglas de salida: (a) con excepción primaria en vuelo se
    preserva — `CancelledError`/`KeyboardInterrupt`/`SystemExit` se
    re-lanzan tal cual, el resto viaja como `BenchmarkFailure` con su causa
    original en `__cause__`; (b) sin primaria, un fallo de cleanup NO puede
    dejar la corrida `compatible`: se emite un resultado
    `failure_type=InternalCleanupError` (marcador clasificado, mensaje
    sanitizado fijo, clase de la causa real del primer paso como
    `failure_cause_type`) que la matriz traduce a `internal-defect`; el
    progreso real y la identidad efectiva se preservan. `CancelledError`
    nunca se traga como fallo ordinario. `engine.dispose()` queda observado
    explícitamente en los canarios de orden (antes las listas terminaban en
    `context_aclose`). Motivo: un fallo de drain o Calc podía ocultar la
    primaria, abandonar los tramos posteriores y (vía el `BaseException`
    tragado en `_close_player_sockets`) permitir reportar `compatible` con
    websockets vivos.

12. **Resultado parcial tipado post-smoke (L-02).** `_matrix._battle_failed`
    ya no fija `battles_completed=0`, `effective_provider=None` ni
    `effective_model=None` para todo fallo: la frontera real de
    `_benchmark_command` envuelve toda excepción no clasificada de
    `run_benchmark` (p.ej. `ConnectionClosedError` en la batalla 2) en
    `BenchmarkFailure` (benchmark.py) con un `BenchmarkResult` PARCIAL —
    campo tipado, no atributos ad hoc sobre excepciones genéricas — con
    `requested`/`completed`/W/L/T reales desde los contadores del agente,
    provider/model efectivos (el pin), y evidencia sanitizada
    (`failure_type`/`failure_cause_type`/`http_status`/
    `provider_error_code`). `_battle_failed` lee el partial cuando existe y
    cae al pin de la fila cuando no (preflight de Showdown antes de crear
    players: completed=0, identidad del smoke preservada). `MatrixModelResult`
    agrega `battles_wins`/`battles_losses`/`battles_ties` (aditivo; los
    artefactos históricos no se regeneran). Canario vinculante: batalla 1
    completa, batalla 2 lanza `ConnectionClosedError` → requested=2,
    completed=1, identidad = pin, stage=battle, externally-limited,
    winrate=None; counterweight preflight → completed=0 con identidad
    preservada. Mutaciones demostradas: completed=0, borrar
    effective_provider/model, perder W/L/T o métricas parciales ponen los
    canarios rojos.

## D51 — MON-20 DIAG-A R2/R3 (review Tasos/Latwan): monitor diagnóstico opt-in del benchmark (`--diagnostic-snapshot-interval`)

La ronda 1 (c143e0b) introdujo `LudexPlayer.decision_snapshot()` (etapas +
stacks sanitizados de decisiones en vuelo), pero su único caller era el test:
`_benchmark_command()` espera completamente a `run_benchmark()`, así que
durante un cuelgue no existía un monitor vivo que capturara la etapa. La
revisión (CHANGES_REQUESTED) pidió el wiring productivo mínimo, explícito y
opt-in. Decisiones de esta ronda (commit `c143e0b` + ronda 2):

1. **Ownership del `POKE_LOOP`.** `poke_env.concurrency.POKE_LOOP` es un
   loop global en su propio thread. Las decisiones (`choose_move` →
   `run_graph`) corren ahí como tasks hermanas de `battle_against`
   (`ps_client.py:260`, `asyncio.create_task` por frame): cancelar o
   timeoutear el wrapper del benchmark nunca las toca (D46) y
   `asyncio.all_tasks()` desde el loop del caller NO las ve. Todo lo que
   inspeccione una decisión debe agendarse en `ps_client.loop` vía
   `run_coroutine_threadsafe`, que es lo que hace `decision_snapshot()`.

2. **Wrapper-task vs node-task.** `run_graph` es la wrapper-task registrada
   en `_decision_tasks`. LangGraph ejecuta cada nodo en una task PROPIA
   (`_executor` de langgraph, `run_coroutine_threadsafe` + `copy_context`):
   `asyncio.current_task()` dentro de un nodo es la node-task, y es ella la
   que tiene el chain de awaits profundo (`cr_await`/`ag_await`) con la
   línea exacta esperada. Por eso `record_decision_stage` registra la etapa
   bajo la NODE-task y `_release_decision` barre los stages de tasks done.
   `Task.get_stack()` no alcanza (solo el frame del coroutine raíz): el
   chain se camina con `cr_frame`/`cr_await` (+ `ag_frame`/`ag_await`,
   LangGraph `astream` es un async generator).

3. **Campos permitidos del snapshot.** Por entrada: `task_id` (`id()` de la
   task), `stage`, `frames` = `[{module, function, line}]`. PROHIBIDO
   `f_locals` — una variable local de un frame del proveedor puede contener
   una clave API, y los frames de contexto traen filas y payloads. Nunca
   prompts, credenciales, respuestas de provider, filas, payloads de
   batalla ni secretos. El monitor emite `LUDEX_SNAPSHOT <json>` por tick.

4. **Flag, intervalo y canal de salida efectivo (R3).**
   `benchmark --diagnostic-snapshot-interval N` (opt-in; default ausente →
   el benchmark no crea monitor y su salida, paths y semántica no cambian).
   Con `N > 0`, `_benchmark_command` agenda desde el caller loop un monitor
   que cada `N` segundos emite `agent.decision_snapshot()`; `snapshot_emit`
   es inyectable para tests y un intervalo no positivo es
   `typer.BadParameter`. El emisor por defecto es `typer.echo(..., err=True)`
   con una línea exacta `LUDEX_SNAPSHOT <json>` por tick en STDERR
   (`grep LUDEX_SNAPSHOT 2>&1`). NO usa `logger.info`: root y
   `ludex_agent.cli` tienen nivel efectivo WARNING y un canal de log
   silencioso haría el diagnóstico invisible (blocker confirmado de la
   ronda 3). El canal solo existe cuando el flag opt-in crea el monitor; la
   ejecución normal no escribe nada nuevo.

5. **Cleanup y lifecycle del monitor.** El monitor se cancela y se espera
   (`cancel()` + `gather(return_exceptions=True)`) en un `finally` interno
   que cubre éxito, fallo (incluida la primaria no clasificada que propaga
   al `except BaseException` existente) y cancelación externa, ANTES del
   cleanup estructurado (drain → players → calc → contexto → engine, D46/
   L-01). El monitor no deja tasks ni reemplaza la primaria: un fallo de
   observabilidad (captura o emisión) se registra con `logger.warning` y NO
   altera la semántica del benchmark — canario con `emit` que lanza en la
   primera llamada: mismo `failure_type` (`BenchmarkDeadlineExceeded`).

6. **DIAG-A no es un fix.** Ni la ronda 1, la 2 ni la 3 tocan el
   comportamiento de decisión: no agregan timeouts, no cancelan nada. El cuelgue de R1C
   (ausencia de progreso ~6 min antes del timeout de batalla de 1800 s)
   sigue pendiente de localización con el monitor en vivo; los únicos awaits
   sin deadline del camino de decisión son los SQL (model_repository vía
   `resolve_provider`, context_repository vía `retrieve_context`) y la
   persistencia post-batalla.

7. **Stop condition de R1C.** Durante el diagnóstico en vivo: si una
   decisión permanece en la MISMA etapa más de `decision_budget_seconds`
   (240 s) o la batalla muestra cero decisiones completadas tras
   2×`decision_budget_seconds`+60 s, detener la corrida SIN reintentar el
   modelo ni rotar claves, clasificar con el snapshot como evidencia y pedir
   veredicto a Latwan. R1C sigue NO AUTORIZADA hasta nuevo veredicto.
## D49 — `_check_showdown_reachable` endurecido a handshake de protocolo real; el defecto de R1C no era `seasons` (MON-25, LATWAN DESIGN VERDICT sobre ROOT-CAUSE CHECKPOINT)

**Contexto.** MON-25 diagnosticó el bloqueo de ~16 min de MON-20/R1C
atribuido en los logs a crash-loops del plugin `seasons`. El ROOT-CAUSE
CHECKPOINT (Linear, 2026-08-13) probó, con reproducción local y timestamps
sanitizados, que **no hubo crash-loop**: `RestartCount=0`, ningún worker se
reinició nunca, `seasons` falla open (`getLadderTop` captura el `HttpError`
y devuelve `null`; la escalada a lockdown es código muerto en
`crashlogger.ts` de la 0.11.10 pineada), y los ~16 min de R1C ocurrieron
enteramente en reintentos del gateway del proveedor -- `matrix.py` nunca
alcanza `run_battles` sin un smoke verde, así que Showdown nunca llegó a
abrir un websocket en esa ronda. LATWAN aceptó el diagnóstico y autorizó
un único defecto real, no causal de R1C pero genuino: `_check_showdown_reachable`
(`apps/agent/src/ludex_agent/cli.py`) era un `asyncio.open_connection` +
`close()` desnudo -- daba preflight VERDE contra cualquier listener TCP que
aceptara la conexión, hablara o no el protocolo de Showdown.

**Arreglo.** `_check_showdown_reachable` abre un websocket real contra
`ws_url` (`websockets.connect(..., open_timeout=3.0)`) y espera, dentro de
ese mismo presupuesto de 3.0s, un frame que contenga la línea
`|challstr|` -- el mensaje que `poke_env.ps_client` (la librería que ya usa
Ludex para jugar) trata como "confirma conexión al server: podemos
loguear" (`ps_client.py:177-179`). No se subió ningún timeout: el nuevo
chequeo mantiene el mismo techo de 3.0s del viejo TCP-connect, sólo exige
más contenido dentro de ese techo. `ShowdownUnavailableError(RuntimeError)`
se agregó a `benchmark.py` (antes sólo declarada por adelantado en un
comentario de `matrix.py` en la rama `nebula/mon-20-provider-matrix`, leída
vía `git show` sin tocar esa rama) y se levanta `from` la causa real
(`OSError`/`TimeoutError` del handshake mudo, o `websockets.exceptions.WebSocketException`
del rechazo de protocolo), preservada en vivo.

**El frame real se inspeccionó antes de escribir el fix, no de memoria.**
Contra el Showdown local pineado: el primer frame trae `|updateuser|` +
`|customgroups|` + `|formats|` concatenados con `\n` (7618 bytes); el
segundo frame, separado, es exactamente `|challstr|4|<hex>` (268 bytes).
El chequeo revisa cada línea de cada frame recibido, no asume que
`challstr` llega en un frame dedicado.

**Hallazgo de plataforma, no de este código (documentado para quien lo
retome).** `asyncio.Server.wait_closed()` cambió de semántica en Python
3.12: además de esperar al cierre del socket de escucha, espera a que
*todas* las conexiones activas terminen. Un handler de test que nunca
retorna (`await asyncio.sleep(3600)`, para simular un listener mudo)
cuelga `wait_closed()` para siempre si se lo espera. Los tests de listener
mudo en `test_cli.py` hacen sólo `server.close()`, sin `await
wait_closed()`, con el porqué documentado inline.

**TDD rojo→verde, ejecutado (no narrado).**

- *Canario 1* (`test_check_showdown_reachable_falla_contra_listener_tcp_mudo`):
  listener que acepta y nunca completa el handshake HTTP. Rojo contra el
  código viejo (`DID NOT RAISE ShowdownUnavailableError`); verde contra el
  nuevo (`ShowdownUnavailableError` con `__cause__` `TimeoutError`, subclase
  de `OSError`).
- *Canario 2* (`test_check_showdown_reachable_falla_contra_endpoint_http_invalido`):
  servidor que responde HTTP 200 real pero rechaza el upgrade a websocket.
  Rojo contra el viejo; verde contra el nuevo (`__cause__`
  `websockets.exceptions.InvalidStatus`, subclase de `WebSocketException`).
- *Counterweight* (`tests/integration/test_showdown_reachable.py`, sin DB,
  gateado en runtime a si el Showdown local real está arriba): pasa sin
  excepción contra el Showdown pinneado real.
- *Mutación dirigida*: `git stash` acotado a `cli.py` (restaura exactamente
  la implementación TCP-connect vieja, dejando benchmark.py/tests
  intactos), corrida de los dos canarios -> **rojo** (`DID NOT RAISE`,
  confirmado en los dos), `git stash pop` restaura el fix, canarios vuelven
  a verde. Suite completa `test_cli.py`: 32/32 verdes, sin regresiones.
- *Clasificación del caller*: verificado (sin tocar la rama de MON-20) que
  la lógica real de `_battle_infrastructure_status`
  (`nebula/mon-20-provider-matrix:apps/agent/src/ludex_agent/matrix.py`,
  leída vía `git show`) sigue clasificando la excepción real levantada por
  el nuevo `_check_showdown_reachable` como `externally-limited` --
  `isinstance(cause, (ConnectionClosedError, ShowdownUnavailableError))` da
  `True` contra la instancia real capturada en un probe contra un listener
  mudo.

**No se tocó.** `seasons` no se modificó, desactivó ni reconfiguró (no es
causal de R1C, D-ROOT-CAUSE CHECKPOINT §2/§4). `exports.routes` en
`docker/showdown/config.js` no se tocó (el ruido histórico de `CRASH` no
fue causal; higiene de logs queda fuera de este issue). `ping_interval`/
`ping_timeout` de poke-env no se tocaron. La rama/worktree de MON-20 no se
modificó -- todo lo que se necesitó de ella se leyó vía `git show`.

**Limitación conocida.** El counterweight de integración depende de que el
Showdown local real esté arriba (`docker compose --profile local up -d
showdown`); se salta en runtime si no lo está, en vez de fallar la suite.
No se corrió `matrix-run` real de MON-20 con el preflight nuevo (exigiría
un request pago, prohibido en esta ronda) -- la prueba de que la
clasificación se preserva es contra la lógica real leída, no contra una
ejecución end-to-end de la matriz.
