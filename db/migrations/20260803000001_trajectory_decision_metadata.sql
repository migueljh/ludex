-- migrate:up

-- F2-08 (MON-13): metadata de la decision completa y de calidad ML, por
-- decision_index. Contracto del TECH LEAD DESIGN VERDICT:
--
-- - rationale: texto breve user-facing, nunca chain-of-thought.
-- - target: NULL valido y esperado en singles; un target no-NULL solo es
--   valido si la mascara legal expone targets explicitos.
-- - confidence: obligatoria para respuesta LLM aceptada, en [0,1]; NULL en
--   fallback/random/historico. Nunca 0.0 inventado.
-- - alternatives: JSON array; [] es valido. Cada elemento ya se valido contra
--   la misma mascara legal en el grafo.
-- - provider/model: unicamente los que produjeron la respuesta LLM aceptada.
--   En fallback quedan NULL para no atribuir la accion determinista a un
--   modelo. Co-ocurrencia obligatoria.
-- - decision_latency_ms: desde el primer intento LLM hasta respuesta aceptada
--   o fallback, retries incluidos. NULL en random/historico.
-- - Tokens: suma de las respuestas facturables del camino (retries semanticos
--   e de infraestructura con usage disponible). Las cuatro columnas son todas
--   NULL o todas no-NULL.
--
-- Sin backfill: la historia queda completamente NULL; nunca se inventa
-- provider/model para filas viejas.

ALTER TABLE trajectory_steps
  ADD COLUMN rationale text,
  ADD COLUMN target jsonb,
  ADD COLUMN confidence double precision,
  ADD COLUMN alternatives jsonb,
  ADD COLUMN provider text,
  ADD COLUMN model text,
  ADD COLUMN decision_latency_ms double precision,
  ADD COLUMN input_tokens integer,
  ADD COLUMN output_tokens integer,
  ADD COLUMN cached_input_tokens integer,
  ADD COLUMN reasoning_tokens integer;

ALTER TABLE trajectory_steps
  ADD CONSTRAINT trajectory_steps_confidence_check
  CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  ADD CONSTRAINT trajectory_steps_provider_model_coherence_check
  CHECK ((provider IS NULL) = (model IS NULL)),
  ADD CONSTRAINT trajectory_steps_usage_coherence_check
  CHECK (
    (input_tokens IS NULL AND output_tokens IS NULL
     AND cached_input_tokens IS NULL AND reasoning_tokens IS NULL)
    OR
    (input_tokens IS NOT NULL AND output_tokens IS NOT NULL
     AND cached_input_tokens IS NOT NULL AND reasoning_tokens IS NOT NULL)
  ),
  -- Un CHECK por columna de tokens (no un solo CHECK con las cuatro): el
  -- borrador del design verdict tenia el CHECK de output_tokens apuntando a
  -- input_tokens, y el canario de test_models.py lo caza por nombre.
  ADD CONSTRAINT trajectory_steps_input_tokens_nonnegative_check
  CHECK (input_tokens IS NULL OR input_tokens >= 0),
  ADD CONSTRAINT trajectory_steps_output_tokens_nonnegative_check
  CHECK (output_tokens IS NULL OR output_tokens >= 0),
  ADD CONSTRAINT trajectory_steps_cached_input_tokens_nonnegative_check
  CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
  ADD CONSTRAINT trajectory_steps_reasoning_tokens_nonnegative_check
  CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
  ADD CONSTRAINT trajectory_steps_cached_input_tokens_check
  CHECK (cached_input_tokens IS NULL OR cached_input_tokens <= input_tokens),
  ADD CONSTRAINT trajectory_steps_latency_check
  CHECK (decision_latency_ms IS NULL OR decision_latency_ms >= 0),
  ADD CONSTRAINT trajectory_steps_alternatives_type_check
  CHECK (alternatives IS NULL OR jsonb_typeof(alternatives) = 'array'),
  ADD CONSTRAINT trajectory_steps_target_type_check
  CHECK (target IS NULL OR jsonb_typeof(target) = 'object');

-- migrate:down
ALTER TABLE trajectory_steps
  DROP COLUMN rationale,
  DROP COLUMN target,
  DROP COLUMN confidence,
  DROP COLUMN alternatives,
  DROP COLUMN provider,
  DROP COLUMN model,
  DROP COLUMN decision_latency_ms,
  DROP COLUMN input_tokens,
  DROP COLUMN output_tokens,
  DROP COLUMN cached_input_tokens,
  DROP COLUMN reasoning_tokens;
