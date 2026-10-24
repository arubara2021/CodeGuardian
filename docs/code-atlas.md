# Code Atlas — Phases 0 through 9

**Snapshot date:** 2026-10-20
**Purpose:** A plain-language map of what exists in CodeGuardian, what each subsystem does, what breaks if it disappears, and what is honestly missing. Not a tutorial. Not a design document. A navigation map.

**How to read this file:** Each section covers one subsystem. Provenance markers state whether the section was generated from code I have read in full ([READ]), from a mix of read and summarized sources ([PARTIAL]), or entirely from phase summaries and public API documentation ([SUMMARY]). Sections marked [SUMMARY] must be verified against the code before being trusted.

**Verification procedure:** For [SUMMARY] sections, open the primary file, run `pytest tests/` for the tests named in "If I delete it," and confirm each claim in the "What it does" line. When a claim does not match, correct this file, not the code. Then investigate why the summary drifted.

---

## 1. Interfaces (Phase 4) — [READ for four, PARTIAL for the rest]

**Files:**
- `python/codeguardian/interfaces/__init__.py` — dynamic re-export plus `TYPE_CHECKING` mirror
- `python/codeguardian/interfaces/repository.py`, `apply.py`, `model.py`, `sandbox.py`, `orchestrator.py`, `agent.py`, `tool.py`, `verifier.py`, `reporter.py`, `language.py`, `mcp_client.py`, `mcp_server.py`

**What it does:** Defines every cross-component boundary as a `typing.Protocol`. 12 Protocols, 43 exported symbols. No core module imports a concrete class across a boundary. Four Protocols (`ToolPlugin`, `VerifierPlugin`, `ReporterPlugin`, `LanguagePlugin`) are `@runtime_checkable` for plugin loading; the rest are static-only for mypy. The package `__init__.py` walks its own modules with `pkgutil.iter_modules` and re-exports every symbol declared in any submodule's `__all__`.

**Public surface:** All 43 symbols are re-exported from `codeguardian.interfaces`. `from codeguardian.interfaces import RepositoryProvider, ModelProvider, SandboxBackend, LanguagePlugin, ToolPlugin, ReporterPlugin, VerifierPlugin, Agent, Orchestrator, MCPClient, MCPServer` plus the DTOs (`FileChange`, `ConsentRecord`, `ModelOptions`, `SandboxSpec`, `VerifierContext`, `VerifierVerdict`, `Finding`, `TestResult`, `RunConfig`, `RunResult`, `RunState`, `ToolDescriptor`, `ToolResult`, `ContractAssertion`).

**Depends on:** nothing beyond the standard library and `typing`.

**If I delete it:** every provider, plugin, and agent that declares conformance to a Protocol loses its contract. The plugin registry's `isinstance(instance, ToolPlugin)` check fails. Mypy loses all cross-module verification. The `docs/public-api.md` contract becomes unbacked. Nothing imports it directly, so no import error — but the entire type system collapses to `Any` silently.

**Honest gaps:**
- `interfaces/__init__.py` uses dynamic `pkgutil.iter_modules` discovery. If any submodule fails to import (syntax error, missing dependency), the entire package fails to import — one broken Protocol file breaks all 12.
- No versioning on individual Protocols. `NFR-MT5` promises "interfaces must be semantically versioned." No file declares a version today.

---

## 2. Errors and Config (Phase 1) — [READ for the taxonomy, SUMMARY for config internals]

**Files:**
- `python/codeguardian/core/errors/codes.py` — 51 string constants, grouped by subsystem
- `python/codeguardian/core/errors/taxonomy.py` — `ErrorCode` enum (51 members), `ErrorCategory` (7 members), `Backoff` (3 members), `RetryPolicy`, `CodeGuardianError`, `UnknownCodeError`, `CATEGORY_MAP`, `RETRY_POLICY`, `RETRY_POLICY_OVERRIDES`, `category_of`, `retry_policy_of`
- `python/codeguardian/core/errors/__init__.py` — public surface re-export
- `python/codeguardian/core/errors/translation.py` — `translate`, `typed`
- `python/codeguardian/core/errors/retry.py` — `compute_delay`, `retry_sync`, `retry_async`
- `python/codeguardian/core/errors/redaction.py` — `redact`, `DEFAULT_MARKER`
- `python/codeguardian/core/config/{schema,defaults,loader,secrets,__init__}.py`
- `python/codeguardian/version.py`

