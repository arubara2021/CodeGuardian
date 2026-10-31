-- Provenance columns for agent_calls. Adds hashes that let a debugger
-- confirm whether two runs used the same prompt and context, plus the
-- finer-grained failure category and verdict reason that coexist with
-- the coarse outcome from migration 002.
--
-- failure_category and verdict_reason are not CHECK-constrained
-- because the valid sets live in codeguardian.core.agents.telemetry as
-- Literal types. New categories can be added without a schema
-- migration.
--
-- The outcome column from migration 002 keeps its five-value CHECK
-- (success, refused, schema_violation, signature_drift, timeout).
-- record_call derives the outcome from failure_category when the
-- caller does not supply one; the mapping is best-fit, not exact.
-- Downstream consumers that need precision should read
-- failure_category and prefer it when present.

ALTER TABLE agent_calls ADD COLUMN prompt_hash TEXT;
ALTER TABLE agent_calls ADD COLUMN context_hash TEXT;
ALTER TABLE agent_calls ADD COLUMN failure_category TEXT;
ALTER TABLE agent_calls ADD COLUMN verdict_reason TEXT;
ALTER TABLE agent_calls ADD COLUMN model_options TEXT;
