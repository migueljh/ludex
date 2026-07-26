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
