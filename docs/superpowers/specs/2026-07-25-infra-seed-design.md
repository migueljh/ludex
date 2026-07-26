# Ludex — Fases 0+1: infraestructura y seed multi-generación

Fecha: 2026-07-25
Estado: aprobado
Alcance: primera rebanada del plan de `docs/PLAN.md` (fases 0 y 1)

## 1. Objetivo

Dejar una base de datos Postgres poblada con la data de juego completa de una
generación arbitraria de Pokémon, cargada desde el paquete npm
`pokemon-showdown`, y la infraestructura local para levantarla. Nada más.

Al terminar esta rebanada, `pnpm seed --gen 6` y `pnpm seed --gen 9` deben poder
convivir en la misma base sin colisionar, y la data debe ser suficiente para que
las fases siguientes (agente, calc, web) consulten únicamente Postgres.

El criterio de diseño transversal del proyecto aplica desde el primer archivo:
**nada hardcodea una generación**. La generación es siempre un parámetro.

## 2. Fuera de alcance

Se nombran explícitamente porque son parte del plan general y su ausencia acá es
deliberada, no un olvido:

- `apps/web`, `apps/agent`, `packages/calc`, `packages/viewer`. Cada uno se
  scaffoldea en la fase que lo estrena, con sus dependencias elegidas entonces.
- Todas las tablas de torneo, batallas, análisis, playbook, evals, trayectorias
  y configuración de la sección 4 del plan. Se agregan en la migración de la fase
  que las consulta, cuando ya se sepa cómo se consultan.
- El comando `seed:usage` que descarga usage stats de Smogon. La tabla se crea
  vacía; el comando llega en la fase que la lee.
- Cualquier despliegue. Todo corre local, pero la config va por env vars
  (12-factor) para no tener que refactorizar después.

## 3. Decisiones

Todas se replican en `docs/DECISIONS.md` al implementar.

### D1 — Migraciones: SQL plano con dbmate

