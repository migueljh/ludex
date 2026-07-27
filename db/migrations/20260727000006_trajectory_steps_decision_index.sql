-- migrate:up

-- C2 (review final de feat/agent-conexion-estado): la PK de trajectory_steps
-- era (trajectory_id, turn_number). Un cambio forzado tras un debilitamiento
-- NO avanza el turno (Showdown manda un |request| con forceSwitch dentro del
-- mismo battle.turn), asi que dos decisiones distintas caian bajo la misma
-- clave y el upsert de save_step pisaba la primera con la segunda: el ~7.5%
-- de las decisiones del agente (el cambio de reemplazo tras un debilitamiento,
-- una de las mas informativas para entrenar) desaparecia en silencio.
--
-- decision_index cuenta decisiones, no turnos: avanza una vez por cada
-- llamada a choose_move, arrancando en 0 por trayectoria. turn_number queda
-- como columna comun (ya no unica): dos decisiones pueden compartir turno y
-- eso ahora es representable.
--
-- Sin backfill: las 57 batallas grabadas hasta hoy son de prueba (random
-- contra random, ver C2 en review-final.md) y no sirven para entrenar. Se
-- vacian y se regraban con `agent play`. TRUNCATE ... CASCADE alcanza a
-- battle_turns y trajectories via las FK ON DELETE CASCADE.
TRUNCATE battles CASCADE;

ALTER TABLE trajectory_steps DROP CONSTRAINT trajectory_steps_pkey;
ALTER TABLE trajectory_steps ADD COLUMN decision_index int NOT NULL;
ALTER TABLE trajectory_steps ADD CONSTRAINT trajectory_steps_pkey
  PRIMARY KEY (trajectory_id, decision_index);

-- migrate:down
ALTER TABLE trajectory_steps DROP CONSTRAINT trajectory_steps_pkey;
ALTER TABLE trajectory_steps DROP COLUMN decision_index;
ALTER TABLE trajectory_steps ADD CONSTRAINT trajectory_steps_pkey
  PRIMARY KEY (trajectory_id, turn_number);
