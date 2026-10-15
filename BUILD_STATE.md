
# CodeGuardian — Build State

**Snapshot date:** 2026-10-15
**Last phase completed:** Phase 5 — Bridges
**Next phase to start:** Phase 6 — Rust Kernel
**Phase 0 remaining work:** deferred to the end of the build, per user decision

---

## Environment

| Item | Value |
|---|---|
| Development shell | WSL2 Ubuntu 24.04 |
| Project path | `/mnt/c/Users/arun0/Videos/CodeGuardian` |
| Python | 3.12.3 (system) |
| Virtual environment | `.venv` at repo root |
| Activate command | `source .venv/bin/activate` |
| Test command | `PYTHONPATH=python python3 -m pytest tests/ -q` |
| Git identity | `arubara2021` / `arubara2021@users.noreply.github.com` |
| Git credential helper | Windows GCM via `/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe` |
| Sandbox backend | Bubblewrap (target), Docker (fallback) |
| Rust toolchain | 1.99.0 (stable, installed via rustup) |
| Cargo target dir | `$HOME/.cache/codeguardian-target` (exported in `.bashrc`, appended to `.venv/bin/activate`) |
| Rust runtime dependency | None installed yet beyond tree-sitter crate |
| Python runtime dependencies installed | `jsonschema` 4.x, `sqlite-vec` 0.1.9, `PyYAML` 6.0.3, `keyring` 25.7.0, `ruff` 0.17.0, `mypy` 2.4.0 |

### Environment notes

- `rust/Cargo.toml` (empty placeholder) was deleted. Cargo now works from any directory because the workspace root at the repo root is the only manifest above the crates.
- The venv's activate script has been modified in `.venv/bin/activate` to append `$HOME/.cargo/bin` to `PATH`. This is an environment fix, not a repo change.
- The PyO3 extension is built with `PYO3_PYTHON="$(pwd)/.venv/bin/python3"` and copied to `python/codeguardian/_native.so` after every build. `.gitignore` excludes the file.

---

## Completed phases

### Phase 0 — Repository Scaffold (partial)

| Block | Status | Files |
|---|---|---|
| B-001 Root Build Configuration | done | pyproject.toml, Cargo.toml, Cargo.lock, rust-toolchain.toml, rustfmt.toml |
| B-002 Root Project Hygiene | done | clippy.toml, justfile, .gitignore, .codeguardianignore, .dockerignore |
| B-003 Root Editor and Documentation | done | .editorconfig, .env.example, .python-version, .pre-commit-config.yaml, mkdocs.yml |
| B-004 through B-009 | deferred to end | 30 files, no runtime impact |

The seven Rust crate stubs under `rust/crates/` carried only a `[package]` section after Phase 0. Phase 5 filled the manifests for the `rpc`, `pyo3`, and `kernel` crates. The remaining four crates remain stubs.

### Phase 1 — Foundation (complete)

| Block | Status | Files |
|---|---|---|
| B-010 Package Identity and Error Codes | done | version.py, __init__.py, python/pyproject.toml, errors/codes.py, errors/taxonomy.py |
| B-011 Error Runtime | done | errors/__init__.py, errors/translation.py, errors/retry.py, errors/redaction.py, test_errors.py |
| B-012 Configuration Package | done | config/__init__.py, config/schema.py, config/defaults.py, config/loader.py, test_config.py |
| B-013 Configuration Secrets and Remaining Tests | done | config/secrets.py, test_translation.py, test_redaction.py, test_config_secrets.py |

`test_config_secrets.py` is deferred. It is listed in `BUILD_ORDER.md` B-013 but does not yet exist in the tree. The chain test currently counts 256 tests without it. When it is written, the count will rise accordingly.

The translation layer was extended after Phase 5 to add two mappings: `json.JSONDecodeError` and `tomllib.TOMLDecodeError`, both to `CONFIG_PARSE_ERROR`. See the Phase 5 deviations below.

