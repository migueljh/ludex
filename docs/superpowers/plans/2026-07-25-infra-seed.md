# Ludex Fases 0+1 — Infraestructura y Seed Multi-Generación: Plan de Implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dejar Postgres poblado con la data de juego completa de cualquier generación de Pokémon, cargada desde el paquete npm `pokemon-showdown`, más la infraestructura local para levantarla.

**Architecture:** Migraciones SQL planas corridas por dbmate son la fuente de verdad del esquema (ningún ORM manda, porque el esquema lo consumen Node y Python). El seed se parte en dos capas: `extract/` convierte el Dex de Showdown en objetos planos y es puro y testeable sin base de datos — ahí está todo el riesgo; `load/` hace upserts en lote y no sabe nada de Pokémon.

**Tech Stack:** pnpm workspaces, TypeScript, tsx, vitest, `pg`, `pokemon-showdown@0.11.10`, Postgres 16 + pgvector, dbmate, Docker Compose.

## Global Constraints

- **Ninguna generación se hardcodea.** La generación es siempre un parámetro. `grep -ri "gen6" packages/ db/ docker/ --exclude-dir=node_modules` no debe devolver nada fuera de configuración y fixtures de test.
- **`pokemon-showdown` se instala con versión exacta:** `0.11.10`. Sin rangos `^` ni `~`. Todos los conteos de este plan son de esa versión.
- **Clave natural:** `(gen_id, showdown_id)`. `showdown_id` es el id normalizado (`charizardmegax`, `thunderbolt`, `leftovers`), nunca el nombre legible.
- **Los learnsets no se aplanan.** `learn_methods` conserva cada método con su generación de origen y su especie de origen. La regla de legalidad se aplica en query time.
- **Un código de método desconocido hace fallar el seed** con error explícito. Nunca se descarta en silencio.
- **Node 22** (`.nvmrc`), pnpm como package manager.
- Toda decisión no trivial se registra en `docs/DECISIONS.md`.

## Hallazgos verificados contra el paquete (no asumir otra cosa)

Estos valores salieron de inspeccionar `pokemon-showdown@0.11.10` directamente. Son la base de los tests.

**Los mods NO filtran por generación.** `Dex.mod('gen6').species.all()` devuelve 1425 entradas, incluidas 523 marcadas `isNonstandard: 'Future'`. `Dex.mod('gen5').types.all()` incluye Hada y Stellar. El filtrado es responsabilidad nuestra.

Regla de filtro que funciona para especies, movimientos, objetos y habilidades:

```ts
entry.gen <= dex.gen && !entry.isNonstandard
```

Conteos resultantes:

| | gen 6 | gen 9 |
|---|---|---|
| especies | 834 | 874 |
| especies base (`name === baseSpecies`) | 721 | 733 |
| megaevoluciones | 48 | 0 |
| movimientos (tras deduplicar por id) | 618 | 685 |
| objetos | 283 | 248 |
| habilidades | 191 | 310 |
| tipos | 18 | 19 |
| filas de `type_chart` | 324 | 361 |
| especies con learnset propio | 740 | — |
| pares directos `(especie, movimiento)` con `gen <= 6` | 49321 | — |

Nota sobre gen 9: 733 especies base no es el dex nacional (1025) sino lo obtenible en SV; el resto está marcado `isNonstandard: 'Past'`. Es el comportamiento deseado. En gen 6 coincide con el dex nacional porque en XY/ORAS todo lo anterior era obtenible.

Lo que el filtro descarta en gen 6, y está bien que descarte: Pichu-Spiky-eared (`Past`), Floette-Eternal (`Unobtainable`), Xerneas-Neutral, MissingNo. y los 17 Pokestar (`Custom`), más 47 CAP.

**Corrección a la spec:** como el filtro excluye todo lo `isNonstandard`, la columna `pokemon.is_nonstandard` sería siempre `NULL`. Se elimina del esquema. Registrar en `DECISIONS.md`.

**El type chart se deriva sin hardcodear.** `types.all()` devuelve 19 tipos en todas las gens, pero la **intersección de las claves de `damageTaken` de todos los tipos** da la lista correcta por generación: 15 en gen 1, 17 en gen 2 y 5, 18 en gen 6, 19 en gen 9. Los códigos de `damageTaken` son `0`=normal (1×), `1`=débil (2×), `2`=resiste (0.5×), `3`=inmune (0×). Ojo: `damageTaken` incluye claves que **no son tipos** (`psn`, `tox`, `sandstorm`) y hay que excluirlas.

Verificación de frontera: `gen6 Steel.damageTaken.Dark === 0` (normal) contra `gen5 Steel.damageTaken.Dark === 2` (resiste). Lo mismo con Ghost. Es el cambio real de gen 6.

**Los learnsets son async y tampoco vienen filtrados.**

```js
const data = await dex.species.getLearnsetData('charizard');
data.learnset.flamethrower
// => ["9M","9L30","8M","8L30","8V","8S10","7M","7L47","7V","7S8","6M","6L47","6S5","5M","5L47","4M","4L42","3M","3L34"]
```

Códigos: `<gen><letra><resto>`. Letras observadas: `D E L M R S T V` (más `C` y `T` en gens viejas). `6L47` = gen 6, por nivel 47. `6S5` = gen 6, evento nº 5. `9M` = gen 9, MT — hay que descartarlo en un seed de gen 6.

**Las formas no tienen learnset propio.** `getLearnsetData('charizardmegax')` devuelve `learnset` vacío, igual que `deoxysattack`. Y `charizardmegax.prevo` es `""`. Por eso la herencia debe resolver **primero a `baseSpecies`, después caminar la cadena `prevo`**. Caminar solo `prevo` deja a todas las megas sin movimientos.

**El caso de oro de herencia, real y verificado:** en gen 6 Charizard **no** aprende Danza Dragón por sí mismo — sus códigos son `["9M","8M","7S9"]`, todos posteriores a la 6. Pero Charmander la tiene como movimiento huevo: `["9M","8M","7E","6E","5E","4E","3E"]`, y `6E` sobrevive al filtro. Charizard la aprende **solo por herencia**. Este par es el golden file de la Tarea 8.

**Otros detalles de campo:** el campo de potencia es `basePower`, no `power`. `accuracy` es `true` para movimientos que nunca fallan (ej. Swift) y debe guardarse como `NULL`. `forme` es `""` (string vacío) para la forma base, no `null`. `prevo` es `""` cuando no hay preevolución. Los objetos usan `desc`.

---

## File Structure

```
ludex/
  .nvmrc                                  # 22
  .gitignore                              # ya existe
  package.json                            # scripts raíz: db:migrate, seed, test
  pnpm-workspace.yaml
  docker-compose.yml
  .env.example
  docker/showdown/Dockerfile              # server local, profile "local"
  db/
    migrations/
      20260725000001_enable_extensions.sql
      20260725000002_game_data.sql
      20260725000003_seed_runs.sql
    schema.sql                            # dump de dbmate, commiteado
  packages/seed/
    package.json
    tsconfig.json
    vitest.config.ts
    src/
      types.ts        # interfaces de fila. Sin lógica. Lo comparten extract/ y load/.
      extract/
        dex.ts        # carga de mods + predicado de filtro por generación
        species.ts    # ModdedDex -> SpeciesRow[]
        moves.ts      # ModdedDex -> MoveRow[]
        simple.ts     # items y abilities (misma forma trivial)
        typechart.ts  # ModdedDex -> TypeChartRow[]
        learnsets.ts  # ModdedDex -> LearnsetRow[] con herencia resuelta
      load/
        client.ts     # pool de pg, helper de upsert en lote
        tables.ts     # una función de carga por tabla
        runs.ts       # seed_runs
      cli.ts          # orquestación del pipeline
    test/
      extract/*.test.ts
      load/integration.test.ts
      __snapshots__/
  docs/
    PLAN.md
    DECISIONS.md
```

Corte que importa: `extract/` es puro y no importa nada de `pg`; `load/` no importa nada de `pokemon-showdown`. `types.ts` es el único módulo que ambos comparten. Si alguna vez `load/` necesita saber qué es una megaevolución, el corte se rompió.

---

## Task 1: Esqueleto del monorepo e infraestructura Docker

**Files:**
- Create: `.nvmrc`, `package.json`, `pnpm-workspace.yaml`, `docker-compose.yml`, `.env.example`
- Create: `db/migrations/20260725000001_enable_extensions.sql`
- Create: `docs/DECISIONS.md`, `docs/PLAN.md`

**Interfaces:**
- Consumes: nada.
- Produces: `DATABASE_URL` apuntando a `postgres://ludex:ludex@localhost:5433/ludex?sslmode=disable`; el comando `pnpm db:migrate`; una base con la extensión `vector` instalada.

- [ ] **Step 1: Crear los archivos raíz del workspace**

`.nvmrc`:
```
22
```

`pnpm-workspace.yaml`:
```yaml
packages:
  - "packages/*"
  - "apps/*"
```

`package.json`:
```json
{
  "name": "ludex",
  "private": true,
  "packageManager": "pnpm@11.1.2",
  "engines": { "node": ">=22" },
  "scripts": {
    "db:migrate": "docker compose run --rm migrate up",
    "db:dump": "docker compose run --rm migrate dump",
    "seed": "pnpm --filter @ludex/seed run seed",
    "test": "pnpm -r run test"
  }
}
```

`.env.example`:
```
# Credenciales del Postgres que levanta docker-compose
POSTGRES_USER=ludex
POSTGRES_PASSWORD=ludex
POSTGRES_DB=ludex

# Unica fuente de conexion. La consumen dbmate y el seed.
DATABASE_URL=postgres://ludex:ludex@localhost:5433/ludex?sslmode=disable

# Puerto del server local de Showdown (profile "local", se usa desde la fase 2)
SHOWDOWN_LOCAL_PORT=8100
```

- [ ] **Step 2: Escribir `docker-compose.yml`**

`DBMATE_*` van al servicio `migrate` porque adentro del contenedor la base no es `localhost`.