**What it does:** Provides the complete error vocabulary every other subsystem uses. Every raise site in the codebase raises `CodeGuardianError` or a subclass. The enum-to-string bijection is enforced by tests — every code in `codes.py` is an `ErrorCode` member and vice versa. Retry behavior is derived from category, with per-code overrides for the three cases where the category default would retry a deterministic failure. `translation.py` converts stdlib exceptions (17 types) into typed errors at call boundaries. `redaction.py` strips secrets from structured data before it reaches the audit log.

**Public surface:** `from codeguardian.core.errors import CodeGuardianError, ErrorCode, ErrorCategory, Backoff, RetryPolicy, category_of, retry_policy_of, compute_delay, retry_sync, retry_async, translate, typed, redact, DEFAULT_MARKER, UnknownCodeError`. `from codeguardian.core.config import load_config, get_api_key, set_api_key, delete_api_key, Config, GeneralConfig, ModelsConfig, ProvidersConfig, StorageConfig, SandboxConfig, MCPConfig, ExclusionsConfig, validate`.

**Depends on:** standard library, `jsonschema`, `keyring`.

**If I delete it:** every raise site fails to import. `tests/unit/core/test_errors.py` (45 tests) fails. Retry policy is gone — a rate-limited model call raises immediately instead of backing off. Secrets leak into audit entries.

**Honest gaps:**
- Adding an error code requires editing two files (`codes.py` and `taxonomy.py`) in exact sync. Test `test_codes_module_matches_enum` catches drift, but the two-file edit is a Rule 1 escalation per code. Phase 9 added five codes, all via this route.
- `translation.py` and `redaction.py` I have not read. Their behavior is known only from `public-api.md` and `test_errors.py` passing.
- `retry.py` uses pre-PEP-695 generic syntax (`TypeVar`). `ruff` rule `UP047` flags this; it is deferred per `pyproject.toml` ignore list.

---

## 3. Storage (Phase 3) — [SUMMARY]

**Files:**
- `python/codeguardian/core/storage/sqlite_store.py` — WAL mode, six PRAGMAs applied per connection
- `python/codeguardian/core/storage/filesystem_store.py` — artifact read/write with schema validation on write
- `python/codeguardian/core/storage/graph_store.py` — CPG binary file metadata and path resolution
- `python/codeguardian/core/storage/cache.py` — LRU memory tier (256 entries) + disk tier keyed by `(repo_id, commit_hash, cache_type)`
- `python/codeguardian/core/storage/migration_runner.py` — reads `.sql` files, tracks `_schema_migrations`
- `python/codeguardian/core/storage/cleanup.py` — retention sweep
- `python/codeguardian/core/storage/skill_store.py` — Markdown + FTS5 + sqlite-vec
- `python/codeguardian/core/storage/migrations/001_initial.sql` — 20 tables

**What it does:** Five separate storage backends, each for a different data shape. SQLite holds queryable metadata. Filesystem holds artifacts (JSON, JSONL, patches, test files). A binary memory-mapped file holds the CPG. An LRU cache sits above disk cache. Markdown is the source of truth for skills; SQLite FTS5 is a keyword index; sqlite-vec is a semantic index. Migrations are versioned and idempotent.

**Public surface:** `from codeguardian.core.storage import SQLiteStore, FilesystemStore, GraphStore, Cache, MigrationRunner, SkillStore, CPGHeader, MigrationResult, CleanupSummary, run_cleanup`.

**Depends on:** `sqlite3`, `sqlite-vec`, `jsonschema`, standard library.