### Phase 2 — Schemas (complete)

| Block | Status | Files |
|---|---|---|
| B-014 Core Structural Schemas | done | cpg.json, runtime-contract-map.json, recon.json, blast-radius.json, contract-assertions.json |
| B-015 Flow and Verification Schemas | done | context-pack.json, verification.json, plan-approval.json, consent.json, checkpoint.json |
| B-016 Output and Extension Schemas | done | audit-entry.json, plugin-manifest.json, skill.json, run-summary.json |

Fourteen schemas, all draft 2020-12. Every schema has `additionalProperties: false` on closed objects and a top-level `extensions` field with `additionalProperties: true`.

### Phase 3 — Storage (complete)

| Block | Status | Files |
|---|---|---|
| B-017 Migrations, SQLite Store, Filesystem Store | done | storage/__init__.py, migrations/__init__.py, migrations/001_initial.sql, sqlite_store.py, filesystem_store.py |
| B-018 Graph, Cache, Migrations Runner, Cleanup | done | graph_store.py, cache.py, migration_runner.py, cleanup.py |
| B-019 Skill Store and Storage Tests | done | skill_store.py, test_storage.py, test_sqlite_wal.py, test_skill_store.py |

Twenty application tables plus `_schema_migrations`. The CPG binary header format is defined here as a 64-byte little-endian structure. The skill store uses FTS5 for keyword search and vec0 for semantic search.

### Phase 4 — Code Interfaces (complete)

| Block | Status | Files |
|---|---|---|
| B-020 Core Provider Interfaces | done | interfaces/__init__.py, repository.py, apply.py, model.py, sandbox.py |
| B-021 Core Engine Interfaces | done | orchestrator.py, agent.py, tool.py, verifier.py, reporter.py |
| B-022 Extension Interfaces and Bridge Protocol | done | language.py, mcp_client.py, mcp_server.py, core/bridges/__init__.py, core/bridges/protocol.py |

Twelve Protocols plus their DTOs. 43 symbols exported from `codeguardian.interfaces`. 11 symbols exported from `codeguardian.core.bridges`. No implementations.

Three design decisions were settled before writing began:

1. DTOs live in their interface's own file. `SandboxConfig` renamed to `SandboxSpec` to avoid collision with `config.SandboxConfig`. `Change` renamed to `FileChange`. `Sandbox` renamed to `SandboxHandle`.
2. Async applies to methods invoked concurrently: Model, Tool, Verifier, MCPClient, MCPServer, Agent. Sync applies to methods invoked once: Repository, Apply, Sandbox, Orchestrator, Reporter, Language.
3. `@runtime_checkable` on exactly four Protocols: ToolPlugin, VerifierPlugin, ReporterPlugin, LanguagePlugin.

### Phase 5 — Bridges (complete)

| Block | Status | Files |
|---|---|---|
| B-023 Python Bridge Implementations | done | pyo3_bridge.py, jsonrpc_bridge.py, rpc/Cargo.toml, rpc/src/lib.rs, rpc/src/protocol.rs |
| B-024 Rust RPC and Kernel Crate Skeleton | done | rpc/src/server.rs, rpc/src/client.rs, kernel/Cargo.toml, kernel/src/lib.rs |
| B-025 Bridge Tests | done | integration/__init__.py, test_pyo3_bridge.py, test_jsonrpc_bridge.py, test_phase_pipeline.py |

Three design decisions were settled before writing began:

1. JSON-RPC framing is line-delimited JSON. Each message is one JSON object followed by a newline.
2. The PyO3 bridge is proved end-to-end in Phase 5 with a single `echo` function. Phase 6 extends the same `lib.rs` to register kernel bindings.
3. The PyO3 extension module is named `codeguardian._native`.

