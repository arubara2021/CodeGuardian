-- Migration 005: skill graph, negative knowledge, provenance, and
-- admission audit tables.
--
-- This migration adds the storage that Phase 12 v2 requires. It does
-- not touch any table created by migrations 001 through 004. Every
-- table created here is derived: dropping any of them and rebuilding
-- from the Markdown skill files and the run history restores the exact
-- state. No source of truth is stored in these tables.
--
-- Every statement uses IF NOT EXISTS so that a partially applied
-- migration can be re-run safely. The migration is forward-only and
-- idempotent, matching the contract documented in migrations/__init__.py.

-- One row per node in the procedural skill graph. A node is either a
-- skill or a capability label. Skill nodes use the skill name as the
-- node identifier. Capability nodes use the prefix "cap:".
CREATE TABLE IF NOT EXISTS skill_graph_nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('skill', 'capability')),
    name TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_graph_nodes_kind
    ON skill_graph_nodes(kind);

CREATE INDEX IF NOT EXISTS idx_skill_graph_nodes_name
    ON skill_graph_nodes(name);

-- One row per typed edge in the procedural skill graph. The unique
-- constraint on (src, dst, kind) prevents duplicate edges regardless
-- of whether the edge identifier matches, which protects against a
-- rebuild from a corrupted source.
CREATE TABLE IF NOT EXISTS skill_graph_edges (
    edge_id TEXT PRIMARY KEY,
    src TEXT NOT NULL REFERENCES skill_graph_nodes(node_id) ON DELETE CASCADE,
    dst TEXT NOT NULL REFERENCES skill_graph_nodes(node_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'prerequisite', 'provides', 'consumes',
        'supersedes', 'semantic', 'conflicts'
    )),
    weight REAL NOT NULL DEFAULT 1.0,
    provenance TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (src, dst, kind)
);

CREATE INDEX IF NOT EXISTS idx_skill_graph_edges_src
    ON skill_graph_edges(src);
CREATE INDEX IF NOT EXISTS idx_skill_graph_edges_dst
    ON skill_graph_edges(dst);
CREATE INDEX IF NOT EXISTS idx_skill_graph_edges_kind
    ON skill_graph_edges(kind);

-- One row per failure record in the negative-knowledge store. A
-- failure record is a distinct knowledge object from a skill; it
-- describes what was attempted, what happened, and in what context.
-- The adopted and rejected counters track how often an agent chose to
-- use this record before a retry attempt.
CREATE TABLE IF NOT EXISTS negative_knowledge (
    failure_id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    failure_category TEXT NOT NULL CHECK (failure_category IN (
        'localization', 'generation', 'schema', 'contract',
        'timeout', 'context_overflow', 'model_unavailable'
    )),
    context_signature TEXT NOT NULL,
    attempted TEXT NOT NULL,
    observed TEXT NOT NULL,
    run_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    provenance_json TEXT,
    adopted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_negative_knowledge_skill
    ON negative_knowledge(skill_name);
CREATE INDEX IF NOT EXISTS idx_negative_knowledge_context
    ON negative_knowledge(context_signature);
CREATE INDEX IF NOT EXISTS idx_negative_knowledge_last_seen
    ON negative_knowledge(last_seen);

-- One row per skill, holding the complete provenance record written at
-- admission. This row is updated on re-admission; the history of
-- admission attempts lives in the skill_admission_log table.
CREATE TABLE IF NOT EXISTS skill_provenance (
    skill_name TEXT PRIMARY KEY,
    source_runs_json TEXT NOT NULL,
    authored_by TEXT NOT NULL CHECK (authored_by IN ('skill_curator', 'human')),
    commit_hash TEXT NOT NULL,
    license TEXT,
    schema_hash TEXT,
    scan_result TEXT,
    eval_score REAL,
    reviewer TEXT CHECK (reviewer IN ('auto', 'human')),
    reviewer_id TEXT,
    kernel_version TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_provenance_authored_by
    ON skill_provenance(authored_by);

-- One row per admission attempt. This table is append-only; rows are
-- never updated. It provides the audit trail for every candidate that
-- was accepted or rejected at the governance gate.
CREATE TABLE IF NOT EXISTS skill_admission_log (
    admission_id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    reason TEXT NOT NULL,
    scan_result TEXT,
    eval_score REAL,
    dedupe_status TEXT,
    blocking_findings_json TEXT,
    warning_findings_json TEXT,
    reviewer TEXT,
    reviewer_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_admission_log_skill
    ON skill_admission_log(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_admission_log_accepted
    ON skill_admission_log(accepted);
CREATE INDEX IF NOT EXISTS idx_skill_admission_log_attempted_at
    ON skill_admission_log(attempted_at);