**If I delete it:** every artifact write fails. `tests/unit/core/test_storage.py`, `test_sqlite_wal.py`, `test_skill_store.py` (62 tests total) fail. The apply provider cannot persist its audit log. Recon output has nowhere to land.

**Honest gaps:**
- Single-writer assumption. `busy_timeout = 5000ms` handles two writers; a third times out. `G-1` in BUILD_STATE.
- No schema version in cache keys. A schema bump without a commit change serves stale entries. `G-4`.
- No SQLite backup. `G-3`.
- I have not read any storage file. Every claim above is from `phases-1-to-5.md` and `public-api.md`. Verify against the code.

---

## 4. Bridges (Phase 5) — [SUMMARY for Python, READ for pyo3 lib/kernel]

**Files:**
- `python/codeguardian/core/bridges/protocol.py` — RPC envelope dataclasses
- `python/codeguardian/core/bridges/pyo3_bridge.py` — in-process FFI wrapper
- `python/codeguardian/core/bridges/jsonrpc_bridge.py` — out-of-process line-delimited JSON
- `rust/crates/codeguardian-rpc/src/{lib,protocol,server,client}.rs`
- `rust/crates/codeguardian-rpcd/src/{main,handlers}.rs`

**What it does:** Two bridges, chosen by trust. PyO3 crosses the boundary in-process: no serialization, batchable, but a Rust panic crashes Python. JSON-RPC crosses out-of-process: line-delimited JSON over stdio, isolation guarantees, but slower. The choice per zone: kernel/blast/policy use PyO3; sandbox/audit use JSON-RPC.

**Public surface:** `from codeguardian.core.bridges import JSONRPCBridge, PyO3Bridge, is_native_available, RpcRequest, RpcResponse, RpcError, RpcNotification`. The Rust side exposes `RpcServer::register`, `RpcServer::serve`, `RpcClient::call`.

**Depends on:** `subprocess`, `codeguardian._native`, `serde_json`.

**If I delete it:** Python cannot reach any Rust subsystem. `tests/integration/test_pyo3_bridge.py` and `test_jsonrpc_bridge.py` (25 tests) fail. Sandbox execution and audit writes stop. The deep test's preflight fails at "rpcd binary exists."

**Honest gaps:**
- `G-12` (partial): JSON-RPC errors all map to `SYS_INTERNAL` on the Python side; the specific Rust error variant is lost at the boundary.
- `G-22`: `maturin` does not work with this workspace; the build path is manual `cargo build` + `cp`. Documented in BUILD_STATE.
- The Rust `RpcServer` is exercised only through rpcd (Phase 7). No unit test calls it directly.

---

## 5. Kernel (Phase 6) — [READ for grammar_loader, parser, langs/mod, symbol_table, call_graph, cpg, incremental, ast; SUMMARY for cfg, pdg, query]

**Files:**
- `rust/crates/codeguardian-kernel/src/ast.rs` — flat arena, 36-byte `AstNode`, `NodeKind` (82 variants), `StringTable`
- `rust/crates/codeguardian-kernel/src/parser.rs` — tree-sitter wrapper; `parse_file(&dyn GrammarSource, &str) -> ParseResult`
- `rust/crates/codeguardian-kernel/src/symbol_table.rs` — symbol extraction and scoped lookup
- `rust/crates/codeguardian-kernel/src/call_graph.rs` — receiver-aware `resolve_callee` (simple, `self.foo`, `Class.foo`)
- `rust/crates/codeguardian-kernel/src/cfg.rs` — per-function CFG, 7 edge kinds
- `rust/crates/codeguardian-kernel/src/pdg.rs` — control + data dependence via post-dominator tree and reaching definitions
- `rust/crates/codeguardian-kernel/src/cpg.rs` — merge into a graph, serialize to the 64-byte-header binary format
- `rust/crates/codeguardian-kernel/src/incremental.rs` — file-level update
- `rust/crates/codeguardian-kernel/src/query.rs` — eleven O(1) query methods
- `rust/crates/codeguardian-kernel/src/grammar_loader.rs` — runtime `.so` grammar loading
- `rust/crates/codeguardian-kernel/src/langs/{mod,python,typescript,javascript,java,go,rust,c,cpp}.rs`

