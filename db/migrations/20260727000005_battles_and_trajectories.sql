-- migrate:up
CREATE TYPE played_by_kind AS ENUM ('bot', 'human');
CREATE TYPE battle_source  AS ENUM ('challenge', 'ladder', 'local', 'import');
CREATE TYPE battle_result  AS ENUM ('win', 'loss', 'tie');
CREATE TYPE action_source  AS ENUM ('agent', 'human', 'opponent');

CREATE TABLE battles (
  id             serial PRIMARY KEY,
  battle_tag     text NOT NULL UNIQUE,
  tournament_id  int,
  round_id       int,
  format         text NOT NULL,
  p1             text NOT NULL,
  p2             text NOT NULL,
  winner         text,
  played_by      played_by_kind NOT NULL,
  source         battle_source  NOT NULL,
  replay_url     text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- player_side esta en la PK porque el stream de protocolo es POR JUGADOR:
-- el |request| de p1 contiene el equipo de p1. Un solo stream por batalla
-- haria imposible re-derivar el estado del otro lado, y meteria el equipo
-- de un jugador en el contexto del otro.
CREATE TABLE battle_turns (
  battle_id       int  NOT NULL REFERENCES battles(id) ON DELETE CASCADE,
  player_side     text NOT NULL,
  turn_number     int  NOT NULL,
  protocol_lines  text[] NOT NULL,
  agent_reasoning jsonb,
  PRIMARY KEY (battle_id, player_side, turn_number)
);

CREATE TABLE trajectories (
  id           serial PRIMARY KEY,
  battle_id    int  NOT NULL REFERENCES battles(id) ON DELETE CASCADE,
  gen_id       int  NOT NULL REFERENCES generations(id),
  format       text NOT NULL,
  player_side  text NOT NULL,
  final_result battle_result,
  elo_bucket   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (battle_id, player_side)
);

CREATE TABLE trajectory_steps (
  trajectory_id        int  NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
  turn_number          int  NOT NULL,
  state                jsonb NOT NULL,
  state_schema_version int  NOT NULL,
  legal_actions        jsonb NOT NULL,
  action_taken         jsonb,
  action_source        action_source NOT NULL,
  reward               numeric,
  PRIMARY KEY (trajectory_id, turn_number)
);

CREATE INDEX trajectory_steps_version_idx ON trajectory_steps (state_schema_version);
CREATE INDEX battles_created_at_idx       ON battles (created_at DESC);

-- migrate:down
DROP TABLE trajectory_steps;
DROP TABLE trajectories;
DROP TABLE battle_turns;
DROP TABLE battles;
DROP TYPE action_source;
DROP TYPE battle_result;
DROP TYPE battle_source;
DROP TYPE played_by_kind;
