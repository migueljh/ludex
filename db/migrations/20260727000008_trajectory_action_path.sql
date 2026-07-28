-- migrate:up
ALTER TABLE trajectory_steps
  ADD COLUMN action_path text NULL
  CONSTRAINT trajectory_steps_action_path_check
  CHECK (action_path IN ('llm', 'llm_retry', 'fallback'));

-- migrate:down
ALTER TABLE trajectory_steps DROP COLUMN action_path;
