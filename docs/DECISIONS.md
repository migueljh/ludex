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
`(pokemon, move)` resueltas para gen 6 es **62157**.

Motivo: la herencia solo puede sumar filas (una especie nunca pierde un
movimiento que ya tenía directo), así que 62157 > 49321 es la primera señal de
que la cadena de herencia corrió. Si un reseed futuro devuelve un número igual
o menor a 49321 para gen 6, algo rompió la resolución de `baseSpecies` o de
`prevo` (por ejemplo, las 48 megaevoluciones de gen 6 quedando sin aportar
movimientos propios de su forma base). Sirve de canario barato sin tener que
inspeccionar fila por fila.
