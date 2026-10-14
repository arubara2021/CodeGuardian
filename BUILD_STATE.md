# CodeGuardian — Build State

**Snapshot date:** 2026-10-14
**Last phase completed:** Phase 3 — Storage
**Next phase to start:** Phase 4 — Code Interfaces
**Phase 0 remaining work:** deferred to the end of the build, per user decision

---

## Environment

| Item | Value |
|---|---|
| Development shell | WSL2 Ubuntu 24.04 |
| Project path | `/mnt/c/Users/arun0/Videos/CodeGuardian` |
| Python | 3.12.3 (Ubuntu system) |
| Virtual environment | `.venv` at repo root |
| Activate command | `source .venv/bin/activate` |
| Test command | `PYTHONPATH=python python3 -m pytest tests/ -v` |
| Git identity | `arubara2021` / `arubara2021@users.noreply.github.com` |
| Git credential helper | Windows GCM via `/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe` |
| Sandbox backend | Bubblewrap (target), Docker (fallback) |
| Rust toolchain | not yet installed; needed at Phase 5 |
| Runtime dependencies installed | `jsonschema` 4.x, `sqlite-vec` 0.1.9, `PyYAML` 6.0.3, `keyring` 25.7.0 |

---

## Completed phases

### Phase 0 — Repository Scaffold (partial)

| Block | Status | Files |
|---|---|---|
| B-001 Root Build Configuration | done | pyproject.toml, Cargo.toml, Cargo.lock, rust-toolchain.toml, rustfmt.toml |
| B-002 Root Project Hygiene | done | clippy.toml, justfile, .gitignore, .codeguardianignore, .dockerignore |
| B-003 Root Editor and Documentation | done | .editorconfig, .env.example, .python-version, .pre-commit-config.yaml, mkdocs.yml |
| B-004 through B-009 | deferred to end | 30 files, no runtime impact |

The seven Rust crate stubs (`rust/crates/*/Cargo.toml`) carry only a `[package]` section. Real crate contents arrive in Phase 5 onward. This was a deviation from BUILD_ORDER to unblock `cargo metadata`.

### Phase 1 — Foundation (complete)

| Block | Status | Files |
|---|---|---|
| B-010 Package Identity and Error Codes | done | version.py, __init__.py, python/pyproject.toml, errors/codes.py, errors/taxonomy.py |
| B-011 Error Runtime | done | errors/__init__.py, errors/translation.py, errors/retry.py, errors/redaction.py, test_errors.py |
| B-012 Configuration Package | done | config/__init__.py, config/schema.py, config/defaults.py, config/loader.py, test_config.py |
| B-013 Configuration Secrets and Remaining Tests | done | config/secrets.py, test_translation.py, test_redaction.py |

### Phase 2 — Schemas (complete)

| Block | Status | Files |
|---|---|---|
| B-014 Core Structural Schemas | done | cpg.json, runtime-contract-map.json, recon.json, blast-radius.json, contract-assertions.json |
| B-015 Flow and Verification Schemas | done | context-pack.json, verification.json, plan-approval.json, consent.json, checkpoint.json |
| B-016 Output and Extension Schemas | done | audit-entry.json, plugin-manifest.json, skill.json, run-summary.json |

Every schema uses draft 2020-12. Every schema has a top-level `extensions` field with `additionalProperties: true`. Every schema's core objects use `additionalProperties: false`.

### Phase 3 — Storage (complete)

| Block | Status | Files |
|---|---|---|
| B-017 Migrations, SQLite Store, Filesystem Store | done | storage/__init__.py, migrations/__init__.py, migrations/001_initial.sql, sqlite_store.py, filesystem_store.py |
| B-018 Graph, Cache, Migrations Runner, Cleanup | done | graph_store.py, cache.py, migration_runner.py, cleanup.py |
| B-019 Skill Store and Storage Tests | done | skill_store.py, test_storage.py, test_sqlite_wal.py, test_skill_store.py |

The initial migration creates twenty application tables plus a `_schema_migrations` bookkeeping table. The CPG binary header format is defined here as a 64-byte little-endian structure. The skill store uses FTS5 for keyword search and vec0 for semantic search.

---

## Test counts

| Phase boundary | Passing tests |
|---|---|
| Phase 1 | 165 |
| Phase 2 | 165 (no new tests) |
| Phase 3 | 227 (165 + 62 new) |