**What it does:** Parses source with tree-sitter into a flat arena. Extracts symbols, builds a call graph, builds a per-function CFG and PDG, merges all of it into one CPG, serializes to disk with a checksum. Answers O(1) queries over the graph. Supports incremental updates when a single file changes. Eight languages compiled in via `LanguageModule` trait; new languages can be loaded at runtime from a shared library.

**Public surface:** `from codeguardian_kernel import CodePropertyGraph, NodeKind, EdgeKind, Language, LanguageModule, GrammarSource, parse_file, build_cfg, build_pdg, extract_symbols, extract_calls, GrammarLoader, load_and_register, dispatch_by_name`. From Python: `codeguardian._native.build_graph(sources)`, `.graph_summary(bytes)`, `.load_grammar(name, path)`, `.list_languages()`.

**Depends on:** `tree-sitter` 0.24, eight grammar crates, `libloading`, `serde`, `thiserror`, `tracing`.

**If I delete it:** blast radius has no graph to query. Every plugin's `parse` fails. The deep test fails at "kernel parse." `tests/unit/rust/test_kernel.py` and `test_kernel_multilang.py` (roughly 60 tests) fail.

**Honest gaps:**
- `G-15`: C/C++ `function_definition` puts the name inside a nested declarator chain. `parser.rs::extract_name_id` does not walk it. C/C++ free functions produce no symbols and cannot be targets of resolved calls. Best-effort tier.
- `G-16`: `mod.foo()` cross-file resolution for ambiguous names. `self.foo()` and `Class.foo()` resolve; module-qualified calls do not when the callee name is not unique. Under-reports caller counts on real code.
- `G-17`: C# removed after `tree-sitter-c-sharp` 0.23.5 proved ABI-incompatible with tree-sitter 0.24.
- `cfg.rs`, `pdg.rs`, `query.rs`, and the eight language modules are known from summaries, not full reads. Verify before touching.

---

## 6. Blast Radius (Phase 7) — [READ for callers.rs, SUMMARY for the rest]

**Files:**
- `rust/crates/codeguardian-blast/src/callers.rs` — direct and transitive caller resolution, `MAX_TRANSITIVE_CALLERS = 2000`
- `rust/crates/codeguardian-blast/src/contracts.rs` — narrow `None`-dereference detector
- `rust/crates/codeguardian-blast/src/coverage.rs` — filename-pattern coverage gap detection
- `rust/crates/codeguardian-blast/src/score.rs` — weighted score, cap 100
- `rust/crates/codeguardian-blast/src/recommendation.rs` — proceed/review/block with critical overrides
- `rust/crates/codeguardian-blast/src/lib.rs` — target resolution and report assembly

**What it does:** Given a CPG and a target symbol, produces a report with direct callers, transitive callers (BFS with visited-set cycle protection), contract violations (one specific shape), coverage gaps (filename heuristic), blast score (0-100), and recommendation (proceed/review/block).

**Public surface:** `_native.compute_blast(cpg_bytes, path, qualified_name) -> dict` with keys `target_node_id`, `direct_callers`, `transitive_callers`, `transitive_truncated`, `contract_violations`, `coverage_gaps`, `blast_score`, `recommendation`. Each caller carries `node_id`, `qualified_name`, `path`, `line`, `depth`, `call_kind`.

**Depends on:** `codeguardian-kernel` CPG.

**If I delete it:** Phase 1 gate disappears. Deep test target ranking, contract verifier input, and the CLI blast view all break. `tests/unit/rust/test_blast.py` and `test_blast_multilang.py` fail.