```yaml
services:
  postgres:
    image: pgvector/pgvector:0.8.5-pg16
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      # 5433 en el host: 5432 lo ocupa otro proyecto del usuario. Adentro del
      # contenedor sigue siendo 5432, asi que el servicio migrate no cambia.
      - "5433:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 10

  migrate:
    image: ghcr.io/amacneil/dbmate:2.21
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}?sslmode=disable
      DBMATE_MIGRATIONS_DIR: /db/migrations
      DBMATE_SCHEMA_FILE: /db/schema.sql
      DBMATE_NO_DUMP_SCHEMA: "false"
    volumes:
      - ./db:/db

  showdown:
    profiles: ["local"]
    build: ./docker/showdown
    ports:
      - "${SHOWDOWN_LOCAL_PORT}:8000"

volumes:
  pgdata:
```

- [ ] **Step 3: Escribir la primera migración**

`db/migrations/20260725000001_enable_extensions.sql`:
```sql
-- migrate:up
CREATE EXTENSION IF NOT EXISTS vector;

-- migrate:down
DROP EXTENSION IF EXISTS vector;
```

- [ ] **Step 4: Levantar y migrar**

```bash
cp .env.example .env
docker compose up -d postgres
docker compose run --rm migrate up
```

Esperado: dbmate reporta la migración aplicada y genera `db/schema.sql`.

- [ ] **Step 5: Verificar que la extensión quedó instalada**

```bash
docker compose exec -T postgres psql -U ludex -d ludex -c \
  "SELECT extname FROM pg_extension WHERE extname='vector';"
```

Esperado: una fila, `vector`.

- [ ] **Step 6: Verificar idempotencia del migrador**

```bash
docker compose run --rm migrate up
```

Esperado: no aplica nada, sale sin error.

- [ ] **Step 7: Crear los documentos**

Copiar el documento de planning general a `docs/PLAN.md` (hoy vive fuera del repo).

Crear `docs/DECISIONS.md` con las decisiones D1 a D5 de `docs/superpowers/specs/2026-07-25-infra-seed-design.md`, más estas dos que salieron de inspeccionar el paquete:

- **D6 — Los mods de Showdown no filtran por generación.** `Dex.mod('genN')` devuelve el dex completo, con contenido posterior marcado `isNonstandard: 'Future'`. El filtro `entry.gen <= dex.gen && !entry.isNonstandard` es responsabilidad del seed. Sin él se cargarían 523 especies de gens futuras en un seed de gen 6.
- **D7 — Se elimina `pokemon.is_nonstandard`.** El filtro de D6 excluye todo lo nonstandard, así que la columna sería siempre `NULL`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(infra): monorepo, docker-compose y migracion de extensiones"
```

---

## Task 2: Migraciones del esquema de data de juego

**Files:**
- Create: `db/migrations/20260725000002_game_data.sql`
- Create: `db/migrations/20260725000003_seed_runs.sql`
- Modify: `db/schema.sql` (lo regenera dbmate)

**Interfaces:**
- Consumes: la base migrada de la Tarea 1.
- Produces: las tablas `generations`, `pokemon`, `moves`, `learnsets`, `items`, `abilities`, `type_chart`, `usage_stats`, `seed_runs`. Las tareas 9 y 10 escriben contra estos nombres de columna exactos.

- [ ] **Step 1: Escribir la migración de data de juego**

`db/migrations/20260725000002_game_data.sql`:
```sql
-- migrate:up
CREATE TABLE generations (
  id         serial PRIMARY KEY,
  gen_number int  NOT NULL UNIQUE,
  label      text NOT NULL
);

CREATE TABLE pokemon (
  id           serial PRIMARY KEY,
  gen_id       int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  showdown_id  text NOT NULL,
  dex_num      int  NOT NULL,
  name         text NOT NULL,
  base_species text NOT NULL,
  forme        text,
  is_default   boolean NOT NULL,
  types        text[]  NOT NULL,
  base_stats   jsonb   NOT NULL,
  abilities    jsonb   NOT NULL,
  weight_kg    numeric,
  evolves_from text,
  tier         text,
  UNIQUE (gen_id, showdown_id)
);
CREATE INDEX pokemon_gen_dex_num_idx      ON pokemon (gen_id, dex_num);
CREATE INDEX pokemon_gen_base_species_idx ON pokemon (gen_id, base_species);

CREATE TABLE moves (
  id          serial PRIMARY KEY,
  gen_id      int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  showdown_id text NOT NULL,
  name        text NOT NULL,
  type        text NOT NULL,
  category    text NOT NULL,
  power       int  NOT NULL,
  accuracy    int,
  pp          int  NOT NULL,
  priority    int  NOT NULL,
  target      text NOT NULL,
  flags       jsonb NOT NULL,
  description text,
  UNIQUE (gen_id, showdown_id)
);
CREATE INDEX moves_gen_type_idx ON moves (gen_id, type);

CREATE TABLE learnsets (
  pokemon_id    int   NOT NULL REFERENCES pokemon(id) ON DELETE CASCADE,
  move_id       int   NOT NULL REFERENCES moves(id)   ON DELETE CASCADE,
  learn_methods jsonb NOT NULL,
  PRIMARY KEY (pokemon_id, move_id)
);
CREATE INDEX learnsets_move_id_idx ON learnsets (move_id);

CREATE TABLE items (
  id          serial PRIMARY KEY,
  gen_id      int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  showdown_id text NOT NULL,
  name        text NOT NULL,
  description text,
  flags       jsonb NOT NULL,
  UNIQUE (gen_id, showdown_id)
);

CREATE TABLE abilities (
  id          serial PRIMARY KEY,
  gen_id      int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  showdown_id text NOT NULL,
  name        text NOT NULL,
  description text,
  UNIQUE (gen_id, showdown_id)
);

CREATE TABLE type_chart (
  gen_id         int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  attacking_type text NOT NULL,
  defending_type text NOT NULL,
  multiplier     numeric NOT NULL,
  PRIMARY KEY (gen_id, attacking_type, defending_type)
);

CREATE TABLE usage_stats (
  id          serial PRIMARY KEY,
  gen_id      int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  format      text NOT NULL,
  pokemon_id  int  NOT NULL REFERENCES pokemon(id) ON DELETE CASCADE,
  usage_pct   numeric,
  common_sets jsonb,
  UNIQUE (gen_id, format, pokemon_id)
);

-- migrate:down
DROP TABLE usage_stats;
DROP TABLE type_chart;
DROP TABLE abilities;
DROP TABLE items;
DROP TABLE learnsets;
DROP TABLE moves;
DROP TABLE pokemon;
DROP TABLE generations;
```

- [ ] **Step 2: Escribir la migración de `seed_runs`**

`db/migrations/20260725000003_seed_runs.sql`:
```sql
-- migrate:up
CREATE TABLE seed_runs (
  id              serial PRIMARY KEY,
  gen_id          int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  package_version text NOT NULL,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  row_counts      jsonb
);

-- migrate:down
DROP TABLE seed_runs;
```

- [ ] **Step 3: Aplicar y verificar**

```bash
docker compose run --rm migrate up
docker compose exec -T postgres psql -U ludex -d ludex -c "\dt"
```

Esperado: las nueve tablas más `schema_migrations`.

- [ ] **Step 4: Verificar que el rollback funciona**

```bash
docker compose run --rm migrate rollback
docker compose run --rm migrate rollback
docker compose run --rm migrate up
```

Esperado: baja `seed_runs`, baja la data de juego, y las vuelve a subir sin error. Un `down` roto se descubre acá o no se descubre nunca.

- [ ] **Step 5: Commit**

```bash
git add db/
git commit -m "feat(db): esquema de data de juego y seed_runs"
```

---

## Task 3: Server local de Showdown

**Files:**
- Create: `docker/showdown/Dockerfile`

**Interfaces:**
- Consumes: el servicio `showdown` de `docker-compose.yml` (Tarea 1).
- Produces: un server de Showdown sin autenticación escuchando en el puerto 8000 del contenedor. Lo consume la fase 2, no esta rebanada.

- [ ] **Step 1: Escribir el Dockerfile**

El tag se fija; un `master` móvil rompe reproducibilidad igual que un rango de versión en npm.

`docker/showdown/Dockerfile`:
```dockerfile
FROM node:22-alpine

RUN apk add --no-cache git

ARG SHOWDOWN_REF=v0.11.10
WORKDIR /app
RUN git clone --depth 1 --branch ${SHOWDOWN_REF} \
      https://github.com/smogon/pokemon-showdown.git . \
 && npm install --omit=dev \
 && node build

COPY config.js /app/config/config.js

EXPOSE 8000
CMD ["node", "pokemon-showdown", "start", "--no-security", "8000"]
```

- [ ] **Step 2: Crear el config**

`docker/showdown/config.js`:
```js
'use strict';
exports.port = 8000;
exports.bindaddress = '0.0.0.0';
exports.workers = 1;
exports.nothrottle = true;
exports.noguestsecurity = true;
exports.backdoor = false;
exports.report_joins = false;
```

- [ ] **Step 3: Buildear y levantar**

```bash
docker compose --profile local up -d --build showdown
```

Si el `git clone` del tag falla porque el repo del server no publica ese tag, listar los disponibles con `git ls-remote --tags https://github.com/smogon/pokemon-showdown.git | tail -20`, elegir el más reciente estable, fijarlo en `SHOWDOWN_REF` y anotarlo en `DECISIONS.md`. No dejar `master`.

- [ ] **Step 4: Verificar que responde**

```bash
curl -sf http://localhost:8100/ -o /dev/null && echo "showdown OK"
```

Esperado: `showdown OK`.

- [ ] **Step 5: Apagarlo y commitear**

```bash
# Solo el servicio showdown. NO uses `docker compose --profile local down`:
# baja TODO el proyecto, incluido postgres con su estado ya verificado.
docker compose stop showdown
docker compose rm -f showdown

git add docker/
git commit -m "feat(infra): server local de Showdown bajo profile local"
```

---

## Task 4: Paquete seed y capa de acceso al Dex

**Files:**
- Create: `packages/seed/package.json`, `packages/seed/tsconfig.json`, `packages/seed/vitest.config.ts`
- Create: `packages/seed/src/types.ts`, `packages/seed/src/extract/dex.ts`
- Test: `packages/seed/test/extract/dex.test.ts`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `loadGen(genNumber: number): ModdedDex`
  - `isAvailable(dex: ModdedDex, entry: {gen: number; isNonstandard?: string | null}): boolean`
  - `GENERATION_LABELS: Record<number, string>`
  - `packageVersion(): string`
  - Todas las interfaces de fila de `types.ts`, que consumen las tareas 5 a 10.

