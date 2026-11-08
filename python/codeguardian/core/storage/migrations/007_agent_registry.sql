-- Migration 007: agent registry table, agent_calls foreign key, and
-- checkpoint heartbeat.
--
-- Three changes, one file. Each is forward-only and independent of the
-- others at the schema level, so a future reader can read this file as
-- three sections even though the runner applies it as one script.
--
-- The runner records this migration in _schema_migrations exactly once.
-- The script itself is therefore not independently idempotent; the
-- runner is the guard. The CREATE TABLE uses IF NOT EXISTS so a partial
-- application is recoverable by inspection, but the agent_calls rebuild
-- cannot be conditionally skipped in pure SQL and does not attempt to
-- be. If the runner ever re-executes this file, the agent_calls DROP
-- and RENAME would fail on the second pass, which is the correct
-- failure mode: it means the migration history was tampered with.
--
-- This migration does not seed registered_agents. The seed lives in
-- the orchestration loop, where the resolved graph is the source of
-- truth: the slot names come from LEGACY_GRAPH, the capability tags
-- come from the resolved AgentSpec, and the timestamp is the moment
-- the loop ran. Keeping the seed out of SQL means a change to the
-- graph does not require a migration; the next run seeds the new
-- slots automatically. A database that runs this migration and then
-- executes the loop ends up with the same rows a hardcoded SQL seed
-- would have written, derived from the source rather than duplicated.
--
-- Because the seed runs after the migration, PRAGMA foreign_key_check
-- is not run before COMMIT. On an existing database, pre-seed rows in
-- agent_calls would reference roles that are not yet in
-- registered_agents, and the check would report them as violations
-- that cannot be fixed within the migration. The foreign key on
-- agent_calls.role is declarative — the project runs with foreign
-- keys off by default — so the check would be noise, not a gate.
--
-- The migration ends with PRAGMA foreign_keys = OFF, matching
-- migration 006 and the project convention documented in
-- sqlite_store.py. A caller that wants enforcement for its own
-- session enables it after opening the connection.

PRAGMA foreign_keys = OFF;

BEGIN;

-- Section 1: the agent registry.
--
-- One row per agent the orchestrator can dispatch. The name is the
-- role identifier written to agent_calls.role. capabilities is a JSON
-- array of capability tags, seeded from the resolved AgentSpec.
-- backend is one of the four execution models the registry
-- recognizes. registered_at is the ISO 8601 timestamp of the first
-- registration. The table is created empty; the loop populates it.
CREATE TABLE IF NOT EXISTS registered_agents (
    name TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '[]',
    backend TEXT NOT NULL DEFAULT 'in-process'
        CHECK (backend IN ('in-process', 'cli', 'acp', 'openai-api')),
    registered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registered_agents_backend
    ON registered_agents(backend);

-- Section 2: rebuild agent_calls with the role foreign key.
--
-- The new table is identical to the post-migration-004 shape except
-- that the nine-value CHECK on role is replaced by a foreign key to
-- registered_agents(name). Every other column, CHECK, default, and
-- nullability is preserved.
CREATE TABLE agent_calls_v2 (
    call_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    phase_id TEXT REFERENCES phase_executions(phase_id),
    target_symbol TEXT,
    role TEXT NOT NULL REFERENCES registered_agents(name),
    prompt_role TEXT,
    prompt_version TEXT,
    model_id TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'success',
        'refused',
        'schema_violation',
        'signature_drift',
        'timeout'
    )),
    refusal_code TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    output_hash TEXT,
    created_at TEXT NOT NULL,
    prompt_hash TEXT,
    context_hash TEXT,
    failure_category TEXT,
    verdict_reason TEXT,
    model_options TEXT,
    tool_chain TEXT,
    decision_log TEXT,
    model_reasoning TEXT,
    evidence_receipt TEXT
);

INSERT INTO agent_calls_v2 (
    call_id, run_id, phase_id, target_symbol, role,
    prompt_role, prompt_version, model_id,
    tokens_in, tokens_out, latency_ms, outcome,
    refusal_code, retry_count, output_hash, created_at,
    prompt_hash, context_hash, failure_category, verdict_reason,
    model_options, tool_chain, decision_log, model_reasoning,
    evidence_receipt
)
SELECT
    call_id, run_id, phase_id, target_symbol, role,
    prompt_role, prompt_version, model_id,
    tokens_in, tokens_out, latency_ms, outcome,
    refusal_code, retry_count, output_hash, created_at,
    prompt_hash, context_hash, failure_category, verdict_reason,
    model_options, tool_chain, decision_log, model_reasoning,
    evidence_receipt
FROM agent_calls;

DROP TABLE agent_calls;

ALTER TABLE agent_calls_v2 RENAME TO agent_calls;

CREATE INDEX IF NOT EXISTS idx_agent_calls_run
    ON agent_calls(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_calls_role_prompt
    ON agent_calls(role, prompt_version);
CREATE INDEX IF NOT EXISTS idx_agent_calls_role_model
    ON agent_calls(role, model_id);

-- Section 3: checkpoint heartbeat.
--
-- A long-running phase updates this column every few seconds so the
-- supervisor can distinguish a slow phase from a hung one. The column
-- is nullable; a checkpoint written before the first heartbeat has
-- NULL and the supervisor falls back to created_at.
ALTER TABLE checkpoints ADD COLUMN heartbeat_at TEXT;

COMMIT;

-- Restore the project default. The FK on agent_calls.role is
-- declarative; the registry enforces agent names at dispatch time, and
-- the loop seeds registered_agents from the resolved graph. A caller
-- that wants enforcement for its own session enables it after opening
-- the connection.
PRAGMA foreign_keys = OFF;
