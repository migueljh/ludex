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

## D14 — Problema conocido: herencia en formas con línea evolutiva propia

`inheritanceChain` (en `packages/seed/src/extract/learnsets.ts`) camina la cadena
de preevoluciones de la **especie base**, no la de la **forma**. Cuando una forma
tiene su propia línea evolutiva, hereda de la línea equivocada.

**Impacto en gen 6, la generación del torneo actual: ninguno.** Afecta a tres
formas —`gourgeistsmall`, `gourgeistlarge`, `gourgeistsuper`— cuyas
preevoluciones propias son los `pumpkaboo` del mismo tamaño. Verificado: los 4
movimientos que aporta `pumpkaboosuper` son todos de evento (`6S0`) y
`pumpkaboo` ya los tiene, así que las cuatro formas de Gourgeist terminan con el
movepool correcto de 66 entradas.

**Impacto en gen 9: real.** Afecta a las líneas regionales. `ninetalesalola`
produce la cadena `[ninetalesalola, ninetales, vulpix]` en vez de
`[ninetalesalola, vulpixalola]`: pierde `moonblast`, que `vulpixalola` aporta
como movimiento huevo, y hereda movimientos de `vulpix`, que es otra especie con
otro tipo (Fuego contra Hielo/Hada).

### Por qué no se arregló acá

Se intentó el arreglo mecánico —caminar `own.prevo || own.baseSpecies`— y
**regresionó gen 6**: al ser un o-exclusivo, `gourgeistsmall` y `gourgeistlarge`
quedaron con cero movimientos y `gourgeistsuper` con cuatro, porque ni la forma
ni su preevolución propia tienen learnset propio y ya no se caía a la base. Se
revirtió y se restauró la base.

El problema es que dos clases de forma necesitan reglas opuestas y el paquete no
las distingue con ninguna bandera: `changesFrom` es `undefined` para todas y
`cosmeticFormes` no cubre los tamaños de Gourgeist.

- **Formas de tamaño** (Gourgeist, Pumpkaboo): comparten movepool con su base y
  necesitan **ambas** ramas de la cadena.
- **Formas regionales** (Alola, Hisui, Galar): son especies funcionalmente
  distintas y heredar de la base es un falso positivo de legalidad.

### Camino sugerido cuando toque

Recorrer la **unión** de las dos cadenas, deduplicada, y dejar que la regla de
legalidad filtre por `sourceSpecies` al consultar. Es lo que ya manda D3: el
seed guarda todo lo que el paquete sabe y quien consulta decide qué acepta. Es
estrictamente aditivo, así que no puede perder datos como el intento fallido.
Requiere su propia rebanada: cambia los conteos, necesita tests de las dos
clases de forma, y la regla de filtrado por `sourceSpecies` hay que definirla
junto con `round_availability` en la fase de torneo.

**Por qué esa solución es viable: `pokemon.evolves_from` guarda la
preevolución de la FORMA, no la de su especie base.** El seed lo resuelve con
`dex.species.get(s.prevo).id` (`packages/seed/src/extract/species.ts`), y
`prevo` de `ninetalesalola` es `vulpixalola`, no `vulpix`. Verificado contra
la base (gen 9):

```
     showdown_id   | base_species | evolves_from
-------------------+--------------+--------------
 ninetalesalola    | ninetales    | vulpixalola
 vulpixalola       | vulpix       | (null)
```

Eso significa que quien consulte puede caminar la línea evolutiva **real** de
la forma con un recursive CTE sobre `evolves_from` y descartar los métodos
cuyo `sourceSpecies` no pertenezca a esa línea, sin necesitar ningún dato
adicional. Es la razón por la que la solución aditiva no genera falsos
positivos permanentes: heredar de más en el seed es inofensivo mientras el
filtro de consulta pueda reconstruir la línea correcta, y puede.

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
multi-golpes, con diferencias por generación. Una reimplementación en Python no
falla ruidosamente: devuelve números sutilmente equivocados, y el agente decide
"esto no lo mata" cuando sí lo mataba.

Costo aceptado: un contenedor más y una llamada HTTP local por turno,
despreciable frente a la latencia de un LLM. Ventaja: la web de la fase 4 le
pega al mismo servicio.

`packages/seed` seguirá siendo Node, pero es una herramienta de build: no hay
Node vivo durante una batalla salvo el servicio de calc.

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