**Honest gaps:**
- Contract detection is one narrow pattern. Detects `x = get(); x.attr` without a guard. Does not detect wrong type, wrong exception, wrong side-effect ordering, or wrong idempotency assumptions.
- Coverage is filename-based, not execution-based. `test_*.py` naming is not coverage. Wrong by 40-60% on real code.
- Score weights (2, 1, 8, 3) are heuristics, never calibrated against real data.
- `count_unresolved_call_sites` exists in `callers.rs` but is not wired into the report. The dict returned to Python does not include unresolved sites. A report saying "3 callers" can hide 30 unresolved call sites.
- G-16 under-reports. Same root cause as §5.

---

## 7. Sandbox (Phase 7) — [SUMMARY]

**Files:**
- `rust/crates/codeguardian-sandbox/src/backend.rs` — `SandboxBackend` trait, environment sanitization, working_dir validation, quota validation
- `rust/crates/codeguardian-sandbox/src/bubblewrap.rs` — Linux backend
- `rust/crates/codeguardian-sandbox/src/docker.rs` — Docker fallback
- `rust/crates/codeguardian-sandbox/src/seatbelt.rs` — stub
- `rust/crates/codeguardian-sandbox/src/firecracker.rs` — stub
- `rust/crates/codeguardian-sandbox/src/{network,pool,quotas}.rs`
- `python/codeguardian/core/sandbox/{bubblewrap,docker,seatbelt,firecracker}_backend.py`, `backend_selector.py`
- `rust/crates/codeguardian-rpcd/` — the daemon that hosts them

**What it does:** Isolates execution. Every `sandbox.execute` is a JSON-RPC call to `rpcd`, which sanitizes env, validates working_dir, checks quotas, spawns `bwrap` or `docker`, enforces a two-layer timeout, and returns the result. `working_dir` is bind-mounted with no path translation. Network is disabled except during dependency resolution. Python never spawns a process; only the daemon does.

**Public surface:** `from codeguardian.core.sandbox import BubblewrapBackend, DockerBackend, SeatbeltBackend, FirecrackerBackend, select_backend`. Each implements `SandboxBackend.create/execute/destroy/supports_language/get_resource_limits`.

**Depends on:** `bwrap` or `docker` binary, `codeguardian-rpcd`.

**If I delete it:** the pipeline cannot run tests, cannot run pytest, cannot run the Tester's characterization tests. `tests/unit/rust/test_sandbox.py` and `test_sandbox_enforcement.py` fail. The deep test's sandbox steps fail.

**Honest gaps:**
- Seatbelt and Firecracker are stubs. Every method raises `SANDBOX_CREATE_FAILED`.
- Docker tested only on CI, not on the dev environment (no WSL integration).
- cgroups v2 writing is not implemented; resource limits use simple `--die-with-parent` + outer timeout, not true memory caps.
- G-16 (unrelated) but visible here: the deep test's `pip install -e .` needs `--break-system-packages` on Python 3.12 systems.
- I have not read any sandbox file. All claims from summaries.

---

## 8. Audit (Phase 7) — [READ for verify, writer, hash_chain]

**Files:**
- `rust/crates/codeguardian-audit/src/hash_chain.rs` — canonical JSON encoding, SHA-256, FNV-1a primitives
- `rust/crates/codeguardian-audit/src/writer.rs` — append-only JSONL, head re-scan per append
- `rust/crates/codeguardian-audit/src/verify.rs` — chain verification
- `rust/crates/codeguardian-audit/src/export.rs` — JSONL and JSON-array formats
- `rust/crates/codeguardian-audit/src/lib.rs` — `AuditEntry`, `GENESIS`, error types

**What it does:** Every entry carries a `payload_hash`, `prev_hash`, and `entry_hash`. The chain formula is `entry_hash = sha256(prev_hash + payload_hash)`, `prev_hash` = previous entry's `entry_hash`, and `GENESIS` for the first. Payload hashing uses canonical JSON — sorted keys, no whitespace — so map insertion order does not change the hash. The file is append-only JSONL, owned by the Rust process, never opened by Python.