La fuente de verdad del esquema son archivos `.sql` versionados en
`db/migrations/`, corridos por [dbmate](https://github.com/amacneil/dbmate).

Motivo: el esquema lo van a consumir dos lenguajes (Python/SQLAlchemy en el
agente, Node en el seed y quizá en la web). Si un ORM es la fuente de verdad, el
otro lenguaje queda subordinado y necesita tipos generados igual. SQL plano deja
a los dos en pie de igualdad, y `pgvector`, los enums y las PKs compuestas se
escriben directo sin pelear con abstracciones.

Costo aceptado: los modelos SQLAlchemy de la fase 2 se escriben a mano.

### D2 — Clave natural: el id normalizado de Showdown

Cada entidad de data de juego se identifica por `(gen_id, showdown_id)`, donde
`showdown_id` es el id normalizado del paquete (`charizardmegax`, `thunderbolt`,
`leftovers`), no el nombre legible.

Motivo: es lo que aparece en el protocolo de batalla en runtime, es estable
entre versiones y evita ambigüedades de acentos, guiones y mayúsculas. El nombre
legible se guarda aparte, solo para mostrar.

### D3 — Herencia de learnsets: resuelta en el seed, sin aplanar

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

Regla por defecto del torneo, a documentar en `DECISIONS.md`: se consideran
legales solo los métodos con `gen == generación del torneo`. Los métodos de
generaciones anteriores quedan almacenados y disponibles, marcados como
transferidos, para que la UI pueda mostrarlos como "legal en ladder, no en el
torneo".

### D4 — Versiones pineadas y registradas en la base

`pokemon-showdown` se instala con versión exacta (`--save-exact`), y cada
corrida del seed escribe una fila en `seed_runs` con la versión del paquete, el
timestamp y los conteos por tabla.

Motivo: el paquete se actualiza seguido y el campo `tier` refleja el tiering de
Smogon vigente, no el histórico. Es el único dato volátil de todo el seed.
Cuando dentro de seis meses un reseed cambie los conteos, `seed_runs` responde
por qué en treinta segundos.

La imagen del server local de Showdown también se pinea a un tag concreto.

### D5 — El seed corre en el host

Postgres expone 5433 al host (5432 adentro del contenedor; ver D8: 5432 lo ocupa otro proyecto del usuario) y `pnpm seed` corre fuera de Docker contra
`localhost`. Menos fricción para iterar y debuggear el volcado.

## 4. Estructura del repositorio

```
ludex/
  docker-compose.yml
  .env.example
  pnpm-workspace.yaml
  package.json                 # scripts raíz: db:migrate, seed, test
  .nvmrc                       # node 22
  db/
    migrations/                # .sql de dbmate
    schema.sql                 # dump generado por dbmate, commiteado
  docker/
    showdown/Dockerfile        # server local desde smogon/pokemon-showdown
  packages/
    seed/
      src/
        extract/               # pokemon-showdown -> objetos planos. Puro, sin DB.
        load/                  # objetos planos -> Postgres. Sin lógica de dominio.
        cli.ts
      test/
        __snapshots__/         # golden files
  docs/
    PLAN.md
    DECISIONS.md
    superpowers/specs/
```

El corte entre `extract/` y `load/` es la decisión estructural de esta rebanada.
Todo el riesgo está en `extract/`: saber qué devuelve `Dex.mod('gen6')` y cómo
codifica los learnsets. Al ser funciones puras se testea sin levantar nada.
`load/` solo hace upserts en lote y no sabe nada de Pokémon.

## 5. Infraestructura

### docker-compose.yml

| servicio | imagen | notas |
|---|---|---|
| `postgres` | `pgvector/pgvector:0.8.5-pg16` | puerto 5433 al host, volumen nombrado, healthcheck `pg_isready` |
| `migrate` | `ghcr.io/amacneil/dbmate` (tag fijo) | one-shot, `docker compose run --rm migrate up` |
| `showdown` | build desde `docker/showdown/Dockerfile` | profile `local`, `--no-security`, puerto 8000 |

`showdown` va bajo profile para que `docker compose up -d` levante solo lo que
esta rebanada necesita, y el server se encienda con
`docker compose --profile local up -d` cuando llegue la fase 2.

El Dockerfile de `showdown` parte de `node:22-alpine`, clona
`smogon/pokemon-showdown` en un tag fijo, corre `npm install --production` y
arranca con `node pokemon-showdown start --no-security`.

### .env.example

Solo las variables que esta rebanada usa, cada una con un comentario de una
línea. Sin placeholders de fases futuras.

```
POSTGRES_USER=ludex
POSTGRES_PASSWORD=ludex
POSTGRES_DB=ludex
DATABASE_URL=postgres://ludex:ludex@localhost:5433/ludex?sslmode=disable
SHOWDOWN_LOCAL_PORT=8100
```

`DATABASE_URL` es la única que consumen tanto dbmate como el seed.

## 6. Esquema

Tres migraciones: extensiones, data de juego, `seed_runs`.

```sql
-- extensiones
CREATE EXTENSION IF NOT EXISTS vector;

-- data de juego
generations(
  id            serial PRIMARY KEY,
  gen_number    int NOT NULL UNIQUE,
  label         text NOT NULL            -- 'XY/ORAS', 'SV'
)

pokemon(
  id            serial PRIMARY KEY,
  gen_id        int NOT NULL REFERENCES generations(id),
  showdown_id   text NOT NULL,
  dex_num       int NOT NULL,
  name          text NOT NULL,
  base_species  text NOT NULL,           -- 'Charizard' para 'Charizard-Mega-X'
  forme         text,                    -- 'Mega-X', NULL si es la base
  is_default    boolean NOT NULL,        -- true solo para la forma base
  types         text[] NOT NULL,
  base_stats    jsonb NOT NULL,          -- {hp,atk,def,spa,spd,spe}
  abilities     jsonb NOT NULL,          -- {"0":..., "1":..., "H":...}
  weight_kg     numeric,
  evolves_from  text,                    -- showdown_id de la preevolución
  tier          text,
  is_nonstandard text,                   -- 'CAP', 'Past', 'Unobtainable', NULL
  UNIQUE(gen_id, showdown_id)
)

moves(
  id            serial PRIMARY KEY,
  gen_id        int NOT NULL REFERENCES generations(id),
  showdown_id   text NOT NULL,
  name          text NOT NULL,
  type          text NOT NULL,
  category      text NOT NULL,           -- Physical | Special | Status
  power         int NOT NULL,
  accuracy      int,                     -- NULL = nunca falla
  pp            int NOT NULL,
  priority      int NOT NULL,
  target        text NOT NULL,
  flags         jsonb NOT NULL,
  description   text,
  UNIQUE(gen_id, showdown_id)
)

learnsets(
  pokemon_id    int NOT NULL REFERENCES pokemon(id) ON DELETE CASCADE,
  move_id       int NOT NULL REFERENCES moves(id) ON DELETE CASCADE,
  learn_methods jsonb NOT NULL,
  PRIMARY KEY(pokemon_id, move_id)
)

items(
  id, gen_id, showdown_id, name, description, flags jsonb,
  UNIQUE(gen_id, showdown_id)
)

abilities(
  id, gen_id, showdown_id, name, description,
  UNIQUE(gen_id, showdown_id)
)

type_chart(
  gen_id          int NOT NULL REFERENCES generations(id),
  attacking_type  text NOT NULL,
  defending_type  text NOT NULL,
  multiplier      numeric NOT NULL,      -- 0 | 0.5 | 1 | 2
  PRIMARY KEY(gen_id, attacking_type, defending_type)
)

usage_stats(
  id, gen_id, format text, pokemon_id, usage_pct numeric, common_sets jsonb,
  UNIQUE(gen_id, format, pokemon_id)
)                                        -- creada vacía en esta rebanada

seed_runs(
  id              serial PRIMARY KEY,
  gen_id          int NOT NULL REFERENCES generations(id),
  package_version text NOT NULL,
  started_at      timestamptz NOT NULL,
  finished_at     timestamptz,
  row_counts      jsonb                  -- {"pokemon": 812, "moves": 621, ...}
)
```

Índices: `pokemon(gen_id, dex_num)`, `pokemon(gen_id, base_species)`,
`moves(gen_id, type)`, `learnsets(move_id)`.

### Forma de `learn_methods`

Array de objetos, uno por método distinto. Preserva generación de origen y
especie de origen.

```json
[
  {"gen": 6, "method": "level", "level": 45, "source_species": "charizard"},
  {"gen": 6, "method": "machine",             "source_species": "charizard"},
  {"gen": 6, "method": "egg",                 "source_species": "charmander"},
  {"gen": 5, "method": "tutor",               "source_species": "charmander"}
]
```

`source_species` distinto del pokémon de la fila significa que el movimiento se
heredó de esa preevolución.

`method` es un valor de un mapa explícito desde los códigos de Showdown
(`L`→`level`, `M`→`machine`, `T`→`tutor`, `E`→`egg`, `S`→`event`, `D`→`dream`,
`V`→`transfer`, `C`→`tradeback`, `R`→`reminder`). **Un código desconocido hace
fallar el seed con un error explícito**, nunca se descarta en silencio: un
código nuevo en una versión futura del paquete es exactamente lo que no se
quiere perder.

## 7. El seed

Comando único: `pnpm seed --gen <n>`. La base sale de `DATABASE_URL`.

Orden del pipeline, por dependencias de FK:

1. `generations` (upsert desde un mapa estático `gen_number -> label`)
2. `pokemon`, `moves`, `items`, `abilities`, `type_chart` (independientes entre sí)
3. `learnsets` (necesita los ids de `pokemon` y `moves`)

Cada tabla se carga en una transacción con upserts en lote sobre la constraint
única (`ON CONFLICT ... DO UPDATE`). Los ~80k registros de learnsets de gen 6 se
insertan en lotes de 1000.

Una corrida abre su fila en `seed_runs` inmediatamente después del paso 1 (antes
no existe el `gen_id` al que la FK apunta) y la cierra con `finished_at` y los
conteos al terminar.

Detalles a resolver empíricamente en el primer paso de implementación, no de
memoria:

- cómo cargar los mods (`Dex.mod('gen6')` requiere que la data esté incluida;
  verificar si hace falta `Dex.includeData()` o equivalente);
- el formato exacto de los learnsets del mod y si ya vienen filtrados por
  generación o hay que filtrarlos;
- la codificación de `damageTaken` del type chart, que además incluye entradas
  de estado (`par`, `brn`, `psn`) que **no** son tipos y hay que excluir.

## 8. Verificación

### Capa 1 — unit, sin DB

Sobre `extract/`, para gen 6 y gen 9:

- conteo de especies base (`is_default = true`) y conteo total con formes;
- un learnset conocido, verificado a mano contra el Dex;
- que los códigos de método se mapeen completos y un código inventado tire error.

### Capa 2 — frontera de generación

Prueban que el mod filtra de verdad, no que el paquete existe:

- el tipo Hada existe en gen 6 y no en gen 5;
- Dragón → Hada es 0×;
- Acero deja de resistir Fantasma y Siniestro en gen 6;
- no hay megaevoluciones en gen 9.

### Capa 3 — integración, con DB

Contra una base de test levantada por compose, migrada con dbmate:

- conteos por tabla después de `--gen 6`;
- gen 6 y gen 9 conviven sin colisión de `showdown_id`;
- **idempotencia real**: correr el seed, mutar una fila a mano
  (`UPDATE pokemon SET tier = 'BOGUS' WHERE ...`), correr el seed otra vez y
  verificar que la fila quedó corregida. Comparar solo conteos no prueba nada:
  pasa igual si los upserts insertan duplicados que una constraint descarta en
  silencio.

### Capa 4 — golden files

Snapshots commiteados de la serialización completa de:

- un pokémon con formes (base + una mega),
- un movimiento,
- un learnset resuelto que incluya al menos un método heredado de preevolución.

Los conteos detectan que algo cambió; los golden files dicen **qué** cambió
cuando un bump de versión del paquete rompa el extractor.

### Sobre los valores esperados

Todos los números y nombres concretos de los tests se fijan **inspeccionando el
Dex durante la implementación**. Ninguno se escribe de memoria: un número
plausible y falso en un test es peor que no tener el test.

## 9. Criterios de aceptación

1. `docker compose up -d` levanta Postgres sano y
   `docker compose run --rm migrate up` deja el esquema creado.
2. `docker compose --profile local up -d showdown` levanta el server local y
   responde en `SHOWDOWN_LOCAL_PORT`.
3. `pnpm seed --gen 6` y `pnpm seed --gen 9` terminan sin error contra la misma
   base y conviven.
4. Las cuatro capas de test pasan.
5. `grep -ri "gen6" packages/ db/ docker/ --exclude-dir=node_modules` no
   devuelve nada fuera de configuración y fixtures de test.
6. `docs/DECISIONS.md` registra D1–D5 más la regla por defecto de legalidad de
   métodos del torneo.
7. `db/schema.sql` está commiteado y al día.
8. El documento de planning general está commiteado en `docs/PLAN.md` (hoy solo
   existe fuera del repo).

## 10. Riesgos

| riesgo | mitigación |
|---|---|
| El formato interno de learnsets del paquete es más enredado de lo previsto | Es el primer paso de implementación, aislado en `extract/`. Si se complica, se resuelve ahí sin tocar nada más. |
| Un bump de `pokemon-showdown` cambia `tier` u otros campos | Versión exacta pineada (D4), `seed_runs` con la versión, golden files que muestran el diff. |
| El type chart trae entradas que no son tipos | Filtro explícito a los 18 tipos, con test de conteo (18×18 = 324 filas en gen 6). |

## 11. Flujo de implementación

Pedido explícito para esta y las siguientes rebanadas: cada unidad de trabajo la
implementa un subagente con modelo Sonnet, un segundo subagente actúa de code
reviewer sobre ese trabajo, y Claude revisa encima antes de dar nada por
terminado.
