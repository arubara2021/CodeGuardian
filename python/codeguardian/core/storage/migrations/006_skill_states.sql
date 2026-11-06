-- Migration 006: expand the skills.state CHECK constraint.
--
-- Phase 12 v2 adds three lifecycle states (trial, stable, retired)
-- that the CHECK constraint created in migration 001 rejects. SQLite
-- cannot alter a CHECK constraint in place, so the table is rebuilt
-- with the expanded constraint. Rowids are preserved because the
-- FTS5 external content index (skills_fts) references them; the index
-- is rebuilt at the end so that it matches the new table.
--
-- Foreign key enforcement is disabled for the duration of the rebuild
-- because skill_feedback references skills(name). Without the PRAGMA,
-- dropping the old table would fail whenever any skill had recorded
-- feedback. The setting is restored to its previous value at the end
-- of the migration. The project's default is OFF (nothing in
-- sqlite_store.py enables it), so the migration ends with OFF. A
-- future migration that changes the default must update this file's
-- closing PRAGMA in the same change.
--
-- The migration runner records successful application in
-- _schema_migrations, so this script executes exactly once per
-- database. A failure partway through leaves the database with a
-- populated skills_v2 table and no skills table; recovery in that
-- case requires manual intervention. The failure mode is documented
-- here so a future migration can add cleanup.

PRAGMA foreign_keys = OFF;

CREATE TABLE skills_v2 (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    description TEXT,
    trigger_text TEXT,
    skill_type TEXT,
    language TEXT,
    effectiveness REAL DEFAULT 0.5,
    total_uses INTEGER DEFAULT 0,
    total_successes INTEGER DEFAULT 0,
    state TEXT DEFAULT 'active' CHECK (state IN (
        'active', 'stale', 'archived', 'draft',
        'trial', 'stable', 'retired'
    )),
    created_at TEXT,
    last_used_at TEXT
);

INSERT INTO skills_v2
    (rowid, name, path, description, trigger_text, skill_type,
     language, effectiveness, total_uses, total_successes, state,
     created_at, last_used_at)
SELECT rowid, name, path, description, trigger_text, skill_type,
       language, effectiveness, total_uses, total_successes, state,
       created_at, last_used_at
FROM skills;

DROP TABLE skills;

ALTER TABLE skills_v2 RENAME TO skills;

CREATE INDEX IF NOT EXISTS idx_skills_language ON skills(language);
CREATE INDEX IF NOT EXISTS idx_skills_state ON skills(state);

INSERT INTO skills_fts(skills_fts) VALUES('rebuild');

-- The project runs with foreign key enforcement off by default, so
-- the migration restores that state rather than turning it on. A
-- caller that needs foreign key enforcement for its own session sets
-- the PRAGMA itself after opening the connection.
PRAGMA foreign_keys = OFF;