- [ ] **Step 1: Crear el paquete**

`packages/seed/package.json`:
```json
{
  "name": "@ludex/seed",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "seed": "tsx src/cli.ts",
    "test": "vitest run"
  },
  "dependencies": {
    "pg": "8.13.1",
    "pokemon-showdown": "0.11.10"
  },
  "devDependencies": {
    "@types/node": "22.10.2",
    "@types/pg": "8.11.10",
    "tsx": "4.19.2",
    "typescript": "5.7.2",
    "vitest": "2.1.8"
  }
}
```

`packages/seed/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "types": ["node"]
  },
  "include": ["src", "test"]
}
```

`packages/seed/vitest.config.ts`:
```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    testTimeout: 120_000,
  },
});
```

Instalar con `pnpm install` desde la raíz. Verificar la versión exacta:
```bash
node -e "console.log(require('./node_modules/pokemon-showdown/package.json').version)"
```
Esperado: `0.11.10`. Si no coincide, corregir el pin antes de seguir: todos los conteos de este plan dependen de eso.

- [ ] **Step 2: Escribir las interfaces de fila**

`packages/seed/src/types.ts`:
```ts
export interface SpeciesRow {
  showdownId: string;
  dexNum: number;
  name: string;
  baseSpecies: string;
  forme: string | null;
  isDefault: boolean;
  types: string[];
  baseStats: { hp: number; atk: number; def: number; spa: number; spd: number; spe: number };
  abilities: Record<string, string>;
  weightKg: number | null;
  evolvesFrom: string | null;
  tier: string | null;
}

export interface MoveRow {
  showdownId: string;
  name: string;
  type: string;
  category: string;
  power: number;
  accuracy: number | null;
  pp: number;
  priority: number;
  target: string;
  flags: Record<string, number>;
  description: string | null;
}

export interface ItemRow {
  showdownId: string;
  name: string;
  description: string | null;
  flags: Record<string, unknown>;
}

export interface AbilityRow {
  showdownId: string;
  name: string;
  description: string | null;
}

export interface TypeChartRow {
  attackingType: string;
  defendingType: string;
  multiplier: number;
}

export type LearnMethodName =
  | "level" | "machine" | "tutor" | "egg" | "event"
  | "dream" | "transfer" | "tradeback" | "reminder";

export interface LearnMethod {
  gen: number;
  method: LearnMethodName;
  level?: number;
  sourceSpecies: string;
}

export interface LearnsetRow {
  speciesId: string;
  moveId: string;
  methods: LearnMethod[];
}
```

- [ ] **Step 3: Escribir el test que falla**

`packages/seed/test/extract/dex.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { GENERATION_LABELS, isAvailable, loadGen, packageVersion } from "../../src/extract/dex.js";

describe("loadGen", () => {
  it("carga el mod de la generacion pedida", () => {
    expect(loadGen(6).gen).toBe(6);
    expect(loadGen(9).gen).toBe(9);
  });

  it("rechaza generaciones fuera de rango", () => {
    expect(() => loadGen(0)).toThrow(/generacion/i);
    expect(() => loadGen(99)).toThrow(/generacion/i);
  });
});

describe("isAvailable", () => {
  const dex = loadGen(6);

  it("acepta contenido de la generacion o anterior sin marca", () => {
    expect(isAvailable(dex, dex.species.get("charizard"))).toBe(true);
    expect(isAvailable(dex, dex.species.get("greninja"))).toBe(true);
  });

  it("rechaza contenido de generaciones futuras", () => {
    const incineroar = dex.species.get("incineroar");
    expect(incineroar.gen).toBe(7);
    expect(incineroar.isNonstandard).toBe("Future");
    expect(isAvailable(dex, incineroar)).toBe(false);
  });

  it("rechaza cualquier cosa marcada como nonstandard", () => {
    expect(isAvailable(dex, dex.species.get("missingno"))).toBe(false);
    expect(isAvailable(dex, dex.species.get("floetteeternal"))).toBe(false);
  });

  it("filtra el dex completo a los conteos conocidos de gen 6", () => {
    const kept = dex.species.all().filter((s) => isAvailable(dex, s));
    expect(dex.species.all().length).toBe(1425);
    expect(kept.length).toBe(834);
  });

  it("aisla la clausula de generacion, no solo la marca nonstandard", () => {
    // Entradas sinteticas a proposito. En la data real de Showdown TODO lo de
    // generaciones futuras ya viene marcado isNonstandard:'Future' (verificado:
    // cero entradas con gen > dex.gen y sin marca, en gen 6 y en gen 9). Sin
    // este test, un `return !entry.isNonstandard` pasaria las otras siete
    // aserciones y la comparacion de generacion quedaria sin cobertura, en el
    // predicado del que depende todo el pipeline.
    expect(isAvailable(dex, { gen: 5, isNonstandard: null })).toBe(true);
    expect(isAvailable(dex, { gen: 6, isNonstandard: null })).toBe(true);
    expect(isAvailable(dex, { gen: 7, isNonstandard: null })).toBe(false);
    expect(isAvailable(dex, { gen: 9, isNonstandard: null })).toBe(false);
  });
});

describe("metadatos", () => {
  it("tiene etiqueta para las generaciones seedeables", () => {
    expect(GENERATION_LABELS[6]).toBe("XY/ORAS");
    expect(GENERATION_LABELS[9]).toBe("SV");
  });

  it("reporta la version exacta pineada del paquete", () => {
    expect(packageVersion()).toBe("0.11.10");
  });
});
```

- [ ] **Step 4: Correr el test y verificar que falla**

```bash
pnpm --filter @ludex/seed test
```
Esperado: FAIL, `Cannot find module '../../src/extract/dex.js'`.

- [ ] **Step 5: Implementar**

`packages/seed/src/extract/dex.ts`:
```ts
import { createRequire } from "node:module";
import { Dex } from "pokemon-showdown";

const require = createRequire(import.meta.url);

export type ModdedDex = ReturnType<typeof Dex.mod>;

export const GENERATION_LABELS: Record<number, string> = {
  1: "RBY", 2: "GSC", 3: "RSE", 4: "DPPt/HGSS", 5: "BW/BW2",
  6: "XY/ORAS", 7: "SM/USUM", 8: "SwSh", 9: "SV",
};

export function loadGen(genNumber: number): ModdedDex {
  if (!Number.isInteger(genNumber) || !(genNumber in GENERATION_LABELS)) {
    throw new Error(
      `Generacion no soportada: ${genNumber}. Validas: ${Object.keys(GENERATION_LABELS).join(", ")}`,
    );
  }
  return Dex.mod(`gen${genNumber}`);
}

/**
 * Los mods de Showdown NO filtran por generacion: Dex.mod('gen6').species.all()
 * devuelve 1425 entradas, con el contenido posterior marcado isNonstandard:'Future'.
 * Este predicado es el unico filtro que separa una generacion de otra.
 */
export function isAvailable(
  dex: ModdedDex,
  entry: { gen: number; isNonstandard?: string | null },
): boolean {
  return entry.gen <= dex.gen && !entry.isNonstandard;
}

export function packageVersion(): string {
  return require("pokemon-showdown/package.json").version as string;
}
```

- [ ] **Step 6: Correr el test y verificar que pasa**

```bash
pnpm --filter @ludex/seed test
```
Esperado: PASS, 8 tests.

- [ ] **Step 7: Commit**

```bash
git add packages/seed pnpm-lock.yaml
git commit -m "feat(seed): paquete y filtro de disponibilidad por generacion"
```

---

## Task 5: Extractor de especies

**Files:**
- Create: `packages/seed/src/extract/species.ts`
- Test: `packages/seed/test/extract/species.test.ts`

**Interfaces:**
- Consumes: `loadGen`, `isAvailable`, `ModdedDex` de `extract/dex.ts`; `SpeciesRow` de `types.ts`.
- Produces: `extractSpecies(dex: ModdedDex): SpeciesRow[]`.

- [ ] **Step 1: Escribir el test que falla**

`packages/seed/test/extract/species.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractSpecies } from "../../src/extract/species.js";

const gen6 = extractSpecies(loadGen(6));
const gen9 = extractSpecies(loadGen(9));
const byId = (rows: typeof gen6, id: string) => rows.find((r) => r.showdownId === id)!;

describe("extractSpecies", () => {
  it("devuelve los conteos conocidos de gen 6", () => {
    expect(gen6).toHaveLength(834);
    expect(gen6.filter((s) => s.isDefault)).toHaveLength(721);
  });

  it("devuelve los conteos conocidos de gen 9", () => {
    expect(gen9).toHaveLength(874);
    expect(gen9.filter((s) => s.isDefault)).toHaveLength(733);
  });

  it("incluye megaevoluciones en gen 6 y ninguna en gen 9", () => {
    expect(gen6.filter((s) => s.forme?.startsWith("Mega"))).toHaveLength(48);
    expect(gen9.filter((s) => s.forme?.startsWith("Mega"))).toHaveLength(0);
  });

  it("separa forma base de forme", () => {
    const base = byId(gen6, "charizard");
    expect(base.forme).toBeNull();
    expect(base.isDefault).toBe(true);
    expect(base.baseSpecies).toBe("Charizard");

    const mega = byId(gen6, "charizardmegax");
    expect(mega.forme).toBe("Mega-X");
    expect(mega.isDefault).toBe(false);
    expect(mega.baseSpecies).toBe("Charizard");
    expect(mega.dexNum).toBe(base.dexNum);
  });

  it("mapea los campos escalares", () => {
    const base = byId(gen6, "charizard");
    expect(base.name).toBe("Charizard");
    expect(base.types).toEqual(["Fire", "Flying"]);
    expect(base.baseStats).toEqual({ hp: 78, atk: 84, def: 78, spa: 109, spd: 85, spe: 100 });
    expect(base.weightKg).toBe(90.5);
    expect(base.evolvesFrom).toBe("charmeleon");
    expect(base.tier).toBe("NU");
  });

  it("normaliza la ausencia de preevolucion a null", () => {
    expect(byId(gen6, "charmander").evolvesFrom).toBeNull();
    expect(byId(gen6, "charizardmegax").evolvesFrom).toBeNull();
  });

  it("aplica el cambio de tipo de la mega", () => {
    expect(byId(gen6, "charizardmegax").types).toEqual(["Fire", "Dragon"]);
  });

  it("excluye contenido futuro y nonstandard", () => {
    expect(gen6.some((s) => s.showdownId === "incineroar")).toBe(false);
    expect(gen6.some((s) => s.showdownId === "missingno")).toBe(false);
    expect(gen6.some((s) => s.showdownId.startsWith("pokestar"))).toBe(false);
  });

  it("no repite showdownId", () => {
    expect(new Set(gen6.map((s) => s.showdownId)).size).toBe(gen6.length);
  });
});
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
pnpm --filter @ludex/seed test species
```
Esperado: FAIL, no existe `src/extract/species.js`.

