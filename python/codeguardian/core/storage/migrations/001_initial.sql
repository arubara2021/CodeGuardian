-- Initial schema for the CodeGuardian metadata and index store.
--
-- This migration creates every table the design specifies in
-- docs/05-data-model.md section 6. It is applied once to a fresh database.
-- Every statement uses IF NOT EXISTS so that a crash in the middle of
-- the migration can be retried without manual repair.
--
-- The bookkeeping table ``_schema_migrations`` is not created here. The
-- SQLiteStore creates it as part of open() so that it exists before the
-- first migration is considered. This file only creates application
-- tables.

-- Repositories that CodeGuardian has been pointed at. One row per URL.
CREATE TABLE IF NOT EXISTS repos (
    repo_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('local', 'github', 'gitlab')),
    default_branch TEXT,
    last_seen_at TEXT NOT NULL,
    meta_json TEXT
);

-- Code Property Graph metadata. The graph itself is a binary file on
-- disk; this table records where it is and how big it is. One row per
-- (repo_id, commit_hash) pair.
CREATE TABLE IF NOT EXISTS cpg_metadata (
    cpg_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    commit_hash TEXT NOT NULL,
    node_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    build_duration_ms INTEGER,
    built_at TEXT NOT NULL,
    incremental INTEGER DEFAULT 0,
    binary_path TEXT NOT NULL,
    UNIQUE(repo_id, commit_hash)
);

CREATE INDEX IF NOT EXISTS idx_cpg_repo ON cpg_metadata(repo_id, commit_hash);

-- Runs. One row per invocation of the pipeline against one target.
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    commit_hash TEXT NOT NULL,
    target_path TEXT,
    mode TEXT CHECK (mode IN ('legacy', 'greenfield')),
    directive TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT CHECK (status IN ('running', 'success', 'failure', 'stopped', 'dry_run', 'paused')),
    sandbox_backend TEXT,
    plan_approval_id TEXT,
    blast_score INTEGER,
    recommendation TEXT CHECK (recommendation IN ('proceed', 'review', 'block')),
    verification_enabled INTEGER DEFAULT 0,
    artifact_dir TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- Plan approvals. The first consent gate. Recorded before Phase 0 begins.
CREATE TABLE IF NOT EXISTS plan_approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    approved_at TEXT NOT NULL,
    approved_by TEXT,
    scope_kind TEXT CHECK (scope_kind IN ('single_target', 'module', 'project')),
    scope_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_approvals_run ON plan_approvals(run_id);

-- Phase executions. One row per phase within a run.
CREATE TABLE IF NOT EXISTS phase_executions (
    phase_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    phase_name TEXT NOT NULL,
    agent_name TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT CHECK (status IN ('running', 'success', 'failure', 'stopped', 'paused')),
    retries INTEGER DEFAULT 0,
    resumed_from TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_phases_run ON phase_executions(run_id, started_at);

-- Checkpoints. Written after each significant step so a crash resumes
-- from the last successful point rather than restarting the run.
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    phase TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, created_at DESC);

-- Model calls. One row per LLM invocation, with token counts and hashes.
CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY,
    phase_id TEXT NOT NULL REFERENCES phase_executions(phase_id),
    agent_name TEXT NOT NULL CHECK (agent_name IN ('recon', 'blast_radius', 'analyst', 'tester', 'writer', 'correctness_verifier', 'security_verifier', 'contract_verifier', 'skill_curator')),
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    prompt_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    output_hash TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_phase ON model_calls(phase_id);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON model_calls(agent_name);

-- MCP tool calls. One row per external MCP tool invocation.
CREATE TABLE IF NOT EXISTS mcp_tool_calls (
    call_id TEXT PRIMARY KEY,
    phase_id TEXT NOT NULL REFERENCES phase_executions(phase_id),
    tool_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    duration_ms INTEGER,
    status TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_phase ON mcp_tool_calls(phase_id);
CREATE INDEX IF NOT EXISTS idx_mcp_tool ON mcp_tool_calls(tool_name);

-- Blast radius reports. One row per analysis, plus the artifacts on disk.
CREATE TABLE IF NOT EXISTS blast_radius_reports (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    target_path TEXT NOT NULL,
    direct_callers INTEGER NOT NULL,
    transitive_callers INTEGER NOT NULL,
    contract_violations INTEGER NOT NULL,
    coverage_gaps INTEGER NOT NULL,
    blast_score INTEGER NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN ('proceed', 'review', 'block')),
    report_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blast_run ON blast_radius_reports(run_id);

-- Contract violations extracted from a blast radius report.
CREATE TABLE IF NOT EXISTS contract_violations (
    violation_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES blast_radius_reports(report_id),
    caller TEXT NOT NULL,
    assumption TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('info', 'warning', 'critical')),
    evidence TEXT
);