Phase 3 integration ran across Phases 0 through 3 in one process. All checks passed.

---

## Deviations from BUILD_ORDER

### Phase 1 deviations

| # | What | Why |
|---|---|---|
| 1 | Added `errors/translation.py` and `test_translation.py` | The leak probe found 12/12 stdlib exceptions leaking past the taxonomy. The translation layer is the single chokepoint that converts them. |
| 2 | Added `FILE_PERMISSION_DENIED` to codes.py | `PermissionError` had no accurate code to map to. |
| 3 | Added `CONFIG_PARSE_ERROR` and `SECRET_KEYCHAIN_ERROR` to codes.py | Distinguishes TOML syntax errors from schema errors, and separates keychain failures from schema errors. |
| 4 | Wrote seven Rust crate `Cargo.toml` stubs early | Empty workspace members broke `cargo metadata` and blocked every commit. |

### Phase 2 deviations

| # | What | Why |
|---|---|---|
| 5 | `additionalProperties: false` on every closed object in all fourteen schemas | The design doc left this off. Closed objects catch typos and drift at validation time. Authorized before Block B-014. |
| 6 | `extensions` field added to every schema and to selected nested objects | The schema needs a path for new fields without a version bump. Top-level on all fourteen, nested on the growth points the design had already exercised (recon.structural, audit-entry sections, plugin-manifest blocks, run-summary entries). Authorized before Block B-014 rewrite. |
| 7 | Tested per block instead of per phase | Per-block commands caught errors one block at a time rather than all at once. The phase-boundary triple test still ran as required. |
| 8 | Test command uses `Draft202012Validator.check_schema()` instead of `jsonschema.validate()` with the meta-schema | `check_schema` is the canonical API for validating a schema document. The earlier form worked but was not idiomatic. |

### Phase 3 deviations

| # | What | Why |
|---|---|---|
| 9 | `migrations.py` renamed to `migration_runner.py` | A module and a package cannot share a name in the same parent. `migrations/` is the SQL package; `migrations.py` was invisible to every importer. BUILD_ORDER.md corrected to match. |
| 10 | Added `STORAGE_MIGRATION_FAILED`, `STORAGE_SCHEMA_INVALID`, `STORAGE_GRAPH_CORRUPTED` to codes.py and taxonomy.py | Storage had no codes that mapped to its failure modes. All three are categorized as SYSTEM. The latter two carry non-retryable overrides because their failures are deterministic. |
| 11 | Added `_schema_migrations` table not in the design doc | Every migration runner needs a table that records applied versions. Without it the runner cannot be idempotent. Created by `SQLiteStore.open()` before any migration is considered. |
| 12 | Defined the CPG binary header format in Phase 3 | The design doc says the CPG is a binary file but does not specify its layout. Phase 3 needs to read node and edge counts from the header without invoking the Rust kernel. A 64-byte little-endian header was defined and committed as the v1 format. Phase 6 must write files that match it byte for byte. |
| 13 | Real pytest files under `tests/` instead of per-block inline scripts | The B-017 and B-018 inline scripts were verification tools. The permanent suite lives in `tests/`. Sixteen test functions in `test_storage.py`, nine in `test_sqlite_wal.py`, and twenty-two in `test_skill_store.py`. |
| 14 | `sqlite_store.py` uses `contextlib.suppress` in two places | Ruff's SIM105 rule flagged `try`/`except`/`pass`. The change is cosmetic and does not alter behavior. |

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md`, and `docs/public-api.md`.
2. Do not attempt to re-verify Phases 1, 2, or 3. They are green and pushed. Trust the state.
3. Ask the user to `source .venv/bin/activate` before any Python command.
4. Start Phase 4 — Code Interfaces. Blocks B-020, B-021, B-022.
5. Before writing any file, settle the decisions Phase 4 needs: the four or five interface design questions that are not fully specified in the docs.

---

## Rules the next chat must follow

- Write full files only. Never fragments, never diffs.
- Never rewrite a file that has already passed.
- Test at phase boundaries, not per block.
- Never claim a test passed. Wait for the user's raw output.
- Ask before guessing. Ask before adding a code, symbol, dependency, or schema field.
- Follow `skills.md` for every file.