- [ ] **Step 3: Implementar**

`packages/seed/src/extract/species.ts`:
```ts
import { isAvailable, type ModdedDex } from "./dex.js";
import type { SpeciesRow } from "../types.js";

/** Showdown usa "" en vez de null para forme y prevo. */
const orNull = (value: string | undefined | null): string | null =>
  value ? value : null;

export function extractSpecies(dex: ModdedDex): SpeciesRow[] {
  return dex.species
    .all()
    .filter((s) => isAvailable(dex, s))
    .map((s) => ({
      showdownId: s.id,
      dexNum: s.num,
      name: s.name,
      baseSpecies: s.baseSpecies,
      forme: orNull(s.forme),
      isDefault: s.name === s.baseSpecies,
      types: [...s.types],
      baseStats: {
        hp: s.baseStats.hp, atk: s.baseStats.atk, def: s.baseStats.def,
        spa: s.baseStats.spa, spd: s.baseStats.spd, spe: s.baseStats.spe,
      },
      abilities: { ...s.abilities } as Record<string, string>,
      weightKg: typeof s.weightkg === "number" ? s.weightkg : null,
      evolvesFrom: orNull(s.prevo) ? dex.species.get(s.prevo).id : null,
      tier: orNull(s.tier),
    }));
}
```

- [ ] **Step 4: Correr y verificar que pasa**

```bash
pnpm --filter @ludex/seed test species
```
Esperado: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/seed
git commit -m "feat(seed): extractor de especies"
```

---

## Task 6: Extractores de movimientos, objetos y habilidades

**Files:**
- Create: `packages/seed/src/extract/moves.ts`, `packages/seed/src/extract/simple.ts`
- Test: `packages/seed/test/extract/moves.test.ts`, `packages/seed/test/extract/simple.test.ts`

**Interfaces:**
- Consumes: `isAvailable`, `ModdedDex`; `MoveRow`, `ItemRow`, `AbilityRow`.
- Produces: `extractMoves(dex): MoveRow[]`, `extractItems(dex): ItemRow[]`, `extractAbilities(dex): AbilityRow[]`.

- [ ] **Step 1: Escribir el test de movimientos**

`packages/seed/test/extract/moves.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractMoves } from "../../src/extract/moves.js";

const gen6 = extractMoves(loadGen(6));
const gen9 = extractMoves(loadGen(9));
const byId = (id: string) => gen6.find((m) => m.showdownId === id)!;

describe("extractMoves", () => {
  it("devuelve los conteos conocidos, ya deduplicados", () => {
    expect(gen6).toHaveLength(618);
    expect(gen9).toHaveLength(685);
  });

  it("colapsa los 17 Hidden Power de gen 6 en la entrada base", () => {
    // Los 17 (base + 16 tipos) comparten id 'hiddenpower'. La columna
    // moves.showdown_id es UNIQUE por generacion, asi que si extract no
    // deduplica, load colapsa a 618 igual y seed_runs.row_counts miente.
    const hp = gen6.filter((m) => m.showdownId === "hiddenpower");
    expect(hp).toHaveLength(1);
    expect(hp[0].name).toBe("Hidden Power");
    expect(hp[0].type).toBe("Normal");
    expect(hp[0].power).toBe(60);
    // En gen 9 el movimiento ya no existe.
    expect(gen9.some((m) => m.showdownId === "hiddenpower")).toBe(false);
  });

  it("mapea basePower al campo power", () => {
    const ft = byId("flamethrower");
    expect(ft.name).toBe("Flamethrower");
    expect(ft.type).toBe("Fire");
    expect(ft.category).toBe("Special");
    expect(ft.power).toBe(90);
    expect(ft.accuracy).toBe(100);
    expect(ft.pp).toBe(15);
    expect(ft.priority).toBe(0);
    expect(ft.target).toBe("normal");
    expect(ft.flags).toMatchObject({ protect: 1, mirror: 1 });
  });

  it("convierte accuracy true en null", () => {
    expect(byId("swift").accuracy).toBeNull();
  });

  it("guarda 0 de potencia para movimientos de estado", () => {
    const th = byId("thunderwave");
    expect(th.category).toBe("Status");
    expect(th.power).toBe(0);
  });

  it("no repite showdownId", () => {
    expect(new Set(gen6.map((m) => m.showdownId)).size).toBe(gen6.length);
  });
});
```

- [ ] **Step 2: Escribir el test de objetos y habilidades**

`packages/seed/test/extract/simple.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractAbilities, extractItems } from "../../src/extract/simple.js";

const items6 = extractItems(loadGen(6));
const items9 = extractItems(loadGen(9));
const abil6 = extractAbilities(loadGen(6));
const abil9 = extractAbilities(loadGen(9));

describe("extractItems", () => {
  it("devuelve los conteos conocidos", () => {
    expect(items6).toHaveLength(283);
    expect(items9).toHaveLength(248);
  });

  it("incluye piedras activadoras en gen 6 y ninguna en gen 9", () => {
    expect(items6.some((i) => i.showdownId === "charizarditex")).toBe(true);
    expect(items9.some((i) => i.showdownId === "charizarditex")).toBe(false);
  });

  it("mapea nombre y descripcion", () => {
    const lefties = items6.find((i) => i.showdownId === "leftovers")!;
    expect(lefties.name).toBe("Leftovers");
    expect(lefties.description).toContain("1/16");
  });
});

describe("extractAbilities", () => {
  it("devuelve los conteos conocidos", () => {
    expect(abil6).toHaveLength(191);
    expect(abil9).toHaveLength(310);
  });

  it("mapea nombre y descripcion", () => {
    const levitate = abil6.find((a) => a.showdownId === "levitate")!;
    expect(levitate.name).toBe("Levitate");
    expect(levitate.description).toBeTruthy();
  });
});
```

- [ ] **Step 3: Correr y verificar que fallan**

```bash
pnpm --filter @ludex/seed test moves simple
```
Esperado: FAIL, módulos inexistentes.

- [ ] **Step 4: Implementar los movimientos**

`packages/seed/src/extract/moves.ts`:
```ts
import { isAvailable, type ModdedDex } from "./dex.js";
import type { MoveRow } from "../types.js";

/**
 * En gen 6 los 17 Hidden Power (base + 16 tipos) comparten id 'hiddenpower'.
 * Se conserva la entrada cuyo nombre normaliza a su propio id, que es la base
 * ("Hidden Power", Normal, 60) — deterministico, sin depender del orden de
 * iteracion. Las variantes tipadas son alias de presentacion: el tipo real lo
 * determinan los IVs del pokemon, no la entrada del movimiento, y el protocolo
 * de batalla siempre reporta 'Hidden Power'.
 */
function dedupeById(dex: ModdedDex, moves: readonly { id: string; name: string }[]) {
  const byId = new Map<string, { id: string; name: string }>();
  for (const m of moves) {
    const existing = byId.get(m.id);
    if (!existing || dex.toID(m.name) === m.id) byId.set(m.id, m);
  }
  return [...byId.values()];
}

export function extractMoves(dex: ModdedDex): MoveRow[] {
  const available = dex.moves.all().filter((m) => isAvailable(dex, m));
  return (dedupeById(dex, available) as typeof available)
    .map((m) => ({
      showdownId: m.id,
      name: m.name,
      type: m.type,
      category: m.category,
      power: m.basePower,
      // Showdown usa true para "nunca falla".
      accuracy: m.accuracy === true ? null : m.accuracy,
      pp: m.pp,
      priority: m.priority,
      target: m.target,
      flags: { ...m.flags } as Record<string, number>,
      description: m.desc || m.shortDesc || null,
    }));
}
```

- [ ] **Step 5: Implementar objetos y habilidades**

`packages/seed/src/extract/simple.ts`:
```ts
import { isAvailable, type ModdedDex } from "./dex.js";
import type { AbilityRow, ItemRow } from "../types.js";

export function extractItems(dex: ModdedDex): ItemRow[] {
  return dex.items
    .all()
    .filter((i) => isAvailable(dex, i))
    .map((i) => ({
      showdownId: i.id,
      name: i.name,
      description: i.desc || i.shortDesc || null,
      flags: { ...(i.flags ?? {}) } as Record<string, unknown>,
    }));
}

export function extractAbilities(dex: ModdedDex): AbilityRow[] {
  return dex.abilities
    .all()
    .filter((a) => isAvailable(dex, a))
    .map((a) => ({
      showdownId: a.id,
      name: a.name,
      description: a.desc || a.shortDesc || null,
    }));
}
```

- [ ] **Step 6: Correr y verificar que pasan**

```bash
pnpm --filter @ludex/seed test moves simple
```
Esperado: PASS, 8 tests.

- [ ] **Step 7: Commit**

```bash
git add packages/seed
git commit -m "feat(seed): extractores de movimientos, objetos y habilidades"
```

---

## Task 7: Extractor de tabla de tipos

**Files:**
- Create: `packages/seed/src/extract/typechart.ts`
- Test: `packages/seed/test/extract/typechart.test.ts`

**Interfaces:**
- Consumes: `ModdedDex`; `TypeChartRow`.
- Produces: `typesForGen(dex: ModdedDex): string[]`, `extractTypeChart(dex: ModdedDex): TypeChartRow[]`.

- [ ] **Step 1: Escribir el test que falla**

`packages/seed/test/extract/typechart.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractTypeChart, typesForGen } from "../../src/extract/typechart.js";

