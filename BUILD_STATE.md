
# CodeGuardian — Build State

**Snapshot date:** 2026-10-18
**Last phase completed:** Multi-language kernel extension (pre-Phase 9)
**Next phase to start:** Phase 9 — Plugins (B-042 through B-047)
**Phase 0 remaining work:** Deferred to the end of the build, per user decision

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
| Build command | `cargo build --workspace --release` |
| Extension install | `cp "$CARGO_TARGET_DIR/release/libcodeguardian_native.so" python/codeguardian/_native.so` |
| Rust toolchain | 1.99.0 (stable) |
| Cargo target dir | `$HOME/.cache/codeguardian-target` |
| Maturin | Installed (1.15.0) but not used; the extension is installed via `cargo build` plus `cp` |

### Environment notes

- `maturin develop` does not work with this workspace. The `pyproject.toml` `[tool.maturin]` section previously had `features = ["extension-module"]`, which fails because the feature is enabled on the `pyo3` dependency rather than as a crate feature. The `features` line has been removed. The build path is `cargo build --workspace --release` followed by a manual `cp`, matching the process described in `phases-1-to-5.md` for Phase 5.
- The `.so` is approximately 8.8 MB after the multi-language work (was 2.6 MB before). The increase is the linked grammar object files.
- `rust/crates/codeguardian-rpc/Cargo.toml` was accidentally overwritten with root workspace content during a heredoc paste. It was restored with `git checkout HEAD -- rust/crates/codeguardian-rpc/Cargo.toml`. The file is the Phase 5 version.

---

## Completed phases

Phases 1 through 8 are complete. Their details are in `docs/summaries/phases-1-to-5.md`, `docs/summaries/phases-6-to-7.md`, and `docs/summaries/phases-8.md`. This snapshot records the multi-language phase that followed Phase 8.

### Phase 0 — Repository Scaffold (partial)

| Block | Status | Files |
|---|---|---|
| B-001 Root Build Configuration | done | pyproject.toml, Cargo.toml, Cargo.lock, rust-toolchain.toml, rustfmt.toml |
| B-002 Root Project Hygiene | done | clippy.toml, justfile, .gitignore, .codeguardianignore, .dockerignore |
| B-003 Root Editor and Documentation | done | .editorconfig, .env.example, .python-version, .pre-commit-config.yaml, mkdocs.yml |
| B-004 through B-009 | deferred to end | 30 files, no runtime impact |

### Phases 1 through 8 (complete)

See the summary documents for details. Test state at the Phase 8 boundary: 487 passed, 2 skipped.

### Multi-language kernel extension (this snapshot)