The Rust `RpcServer` and `RpcClient` compile and pass `cargo test --workspace` with zero tests. The Rust `RpcServer` has not been exercised against real input. Its first real exercise will be in Phase 6, when a Rust binary is wired that the Python bridge can spawn.

---

## Test counts

| Phase boundary | Passing tests |
|---|---|
| Phase 1 | 165 |
| Phase 2 | 165 (no new tests) |
| Phase 3 | 227 (165 + 62 new) |
| Phase 4 | 227 (no new tests) |
| Phase 5 | 252 (227 + 25 new integration tests) |
| Post-Phase-5 translation fix | 256 (252 + 4 new translation tests) |

Chain test command: `PYTHONPATH=python python3 -m pytest tests/ -q`
Expected result: `256 passed`.

Isolation test command for Phase 5 tests: `PYTHONPATH=python python3 -m pytest tests/integration/ -v`
Expected result: `25 passed`.

Rust workspace: `cargo test --workspace`
Expected result: seven test binaries, all reporting zero tests. No failures.

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
| 5 | `additionalProperties: false` on every closed object in all fourteen schemas | The design doc left this off. Closed objects catch typos and field drift at validation time. Authorized before Block B-014. |
| 6 | `extensions` field added to every schema and to selected nested objects | The schema needs a path for new fields without a version bump. Authorized before Block B-014 rewrite. |
| 7 | Tested per block instead of per phase | Per-block commands caught errors one block at a time rather than all at once. The phase-boundary triple test still ran as required. |
| 8 | Test command uses `Draft202012Validator.check_schema()` instead of `jsonschema.validate()` with the meta-schema | `check_schema` is the canonical API for validating a schema document. |

### Phase 3 deviations

| # | What | Why |
|---|---|---|
| 9 | `migrations.py` renamed to `migration_runner.py` | A module and a package cannot share a name in the same parent. BUILD_ORDER.md corrected to match. |
| 10 | Added `STORAGE_MIGRATION_FAILED`, `STORAGE_SCHEMA_INVALID`, `STORAGE_GRAPH_CORRUPTED` to codes.py and taxonomy.py | Storage had no codes that mapped to its failure modes. All three are categorized as SYSTEM. The latter two carry non-retryable overrides because their failures are deterministic. |
| 11 | Added `_schema_migrations` table not in the design doc | Every migration runner needs a table that records applied versions. Created by `SQLiteStore.open()` before any migration is considered. |
| 12 | Defined the CPG binary header format in Phase 3 | The design doc says the CPG is a binary file but does not specify its layout. A 64-byte little-endian header was defined and committed as the v1 format. Phase 6 writes files that match it byte for byte. |
| 13 | Real pytest files under `tests/` instead of per-block inline scripts | The B-017 and B-018 inline scripts were verification tools. The permanent suite lives in `tests/`. |
| 14 | `sqlite_store.py` uses `contextlib.suppress` in two places | Ruff's SIM105 rule flagged `try`/`except`/`pass`. The change is cosmetic. |

### Phase 4 deviations

| # | What | Why |
|---|---|---|
| 15 | `SandboxConfig` renamed to `SandboxSpec` | Avoids collision with `config.SandboxConfig` from Phase 1. |
| 16 | `Change` renamed to `FileChange` in repository.py | `Change` was too generic. `FileChange` states what it is. |
| 17 | `Sandbox` renamed to `SandboxHandle` in sandbox.py | Avoids confusion with `SandboxSpec`. |
| 18 | `ModelProvider.stream` is a regular `def` returning `AsyncIterator[ModelChunk]`, not `async def` | An `async def` returning an async iterator forces callers to `await` before iterating. The regular `def` is the correct shape for an async generator. |
| 19 | `Agent` is generic over `[InputT, OutputT]` | Each agent has a different input and output. A non-generic protocol would force every agent to accept a generic bag and return an untyped result. |
| 20 | `VerifierPlugin.verify` takes one argument, not two | Both docs show `verify(diff, context)`, but `context` contains `diff`. Collapsed to `verify(context)`. |
| 21 | `interfaces/__init__.py` rewritten with a `TYPE_CHECKING` block | The dynamic import loop was invisible to mypy. The `TYPE_CHECKING` block declares the same names statically without changing runtime behavior. **Rule 1 escalation.** |