const mult = (rows: ReturnType<typeof extractTypeChart>, atk: string, def: string) =>
  rows.find((r) => r.attackingType === atk && r.defendingType === def)!.multiplier;

describe("typesForGen", () => {
  it("deriva la lista de tipos por generacion sin hardcodearla", () => {
    expect(typesForGen(loadGen(1))).toHaveLength(15);
    expect(typesForGen(loadGen(2))).toHaveLength(17);
    expect(typesForGen(loadGen(5))).toHaveLength(17);
    expect(typesForGen(loadGen(6))).toHaveLength(18);
    expect(typesForGen(loadGen(9))).toHaveLength(19);
  });

  it("introduce Hada en gen 6 y Stellar en gen 9", () => {
    expect(typesForGen(loadGen(5))).not.toContain("Fairy");
    expect(typesForGen(loadGen(6))).toContain("Fairy");
    expect(typesForGen(loadGen(6))).not.toContain("Stellar");
    expect(typesForGen(loadGen(9))).toContain("Stellar");
  });

  it("excluye las claves de damageTaken que no son tipos", () => {
    for (const t of typesForGen(loadGen(6))) {
      expect(["psn", "tox", "sandstorm", "hail", "powder", "frz", "par"]).not.toContain(t);
    }
  });
});

describe("extractTypeChart", () => {
  const gen5 = extractTypeChart(loadGen(5));
  const gen6 = extractTypeChart(loadGen(6));

  it("produce la matriz completa", () => {
    expect(gen6).toHaveLength(324); // 18 x 18
    expect(gen5).toHaveLength(289); // 17 x 17
    expect(extractTypeChart(loadGen(9))).toHaveLength(361); // 19 x 19
  });

  it("traduce los codigos de damageTaken a multiplicadores", () => {
    expect(mult(gen6, "Fire", "Grass")).toBe(2);
    expect(mult(gen6, "Fire", "Water")).toBe(0.5);
    expect(mult(gen6, "Normal", "Normal")).toBe(1);
    expect(mult(gen6, "Normal", "Ghost")).toBe(0);
  });

  it("aplica la inmunidad de Hada a Dragon", () => {
    expect(mult(gen6, "Dragon", "Fairy")).toBe(0);
    expect(mult(gen6, "Fairy", "Dragon")).toBe(2);
  });

  it("quita las resistencias de Acero a Siniestro y Fantasma en gen 6", () => {
    expect(mult(gen5, "Dark", "Steel")).toBe(0.5);
    expect(mult(gen5, "Ghost", "Steel")).toBe(0.5);
    expect(mult(gen6, "Dark", "Steel")).toBe(1);
    expect(mult(gen6, "Ghost", "Steel")).toBe(1);
  });
});
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
pnpm --filter @ludex/seed test typechart
```
Esperado: FAIL, módulo inexistente.

- [ ] **Step 3: Implementar**

`packages/seed/src/extract/typechart.ts`:
```ts
import type { ModdedDex } from "./dex.js";
import type { TypeChartRow } from "../types.js";

/** Codigos de damageTaken de Showdown. */
const MULTIPLIER_BY_CODE: Record<number, number> = {
  0: 1,    // dano normal
  1: 2,    // super efectivo
  2: 0.5,  // resiste
  3: 0,    // inmune
};

/**
 * types.all() devuelve 19 tipos en TODAS las generaciones, asi que no sirve.
 * La interseccion de las claves de damageTaken de todos los tipos si da la
 * lista correcta por generacion: 15 en gen 1, 17 en gen 2 y 5, 18 en gen 6,
 * 19 en gen 9. Ademas descarta claves que no son tipos (psn, tox, sandstorm).
 */
export function typesForGen(dex: ModdedDex): string[] {
  const keySets = dex.types.all().map(
    (t) => new Set(Object.keys(t.damageTaken).filter((k) => dex.types.get(k).exists)),
  );
  if (keySets.length === 0) return [];
  let shared = keySets[0];
  for (const keys of keySets.slice(1)) {
    shared = new Set([...shared].filter((k) => keys.has(k)));
  }
  return [...shared].sort();
}

export function extractTypeChart(dex: ModdedDex): TypeChartRow[] {
  const types = typesForGen(dex);
  const rows: TypeChartRow[] = [];
  for (const defendingType of types) {
    const damageTaken = dex.types.get(defendingType).damageTaken;
    for (const attackingType of types) {
      const code = damageTaken[attackingType];
      if (code === undefined) {
        throw new Error(
          `gen${dex.gen}: falta damageTaken[${attackingType}] en el tipo ${defendingType}`,
        );
      }
      const multiplier = MULTIPLIER_BY_CODE[code];
      if (multiplier === undefined) {
        throw new Error(
          `gen${dex.gen}: codigo de damageTaken desconocido ${code} en ${attackingType}->${defendingType}`,
        );
      }
      rows.push({ attackingType, defendingType, multiplier });
    }
  }
  return rows;
}
```

- [ ] **Step 4: Correr y verificar que pasa**

```bash
pnpm --filter @ludex/seed test typechart
```
Esperado: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/seed
git commit -m "feat(seed): tabla de tipos derivada por generacion"
```

---

## Task 8: Extractor de learnsets con herencia

Es la tarea con más riesgo del plan. Los tres puntos donde se rompe: el filtro por generación de cada código, la herencia por `baseSpecies` (las formas no tienen learnset propio) y la herencia por cadena `prevo`.

**Files:**
- Create: `packages/seed/src/extract/learnsets.ts`
- Test: `packages/seed/test/extract/learnsets.test.ts`

**Interfaces:**
- Consumes: `isAvailable`, `ModdedDex`; `LearnMethod`, `LearnMethodName`, `LearnsetRow`.
- Produces:
  - `parseLearnCode(code: string, sourceSpecies: string): LearnMethod`
  - `extractLearnsets(dex: ModdedDex): Promise<LearnsetRow[]>` (async: `getLearnsetData` lo es)

- [ ] **Step 1: Escribir el test que falla**

`packages/seed/test/extract/learnsets.test.ts`:
```ts
import { beforeAll, describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractLearnsets, parseLearnCode } from "../../src/extract/learnsets.js";
import type { LearnsetRow } from "../../src/types.js";

describe("parseLearnCode", () => {
  it("parsea nivel con su numero", () => {
    expect(parseLearnCode("6L47", "charizard")).toEqual({
      gen: 6, method: "level", level: 47, sourceSpecies: "charizard",
    });
  });

  it("parsea metodos sin argumento", () => {
    expect(parseLearnCode("6M", "charizard")).toEqual({
      gen: 6, method: "machine", sourceSpecies: "charizard",
    });
    expect(parseLearnCode("5T", "charmander")).toEqual({
      gen: 5, method: "tutor", sourceSpecies: "charmander",
    });
    expect(parseLearnCode("6E", "charmander")).toEqual({
      gen: 6, method: "egg", sourceSpecies: "charmander",
    });
  });

  it("ignora el sufijo numerico de los eventos", () => {
    expect(parseLearnCode("6S5", "charizard")).toEqual({
      gen: 6, method: "event", sourceSpecies: "charizard",
    });
  });

  it("falla ruidosamente ante un codigo desconocido", () => {
    expect(() => parseLearnCode("6Z", "charizard")).toThrow(/desconocido/i);
    expect(() => parseLearnCode("basura", "charizard")).toThrow(/invalido/i);
  });
});

describe("extractLearnsets gen 6", () => {
  let rows: LearnsetRow[];
  const of = (speciesId: string, moveId: string) =>
    rows.find((r) => r.speciesId === speciesId && r.moveId === moveId);

  beforeAll(async () => {
    rows = await extractLearnsets(loadGen(6));
  }, 180_000);

  it("descarta los codigos de generaciones futuras", () => {
    const ft = of("charizard", "flamethrower")!;
    expect(ft.methods.every((m) => m.gen <= 6)).toBe(true);
    expect(ft.methods).toContainEqual({ gen: 6, method: "machine", sourceSpecies: "charizard" });
    expect(ft.methods).toContainEqual({ gen: 6, method: "level", level: 47, sourceSpecies: "charizard" });
  });

  it("conserva metodos de generaciones anteriores sin aplanarlos", () => {
    const ft = of("charizard", "flamethrower")!;
    expect(ft.methods).toContainEqual({ gen: 5, method: "machine", sourceSpecies: "charizard" });
    expect(ft.methods.filter((m) => m.gen === 6).length).toBeGreaterThan(0);
    expect(ft.methods.filter((m) => m.gen < 6).length).toBeGreaterThan(0);
  });

  it("hereda movimientos huevo desde la preevolucion", () => {
    // Charizard no aprende Dragon Dance por si mismo en gen 6: sus codigos son
    // 9M, 8M y 7S9, todos posteriores. Charmander la tiene como 6E.
    const dd = of("charizard", "dragondance")!;
    expect(dd).toBeDefined();
    expect(dd.methods).toContainEqual({ gen: 6, method: "egg", sourceSpecies: "charmander" });
    expect(dd.methods.every((m) => m.sourceSpecies !== "charizard")).toBe(true);
  });

  it("da a las formas el learnset de su especie base", () => {
    const megaMoves = rows.filter((r) => r.speciesId === "charizardmegax");
    const baseMoves = rows.filter((r) => r.speciesId === "charizard");
    expect(megaMoves.length).toBe(baseMoves.length);
    expect(megaMoves.length).toBeGreaterThan(100);
    expect(of("charizardmegax", "dragondance")).toBeDefined();
  });

  it("no genera filas para especies fuera de la generacion", () => {
    expect(rows.some((r) => r.speciesId === "incineroar")).toBe(false);
  });

  it("no genera filas para movimientos fuera de la generacion", () => {
    const gen6MoveIds = new Set(
      loadGen(6).moves.all().filter((m) => m.gen <= 6 && !m.isNonstandard).map((m) => m.id),
    );
    expect(rows.every((r) => gen6MoveIds.has(r.moveId))).toBe(true);
  });

  it("no repite el par (especie, movimiento)", () => {
    const keys = rows.map((r) => `${r.speciesId}|${r.moveId}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("nunca deja methods vacio", () => {
    expect(rows.every((r) => r.methods.length > 0)).toBe(true);
  });
});
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
pnpm --filter @ludex/seed test learnsets
```
Esperado: FAIL, módulo inexistente.

- [ ] **Step 3: Implementar**

`packages/seed/src/extract/learnsets.ts`:
```ts
import { isAvailable, type ModdedDex } from "./dex.js";
import type { LearnMethod, LearnMethodName, LearnsetRow } from "../types.js";

