# CodeGuardian — Build State

**Snapshot date:** 2026-10-10
**Last phase completed:** Phase 1 — Foundation
**Next phase to start:** Phase 2 — Schemas
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

**Tests passing at Phase 1 boundary:** 165
**Test files:** test_errors.py, test_translation.py, test_redaction.py, test_config.py

---

## Deviations from BUILD_ORDER

| # | What | Why |
|---|---|---|
| 1 | Added `errors/translation.py` and `test_translation.py` | The leak probe found 12/12 stdlib exceptions leaking past the taxonomy. The translation layer is the single chokepoint that converts them. |
| 2 | Added `FILE_PERMISSION_DENIED` to codes.py | `PermissionError` had no accurate code to map to. |
| 3 | Added `CONFIG_PARSE_ERROR` and `SECRET_KEYCHAIN_ERROR` to codes.py | Distinguishes TOML syntax errors from schema errors, and separates keychain failures from schema errors. |
| 4 | Wrote seven Rust crate `Cargo.toml` stubs early | Empty workspace members broke `cargo metadata` and blocked every commit. |

No other deviations. Phase 1 shipped exactly what BUILD_ORDER specified plus the four items above.

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md`, and `docs/public-api.md`.
2. Do not attempt to re-verify Phase 1. It is green. Trust the state.
3. Ask the user to `source .venv/bin/activate` before any Python command.
4. Start Phase 2 — Schemas. Blocks B-014, B-015, B-016.

---

## Rules the next chat must follow

- Write full files only. Never fragments, never diffs.
- Never rewrite a file that has already passed.
- Test at phase boundaries, not per block.
- Never claim a test passed. Wait for the user's raw output.
- Ask before guessing. Ask before adding a code, symbol, dependency, or schema field.
- Follow `skills.md` for every file.
