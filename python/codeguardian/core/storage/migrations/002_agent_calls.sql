-- Agent call telemetry. One row per agent invocation, capturing prompt
-- metadata, model identity, token counts, latency, and outcome. Two
-- analytical queries drive the table's design: success rate by prompt
-- version and role, and latency plus success rate by role and model.
-- Both are aggregation-only and do not require a run context.
--
-- run_id and phase_id are nullable. An agent can run in an eval or
-- calibration context where no pipeline run exists; the row still
-- contributes to the statistics the table exists to collect. Tracing
-- a row back to a run is opt-in: the orchestrator supplies locators
-- when a run exists, an isolated eval does not, and the write
-- succeeds either way.
--
-- The role CHECK uses Phase 11 agent names. The model_calls table
-- (Phase 7) uses 'blast_radius' where this table uses 'blast'; the
-- two tables are not joined today, and a future join must normalize
-- the name.
--
-- refusal_code is not CHECK-constrained. The valid set lives in
-- codeguardian/core/prompts/schema.py and is expected to grow;
-- validation happens at the agent boundary, not at the database.

CREATE TABLE IF NOT EXISTS agent_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    phase_id TEXT REFERENCES phase_executions(phase_id),
    target_symbol TEXT,
    role TEXT NOT NULL CHECK (role IN (
        'recon',
        'blast',
        'analyst',
        'tester',
        'writer',
        'correctness_verifier',
        'security_verifier',
        'contract_verifier',
        'skill_curator'
    )),
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
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_calls_run
    ON agent_calls(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_calls_role_prompt
    ON agent_calls(role, prompt_version);
CREATE INDEX IF NOT EXISTS idx_agent_calls_role_model
    ON agent_calls(role, model_id);