const METHOD_BY_LETTER: Record<string, LearnMethodName> = {
  L: "level",
  M: "machine",
  T: "tutor",
  E: "egg",
  S: "event",
  D: "dream",
  V: "transfer",
  C: "tradeback",
  R: "reminder",
};

/** Formato: <gen><letra><resto>. Ej: "6L47", "6M", "6S5", "9M". */
export function parseLearnCode(code: string, sourceSpecies: string): LearnMethod {
  const match = /^(\d)([A-Z])(\d*)$/.exec(code);
  if (!match) {
    throw new Error(`Codigo de aprendizaje invalido: ${JSON.stringify(code)}`);
  }
  const [, genPart, letter, rest] = match;
  const method = METHOD_BY_LETTER[letter];
  if (!method) {
    throw new Error(
      `Metodo de aprendizaje desconocido ${JSON.stringify(letter)} en el codigo ${code}. ` +
        `Conocidos: ${Object.keys(METHOD_BY_LETTER).join(", ")}. ` +
        `Si el paquete agrego un codigo nuevo, mapearlo antes de seedear.`,
    );
  }
  const parsed: LearnMethod = { gen: Number(genPart), method, sourceSpecies };
  // Solo el nivel usa el sufijo numerico; en los eventos es el indice y se descarta.
  if (method === "level" && rest !== "") parsed.level = Number(rest);
  return parsed;
}

/**
 * Las formas (megas, Deoxys-Attack) no tienen learnset propio y su prevo es "".
 * Hay que resolver primero a baseSpecies y despues caminar la cadena prevo.
 */
function inheritanceChain(dex: ModdedDex, speciesId: string): string[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  let current = dex.species.get(speciesId);
  if (current.name !== current.baseSpecies) current = dex.species.get(current.baseSpecies);
  while (current?.exists && !seen.has(current.id)) {
    seen.add(current.id);
    chain.push(current.id);
    if (!current.prevo) break;
    current = dex.species.get(current.prevo);
  }
  return chain;
}

export async function extractLearnsets(dex: ModdedDex): Promise<LearnsetRow[]> {
  const species = dex.species.all().filter((s) => isAvailable(dex, s));
  const legalMoveIds = new Set(
    dex.moves.all().filter((m) => isAvailable(dex, m)).map((m) => m.id),
  );

  // Cache: la mega y la base comparten cadena, no vale la pena releer el archivo.
  const directCache = new Map<string, Record<string, string[]>>();
  const directLearnset = async (id: string): Promise<Record<string, string[]>> => {
    const cached = directCache.get(id);
    if (cached) return cached;
    const data = await dex.species.getLearnsetData(id);
    const learnset = (data?.learnset ?? {}) as Record<string, string[]>;
    directCache.set(id, learnset);
    return learnset;
  };

  const rows: LearnsetRow[] = [];
  for (const s of species) {
    const methodsByMove = new Map<string, LearnMethod[]>();
    for (const ancestorId of inheritanceChain(dex, s.id)) {
      const learnset = await directLearnset(ancestorId);
      for (const [moveId, codes] of Object.entries(learnset)) {
        if (!legalMoveIds.has(moveId)) continue;
        for (const code of codes) {
          const method = parseLearnCode(code, ancestorId);
          if (method.gen > dex.gen) continue;
          const existing = methodsByMove.get(moveId);
          if (existing) existing.push(method);
          else methodsByMove.set(moveId, [method]);
        }
      }
    }
    for (const [moveId, methods] of methodsByMove) {
      rows.push({ speciesId: s.id, moveId, methods });
    }
  }
  return rows;
}
```

- [ ] **Step 4: Correr y verificar que pasa**

```bash
pnpm --filter @ludex/seed test learnsets
```
Esperado: PASS, 13 tests. Tarda uno o dos minutos: lee el learnset de 834 especies.

- [ ] **Step 5: Registrar el volumen real**

```bash
pnpm --filter @ludex/seed exec tsx -e "
import { loadGen } from './src/extract/dex.js';
import { extractLearnsets } from './src/extract/learnsets.js';
const rows = await extractLearnsets(loadGen(6));
console.log('filas resueltas gen6:', rows.length);
"
```

Anotar el número en `DECISIONS.md` junto a los 49321 pares directos, como referencia para detectar regresiones de la herencia.

- [ ] **Step 6: Commit**

```bash
git add packages/seed docs/DECISIONS.md
git commit -m "feat(seed): learnsets con herencia por forma y preevolucion"
```

---

## Task 9: Capa de carga a Postgres

**Files:**
- Create: `packages/seed/src/load/client.ts`, `packages/seed/src/load/tables.ts`, `packages/seed/src/load/runs.ts`
- Test: `packages/seed/test/load/client.test.ts`

**Interfaces:**
- Consumes: las interfaces de fila de `types.ts`. **No importa `pokemon-showdown`.**
- Produces:
  - `withPool<T>(fn: (pool: Pool) => Promise<T>): Promise<T>`
  - `upsertBatch(pool, opts: { table: string; columns: string[]; conflict: string[]; rows: unknown[][] }): Promise<number>`
  - `upsertGeneration(pool, genNumber: number, label: string): Promise<number>` → devuelve `generations.id`
  - `loadSpecies`, `loadMoves`, `loadItems`, `loadAbilities`, `loadTypeChart`, `loadLearnsets`
  - `startRun(pool, genId, packageVersion): Promise<number>`, `finishRun(pool, runId, counts): Promise<void>`

- [ ] **Step 1: Escribir el test que falla**

Necesita la base levantada. Usa una tabla temporal propia para no depender del esquema real.

`packages/seed/test/load/client.test.ts`:
```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Pool } from "pg";
import { createPool, upsertBatch } from "../../src/load/client.js";

describe("upsertBatch", () => {
  let pool: Pool;

  beforeAll(async () => {
    pool = createPool();
    await pool.query(`
      CREATE TABLE IF NOT EXISTS upsert_probe (
        id serial PRIMARY KEY,
        k  text NOT NULL,
        v  text NOT NULL,
        UNIQUE (k)
      )`);
    await pool.query("TRUNCATE upsert_probe");
  });

  afterAll(async () => {
    await pool.query("DROP TABLE IF EXISTS upsert_probe");
    await pool.end();
  });

  it("inserta filas nuevas", async () => {
    const n = await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"],
      rows: [["a", "1"], ["b", "2"]],
    });
    expect(n).toBe(2);
  });

  it("actualiza en vez de duplicar cuando la clave ya existe", async () => {
    await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"],
      rows: [["a", "MODIFICADO"]],
    });
    const { rows } = await pool.query("SELECT k, v FROM upsert_probe ORDER BY k");
    expect(rows).toEqual([{ k: "a", v: "MODIFICADO" }, { k: "b", v: "2" }]);
  });

  it("corta en lotes sin pasarse del limite de parametros de Postgres", async () => {
    const many = Array.from({ length: 5000 }, (_, i) => [`k${i}`, `v${i}`]);
    const n = await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"], rows: many,
    });
    expect(n).toBe(5000);
    const { rows } = await pool.query("SELECT count(*)::int AS c FROM upsert_probe");
    expect(rows[0].c).toBe(5002);
  });

  it("no rompe con cero filas", async () => {
    expect(await upsertBatch(pool, {
      table: "upsert_probe", columns: ["k", "v"], conflict: ["k"], rows: [],
    })).toBe(0);
  });
});
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
docker compose up -d postgres
pnpm --filter @ludex/seed test load
```
Esperado: FAIL, no existe `src/load/client.js`.

- [ ] **Step 3: Implementar el cliente**

`packages/seed/src/load/client.ts`:
```ts
import { Pool } from "pg";

/** Postgres admite 65535 parametros por sentencia; se deja margen. */
const MAX_PARAMS = 30_000;

export function createPool(): Pool {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("Falta DATABASE_URL. Copiar .env.example a .env.");
  }
  return new Pool({ connectionString });
}

export async function withPool<T>(fn: (pool: Pool) => Promise<T>): Promise<T> {
  const pool = createPool();
  try {
    return await fn(pool);
  } finally {
    await pool.end();
  }
}

export interface UpsertOptions {
  table: string;
  columns: string[];
  conflict: string[];
  rows: unknown[][];
  /** Columnas a no pisar en el UPDATE. Por defecto se pisan todas menos las de conflicto. */
  updateColumns?: string[];
}

export async function upsertBatch(pool: Pool, opts: UpsertOptions): Promise<number> {
  const { table, columns, conflict, rows } = opts;
  if (rows.length === 0) return 0;

  const updateColumns =
    opts.updateColumns ?? columns.filter((c) => !conflict.includes(c));
  const setClause =
    updateColumns.length > 0
      ? `DO UPDATE SET ${updateColumns.map((c) => `${c} = EXCLUDED.${c}`).join(", ")}`
      : "DO NOTHING";

  const batchSize = Math.max(1, Math.floor(MAX_PARAMS / columns.length));
  let written = 0;

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    for (let start = 0; start < rows.length; start += batchSize) {
      const batch = rows.slice(start, start + batchSize);
      const values: unknown[] = [];
      const tuples = batch.map((row, r) => {
        const placeholders = row.map((_, c) => `$${r * columns.length + c + 1}`);
        values.push(...row);
        return `(${placeholders.join(", ")})`;
      });
      await client.query(
        `INSERT INTO ${table} (${columns.join(", ")}) VALUES ${tuples.join(", ")}
         ON CONFLICT (${conflict.join(", ")}) ${setClause}`,
        values,
      );
      written += batch.length;
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
  return written;
}
```

- [ ] **Step 4: Correr y verificar que pasa**

```bash
pnpm --filter @ludex/seed test load
```
Esperado: PASS, 4 tests.

- [ ] **Step 5: Implementar las cargas por tabla**

`packages/seed/src/load/tables.ts`:
```ts
import type { Pool } from "pg";
import { upsertBatch } from "./client.js";
import type {
  AbilityRow, ItemRow, LearnsetRow, MoveRow, SpeciesRow, TypeChartRow,
} from "../types.js";

export async function upsertGeneration(
  pool: Pool, genNumber: number, label: string,
): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    `INSERT INTO generations (gen_number, label) VALUES ($1, $2)
     ON CONFLICT (gen_number) DO UPDATE SET label = EXCLUDED.label
     RETURNING id`,
    [genNumber, label],
  );
  return rows[0].id;
}