**Public surface:** The `codeguardian-rpcd` methods `audit.write`, `audit.verify`, `audit.export`. Called from Python via `JSONRPCBridge.call(...)`.

**Depends on:** `tokio`, `serde_json`, `sha2`.

**If I delete it:** no audit trail exists. Every run's provenance is lost. `tests/unit/rust/test_audit.py` fails. The consent gate has nothing to append to.

**Honest gaps:**
- No file locking. Single-writer assumption. A second writer corrupts the chain silently.
- `head_hash()` on every append reads the full file. O(n) per append. Fine up to ~10k entries.
- No cryptographic signing (deferred to v2 per `D-024`).

---

## 9. Policy, RPC, rpcd (Phase 7) — [SUMMARY]

**Files:**
- `rust/crates/codeguardian-policy/src/{sandbox_policy,plugin_permissions,resource_limits,lib}.rs`
- `rust/crates/codeguardian-rpc/src/{protocol,server,client,lib}.rs`
- `rust/crates/codeguardian-rpcd/src/{main,handlers}.rs`

**What it does:** Three pre-flight checks (sandbox spec against policy, plugin permissions against grant, quota against host) plus the JSON-RPC server that hosts the seven methods, plus the daemon binary. The policy crate exposes three PyO3 functions: `check_sandbox_spec`, `check_plugin_permissions`, `detect_host_limits`.

**Public surface:** `_native.check_sandbox_spec(...)`, `_native.check_plugin_permissions(...)`, `_native.detect_host_limits()`. The daemon binary `codeguardian-rpcd` at `$HOME/.cache/codeguardian-target/release/`.

**Depends on:** `tokio`, `serde_json`, `codeguardian-sandbox`, `codeguardian-audit`.

**If I delete it:** sandbox requests are not policy-checked. Plugin manifests are not validated against grants. Sandbox and audit calls have no transport. `tests/unit/rust/test_sandbox.py` fails.

**Honest gaps:**
- Policy checks are not wired into the current pipeline. They exist; Phase 13 will call them.
- I have not read any policy or rpc file. All claims from summaries.

---

## 10. Providers (Phase 8) — [PARTIAL]

**Files:**
- `python/codeguardian/core/providers/local_repository.py`, `github_repository.py`, `gitlab_repository.py`
- `python/codeguardian/core/providers/apply_provider.py` — [READ in full]
- `python/codeguardian/core/providers/anthropic_model.py`, `openai_model.py`, `deepseek_model.py`, `openai_compatible.py`
- `python/codeguardian/core/providers/model_registry.py`, `fallback_router.py`, `metadata.json`, `registry.json`

**What it does:** Three repository providers (all shell out to real `git`), one apply provider (four-step consent gate, audit entry before first `git apply`, rollback writes its own audit entry — G-20 closed), four model adapters, a discovery-driven model registry (calls `/models` per provider, merges with `metadata.json`, filters by role constraints, excludes families for verifier independence), and a fallback router that retries per error class.

**Public surface:** `from codeguardian.core.providers import LocalProvider, GitHubProvider, GitLabProvider, ApplyProvider, AnthropicProvider, OpenAIProvider, DeepSeekProvider, OpenAICompatibleProvider, ModelRegistry, FallbackModelProvider`.

**Depends on:** `git` binary, `httpx`, `anthropic`, `openai`, `codeguardian.interfaces`.

**If I delete it:** nothing can clone a repo or call a model. `tests/contracts/test_repository_contract.py`, `test_apply_contract.py`, `test_model_contract.py` (~62 tests) fail. The deep test fails at "clone is real git repo."

**Honest gaps:**
- `_ADAPTER_CLASSES` is a hardcoded dict in `model_registry.py`. Adding a new provider adapter is a Rule 1 escalation on that file. See `docs/modularity-audit.md §4.2`.
- `G-16` (kernel) means blast results are under-reported for cross-file ambiguous names. This is the highest-impact gap on real repos.
- `G-21` (closed): `pyo3/blast.rs` emitted `file_path` instead of schema-required `path`. Fixed.
- `G-20` (closed): `apply_provider.rollback` did not write an audit entry. Fixed. Chain now shows 4 entries after apply + rollback.