CREATE INDEX IF NOT EXISTS idx_violations_report ON contract_violations(report_id);

-- Coverage gaps extracted from a blast radius report.
CREATE TABLE IF NOT EXISTS coverage_gaps (
    gap_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES blast_radius_reports(report_id),
    caller TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gaps_report ON coverage_gaps(report_id);

-- Verifier verdicts. One row per verifier that ran on a diff.
CREATE TABLE IF NOT EXISTS verifier_verdicts (
    verdict_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    verifier_type TEXT NOT NULL CHECK (verifier_type IN ('correctness', 'security', 'contract')),
    model_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'uncertain')),
    confidence REAL,
    context_hash TEXT,
    evidence_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verdicts_run ON verifier_verdicts(run_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_type ON verifier_verdicts(verifier_type);

-- Apply consent records. The second consent gate. Recorded only when the
-- user approves the diff for apply.
CREATE TABLE IF NOT EXISTS consent_records (
    consent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    granted_at TEXT NOT NULL,
    granted_by TEXT,
    scope TEXT NOT NULL CHECK (scope IN ('apply', 'verify')),
    diff_hash TEXT,
    rollback_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_consent_run ON consent_records(run_id);

-- Skills. The Markdown files on disk are the source of truth; this table
-- is a derived index. If the table is deleted, it rebuilds from the
-- Markdown without loss.
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    description TEXT,
    trigger_text TEXT,
    skill_type TEXT,
    language TEXT,
    effectiveness REAL DEFAULT 0.5,
    total_uses INTEGER DEFAULT 0,
    total_successes INTEGER DEFAULT 0,
    state TEXT DEFAULT 'active' CHECK (state IN ('active', 'stale', 'archived', 'draft')),
    created_at TEXT,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_skills_language ON skills(language);
CREATE INDEX IF NOT EXISTS idx_skills_state ON skills(state);

-- Full-text search index over the skill metadata. This is an FTS5
-- external-content table: the data lives in ``skills`` and the index
-- is derived from it.
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name,
    description,
    trigger_text,
    content='skills',
    content_rowid='rowid'
);

-- Vector index for semantic skill retrieval. Uses the vec0 extension
-- from sqlite-vec. The embedding dimension of 384 matches the default
-- model used by the skill curator.
CREATE VIRTUAL TABLE IF NOT EXISTS skill_embeddings USING vec0(
    name TEXT PRIMARY KEY,
    embedding float[384]
);

-- Feedback rows for a skill. Used to compute effectiveness over time.
CREATE TABLE IF NOT EXISTS skill_feedback (
    feedback_id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL REFERENCES skills(name),
    run_id TEXT REFERENCES runs(run_id),
    outcome TEXT CHECK (outcome IN ('success', 'partial', 'failure')),
    task_description TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_skill ON skill_feedback(skill_name);
CREATE INDEX IF NOT EXISTS idx_feedback_run ON skill_feedback(run_id);

-- Cache entries. Metadata only; the payload lives on the filesystem or
-- in a column, depending on size. Keys are (repo_id, commit_hash, type).
CREATE TABLE IF NOT EXISTS cache_entries (
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    commit_hash TEXT NOT NULL,
    cache_type TEXT NOT NULL CHECK (cache_type IN ('recon', 'runtime_contract_map', 'blast', 'ast', 'mcp_response')),
    payload_path TEXT,
    payload_inline BLOB,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    access_count INTEGER DEFAULT 1,
    PRIMARY KEY (repo_id, commit_hash, cache_type)
);

CREATE INDEX IF NOT EXISTS idx_cache_accessed ON cache_entries(last_accessed_at);

-- Plugin registry. Rebuilt on every startup from entry-point discovery,
-- but persisted so that a plugin's manifest is available without
-- re-importing the plugin package.
CREATE TABLE IF NOT EXISTS plugin_registry (
    plugin_name TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    plugin_type TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    PRIMARY KEY (plugin_name, plugin_version)
);

-- Cleanup log. Records what the background retention thread deleted and
-- when, so a user can audit disk usage growth.
CREATE TABLE IF NOT EXISTS cleanup_log (
    cleanup_id TEXT PRIMARY KEY,
    ran_at TEXT NOT NULL,
    artifacts_removed INTEGER DEFAULT 0,
    cache_entries_removed INTEGER DEFAULT 0,
    bytes_freed INTEGER DEFAULT 0
);
