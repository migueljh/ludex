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

-- Ojo: el tipo Item de pokemon-showdown NO tiene `flags` (cero de 537 objetos
-- la exponen). La columna guarda propiedades reales del paquete, incluidas
-- megaStone y megaEvolves, que son las que filtra round_availability en un
-- torneo de gen 6.
CREATE TABLE items (
  id          serial PRIMARY KEY,
  gen_id      int  NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
  showdown_id text NOT NULL,
  name        text NOT NULL,
  description text,
  properties  jsonb NOT NULL,
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