---

## 11. Plugins (Phase 9) — [READ in full]

**Files:**
- `python/codeguardian/core/plugins/{__init__,manifest,versioning,loader,fault_barrier,permissions,registry}.py`
- `python/codeguardian/plugins_bundled/languages/{python,typescript,javascript,java,go,rust,c,cpp}/plugin.py`
- `python/codeguardian/plugins_bundled/tools/ast_grep/plugin.py`, `tools/code_analysis/plugin.py`
- `python/codeguardian/plugins_bundled/reporters/sarif_reporter/plugin.py`
- `rust/crates/codeguardian-kernel/src/grammar_loader.rs`

**What it does:** Discovery, validation, loading, permission enforcement, timeout enforcement, and dispatch for 11 bundled plugins (8 languages, 2 tools, 1 reporter). Two discovery sources (`BundledSource` walks directories, `EntryPointSource` uses `importlib.metadata`). Manifests validated against `schemas/plugin-manifest.json` v1.2. Interface version negotiated N/N-1 with clear rejection on `2.x+` and `0.x`. Audit hook installed at package import. Fault barrier with sync `call` and async `call_async` paths. Runtime grammar loading for third-party languages.

**Public surface:** `from codeguardian.core.plugins import PluginRegistry, PluginSpec, PluginSource, BundledSource, EntryPointSource, RegisteredPlugin, FaultBarrier, PluginManifest, PluginPermissions, check_interface_version, validate, install_audit_hook, is_installed, plugin_active, sandbox_scope, set_schema_path, shutdown_executor`. 22 public symbols.

**Depends on:** `jsonschema`, `importlib.metadata`, `concurrent.futures`, `contextvars`, `sys.addaudithook`, `_native.load_grammar` / `list_languages`.

**If I delete it:** every language, tool, and reporter plugin disappears. `tests/contracts/test_language_contract.py` (72 tests), `test_tool_contract.py` (14 tests), `test_reporter_contract.py` (4 tests), `tests/integration/test_plugin_loading.py` (17 tests), `test_dynamic_grammar_loading.py` (6 tests) all fail. The kernel's grammar loader has no caller.

**Honest gaps:**
- Audit hook bypassed by C extensions. `sys.addaudithook` is Python-level only.
- `FaultBarrier` cannot time out a pure-Python busy loop holding the GIL. Documented in the docstring.
- `_TYPE_DIRS`, `_PROTOCOLS`, `_ENTRY_POINT_GROUPS`, `_EXPECTED_HIGH_TIER` are four separate tables that must agree. See `docs/modularity-audit.md §4.4, §4.5`.
- Dynamic grammar loading is proven only against a `.so` that was not available in the dev environment (4 tests skipped). `tests/integration/test_dynamic_grammar_loading.py` skips cleanly when `libtree_sitter_ruby` is absent.

---

## 12. Deep Test (cross-cutting) — [READ in full]

**Files:** `sandtest/deep_test.py`

**What it does:** Clones a real repository, parses it, discovers candidate functions structurally via AST, ranks by blast fan-in, runs each candidate twice inside a sandbox to classify determinism, generates a characterization test from the recorded output, refactors the picked target with a real model, applies the diff, verifies behavior via characterization test + three verifiers, rolls back, and proves the moat by injecting AST behavioral drift and asserting the value check catches it. Every step reports `[PASS]`, `[FAIL]`, `[SKIP]`, `[XFAIL]`, or `[WARN]`.

**Public surface:** `python sandtest/deep_test.py`. Reads env vars `CODEGUARDIAN_DEEP_REPO` (default `psf/requests`), `CODEGUARDIAN_DEEP_DEPTH` (default 20), `CODEGUARDIAN_DEEP_SEED` (default 42). Requires `bwrap`, `codeguardian-rpcd`, `_native`, and `GROQ_API_KEY`.