### Phase 5 deviations

| # | What | Why |
|---|---|---|
| 22 | `rust/Cargo.toml` (empty placeholder) deleted | It broke cargo commands run from inside `rust/`. The workspace root at the repo root is the only manifest needed. |
| 23 | `rust/crates/codeguardian-pyo3/Cargo.toml` and `src/lib.rs` written in Phase 5 | Chosen so the PyO3 bridge is proved end-to-end in Phase 5 rather than deferred. **Rule 1 escalation, approved before Phase 5 started.** |
| 24 | `rust/crates/codeguardian-rpc/src/lib.rs` rewritten in B-024 to add `pub mod client; pub mod server;` | The expected pattern for a Rust crate: `lib.rs` is the module registry and grows as modules land. **Rule 1 escalation.** |
| 25 | Two new entries in `errors/translation.py`'s `_EXCEPTION_CODES` for `json.JSONDecodeError` and `tomllib.TOMLDecodeError`, both mapping to `CONFIG_PARSE_ERROR` | Without these, a malformed JSON or TOML file reaches the fallback and is classified as `SYS_INTERNAL`, which is retryable. Retrying a syntax error wastes three attempts. **Rule 1 escalation, done after Phase 5.** |
| 26 | Four new tests in `test_translation.py` for the two new mappings | Each mapping gets a test for the code and a test for the non-retryable policy. |

---

## Escalation queue

| # | File | Status |
|---|---|---|
| 1 | `interfaces/__init__.py` | Resolved in Phase 4. |
| 2 | `rust/crates/codeguardian-pyo3/src/lib.rs` | **Open.** Phase 6 must extend this file to register kernel bindings. This is the pre-approved extension of the PyO3 module. |
| 3 | `rust/crates/codeguardian-rpc/src/lib.rs` | Resolved in Phase 5 B-024. |
| 4 | `errors/translation.py` and `tests/unit/core/test_translation.py` | Resolved after Phase 5. |