export const loadSpecies = (pool: Pool, genId: number, rows: SpeciesRow[]) =>
  upsertBatch(pool, {
    table: "pokemon",
    columns: ["gen_id", "showdown_id", "dex_num", "name", "base_species", "forme",
      "is_default", "types", "base_stats", "abilities", "weight_kg", "evolves_from", "tier"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((s) => [genId, s.showdownId, s.dexNum, s.name, s.baseSpecies, s.forme,
      s.isDefault, s.types, JSON.stringify(s.baseStats), JSON.stringify(s.abilities),
      s.weightKg, s.evolvesFrom, s.tier]),
  });

export const loadMoves = (pool: Pool, genId: number, rows: MoveRow[]) =>
  upsertBatch(pool, {
    table: "moves",
    columns: ["gen_id", "showdown_id", "name", "type", "category", "power",
      "accuracy", "pp", "priority", "target", "flags", "description"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((m) => [genId, m.showdownId, m.name, m.type, m.category, m.power,
      m.accuracy, m.pp, m.priority, m.target, JSON.stringify(m.flags), m.description]),
  });

export const loadItems = (pool: Pool, genId: number, rows: ItemRow[]) =>
  upsertBatch(pool, {
    table: "items",
    columns: ["gen_id", "showdown_id", "name", "description", "flags"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((i) => [genId, i.showdownId, i.name, i.description, JSON.stringify(i.flags)]),
  });

export const loadAbilities = (pool: Pool, genId: number, rows: AbilityRow[]) =>
  upsertBatch(pool, {
    table: "abilities",
    columns: ["gen_id", "showdown_id", "name", "description"],
    conflict: ["gen_id", "showdown_id"],
    rows: rows.map((a) => [genId, a.showdownId, a.name, a.description]),
  });

export const loadTypeChart = (pool: Pool, genId: number, rows: TypeChartRow[]) =>
  upsertBatch(pool, {
    table: "type_chart",
    columns: ["gen_id", "attacking_type", "defending_type", "multiplier"],
    conflict: ["gen_id", "attacking_type", "defending_type"],
    rows: rows.map((t) => [genId, t.attackingType, t.defendingType, t.multiplier]),
  });

/** Necesita los ids ya insertados de pokemon y moves. */
export async function loadLearnsets(
  pool: Pool, genId: number, rows: LearnsetRow[],
): Promise<number> {
  const idsFor = async (table: string) => {
    const { rows: found } = await pool.query<{ showdown_id: string; id: number }>(
      `SELECT showdown_id, id FROM ${table} WHERE gen_id = $1`, [genId],
    );
    return new Map(found.map((r) => [r.showdown_id, r.id]));
  };
  const speciesIds = await idsFor("pokemon");
  const moveIds = await idsFor("moves");

  const tuples = rows.map((r) => {
    const speciesId = speciesIds.get(r.speciesId);
    const moveId = moveIds.get(r.moveId);
    if (speciesId === undefined) throw new Error(`Especie no cargada: ${r.speciesId}`);
    if (moveId === undefined) throw new Error(`Movimiento no cargado: ${r.moveId}`);
    return [speciesId, moveId, JSON.stringify(r.methods)];
  });

  return upsertBatch(pool, {
    table: "learnsets",
    columns: ["pokemon_id", "move_id", "learn_methods"],
    conflict: ["pokemon_id", "move_id"],
    rows: tuples,
  });
}
```

- [ ] **Step 6: Implementar `seed_runs`**

`packages/seed/src/load/runs.ts`:
```ts
import type { Pool } from "pg";

export async function startRun(
  pool: Pool, genId: number, packageVersion: string,
): Promise<number> {
  const { rows } = await pool.query<{ id: number }>(
    `INSERT INTO seed_runs (gen_id, package_version, started_at)
     VALUES ($1, $2, now()) RETURNING id`,
    [genId, packageVersion],
  );
  return rows[0].id;
}

export async function finishRun(
  pool: Pool, runId: number, counts: Record<string, number>,
): Promise<void> {
  await pool.query(
    `UPDATE seed_runs SET finished_at = now(), row_counts = $2 WHERE id = $1`,
    [runId, JSON.stringify(counts)],
  );
}
```

- [ ] **Step 7: Verificar el corte de capas**

```bash
grep -rn "pokemon-showdown" packages/seed/src/load/ && echo "FALLA: load/ importa el Dex" || echo "OK: load/ no conoce el Dex"
grep -rn "from \"pg\"" packages/seed/src/extract/ && echo "FALLA: extract/ importa pg" || echo "OK: extract/ no conoce la DB"
```
Esperado: las dos líneas `OK`.

- [ ] **Step 8: Commit**

```bash
git add packages/seed
git commit -m "feat(seed): capa de carga con upserts en lote"
```

---

## Task 10: CLI, pipeline e integración

**Files:**
- Create: `packages/seed/src/cli.ts`
- Test: `packages/seed/test/load/integration.test.ts`

**Interfaces:**
- Consumes: todos los extractores y cargadores de las tareas 4 a 9.
- Produces: el comando `pnpm seed --gen <n>` y la función `seedGeneration(genNumber: number): Promise<Record<string, number>>`.

- [ ] **Step 1: Escribir el test de integración**

`packages/seed/test/load/integration.test.ts`:
```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Pool } from "pg";
import { createPool } from "../../src/load/client.js";
import { seedGeneration } from "../../src/cli.js";

const GEN6_COUNTS = {
  pokemon: 834, moves: 618, items: 283, abilities: 191, typeChart: 324,
};

describe("seedGeneration", () => {
  let pool: Pool;
  let counts: Record<string, number>;

  beforeAll(async () => {
    pool = createPool();
    counts = await seedGeneration(6);
  }, 600_000);

  afterAll(async () => { await pool.end(); });

  it("carga los conteos conocidos de gen 6", async () => {
    expect(counts).toMatchObject(GEN6_COUNTS);
    const genId = (await pool.query("SELECT id FROM generations WHERE gen_number = 6")).rows[0].id;
    for (const [table, expected] of [
      ["pokemon", GEN6_COUNTS.pokemon], ["moves", GEN6_COUNTS.moves],
      ["items", GEN6_COUNTS.items], ["abilities", GEN6_COUNTS.abilities],
      ["type_chart", GEN6_COUNTS.typeChart],
    ] as const) {
      const { rows } = await pool.query(
        `SELECT count(*)::int AS c FROM ${table} WHERE gen_id = $1`, [genId],
      );
      expect({ table, c: rows[0].c }).toEqual({ table, c: expected });
    }
  });

  it("registra la corrida con la version del paquete", async () => {
    const { rows } = await pool.query(
      `SELECT package_version, finished_at, row_counts FROM seed_runs ORDER BY id DESC LIMIT 1`,
    );
    expect(rows[0].package_version).toBe("0.11.10");
    expect(rows[0].finished_at).not.toBeNull();
    expect(rows[0].row_counts).toMatchObject(GEN6_COUNTS);
  });

  it("preserva los metodos de learnset sin aplanarlos", async () => {
    const { rows } = await pool.query(`
      SELECT l.learn_methods FROM learnsets l
      JOIN pokemon p ON p.id = l.pokemon_id
      JOIN moves   m ON m.id = l.move_id
      JOIN generations g ON g.id = p.gen_id
      WHERE g.gen_number = 6 AND p.showdown_id = 'charizard' AND m.showdown_id = 'dragondance'`);
    expect(rows).toHaveLength(1);
    expect(rows[0].learn_methods).toContainEqual({
      gen: 6, method: "egg", sourceSpecies: "charmander",
    });
  });

  it("ACTUALIZA las filas existentes al reseedear, no las ignora", async () => {
    // Comparar solo conteos no prueba nada: pasa igual si los upserts insertan
    // duplicados que una constraint descarta en silencio. Hay que mutar y verificar.
    const genId = (await pool.query("SELECT id FROM generations WHERE gen_number = 6")).rows[0].id;
    await pool.query(
      `UPDATE pokemon SET tier = 'BOGUS', weight_kg = 999 WHERE gen_id = $1 AND showdown_id = 'charizard'`,
      [genId],
    );
    await pool.query(
      `UPDATE moves SET power = 1 WHERE gen_id = $1 AND showdown_id = 'flamethrower'`,
      [genId],
    );

    await seedGeneration(6);

    const poke = await pool.query(
      `SELECT tier, weight_kg FROM pokemon WHERE gen_id = $1 AND showdown_id = 'charizard'`, [genId],
    );
    expect(poke.rows[0].tier).toBe("NU");
    expect(Number(poke.rows[0].weight_kg)).toBe(90.5);

    const move = await pool.query(
      `SELECT power FROM moves WHERE gen_id = $1 AND showdown_id = 'flamethrower'`, [genId],
    );
    expect(move.rows[0].power).toBe(90);

    const total = await pool.query(
      `SELECT count(*)::int AS c FROM pokemon WHERE gen_id = $1`, [genId],
    );
    expect(total.rows[0].c).toBe(GEN6_COUNTS.pokemon);
  }, 600_000);

  it("convive con otra generacion sin colisionar", async () => {
    await seedGeneration(9);
    const { rows } = await pool.query(`
      SELECT g.gen_number, count(*)::int AS c
      FROM pokemon p JOIN generations g ON g.id = p.gen_id
      GROUP BY g.gen_number ORDER BY g.gen_number`);
    expect(rows).toEqual([
      { gen_number: 6, c: 834 },
      { gen_number: 9, c: 874 },
    ]);
  }, 900_000);
});
```

- [ ] **Step 2: Correr y verificar que falla**

```bash
pnpm --filter @ludex/seed test integration
```
Esperado: FAIL, no existe `src/cli.js`.

- [ ] **Step 3: Implementar el CLI**

`packages/seed/src/cli.ts`:
```ts
import { parseArgs } from "node:util";
import { GENERATION_LABELS, loadGen, packageVersion } from "./extract/dex.js";
import { extractSpecies } from "./extract/species.js";
import { extractMoves } from "./extract/moves.js";
import { extractAbilities, extractItems } from "./extract/simple.js";
import { extractTypeChart } from "./extract/typechart.js";
import { extractLearnsets } from "./extract/learnsets.js";
import { withPool } from "./load/client.js";
import {
  loadAbilities, loadItems, loadLearnsets, loadMoves, loadSpecies,
  loadTypeChart, upsertGeneration,
} from "./load/tables.js";
import { finishRun, startRun } from "./load/runs.js";

export async function seedGeneration(genNumber: number): Promise<Record<string, number>> {
  const dex = loadGen(genNumber);
  const label = GENERATION_LABELS[genNumber];
  const version = packageVersion();

  console.log(`Seedeando gen ${genNumber} (${label}) con pokemon-showdown@${version}`);

  const species = extractSpecies(dex);
  const moves = extractMoves(dex);
  const items = extractItems(dex);
  const abilities = extractAbilities(dex);
  const typeChart = extractTypeChart(dex);
  console.log("Resolviendo learnsets con herencia...");
  const learnsets = await extractLearnsets(dex);

  return withPool(async (pool) => {
    const genId = await upsertGeneration(pool, genNumber, label);
    const runId = await startRun(pool, genId, version);

    const counts: Record<string, number> = {
      pokemon: await loadSpecies(pool, genId, species),
      moves: await loadMoves(pool, genId, moves),
      items: await loadItems(pool, genId, items),
      abilities: await loadAbilities(pool, genId, abilities),
      typeChart: await loadTypeChart(pool, genId, typeChart),
      learnsets: await loadLearnsets(pool, genId, learnsets),
    };

    await finishRun(pool, runId, counts);
    for (const [table, n] of Object.entries(counts)) {
      console.log(`  ${table.padEnd(12)} ${n}`);
    }
    return counts;
  });
}

async function main(): Promise<void> {
  const { values } = parseArgs({ options: { gen: { type: "string" } } });
  if (!values.gen) {
    console.error("Uso: pnpm seed --gen <n>");
    process.exit(1);
  }
  await seedGeneration(Number(values.gen));
}

// Solo corre como CLI, no cuando lo importa un test.
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop()!)) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