**Depends on:** every subsystem in §1 through §11.

**If I delete it:** nothing in `pytest tests/` fails. This is the only end-to-end proof of the system. Deleting it means no proof that the pipeline works, no proof that the moat catches a bad refactor, no proof of real-repo behavior.

**Last run result:** 59 passed, 2 xfailed (C/C++ declarators), 0 warnings. Real Groq writer (openai/gpt-oss-120b), real verifier (qwen/qwen3.8-27b), byte-identical rollback, 4 audit entries verified, AST drift caught as `semantic-drift`.

**Honest gaps:**
- Only one real repository proven: `psf/requests`. TypeScript, JavaScript, Java, Go, Rust refactor flows have never been run end-to-end.
- Target discovery uses Python `ast`. Non-Python deep tests require new discovery functions.
- Fault injection is AST-based, not model-based. The Writer's attempt to break behavior is combined with AST drift on top, so the value check fires even if the model did not comply. This is correct for proving the moat but does not test whether the model can be relied on to produce bad refactors on command.
- Flask fails at generator imports because transitive deps (`werkzeug`, `jinja2`, `click`) do not install with `-e .`. Flask is not a usable fixture until this is resolved.

---

## 13. What Does Not Exist Yet (Phases 10 through 16)

For completeness — not written, no files to read:

| Phase | Purpose | Files (planned) |
|---|---|---|
| **10** | Prompts for seven agents | `prompts/{analyst,tester,writer,correctness_verifier,security_verifier,contract_verifier,skill_curator}/v1.0.0.md` |
| **11** | Agent implementations | `core/agents/{recon,blast,analyst,tester,writer,correctness_verifier,security_verifier,contract_verifier,skill_curator}.py` |
| **12** | Skill library operations | `core/skills/{library,indexer,retriever,embeddings}.py` |
| **13** | LangGraph orchestrator | `core/orchestrator/{phase_gates,consent,verification_prompt,checkpointing,langgraph_orchestrator}.py` |
| **14** | CLI commands and TUI | `cli/main.py`, `cli/commands/*.py`, `cli/views/*.py`, `cli/ui/*.py`, `cli/output/*.py` |
| **15** | MCP client/server + reporters | `core/mcp/*.py`, `core/reporting/*.py` |
| **16** | Full assembly | `scripts/integration_phase.py`, `scripts/validate_schemas.py`, `tests/e2e/*.py`, `tests/fixtures/*.py` |

Every file in these phases is currently a stub or does not exist. The pipeline has never run end-to-end without the deep test's manual orchestration. `codeguardian refactor` does not work today.

---

## 14. How to verify this file

For each [SUMMARY] section, in order:

1. Open the primary file.
2. Read the top-of-file module docstring and the type signatures of the public functions.
3. Run `pytest tests/ -k "<subsystem_name>"` and read the test names.
4. Confirm the "What it does" line matches what you now know.

For each [PARTIAL] section, do the same for the files marked as unread.

For each [READ] section, spot-check one claim per subsection against the actual file. The claim I am most likely to have wrong is a `depends on` entry (I named dependencies from memory).

For the `If I delete it` claims, run one test named in the section and confirm it fails when the subsystem is disabled. If it does not fail, the claim is wrong.

When a claim is wrong, correct this file. Do not correct the code. The code is what it is; this file is a map, and maps drift. Fixing the map is cheaper than fixing the code, and it is what makes the map trustworthy next time.

---

## 15. Related Documents

- `docs/modularity-audit.md` — modular vs non-modular analysis through Phase 9
- `BUILD_STATE.md` — current gaps (G-1 through G-25)
- `BUILD_ORDER.md` — phase plan
- `docs/04-architecture.md` — full system design
- `docs/05-data-model.md` — schemas and storage
- `docs/public-api.md` — public surface reference (Phase 5 snapshot)
- `sandtest/README.md` — deep test documentation
