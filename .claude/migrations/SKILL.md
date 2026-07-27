---
name: migrations
description: Convenciones de esquema y migraciones de Ludex con dbmate y Postgres. Usar al crear o modificar migraciones en db/migrations, al agregar tablas o columnas, al elegir tipos o claves, al escribir queries contra el esquema, o al configurar la conexión a la base.
---

# Migraciones y esquema en Ludex

Fuente de verdad del esquema: archivos `.sql` versionados en `db/migrations/`,
corridos por dbmate pineado a `2.21`. Ver D1, D2, D8, D9, D11, D13.

## Por qué SQL plano y no un ORM

El esquema lo consumen tres lenguajes: Python/SQLAlchemy en el agente, Node en el seed
y en `packages/calc`, y más adelante scripts de ML leyendo con SQL crudo. Si un ORM es
la fuente de verdad, los otros quedan subordinados y necesitan tipos generados igual.

Además el esquema usa justo lo que hace sufrir a los ORMs: `pgvector`, enums nativos,
arrays (`text[]`), PKs compuestas y bastante `jsonb`.

Costo aceptado: los modelos SQLAlchemy se escriben a mano. Se compensa generándolos
desde la base (`sqlacodegen` para Python, `kysely-codegen` o `pg-to-ts` para
TypeScript) como paso post-migración, no escribiéndolos.

## Reglas de esquema

**Clave natural: `(gen_id, showdown_id)`** para toda entidad de data de juego (D2).
`showdown_id` es el id normalizado del paquete (`charizardmegax`, `thunderbolt`), no el
nombre legible. Es lo que llega en el protocolo de batalla en runtime.

**Toda referencia entre entidades usa ids normalizados, nunca nombres legibles.**
El paquete devuelve `baseSpecies` y `prevo` como nombre legible: normalizá con
`dex.species.get(x).id` antes de guardar. Un nombre legible en una columna que después
se joinea contra `showdown_id` obliga a normalizar en cada query y es una fuente
silenciosa de misses.

**El nombre legible se guarda aparte, solo para mostrar.**

**Toda tabla de data de juego lleva `gen_id` con FK a `generations` y
`ON DELETE CASCADE`.** La única excepción es `learnsets`, que lo hereda por join desde
`pokemon` — recordalo al escribir conteos filtrados por generación (D13).

**`NULL` solo con semántica documentada.** Si una columna admite `NULL` con un
significado especial, va en `DECISIONS.md`. Precedente: `moves.accuracy IS NULL`
significa "nunca falla", no "desconocida" (D15). Preferí `NULL` antes que inventar un
centinela numérico, que rompe agregados sin que la base lo impida.

**Nada de columnas que van a ser siempre `NULL`.** Se eliminó
`pokemon.is_nonstandard` porque el filtro del seed ya excluye todo lo nonstandard (D7).

## Reglas de migración

- Prefijo de timestamp en el nombre, un cambio conceptual por archivo.
- **`migrate:down` completo y correcto**, en orden inverso de dependencias.
- Extensiones en su propia migración inicial (`vector`, `pg_trgm`).
- Aditivas: cada fase agrega las suyas. **No crear tablas de fases futuras.**
  La forma correcta de una tabla se conoce cuando se sabe cómo se consulta; congelar
  hoy los FKs, enums y la forma de un `jsonb` que nadie va a leer hasta dentro de tres
  fases es adivinar, y se corrige después igual.
- Índices junto a la tabla que los necesita, no en una migración suelta.
- Toda migración con una decisión detrás lleva su entrada en `DECISIONS.md`.

## Conexión: el puerto es 15432

`DATABASE_URL` apunta a `localhost:15432` desde el host. Adentro de la red de compose
el servicio sigue siendo `postgres:5432`.

**No lo cambies a 5432 ni a 5433.** Esta máquina tiene un `postgresql@14` de Homebrew
que bindea `127.0.0.1:5433`, y el bind específico le gana al wildcard de Docker: una
conexión a `localhost:5433` llega a la base equivocada. El síntoma es
`FATAL: role "ludex" does not exist`, o peor, escrituras silenciosas en otra base.

Esto no falla al levantar. El contenedor arranca sano y las migraciones corren bien,
porque el servicio `migrate` habla por la red interna sin pasar por el host. Solo se
rompe lo que corre en el host, que es el seed (D5, D11).

Nunca se toca infraestructura de otro proyecto para hacerle lugar a Ludex: se remapea
el puerto del host.

## Versiones

Todo componente cuya versión afecta el comportamiento observable se pinea a un tag
concreto, no flotante:

- Postgres: `pgvector/pgvector:0.8.5-pg16` (versión, no digest: fija pgvector y deja
  fluir los patches de 16.x).
- dbmate `2.21`.
- `pokemon-showdown` y `@smogon/calc` con versión exacta en `package.json`.
- La imagen del server local de Showdown al mismo ref que `pokemon-showdown`.

Los servicios se bindean a `127.0.0.1` en compose.

## Al escribir queries

**El pipeline es upsert-only.** No hay `DELETE` ni `TRUNCATE` en `packages/seed/src/`.
Una especie o movimiento retirado en un bump de versión sobrevive indefinidamente en la
tabla y una query de legalidad lo va a seguir viendo como legal. Es un límite conocido y
acotado (D13), no un descuido.

Los conteos que se persisten salen de `count(*)` sobre la tabla, filtrado por
generación, no de la cantidad de filas enviadas por el upsert.

**Legalidad estricta de movimientos:** caminar la línea evolutiva real de la forma con
un recursive CTE sobre `evolves_from` y evaluar los métodos y su generación de origen.
`sourceSpecies` permite reconstruir la procedencia, pero pertenecer a la línea correcta
no alcanza: también hay reglas de transferencia y compatibilidad de eventos que aplica
el consumidor, no el seed (D3, D14).