Only escalation 2 remains open.

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md`, and `docs/public-api.md`.
2. Do not attempt to re-verify Phases 1 through 5. They are green and pushed. Trust the state.
3. Ask the user to `source .venv/bin/activate` before any Python command.
4. Confirm the Rust toolchain is on `PATH` inside the venv. If `cargo --version` fails, the venv activate script no longer appends `$HOME/.cargo/bin`.
5. Start Phase 6 — Rust Kernel. Blocks B-026 through B-030.
6. Before writing any file, settle the design questions Phase 6 raises:
   - What is the concrete AST node type?
   - What is the concrete CPG node and edge representation?
   - How is the graph serialized to match the Phase 3 header?
   - What is the incremental update algorithm?
   - What queries does the query interface expose?
   - Which Tree-sitter grammars are bundled?
7. Remember escalation 2. Phase 6 extends `rust/crates/codeguardian-pyo3/src/lib.rs` to register kernel bindings. That rewrite is pre-approved.

---

## Rules the next chat must follow

- Think before writing. Read the problem twice. Name the invariant. Name the failure mode.
- Write full files only. Never fragments, never diffs.
- Never rewrite a file that has already passed without stopping and asking.
- Test at phase boundaries, not per block.
- Never claim a test passed. Wait for the user's raw output.
- Ask before guessing. Ask before adding a code, symbol, dependency, or schema field.
- Follow `skills.md` for every file.

---

## Known gaps carried forward

These are gaps that were identified during Phases 1 through 5 and deliberately deferred to the phase that needs them. They are recorded here so they are not forgotten.

| # | Gap | Phase it becomes relevant | Notes |
|---|---|---|---|
| G-1 | Multi-process write contention on the SQLite database | Phase 13 | `busy_timeout` handles two writers. Three writers (orchestrator, cleanup thread, MCP server) will eventually time out. |
| G-2 | No retention for metadata tables (`model_calls`, `mcp_tool_calls`, `phase_executions`, `verifier_verdicts`) | Phase 16 | These tables grow forever. The cleanup layer does not touch them. |
| G-3 | No SQLite backup story | Before v1 ships | The database is not backed up. The audit log is safe. |
| G-4 | Cache keys do not include a schema version | When a schema changes | A schema change without a commit change produces stale cache hits. |
| G-5 | No model provenance on skill embeddings | When the caller switches embedding models | If two embedding models are used, vectors become incomparable. |
| G-6 | CPG header has no compression flag | Optional | A 200 MB graph is 200 MB on disk. |
| G-7 | Cleanup runs as a background thread, not a process | Optional | If the main process crashes, cleanup crashes with it. No `codeguardian cleanup` command exists. |
| G-8 | The 64 KB cache threshold is a magic number | Optional | Not configurable. |
| G-9 | The `@typed` decorator discipline is not enforced | Phase 16 | No lint rule, no codebase-walk test. The convention is enforced by memory. |
| G-10 | The Rust `RpcServer` has not been exercised against real input | Phase 6 | It compiles. It has not been run. |
| G-11 | `test_config_secrets.py` does not exist | Phase 1 backlog | Deferred by user decision. |
| G-12 | The Rust error-to-Python mapping at the bridge boundary is not written | Phase 6 | When Rust errors cross the PyO3 or JSON-RPC boundary, they must be mapped to typed `CodeGuardianError` codes. |
| G-13 | The CLI exit-code mapping is not written | Phase 14 | The design doc specifies the mapping. No code implements it. |
| G-14 | Phase 0 B-004 through B-009 are deferred | End of build | 30 files: GitHub configs, workflows, dev container, docs, examples. No runtime impact. |

---

## Git history relevant to Phases 0 through 5

```
<latest>  docs: add build history for phases 1 through 5
<latest>  fix(errors): map json and tomllib decode errors to CONFIG_PARSE_ERROR
<latest>  feat(bridges): complete Phase 5 with PyO3 and JSON-RPC bridges
<earlier> feat(interfaces): complete Phase 4 with twelve Protocols and bridge envelope
<earlier> feat(storage): complete Phase 3 with SQLite, filesystem, graph, cache, and skill stores
34bbe0c   feat: complete Phase 1 foundation and Phase 2 schemas
dc20e43   feat: complete Phase 1 foundation and Phase 2 schemas
2b1b77c   chore(pre-commit): make hooks phase-aware
2b71c5b   fix(core): rename UnknownCodeError, add translation layer, add Rust workspace manifests
36de85d   Full Docs updated
```

Full hashes are on `origin/main`. The working tree is clean at the time of this snapshot.

---

## Where the next phase starts

Phase 6 — Rust Kernel. Five blocks:

- B-026: `parser.rs`, `ast.rs`, `symbol_table.rs`, `call_graph.rs`
- B-027: `cfg.rs`, `pdg.rs`
- B-028: `cpg.rs`, `incremental.rs`, `query.rs`
- B-029: `codeguardian-pyo3/Cargo.toml` (already populated), `codeguardian-pyo3/src/lib.rs` (escalation 2), `codeguardian-pyo3/src/kernel.rs`
- B-030: `tests/unit/rust/__init__.py`, `tests/unit/rust/test_kernel.py`

This is the largest single phase in terms of Rust code. It is where the Code Property Graph is actually built, serialized to the binary format Phase 3 defined, and exposed to Python through PyO3.

The next chat should treat Phase 6 as the point where the CPG header format stops being a promise and becomes a constraint that must be honored byte for byte.
