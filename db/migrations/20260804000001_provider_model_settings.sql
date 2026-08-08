-- migrate:up

-- F2-09 (MON-14): catalogo de providers/models y seleccion activa.
--
-- La DB gobierna la seleccion activa de modelo; el env es solo bootstrap
-- (config.py). Los cambios de modelo activo surten efecto en la SIGUIENTE
-- decision del grafo, sin recompilar ni reiniciar la batalla.
--
-- `providers.api_key_env` guarda el NOMBRE de la variable de entorno, NUNCA
-- el valor: las API keys no existen en la DB, en logs ni en snapshots.
-- `models` referencia al provider, expone el model_id del proveedor, la
-- label legible, disponibilidad (enabled) y el default (is_default).
-- `settings` es key/value jsonb (patron del PLAN); la clave 'active_model'
-- guarda {"provider": ..., "model": ...}, la seleccion activa sin secretos.

CREATE TABLE providers (
  id          serial PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  base_url    text,
  api_key_env text NOT NULL,
  enabled     boolean NOT NULL DEFAULT true
);

CREATE TABLE models (
  id          serial PRIMARY KEY,
  provider_id int NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
  model_id    text NOT NULL,
  label       text NOT NULL,
  is_default  boolean NOT NULL DEFAULT false,
  enabled     boolean NOT NULL DEFAULT true,
  UNIQUE (provider_id, model_id)
);

CREATE TABLE settings (
  key   text PRIMARY KEY,
  value jsonb NOT NULL
);

-- migrate:down
DROP TABLE settings;
DROP TABLE models;
DROP TABLE providers;