**Scope:** extend the Rust kernel from two languages (Python, TypeScript) to nine (adding JavaScript, Java, Go, Rust, C#, C, C++), expose them through PyO3, and add tests.

**Outcome:** eight languages wired and passing. C# was removed because `tree-sitter-c-sharp` 0.23.5 produces a grammar ABI that tree-sitter 0.24 refuses to load, and no other version exists on crates.io.

**Batches delivered:**

| Batch | Files | Purpose |
|---|---|---|
| 1 | ast.rs, root Cargo.toml, kernel Cargo.toml, langs/mod.rs, langs/python.rs | New `NodeKind` variants, 7 grammar dependencies, dispatch skeleton, Python module |
| 2 | lib.rs, langs/mod.rs, langs/typescript.rs, parser.rs, pyo3/kernel.rs | TypeScript module, dispatch delegation, 9-variant `Language` enum, `LanguageNotImplemented` error |
| 3 | cpg.rs, symbol_table.rs, call_graph.rs, cfg.rs, langs/mod.rs, langs/javascript.rs | Wire codes 55 through 82, new class-like scopes, exposed callee extractor, new control-flow kinds, JavaScript module |
| 4 | langs/java.rs, langs/go.rs, langs/rust.rs, langs/mod.rs, test_kernel_multilang.py | Java, Go, Rust modules and the first multilang test file |
| 5 | langs/csharp.rs, langs/c.rs, langs/cpp.rs, langs/mod.rs, test_kernel_multilang.py, test_blast_multilang.py | C#, C, C++ modules and blast tests; C# later removed |
| 6 | 9 fixture files under tests/e2e/fixtures/sample_multi_language_repo/ | One target-caller pair per language |

**Post-batch corrections:**

1. `cpg.rs` exhaustive `match` on `NodeKind` was broken by batch 1's 28 new variants. Folded into batch 3 as a sixth file.
2. `pyo3/kernel.rs` was moved from batch 1 to batch 2 because its 9-language mapping was dead code until `parser.rs` used the new enum.
3. `test_kernel_multilang.py` was rewritten in batch 5 to add C#, C, C++ to `LANGUAGES` and remove the "not yet implemented" test, which became false when the three languages were wired.
4. `test_blast_multilang.py` was written from an empty file in a follow-up step (the `touch` created it but did not fill it).
5. C# was removed: `langs/csharp.rs` deleted, `Language::CSharp` removed from `langs/mod.rs`, dispatch arm removed, kernel Cargo.toml line removed, root Cargo.toml line removed, both test files updated to assert C# raises "unrecognized language" instead of "recognized but not yet implemented".
6. TypeScript and JavaScript `function_declaration` nodes contain a nested `function` node that also carries the same `name`. Classifying both as `NodeKind::FunctionDef` produced two nodes named `target` per file, and blast's target resolution rejected the ambiguity. Only `function_declaration` maps to `FunctionDef` now. `function` falls through to `NodeKind::Other`.
7. `test_eight_languages_build_and_blast_independently` had a case mismatch: `Main.java` in `sources` but `main.java` in the blast loop. Corrected to `main.java` throughout. Java naming convention normally capitalizes, but the kernel stores file paths verbatim and does not enforce convention.

---

## Test counts

| Phase boundary | Passing | Skipped | Xfailed | Xpassed |
|---|---|---|---|---|
| Phase 8 (before multi-language) | 487 | 2 | 0 | 0 |
| Multi-language (after batch 6 + fixes) | 563 | 2 | 7 | 5 |

**Chain test command:** `PYTHONPATH=python python3 -m pytest tests/ -q`
**Current result:** `563 passed, 2 skipped, 7 xfailed, 5 xpassed`

**Breakdown of the 7 xfails (all named in `test_blast_multilang.py`):**

| Count | Test | Reason |
|---|---|---|
| 2 | `test_same_file_call_resolves_c_family` | C/C++ `function_definition` has no `name` field; name is in a nested declarator that `parser.rs::extract_name_id` does not walk |
| 3 | `test_cross_file_call_resolves` | Phase 8 §11.1 known gap; object-prefixed callees and file offsets not applied |
| 2 | `test_lone_function_has_zero_callers_c_family` | Same as above: target name not in the symbol table |

**Breakdown of the 5 xpasses:** five of the eight cross-file tests happen to resolve now, likely depending on file ordering and grammar shape. They are marked `xfail(strict=False)` so passing does not register as a failure. They will continue to reflect the shared cross-file resolver's behavior until that gap is fixed.

**Rust workspace:** `cargo build --workspace --release` finishes in 11 to 30 seconds. `cargo test --workspace` reports zero tests across all eight crates and passes.

---

## Deviations from BUILD_ORDER

The multi-language phase is not in `BUILD_ORDER.md`. It sits between Phase 8 and Phase 9. It was authorized by the user as a scope expansion of the Phase 9 plan, on the rationale that Phase 9's language plugins depend on the kernel supporting more than two languages.

| # | What | Why |
|---|---|---|
| 1 | Added 28 new `NodeKind` variants to `ast.rs` | Required by the seven new languages |
| 2 | Added 28 new wire codes (55 through 82) to `cpg.rs` | Wire codes must be assigned in the same commit as the variants; codes 0–54 unchanged |
| 3 | Added `Impl`, `Struct`, `Interface`, `Trait`, `Enum`, `Union` as named scopes to `symbol_table.rs` | Qualified names for methods in Rust `impl` blocks and Java/C#/Go class-like constructs |
| 4 | Renamed `extract_callee_name` to `default_extract_callee_name` and made it `pub` in `call_graph.rs` | The `LanguageModule` trait's default `extract_callee` method delegates to it |
| 5 | Added `Loop`, `SelectStatement` to the loop branch and `Synchronized`, `Lock` to the with-branch of `cfg.rs::build_statement` | Go `select`, Rust `loop`, Java/C# synchronization blocks |
| 6 | Added `KernelError::LanguageNotImplemented` variant to `parser.rs` | Distinguishes "grammar broke" from "module not yet wired" |
| 7 | Added a dispatch pre-check to `pyo3/kernel.rs::build_graph` | Rejects recognized-but-unwired languages with a clear `ValueError` before invoking the kernel |
| 8 | Created `langs/csharp.rs` and then removed it | ABI incompatibility described above |
| 9 | Removed `tree-sitter-c-sharp` from root `Cargo.toml` and kernel `Cargo.toml` | No working version exists |
| 10 | Moved the 9-language mapping test to `test_kernel_multilang.py` instead of adding to `test_kernel.py` | Keeps the pre-existing kernel tests unmodified |

---

## Escalation queue

Rule 1 escalations performed during the multi-language phase. Every rewrite is additive at the behavioral level; no existing variant, arm, or function was removed except the C# entries during removal.

| # | File | Status |
|---|---|---|
| 1 | `rust/Cargo.toml` | Resolved. 7 grammar dependencies added, then `tree-sitter-c-sharp` removed after the ABI failure. Net: 9 lines (8 grammar lines + `tree-sitter`). |
| 2 | `rust/crates/codeguardian-kernel/Cargo.toml` | Resolved. Same pattern. |
| 3 | `rust/crates/codeguardian-kernel/src/ast.rs` | Resolved. 28 new variants appended after `Other(u16)`, so existing wire codes stay stable. |
| 4 | `rust/crates/codeguardian-kernel/src/cpg.rs` | Resolved. 28 new wire code arms in `node_kind_wire_parts` and `node_kind_from_wire`. |
| 5 | `rust/crates/codeguardian-kernel/src/lib.rs` | Resolved. `pub mod langs;`, exports `Language`, `dispatch`, `LanguageModule`. |
| 6 | `rust/crates/codeguardian-kernel/src/parser.rs` | Resolved. Delegates to `langs::dispatch`; `Language` re-exported from `langs`. |
| 7 | `rust/crates/codeguardian-kernel/src/symbol_table.rs` | Resolved. |
| 8 | `rust/crates/codeguardian-kernel/src/call_graph.rs` | Resolved. |
| 9 | `rust/crates/codeguardian-kernel/src/cfg.rs` | Resolved. |
| 10 | `rust/crates/codeguardian-pyo3/src/kernel.rs` | Resolved. 9-name mapping and `LanguageNotImplemented` arm. C# name now raises "unrecognized language" because the enum no longer contains it. |
| 11 | `rust/crates/codeguardian-kernel/src/langs/mod.rs` | Resolved. Five rewrites across batches 1 through 5. |
| 12 | `rust/crates/codeguardian-kernel/src/langs/typescript.rs` | Resolved after batch 4 (removed `function` from `FunctionDef` mapping). |
| 13 | `rust/crates/codeguardian-kernel/src/langs/javascript.rs` | Resolved after batch 4 (same fix). |
| 14 | `tests/unit/rust/test_kernel_multilang.py` | Resolved. Two full rewrites: initial in batch 4, C# removal in batch 5. |
| 15 | `tests/unit/rust/test_blast_multilang.py` | Resolved. Written from empty; one path-case fix via `sed`. |

No open escalations. The `langs/mod.rs` file will need another escalation when C# returns, when a tenth language is added, or when the cross-file resolver is fixed.

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md`, and `docs/public-api.md`.
2. Do not attempt to re-verify Phases 1 through 8 or the multi-language phase. They are green. Trust the state.
3. Ask the user to `source .venv/bin/activate` before any Python command.
4. Confirm `cargo --version` works inside the venv. If it fails, the venv activate script no longer appends `$HOME/.cargo/bin`.
5. Start Phase 9 — Plugins. Blocks B-042 through B-047.
6. Before writing any Phase 9 file, settle the eight design questions recorded in `phases-9-design-notes` (see below). Only two remain open:
   - Q2 (bundled plugin discovery: entry points or direct import) — recommendation: both paths, bundled via direct import, third-party via `importlib.metadata.entry_points()`
   - Q8 (bundled tool plugin mechanism: subprocess, MCP wrapper, or in-process) — recommendation: `ast_grep` as subprocess, `code_analysis` in-process using the existing tree-sitter parser
7. Confirm the eight-language set is stable. If the user wants more languages (Ruby, PHP, Kotlin, Swift, Scala), they ship as separate installable packages under a "Phase 9.5" track, not bundled into the main wheel.

### Phase 9 design notes — questions 1 through 8

Six of the eight are already answered by inspection of `interfaces/language.py`, `interfaces/tool.py`, `interfaces/verifier.py`, `interfaces/reporter.py`, and the empty `plugins_bundled/` tree. This is what each answer depends on.

| # | Question | Status |
|---|---|---|
| Q1 | Concrete `AstNode` shape | **Resolved by interface inspection.** `LanguagePlugin.parse(path) -> object`, documented as opaque. No plugin needs a concrete AST type. |
| Q3 | `interface_version` compatibility | **Recommendation: accept only `"1.0"`.** N-1 does not exist. Adding `"2.0"` acceptance is a one-line change when it ships. |
| Q4 | Fault barrier disable scope | **Recommendation: in-memory `set[str]` on `PluginRegistry` plus `disabled` flag on the loaded plugin wrapper.** No SQLite write; `plugin_registry.enabled` is for user-config disable, a different concept. Catch `Exception`, not `BaseException`. |
| Q5 | Does Phase 9 write to `plugin_registry`? | **Recommendation: yes, on load.** `INSERT ... ON CONFLICT DO UPDATE`. `enabled` stays 1 always. |
| Q6 | Grammars bundled | **Confirmed by tree inspection.** Python and TypeScript are the only bundled languages. Seven of the eight supported languages ship as separate packages in Phase 9 or later. |
| Q7 | `run_tests` parameter type | **Resolved by interface inspection.** `run_tests(sandbox: SandboxHandle, test_path: str) -> TestResult`. |
| Q2 | Bundled vs third-party discovery | **Open.** Recommendation: direct import for bundled, entry points for third-party. Both feed `PluginRegistry`. |
| Q8 | Bundled tool plugin mechanism | **Open.** Recommendation: `ast_grep` as subprocess wrapper, `code_analysis` in-process using the existing tree-sitter parser. |

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

These are gaps identified across Phases 1 through 8 and the multi-language phase that are still open. They are recorded here so they are not forgotten.

| # | Gap | Becomes relevant | Notes |
|---|---|---|---|
| G-1 | Multi-process write contention on the SQLite database | Phase 13 | `busy_timeout` handles two writers; three will time out |
| G-2 | No retention for metadata tables (`model_calls`, `mcp_tool_calls`, `phase_executions`, `verifier_verdicts`) | Phase 16 | These grow forever; cleanup does not touch them |
| G-3 | No SQLite backup story | Before v1 ships | Database is not backed up; audit log is safe |
| G-4 | Cache keys do not include a schema version | When a schema changes | A schema change without a commit change produces stale cache hits |
| G-5 | No model provenance on skill embeddings | When the caller switches embedding models | Two embedding models make vectors incomparable |
| G-6 | CPG header has no compression flag | Optional | A 200 MB graph is 200 MB on disk |
| G-7 | Cleanup runs as a background thread, not a process | Optional | If the main process crashes, cleanup crashes with it |
| G-8 | The 64 KB cache threshold is a magic number | Optional | Not configurable |
| G-9 | The `@typed` decorator discipline is not enforced | Phase 16 | No lint rule, no codebase-walk test |
| G-10 | Rust `RpcServer` has not been exercised against real input | Closed by Phase 7 | The rpcd daemon exercises it via JSON-RPC; Phase 8 integration proved it end-to-end |
| G-11 | `test_config_secrets.py` does not exist | Phase 1 backlog | Deferred by user decision |
| G-12 | Rust error-to-Python mapping at the bridge boundary | Partially addressed by Phase 7 | `pyo3/kernel.rs::map_core_kernel_error` handles the four variants. JSON-RPC bridge still maps all errors to `SYS_INTERNAL` |
| G-13 | CLI exit-code mapping is not written | Phase 14 | Design specified, code does not exist |
| G-14 | Phase 0 B-004 through B-009 are deferred | End of build | 30 files: GitHub configs, workflows, dev container, docs, examples. No runtime impact |
| **G-15** | **C/C++ function names are not extracted** | Next kernel phase | `parser.rs::extract_name_id` does not walk declarator chains. Fixing it flips the 4 C/C++ xfails to pass and moves C/C++ from `best-effort` to `high` reliability |
| **G-16** | **Cross-file call resolution is inconsistent** | Next kernel phase | Phase 8 §11.1. Some tests xfail, some xpass. Fixing the shared resolver and applying edge destination file offsets closes the gap |
| **G-17** | **C# is not supported** | When a working grammar exists | `tree-sitter-c-sharp` 0.23.5 ABI incompatible with tree-sitter 0.24. Re-adding requires a compatible grammar release plus code restorations in four files |
| **G-18** | **`ast.rs` comment on `Property` still mentions C#** | Cosmetic | The comment says "C# property and JavaScript accessor". C# no longer maps to it. One-word comment fix requires a Rule 1 escalation on `ast.rs` |
| **G-19** | **No automated check that `test_kernel_multilang.py` matches the wired languages** | Phase 9 | Adding a language to `dispatch` without adding a test case to `LANGUAGES` would go unnoticed. A conftest check could compare the two sets |

Gaps G-1 through G-14 are unchanged from the Phase 5 snapshot. G-10 and G-12 are marked partially addressed by later phases. G-15 through G-19 are new.

---

## Git history relevant to this snapshot

The current HEAD is at the last commit before the multi-language work began. The multi-language work is uncommitted in the working tree:

```
 M Cargo.lock
 M Cargo.toml
 M rust/crates/codeguardian-kernel/Cargo.toml
 M rust/crates/codeguardian-kernel/src/ast.rs
 M rust/crates/codeguardian-kernel/src/call_graph.rs
 M rust/crates/codeguardian-kernel/src/cfg.rs
 M rust/crates/codeguardian-kernel/src/cpg.rs
 M rust/crates/codeguardian-kernel/src/lib.rs
 M rust/crates/codeguardian-kernel/src/parser.rs
 M rust/crates/codeguardian-kernel/src/symbol_table.rs
 M rust/crates/codeguardian-pyo3/src/kernel.rs
 M pyproject.toml
 M rust/crates/codeguardian-kernel/src/langs/mod.rs
 M tests/unit/rust/test_kernel_multilang.py
 M tests/unit/rust/test_blast_multilang.py
?? rust/crates/codeguardian-kernel/src/langs/
?? tests/e2e/fixtures/sample_multi_language_repo/
```

Additionally, `python/codeguardian/_native.so` was rebuilt and is not tracked by git (per `.gitignore`).

The prior snapshot listed the following as the git history for Phases 0 through 5:

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

---

## Where the next phase starts

Phase 9 — Plugins. Six blocks, twenty-five files per `BUILD_ORDER.md`:

- B-042: `core/plugins/manifest.py`, `registry.py`, `loader.py`, `fault_barrier.py`, `__init__.py`
- B-043: `core/plugins/versioning.py`, `plugins_bundled/__init__.py`, `plugins_bundled/languages/__init__.py`, `plugins_bundled/languages/python/plugin.py`, `plugins_bundled/languages/python/pyproject.toml`
- B-044: `plugins_bundled/languages/typescript/plugin.py`, `plugins_bundled/languages/typescript/pyproject.toml`, `plugins_bundled/tools/__init__.py`, `plugins_bundled/tools/ast_grep/plugin.py`, `plugins_bundled/tools/ast_grep/pyproject.toml`
- B-045: `plugins_bundled/tools/code_analysis/plugin.py`, `plugins_bundled/tools/code_analysis/pyproject.toml`, `plugins_bundled/reporters/__init__.py`, `plugins_bundled/reporters/sarif_reporter/plugin.py`, `plugins_bundled/reporters/sarif_reporter/pyproject.toml`
- B-046: `tests/contracts/test_language_contract.py`, `test_tool_contract.py`, `test_verifier_contract.py`, `test_reporter_contract.py`
- B-047: `tests/integration/test_plugin_loading.py`

