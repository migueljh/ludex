-- migrate:up

-- MON-33 (Phase 3 Task 3, D65 S5.1): auditoria durable del gate exact-once
-- de aprobacion humana. La fila `awaiting` se persiste ANTES de publicar la
-- propuesta por WebSocket; el Future del gate (apps/agent/.../hitl/gate.py)
-- sigue siendo la fuente de verdad de `/choose`, esta tabla es auditoria,
-- no el mecanismo. Al iniciar el proceso, un sweep (`abort_stale`) cambia
-- toda fila huerfana `awaiting` a `aborted/process_restart`.
--
-- PK natural compuesta (battle_tag, decision_index, attempt_index), misma
-- convencion que `trajectory_steps` (D21/C2): cada intento de decision es
-- su propia fila, nunca una fila mutable compartida entre intentos.
--
-- `action`/`legal_actions`/`model_envelope` espejan 1:1 los campos de
-- `ApprovalProposal` (hitl/gate.py, Task 2): `model_envelope` es el
-- envelope D38 completo (rationale/confidence/alternatives/provider/model/
-- usage/latencia/target) como UN solo jsonb, no columnas planas -- exacto
-- al tipo `dict[str, object]` que ya expone el dominio puro. `status` es la
-- maquina de estados de D65 S5.1: `awaiting` (abierto) o uno de los cinco
-- estados cerrados (`human_approved`, `human_override`, `timeout_auto`,
-- `superseded`, `aborted`).
--
-- Costo de un override (D65): la propuesta DESCARTADA permanece completa
-- aca (accion/legal_actions/model_envelope originales, nunca sobreescritos
-- por `resolve`); `resolved_action` es la accion que efectivamente gano el
-- CAS. El costo real de una batalla con overrides es `trajectory_steps`
-- (la accion ejecutada) MAS `pending_decisions` (lo que el LLM propuso y el
-- humano piso).

CREATE TABLE pending_decisions (
  battle_tag       text NOT NULL,
  decision_index   integer NOT NULL,
  attempt_index    integer NOT NULL,
  status           text NOT NULL DEFAULT 'awaiting',
  action           jsonb NOT NULL,
  legal_actions    jsonb NOT NULL,
  model_envelope   jsonb NOT NULL,
  resolved_action  jsonb,
  resolved_by      text,
  resolved_reason  text,
  approval_wait_ms double precision,
  created_at       timestamptz NOT NULL DEFAULT now(),
  resolved_at      timestamptz,
  PRIMARY KEY (battle_tag, decision_index, attempt_index),
  CONSTRAINT pending_decisions_decision_index_nonnegative_check
    CHECK (decision_index >= 0),
  CONSTRAINT pending_decisions_attempt_index_nonnegative_check
    CHECK (attempt_index >= 0),
  -- Estado cerrado (D65 S5.1): sólo estos seis valores existen. Cualquier
  -- otro string revienta al insertar, nunca queda un status inventado.
  CONSTRAINT pending_decisions_status_check
    CHECK (status IN (
      'awaiting', 'human_approved', 'human_override', 'timeout_auto',
      'superseded', 'aborted'
    )),
  CONSTRAINT pending_decisions_resolved_by_check
    CHECK (resolved_by IS NULL OR resolved_by IN ('operator', 'timer', 'system')),
  -- `awaiting` es exactamente "todavia sin resolved_at"; cualquier otro
  -- status YA tiene resolved_at. Ninguna fila puede quedar en un estado
  -- intermedio ambiguo.
  CONSTRAINT pending_decisions_awaiting_has_no_resolution_check
    CHECK ((status = 'awaiting') = (resolved_at IS NULL)),
  -- Una resolucion real (no abort/supersede) siempre trae la accion que
  -- gano el CAS. `aborted`/`superseded` pueden no tener ninguna: nunca se
  -- llego a resolver una accion.
  CONSTRAINT pending_decisions_resolution_action_check
    CHECK (
      status NOT IN ('human_approved', 'human_override', 'timeout_auto')
      OR resolved_action IS NOT NULL
    ),
  CONSTRAINT pending_decisions_approval_wait_ms_check
    CHECK (approval_wait_ms IS NULL OR approval_wait_ms >= 0),
  CONSTRAINT pending_decisions_action_type_check
    CHECK (jsonb_typeof(action) = 'object'),
  CONSTRAINT pending_decisions_resolved_action_type_check
    CHECK (resolved_action IS NULL OR jsonb_typeof(resolved_action) = 'object'),
  CONSTRAINT pending_decisions_legal_actions_type_check
    CHECK (jsonb_typeof(legal_actions) = 'array'),
  CONSTRAINT pending_decisions_model_envelope_type_check
    CHECK (jsonb_typeof(model_envelope) = 'object')
);

CREATE INDEX pending_decisions_status_idx ON pending_decisions (status);

-- D65 S5.2/S3: tres ejes ortogonales de metadata que NUNCA se colapsan en
-- uno solo: `action_source`, `action_path` y `approval_outcome`.
-- `human_override` implica las 11 columnas D38 de `trajectory_steps` NULL
-- COMO GRUPO (rechazado antes que parcialmente pobladas); `human_approved`
-- y `timeout_auto` llevan la metadata D38 completa. `played_by` sigue
-- siendo siempre `bot`: describe el cliente que manda el choice, nunca
-- quien decidio la accion.
ALTER TABLE trajectory_steps
  ADD COLUMN approval_outcome text;

ALTER TABLE trajectory_steps
  ADD CONSTRAINT trajectory_steps_approval_outcome_check
  CHECK (approval_outcome IN ('human_approved', 'human_override', 'timeout_auto')),
  -- El CHECK "global D38" (D65): si approval_outcome='human_override', las
  -- 11 columnas de metadata F2-08/D38 tienen que ser NULL, como grupo, sin
  -- excepcion -- nunca una fila con override parcialmente poblada.
  ADD CONSTRAINT trajectory_steps_human_override_metadata_null_check
  CHECK (
    approval_outcome IS DISTINCT FROM 'human_override'
    OR (
      rationale IS NULL AND target IS NULL AND confidence IS NULL
      AND alternatives IS NULL AND provider IS NULL AND model IS NULL
      AND decision_latency_ms IS NULL AND input_tokens IS NULL
      AND output_tokens IS NULL AND cached_input_tokens IS NULL
      AND reasoning_tokens IS NULL
    )
  );

-- migrate:down
ALTER TABLE trajectory_steps
  DROP CONSTRAINT trajectory_steps_human_override_metadata_null_check,
  DROP CONSTRAINT trajectory_steps_approval_outcome_check,
  DROP COLUMN approval_outcome;

DROP INDEX pending_decisions_status_idx;
DROP TABLE pending_decisions;
