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
| pokemon     |   834 |   874 |
| moves       |   618 |   685 |
| items       |   283 |   248 |
| abilities   |   191 |   310 |
| type_chart  |   324 |   361 |
| learnsets   | 62198 | 65624 |

**Nota: por qué gen 6 da 618 movimientos y no los 621 del dex nacional hasta
ORAS.** Los 618 son los usables. Los 5 que faltan para 621 están en la data
del paquete pero el filtro `isAvailable` (D6) los excluye correctamente:

- `thousandarrows`, `thousandwaves` (firma de Zygarde-Completo) y
  `lightofruin` (firma de Floette-Eterna): `gen=6` pero
  `isNonstandard: 'Unobtainable'` — existen en el código de XY/ORAS y no son
  obtenibles en ningún juego.
- `paleowave` y `shadowstrike`: `isNonstandard: 'CAP'` — son movimientos del
  proyecto CAP de Smogon, no movimientos reales.

618 + 3 unobtainables = 621 del dex nacional; los 2 CAP no cuentan en ninguna
suma contra el dex nacional. Nadie vuelva a investigar esta diferencia.

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

`decision_index` cuenta decisiones, no turnos: arranca en 0 por trayectoria y
avanza una vez por cada llamada a `choose_move` (el índice de
`LudexPlayer.steps[tag]` en `client.py`, que ya numera así por construcción).
`turn_number` queda como columna común, ya no parte de la clave: dos
decisiones pueden compartir turno y eso ahora es representable.

Sin backfill (migración `20260727000006`): las 57 batallas grabadas hasta la
review final eran de prueba, random contra random, y no sirven para entrenar.
Se truncan y se regraban con `agent play`.

## D22 — C-1 vuelve a ser síncrona; el cursor de `_correct_step_turns` reconoce
tres formas de "la acción no se ejecutó" y deja de robarle líneas a la
decisión siguiente

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

El presupuesto total por decisión es 240 s, por debajo de los 300 s medidos
en el reloj del Showdown local (más 60 s de gracia). Fotografía, máscara y
mapa acción→`BattleOrder` se capturan sincrónicamente antes del primer
`await`; el grafo nunca relee el `Battle` mutable. El corrector de turnos
continúa juzgando contra protocolo crudo y no fue modificado.

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