- [ ] **Step 4: Correr el seed a mano**

```bash
docker compose up -d postgres
docker compose run --rm migrate up
pnpm seed --gen 6
```
Esperado: imprime los conteos `pokemon 834`, `moves 634`, `items 283`, `abilities 191`, `typeChart 324`, más el total de learnsets.

- [ ] **Step 5: Correr el test de integración**

```bash
pnpm --filter @ludex/seed test integration
```
Esperado: PASS, 5 tests. Tarda varios minutos porque seedea gen 6 dos veces y gen 9 una.

- [ ] **Step 6: Commit**

```bash
git add packages/seed
git commit -m "feat(seed): CLI y pipeline completo multi-generacion"
```

---

## Task 11: Golden files y cierre de la rebanada

**Files:**
- Create: `packages/seed/test/extract/golden.test.ts`
- Create: `packages/seed/test/__snapshots__/golden.test.ts.snap` (lo genera vitest)
- Modify: `docs/DECISIONS.md`, `db/schema.sql`

**Interfaces:**
- Consumes: todos los extractores.
- Produces: snapshots commiteados que detectan cambios de forma al subir la versión del paquete.

- [ ] **Step 1: Escribir el test de golden files**

Los conteos detectan que algo cambió; los snapshots dicen **qué**.

`packages/seed/test/extract/golden.test.ts`:
```ts
import { beforeAll, describe, expect, it } from "vitest";
import { loadGen } from "../../src/extract/dex.js";
import { extractSpecies } from "../../src/extract/species.js";
import { extractMoves } from "../../src/extract/moves.js";
import { extractTypeChart } from "../../src/extract/typechart.js";
import { extractLearnsets } from "../../src/extract/learnsets.js";
import type { LearnsetRow } from "../../src/types.js";

const dex = loadGen(6);
const sortMethods = (row: LearnsetRow) => ({
  ...row,
  methods: [...row.methods].sort((a, b) =>
    `${a.gen}${a.method}${a.level ?? ""}${a.sourceSpecies}`.localeCompare(
      `${b.gen}${b.method}${b.level ?? ""}${b.sourceSpecies}`)),
});

describe("golden files gen 6", () => {
  it("especie base con evolucion", () => {
    const species = extractSpecies(dex);
    expect(species.find((s) => s.showdownId === "charizard")).toMatchSnapshot();
  });

  it("forma alternativa que comparte dex_num", () => {
    const species = extractSpecies(dex);
    expect(species.find((s) => s.showdownId === "charizardmegax")).toMatchSnapshot();
  });

  it("movimiento con flags y precision", () => {
    expect(extractMoves(dex).find((m) => m.showdownId === "flamethrower")).toMatchSnapshot();
  });

  it("fila de tabla de tipos con inmunidad", () => {
    const chart = extractTypeChart(dex);
    expect(chart.filter((r) => r.attackingType === "Dragon")).toMatchSnapshot();
  });

  describe("learnsets", () => {
    let rows: LearnsetRow[];
    beforeAll(async () => { rows = await extractLearnsets(dex); }, 180_000);

    it("movimiento propio con metodos de varias generaciones", () => {
      expect(sortMethods(
        rows.find((r) => r.speciesId === "charizard" && r.moveId === "flamethrower")!,
      )).toMatchSnapshot();
    });

    it("movimiento heredado de la preevolucion", () => {
      expect(sortMethods(
        rows.find((r) => r.speciesId === "charizard" && r.moveId === "dragondance")!,
      )).toMatchSnapshot();
    });
  });
});
```

- [ ] **Step 2: Generar los snapshots y revisarlos a mano**

```bash
pnpm --filter @ludex/seed test golden
```

Abrir `packages/seed/test/__snapshots__/golden.test.ts.snap` y **leerlo entero** antes de commitear. Un snapshot generado sin mirar convierte un bug en la referencia oficial. Confirmar en particular que el snapshot de `dragondance` muestra `sourceSpecies: "charmander"` y ningún método con `gen > 6`.

- [ ] **Step 3: Verificar que el snapshot detecta cambios**

Cambiar a mano un valor del `.snap` (por ejemplo `90.5` por `91`), correr el test, verificar que falla, y revertir el cambio.

```bash
pnpm --filter @ludex/seed test golden
```
Esperado: FAIL antes de revertir, PASS después.

- [ ] **Step 4: Correr la suite completa**

```bash
docker compose up -d postgres
docker compose run --rm migrate up
pnpm -r run test
```
Esperado: todo verde.

- [ ] **Step 5: Verificar que no quedó ninguna generación hardcodeada**

```bash
grep -rin "gen6\|gen 6\|generacion 6" packages/seed/src/ db/ docker/ --exclude-dir=node_modules
```
Esperado: cero resultados en `src/`, `db/` y `docker/`. Las apariciones válidas están solo en `test/` y en `docs/`. Si aparece algo en `src/`, es un bug de parametrización y hay que arreglarlo antes de cerrar.

- [ ] **Step 6: Regenerar y commitear el dump del esquema**

```bash
docker compose run --rm migrate dump
git diff --stat db/schema.sql
```
Esperado: `db/schema.sql` refleja las nueve tablas.

- [ ] **Step 7: Completar `docs/DECISIONS.md`**

Verificar que estén registradas D1 a D7 más:

- La regla por defecto de legalidad de métodos para el torneo: **se consideran legales solo los métodos con `gen == generación del torneo`**; los de generaciones anteriores quedan almacenados y marcados como transferidos, para que la UI pueda mostrarlos como "legal en ladder, no en el torneo".
- Los conteos de referencia de `pokemon-showdown@0.11.10` (la tabla de este plan), para poder comparar tras un bump de versión.
- El tag exacto usado en `SHOWDOWN_REF`.

- [ ] **Step 8: Verificar los criterios de aceptación de la spec, uno por uno**

```bash
docker compose down -v
docker compose up -d postgres
docker compose run --rm migrate up
docker compose --profile local up -d --build showdown
curl -sf http://localhost:8100/ -o /dev/null && echo "showdown OK"
pnpm seed --gen 6
pnpm seed --gen 9
pnpm -r run test
```
Esperado: todo pasa desde una base vacía. Es la prueba de que un clon limpio del repo funciona.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "test(seed): golden files y cierre de fases 0+1"
```

---

## Self-Review

**Cobertura de la spec:**

| requisito de la spec | tarea |
|---|---|
| §3 D1 migraciones dbmate | 1, 2 |
| §3 D2 clave natural showdown_id | 2 (constraints), 5-8 |
| §3 D3 learnsets sin aplanar, con gen de origen | 8 |
| §3 D4 versión pineada + seed_runs | 4 (pin), 9 (runs), 10 (test) |
| §3 D5 seed en el host | 1 (.env), 10 |
| §4 estructura del repo | 1, 4 |
| §5 docker-compose y .env.example | 1 |
| §5 server local de Showdown | 3 |
| §6 esquema completo | 2 |
| §6 forma de learn_methods | 4 (types.ts), 8 |
| §7 pipeline y orden de FKs | 10 |
| §7 códigos desconocidos fallan | 8 |
| §8 capa 1 unit sin DB | 4, 5, 6, 7, 8 |
| §8 capa 2 frontera de generación | 5 (megas), 6 (piedras), 7 (Hada, Acero) |
| §8 capa 3 integración e idempotencia con mutación | 10 |
| §8 capa 4 golden files | 11 |
| §9 criterios de aceptación 1-8 | 11 step 8 |

Sin huecos. La única desviación es la eliminación de `pokemon.is_nonstandard`, justificada y documentada como D7.

**Consistencia de tipos:** `SpeciesRow`, `MoveRow`, `ItemRow`, `AbilityRow`, `TypeChartRow`, `LearnMethod` y `LearnsetRow` se definen una sola vez en la Tarea 4 y las tareas 5 a 10 usan esos nombres exactos. `isAvailable` y `loadGen` conservan su firma en las tareas 5 a 8. `upsertBatch` se define en la Tarea 9 y la usan solo `tables.ts` y su propio test.
