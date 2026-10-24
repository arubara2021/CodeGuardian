# CodeGuardian — Build State

**Snapshot date:** 2026-10-24
**Last phase completed:** Phase 8 — Providers, plus the multi-language kernel extension, plus the deep integration test
**Next phase to start:** Phase 9 — Plugins (merged scope: 8 blocks, 36 files)
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
| Maturin | 1.15.0 installed but not used (see environment notes) |
| GROQ_API_KEY | Set in shell before deep test runs |

### Environment notes

- **Maturin does not work with this workspace.** The `pyproject.toml` `[tool.maturin]` section originally had `features = ["extension-module"]`, which fails because the feature is enabled on the `pyo3` dependency rather than as a crate feature. The `features` line has been removed. The build path is `cargo build --workspace --release` followed by a manual `cp`, matching the process described in `phases-1-to-5.md` for Phase 5. This is a known deviation. Do not re-add `features = ["extension-module"]` to `pyproject.toml`.
- **Line endings are normalized.** `.gitattributes` with `* text=auto eol=lf` is committed. WSL2 writing to a Windows filesystem caused mixed line endings that broke pre-commit for eight consecutive commit attempts during the multi-language phase. The permanent fix is in place. Future commits that stage all changes at once (`git add -A`) will not hit this problem.
- **Pre-commit is installed and active.** Every commit runs the hooks. `end-of-file-fixer`, `mixed-line-ending`, `ruff`, `ruff-format` will modify files on the first commit attempt if they find issues. Second attempt passes. This is normal pre-commit behavior, not a bug.
- **`.so` size is approximately 8.8 MB** after the multi-language work (was 2.6 MB before). The increase is the linked grammar object files.
- **`rust/crates/codeguardian-rpc/Cargo.toml` was accidentally overwritten** with root workspace content during the multi-language commit attempts. Restored with `git checkout HEAD -- rust/crates/codeguardian-rpc/Cargo.toml`. The file is the Phase 5 version.

---

## Completed phases

### Phase 0 — Repository Scaffold (partial)

| Block | Status | Files |
|---|---|---|
| B-001 Root Build Configuration | done | pyproject.toml, Cargo.toml, Cargo.lock, rust-toolchain.toml, rustfmt.toml |
| B-002 Root Project Hygiene | done | clippy.toml, justfile, .gitignore, .codeguardianignore, .dockerignore |
| B-003 Root Editor and Documentation | done | .editorconfig, .env.example, .python-version, .pre-commit-config.yaml, mkdocs.yml |
| B-004 through B-009 | deferred to end | 30 files, no runtime impact |

### Phases 1 through 8 — complete

See `docs/summaries/phases-0-to-8.md` for the detailed build history of every phase through Phase 8, plus the multi-language extension and the deep integration test result.

Test state at the Phase 8 boundary: **487 passed, 2 skipped** (Python pytest). Deep test later extended this to 40 additional assertions passing end-to-end on a real repository.

### Multi-language kernel extension — complete

Scope extension between Phase 8 and Phase 9. Extended the kernel from 2 languages (Python, TypeScript) to 8 (adding JavaScript, Java, Go, Rust, C, C++). C# was attempted but removed after `tree-sitter-c-sharp` 0.23.5 produced an ABI incompatible with `tree-sitter` 0.24.

| Batch | Files | Purpose |
|---|---|---|
| 1 | `ast.rs`, root `Cargo.toml`, kernel `Cargo.toml`, `langs/mod.rs`, `langs/python.rs` | New `NodeKind` variants, 7 grammar dependencies, dispatch skeleton |
| 2 | `lib.rs`, `langs/mod.rs`, `langs/typescript.rs`, `parser.rs`, `pyo3/kernel.rs` | TypeScript module, dispatch delegation, 9-variant `Language` enum |
| 3 | `cpg.rs`, `symbol_table.rs`, `call_graph.rs`, `cfg.rs`, `langs/mod.rs`, `langs/javascript.rs` | Wire codes 55–82, new class-like scopes, JavaScript module |
| 4 | `langs/java.rs`, `langs/go.rs`, `langs/rust.rs`, `langs/mod.rs`, test file | Java, Go, Rust modules |
| 5 | `langs/csharp.rs`, `langs/c.rs`, `langs/cpp.rs`, `langs/mod.rs`, two test files | C#, C, C++ and blast tests |
| 6 | 9 fixture files under `tests/e2e/fixtures/sample_multi_language_repo/` | One target-caller pair per language |

**Post-batch corrections (recorded in phases-0-to-8.md §13.3):**
- `cpg.rs` exhaustive `match` on `NodeKind` broke after batch 1's 28 new variants. Fixed in batch 3.
- `pyo3/kernel.rs` moved from batch 1 to batch 2 (its 9-language mapping was dead code until `parser.rs` used the new enum).
- TypeScript and JavaScript `function` node duplicates `FunctionDef` and broke blast's target resolution. Fixed by classifying only `function_declaration`.
- C# was removed after `tree-sitter-c-sharp` 0.23.5's ABI proved incompatible. `langs/csharp.rs` deleted, `Language::CSharp` removed from the enum, dispatch arm removed, grammar dependency removed from both Cargo.toml files, tests updated to assert C# raises `ValueError: unrecognized language`.

### Deep integration test — complete

`sandtest/deep_test.py` (795 lines). Permanent artifact. Clones `pallets/click` into `sandtest/work/`, runs every phase that exists, reports pass/fail/skip/xfail/warn per section. Does not stop at first failure. Downstream sections mark SKIP when a dependency fails.

Last run result:
```
Passed: 40
Failed: 0
Skipped: 0
Xfailed: 2
Warnings: 0
```

The two xfails are C/C++ same-file blast — the declarator-chain gap, expected and documented in the test.

**What the deep test proved end-to-end:**

| Section | Evidence |
|---|---|
| Clone | `pallets/click`, 178 files, HEAD `2247b35ea1c4` |
| Kernel parse | 91 Python files → 12,074,306-byte CPG |
| Graph summary | 242,519 nodes, 266,013 edges |
| Blast `option` | 166 direct, 219 transitive, score 100 |
| Blast `argument` | 86 / 86 / 100 |
| Blast `Command` | 80 / 80 / 100 |
| Blast `Context` | 41 / 41 / 100 |
| Multi-lang parse | 8 files, one CPG |
| Multi-lang blast | 6 languages same-file pass, 2 xfail (C/C++) |
| C# rejection | `ValueError: unrecognized language: csharp` |
| Sandbox #1 | bubblewrap created, network ON, pip install pytest |
| Sandbox #2 | network OFF, pytest green on old code (3 passed) |
| Writer | `openai/gpt-oss-120b` produced 3,583-char refactor |
| Diff + apply | 5,353-char `git apply`-compatible diff, file changed on disk |
| Pytest new | 3 passed — behavior preserved after refactor |
| Verifier | `qwen/qwen3.8-27b` (different family), `PASS` verdict |
| Rollback | file restored byte-identical (21,419 chars both) |
| Audit chain | 1 entry verified |

**What the deep test found (real findings, not test bugs):**
- `click.utils.echo` returns 0 callers — the Phase 8 §11.1 cross-file gap confirmed on a real library (G-21)
- `audit.verify` returns `entries_checked: 1` after apply + rollback — should be at least 2 (G-20)

### Documentation additions — complete

| File | Purpose |
|---|---|
| `docs/git-workflow.md` | Practical git reference for the repo. Branching, recovery, pre-commit troubleshooting, cheat sheet. Read this at the start of every session. |
| `docs/summaries/phases-0-to-8.md` | Complete build history through the multi-language extension, with the deep test result. Supersedes `phases-1-to-5.md`, `phases-6-to-7.md`, `phases-8.md`. |
| `docs/summaries/phase-9-design.md` | The complete Phase 9 design settlement. Authoritative spec for the phase. |
| `schemas/plugin-manifest.json` | Updated to v1.2 — adds `capabilities`, `blast_scope`, `kernel_extensible`, `grammar_path`. |
| `BUILD_ORDER.md` | Phase 9 merged-scope section. Blocks B-042 through B-049. All subsequent blocks renumbered by +2. |

**Git status of these files:** The two docs (`git-workflow.md` and `phases-0-to-8.md`) are committed on branch `docs-workflow-and-history`, pushed, and awaiting a pull request merge. Once merged, they are on `main`. `phase-9-design.md`, the updated `plugin-manifest.json`, and the updated `BUILD_ORDER.md` are being committed in the current session; check `git log` on `main` for the exact state.

---

### Phase 7 hardening — 2026-10-24

Five bugs found after Phase 7 shipped. Four by code review. One while
writing the positive test that was missing.

| Bug | File | Effect |
|---|---|---|
| Working directory check only rejected exact paths | `sandbox/src/backend.rs` | `/etc/systemd`, `/var/lib/dpkg`, `/usr/local/lib` were accepted as working directories. Now rejected. |
| Docker timeout did not await `docker kill` | `sandbox/src/docker.rs` | Every timeout orphaned a container. Now awaited with a 5-second bound, escalating to `docker rm -f`. |
| Sandbox errors too coarse | `sandbox/src/lib.rs`, `backend.rs`, `quotas.rs` | Added `QuotaOutOfRange`, `ForbiddenEnvKey`, `WorkingDirForbidden`, `WorkingDirInvalid`. Every `CreateFailed` reason now has a specific variant. |
| `bound_variable_names` missed tuple unpacking | `blast/src/contracts.rs` | `a, b = get()` was not recognized as binding `a` and `b`. Now handled. |
| `has_bare_return` treated the `return` keyword as a named child | `blast/src/contracts.rs` | **Contract detection had been silently returning empty on every input since Phase 7 shipped.** The `return` keyword is an anonymous child, so a "no children" check always returned false. Now filters to named children. |

New tests added to `tests/unit/rust/test_blast.py`: five, bringing the
file to 25 tests. Two prove contract detection fires on the pattern it
was designed for and stays quiet when guarded or when the target has no
bare return. Two prove coverage gaps are cleared by a reachable test file
and reported when no test files exist.

Phase 7 grade revised: **B+ → A-**. The "one pattern" narrowness remains,
documented. The silent failure does not.

**Remaining narrowness, deliberately not fixed:** `return None` is not
treated as a bare return because the `None` literal is a named child.
Most bare returns in real Python are `return`, not `return None`.
Documented, deferred.

---

## Phase 9 — designed, not yet started

**Status:** Design settlement complete. No code written. Awaiting start of Block 9-1 (schema verification).

**Scope:** Merged phase. Replaces what was originally planned as Phase 9 (plugin metadata and discovery) plus Phase 9.5 (runtime grammar loading). Total: 8 blocks, 36 files.

**Design settlement:** `docs/summaries/phase-9-design.md`. That document is the authoritative spec for the phase. This section summarizes what it says.

### Phase 9 delivers

| Area | Delivered |
|---|---|
| Plugin loader infrastructure | Manifest parser, registry, unified discovery, fault barrier, audit-hook permission enforcement |
| Runtime grammar loading | `tree_sitter::Loader` integration in the Rust kernel |
| Bundled language plugins | Python, TypeScript, JavaScript, Java, Go, Rust, C, C++ — 8 plugins |
| Bundled tool plugins | ast-grep (subprocess), code_analysis (in-process) |
| Bundled reporter plugin | SARIF 2.1.0 |
| Contract tests | Behavioral assertions per plugin type |
| Integration tests | Plugin loading, dynamic grammar loading end-to-end |
| Version negotiation | N / N-1 SemVer compatibility with clear rejection messages |
| Schema | `plugin-manifest.json` v1.2 |

### Phase 9 blocks

| Block | Files | Purpose |
|---|---|---|
| B-042 | 1 | Schema v1.2 verification (already drafted) |
| B-043 | 7 | Rust kernel runtime grammar loading |
| B-044 | 7 | Python plugin infrastructure (including `permissions.py` audit hook) |
| B-045 | 10 | Bundled language plugins (8 plugins + 2 init files) |
| B-046 | 3 | Bundled tool plugins |
| B-047 | 2 | Bundled reporter plugin |
| B-048 | 4 | Behavioral contract tests |
| B-049 | 2 | Integration tests |

### The seven weaknesses and their fixes

| # | Weakness | Status after Phase 9 |
|---|---|---|
| 1 | Language plugins cannot add new languages | **Fully fixed** — runtime `tree_sitter::Loader` integration |
| 2 | Permissions declared, not enforced | **Fixed for pure-Python** — `sys.addaudithook`. C extensions bypass (documented residual, Phase 13+) |
| 3 | Fault barrier only catches Python exceptions | **Fixed for exceptions and hangs** — `Exception` catching plus per-type timeouts. Thread leaks on hangs (documented residual) |
| 4 | Bundled vs third-party paths diverge | **Fully fixed** — `PluginSource` protocol, one code path |
| 5 | Cannot prove plugins work without agents | **Fixed at unit level** — behavioral contract tests. End-to-end proof is Phase 11 + Phase 13 |
| 6 | Version compat only handles `"1.0"` | **Fully fixed** — N / N-1 SemVer negotiation |
| 7 | Contract tests check shape, not behavior | **Fully fixed** — behavioral assertions |

### Documented residuals (not Phase 9's problem)

| Residual | Target phase |
|---|---|
| Permission enforcement against C extensions | Phase 13+ (WASM or OS-level capability model) |
| Plugin process crash isolation | Phase 13 (orchestrator owns plugin lifetimes) |
| End-to-end pipeline proof of plugins | Phase 11 (agents) + Phase 13 (orchestrator) |
| Third-party verifier plugins | Phase 11 (real verifier implementations) |
| MCP dynamic tool discovery | Phase 15 |
| User-facing plugin CLI (`codeguardian plugin list`) | Phase 14 |

### Design questions settled in phase-9-design.md

- **Q2 (bundled vs third-party discovery):** both paths implemented via `PluginSource` protocol. `BundledSource` for in-wheel plugins (direct import), `EntryPointSource` for third-party (importlib.metadata).
- **Q3 (interface version compat):** N / N-1 SemVer. Currently accepts `"1.0"` and `"1.x"` (with warning for x > 0). `"2.0"` and above rejected with clear message. When 2.0 ships, `"1.x"` becomes N-1 and remains accepted.
- **Q4 (fault barrier scope):** catches `Exception`, not `BaseException`. `KeyboardInterrupt`, `SystemExit`, `asyncio.CancelledError` propagate. In-memory disable flag per plugin per process.
- **Q5 (SQLite write):** yes on load. `INSERT ... ON CONFLICT DO UPDATE`. `enabled` stays 1 always. Run-time faults do not write to SQLite (they are in-memory only).
- **Q6 (bundled grammars):** Python and TypeScript remain bundled (compiled into `_native.so`). JavaScript, Java, Go, Rust, C, C++ remain bundled. Ruby, PHP, Kotlin, Swift, and future languages ship as separate packages via runtime grammar loading.
- **Q7 (`run_tests` parameter type):** `run_tests(sandbox: SandboxHandle, test_path: str) -> TestResult`. Confirmed against Phase 4 interface.
- **Q8 (bundled tool mechanism):** `ast_grep` as subprocess wrapper (its query language is nontrivial), `code_analysis` in-process (uses existing kernel parser, no new dependency).

### What Phase 9 does NOT claim

- Plugins are not fully isolated — exceptions and hangs only
- Permissions are not enforced against C extensions
- New languages require runtime grammar loading to work (which Phase 9 provides, but the plugin author must ship platform-specific `.so` files)
- Third-party plugins are trusted once loaded (same trust model as `pip install` today)
- Behavioral contract tests prove unit-level behavior, not pipeline behavior

Every one of those limitations is documented in `docs/summaries/phase-9-design.md` and in the residual table above.

---

## Test counts

| Phase boundary | Passing | Skipped | Xfailed | Xpassed | Deep test |
|---|---|---|---|---|---|
| Phase 1 | 165 | — | — | — | — |
| Phase 2 | 165 | — | — | — | — |
| Phase 3 | 227 | — | — | — | — |
| Phase 4 | 227 | — | — | — | — |
| Phase 5 | 252 | — | — | — | — |
| Post-5 fix | 256 | — | — | — | — |
| Phase 6 | 298 | — | — | — | — |
| Phase 7 | 364 | — | — | — | — |
| Phase 8 | 487 | 2 | 0 | 0 | — |
| Multi-language | 563 | 2 | 7 | 5 | — |
| **Current** | **568** | **2** | **7** | **5** | **59 pass, 2 xfail** |

**Pytest command:** `PYTHONPATH=python python3 -m pytest tests/ -q`
**Current result:** `568 passed, 2 skipped, 7 xfailed, 5 xpassed`

**Deep test command:** `python sandtest/deep_test.py 2>&1 | tee sandtest_output.txt`
**Last result:** `Passed: 40, Failed: 0, Skipped: 0, Xfailed: 2, Warnings: 0`

**Breakdown of the 7 pytest xfails:**

| Count | Test | Reason |
|---|---|---|
| 2 | `test_same_file_call_resolves_c_family` | C/C++ declarator chain not walked by `parser.rs::extract_name_id` |
| 3 | `test_cross_file_call_resolves` | Phase 8 §11.1 known gap; object-prefixed callees and file offsets |
| 2 | `test_lone_function_has_zero_callers_c_family` | Same as above — target name not in the symbol table |

**The 5 pytest xpasses:** five of the eight cross-file tests happen to resolve. `strict=False` counts them as passed. They will continue to reflect the shared cross-file resolver's behavior until that gap is fixed.

**Breakdown of the 2 deep test xfails:**
- `blast c:c_add` — C/C++ declarator chain not walked
- `blast cpp:cpp_add` — C/C++ declarator chain not walked

**Rust workspace:** `cargo build --workspace --release` finishes in 11 to 30 seconds. `cargo test --workspace` reports zero tests across all eight crates and passes.

---

## Deviations from BUILD_ORDER

### Phase 1 deviations (recorded, not reverting)

| # | What | Why |
|---|---|---|
| 1 | Added `errors/translation.py` and `test_translation.py` | Leak probe found stdlib exceptions reaching the caller untranslated |
| 2 | Added `FILE_PERMISSION_DENIED` to codes.py | `PermissionError` had no accurate code to map to |
| 3 | Added `CONFIG_PARSE_ERROR` and `SECRET_KEYCHAIN_ERROR` to codes.py | Distinguishes parse errors from schema errors; separates keychain failures |
| 4 | Wrote seven Rust crate `Cargo.toml` stubs early | Empty workspace members broke `cargo metadata` and blocked commits |

### Phase 2 deviations

| # | What | Why |
|---|---|---|
| 5 | `additionalProperties: false` on every closed object | Catches typos and field drift at validation time |
| 6 | `extensions` field added to every schema | Growth path without a version bump |
| 7 | Tested per block instead of per phase | Per-block commands caught errors one at a time |
| 8 | `Draft202012Validator.check_schema()` used | Canonical API for validating a schema document |

### Phase 3 deviations

| # | What | Why |
|---|---|---|
| 9 | `migrations.py` renamed to `migration_runner.py` | Module and package cannot share a name |
| 10 | Added `STORAGE_MIGRATION_FAILED`, `STORAGE_SCHEMA_INVALID`, `STORAGE_GRAPH_CORRUPTED` | Storage had no codes for its failure modes |
| 11 | Added `_schema_migrations` table | Migration runner needs it before any migration |
| 12 | Defined the CPG binary header format | Phase 6 must match byte for byte |
| 13 | Real pytest files under `tests/` instead of inline scripts | Permanent suite |
| 14 | `contextlib.suppress` in two places | Ruff SIM105 |

### Phase 4 deviations

| # | What | Why |
|---|---|---|
| 15 | `SandboxConfig` renamed to `SandboxSpec` | Avoids collision with `config.SandboxConfig` |
| 16 | `Change` renamed to `FileChange` | Too generic |
| 17 | `Sandbox` renamed to `SandboxHandle` | Avoids confusion with `SandboxSpec` |
| 18 | `ModelProvider.stream` is `def` not `async def` | Returns an async iterator; `async def` would force `await` before iterating |
| 19 | `Agent` generic over `[InputT, OutputT]` | Each agent has different input and output |
| 20 | `VerifierPlugin.verify` takes one argument | Both docs show `verify(diff, context)` but `context` contains `diff` |
| 21 | `interfaces/__init__.py` rewritten with `TYPE_CHECKING` block | Dynamic loop invisible to mypy. **Rule 1 escalation.** |

### Phase 5 deviations

| # | What | Why |
|---|---|---|
| 22 | `rust/Cargo.toml` (empty placeholder) deleted | Broke cargo commands from inside `rust/` |
| 23 | `codeguardian-pyo3/Cargo.toml` and `src/lib.rs` written in Phase 5 | PyO3 proved end-to-end in Phase 5. **Rule 1 escalation, approved.** |
| 24 | `rpc/src/lib.rs` rewritten in B-024 | Expected pattern for a Rust crate. **Rule 1 escalation.** |
| 25 | Added `json.JSONDecodeError` and `tomllib.TOMLDecodeError` to translation | Otherwise malformed JSON/TOML classified as retryable `SYS_INTERNAL` |
| 26 | Four new translation tests | Each mapping gets a test |

### Phase 6, 7, 8 deviations

Recorded in `docs/summaries/phases-0-to-8.md` §10–§13.

### Multi-language extension deviations

Recorded in `docs/summaries/phases-0-to-8.md` §13.3.

### Documentation and process deviations

| # | What | Why |
|---|---|---|
| 27 | Added `.gitattributes` with `* text=auto eol=lf` | Line-ending normalization. Prevents pre-commit failure loop on every commit |
| 28 | Removed `features = ["extension-module"]` from `pyproject.toml` | Maturin auto-detect conflicted with the pyo3 dependency feature |
| 29 | Added `sandtest/` folder with `deep_test.py` | Permanent integration test. Runs against real repo, real sandbox, real model |
| 30 | Added `docs/git-workflow.md` | Operational reference for git in this repo |
| 31 | Added `docs/summaries/phases-0-to-8.md` | Supersedes the three earlier summaries |
| 32 | Added `docs/summaries/phase-9-design.md` | Phase 9 design settlement |
| 33 | `plugin-manifest.json` updated to v1.2 | Adds `capabilities`, `blast_scope`, `kernel_extensible`, `grammar_path` |
| 34 | `BUILD_ORDER.md` Phase 9 section rewritten | Merged with Phase 9.5, 8 blocks, 36 files. All subsequent blocks renumbered +2 |

---

## Escalation queue

Rule 1 escalations performed across all phases. Every rewrite is additive at the behavioral level.

**Phases 1–8:** recorded in `docs/summaries/phases-0-to-8.md`. All resolved.

**Multi-language extension:** recorded in `docs/summaries/phases-0-to-8.md` §13.3. All resolved. No open escalations from that work.

**Phase 9 (designed, not started):** the following files will need rewriting per the design settlement:

| File | Nature of rewrite | Approved |
|---|---|---|
| `schemas/plugin-manifest.json` | v1.1 → v1.2: add four optional fields | Yes (design settlement) |
| `rust/Cargo.toml` | Add `libloading`, enable `tree-sitter`'s `loader` feature | Yes (design settlement) |
| `rust/crates/codeguardian-kernel/Cargo.toml` | Add `libloading` | Yes (design settlement) |
| `rust/crates/codeguardian-kernel/src/langs/mod.rs` | Add runtime language registry | Yes (design settlement) |
| `rust/crates/codeguardian-kernel/src/parser.rs` | Accept `&dyn LanguageModule` | Yes (design settlement) |
| `rust/crates/codeguardian-kernel/src/lib.rs` | Export grammar_loader module | Yes (design settlement) |
| `rust/crates/codeguardian-pyo3/src/kernel.rs` | Add `load_grammar`, `list_languages` | Yes (design settlement) |

Every rewrite is additive. No existing variant, arm, function, or field is removed.

**Open escalations:** zero.

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md`, `docs/summaries/phase-9-design.md`, and `docs/public-api.md`.
2. Read `docs/git-workflow.md` for the branch and commit workflow.
3. Do not attempt to re-verify Phases 1 through 8 or the multi-language work or the deep test. They are green. Trust the state.
4. Ask the user to `source .venv/bin/activate` before any Python command.
5. Confirm `cargo --version` works inside the venv. If it fails, the venv activate script no longer appends `$HOME/.cargo/bin`.
6. Start Phase 9 — Plugins. **Use `docs/summaries/phase-9-design.md` as the authoritative spec.**
7. Create the branch first: `git checkout -b phase-9-plugins`. All Phase 9 work happens on that branch. `main` stays untouched until the PR is merged.
8. Write blocks in this order: B-042 (schema verification), B-043 (Rust kernel), B-044 (Python infrastructure), B-045 (language plugins), B-046 (tool plugins), B-047 (reporter plugin), B-048 (contract tests), B-049 (integration tests).
9. After each block, run the affected tests. The design settlement §10 defines the block-boundary testing pattern.
10. After block 9-8, run the full phase boundary test plus the updated deep test (the deep test gains a plugin section per the design settlement §10).
11. On green, commit to `phase-9-plugins` branch, push, open PR, merge, tag `v1.1.0`.

### Before writing any Phase 9 file

Confirm the two open questions in the design settlement are settled. They are:
- Q2 — both discovery paths (BundledSource + EntryPointSource)
- Q8 — ast_grep subprocess, code_analysis in-process

Both are already documented as settled in `docs/summaries/phase-9-design.md` §4.2 and in this BUILD_STATE's "Design questions settled" section. No further decision needed.

---

## Rules the next chat must follow

- Think before writing. Read the problem twice. Name the invariant. Name the failure mode.
- Write full files only. Never fragments, never diffs.
- Never rewrite a file that has already passed without stopping and asking. (Phase 9's seven rewrites are pre-approved per the design settlement.)
- Test at phase boundaries, not per block. But Phase 9 also runs block-boundary tests because the phase is large enough that catching block-level failures early saves time.
- Never claim a test passed. Wait for the user's raw output.
- Ask before guessing. Ask before adding a code, symbol, dependency, or schema field.
- Follow `skills.md` for every file.

---

## Known gaps carried forward

These are gaps identified across Phases 1 through 8, the multi-language extension, and the deep test. They are recorded here so they are not forgotten.

| # | Gap | Becomes relevant | Notes |
|---|---|---|---|
| G-1 | Multi-process SQLite write contention | Phase 13 | `busy_timeout` handles two writers; three will time out |
| G-2 | No retention for metadata tables | Phase 16 | `model_calls`, `mcp_tool_calls`, `phase_executions`, `verifier_verdicts` grow forever |
| G-3 | No SQLite backup | Before v1 ships | Database is not backed up; audit log is safe |
| G-4 | Cache keys do not include schema version | When schema changes | Stale cache hits |
| G-5 | No model provenance on skill embeddings | When embedding model switches | Vectors become incomparable |
| G-6 | CPG header has no compression flag | Optional | 200 MB graph is 200 MB on disk |
| G-7 | Cleanup runs as background thread | Optional | Crashes with the process |
| G-8 | 64 KB cache threshold is a magic number | Optional | Not configurable |
| G-9 | `@typed` decorator discipline not enforced | Phase 16 | No lint rule |
| G-10 | Rust `RpcServer` never exercised against real input | Closed by Phase 7 | rpcd exercises it via JSON-RPC |
| G-11 | `test_config_secrets.py` does not exist | Phase 1 backlog | Deferred by user decision |
| G-12 | Rust error-to-Python mapping at bridge boundary | Partially closed | `pyo3/kernel.rs` handles all kernel variants; JSON-RPC still maps all errors to `SYS_INTERNAL` |
| G-13 | CLI exit-code mapping not written | Phase 14 | Design specified, code does not exist |
| G-14 | Phase 0 B-004 through B-009 deferred | End of build | 30 files, no runtime impact |
| **G-15** | **C/C++ function names not extracted** | Next kernel phase | `extract_name_id` does not walk declarator chains. Fix flips 4 xfails to pass and moves C/C++ to `high` reliability |
| **G-16** | **Cross-file call resolution incomplete** | Next kernel phase | Narrowed by Phase 9: `self.foo()` and `Class.foo()` resolve. `mod.foo()` ambiguous does not. Also fixed `pyo3/blast.rs` emitting `file_path` where schema requires `path` — that key mismatch was masking all cross-file results until fixed. Deep test now shows 21 targets with cross-file callers on `psf/requests`. Under-counts remain for ambiguous names. |
| **G-17** | **C# not supported** | When grammar ships | ABI incompatibility with tree-sitter 0.24. Re-adding requires a compatible grammar release plus four file changes |
| **G-18** | **`ast.rs` comment on `Property` mentions C#** | Cosmetic | One-word fix, requires Rule 1 escalation on `ast.rs` |
| **G-19** | **No automated test/language drift check** | Phase 9 | Adding a language to `dispatch` without adding a test case goes unnoticed. Consider a conftest comparison |
| **G-20** | **`audit.verify` returns `entries_checked: 1` after apply + rollback** | Investigate | Real finding from the deep test. Should be at least 2 entries |
| **G-21** | **`echo` returns 0 callers on `pallets/click`** | Next kernel phase | Partly resolved by the pyo3 `path` key fix. On `psf/requests`, 21 targets now show cross-file callers. `echo` in click has not been re-tested since the fix. Ambiguous names (`echo` is defined in multiple modules across the click tree) may still under-count. |
| **G-22** | **Maturin does not work with this workspace** | Cosmetic | `features = ["extension-module"]` removed from `pyproject.toml` because the feature is enabled on the `pyo3` dependency. Build path is `cargo build` + `cp`. Do not re-add |
| **G-23** | **Phase 9 residuals: permission bypass via C extensions** | Phase 13+ | Audit hook is Python-level. Documented in design settlement §3.2 |
| **G-24** | **Phase 9 residuals: plugin process crash isolation** | Phase 13 | Fault barrier catches `Exception`; segfaults need process pool. Documented in design settlement §3.3 |
| **G-25** | **Phase 9 residuals: pipeline proof requires agents** | Phase 11 + 13 | Behavioral contract tests prove unit level; end-to-end proof needs Phase 11 agents and Phase 13 orchestrator |

Gaps G-1 through G-14 are unchanged from the Phase 5 snapshot. G-10 and G-12 are marked partially addressed. G-15 through G-25 are new since Phase 6.

**Gaps that Phase 9 closes:**
- Partial G-19 (adds behavioral contract tests that check every plugin type)
- G-22 (documentation only — the workaround is already in place)

**Gaps that remain open after Phase 9:**
- G-1 through G-9, G-11, G-13, G-14 (unchanged)
- G-12 (JSON-RPC error mapping — Phase 9 does not touch this)
- G-15, G-16, G-17, G-18, G-20, G-21 (kernel issues — Phase 9 does not touch them)
- G-23, G-24, G-25 (Phase 9 residuals — documented for later phases)

---

## Git history

The current HEAD is on `main`. The docs branch `docs-workflow-and-history` is pushed to origin, awaiting PR merge. Recent commits (order not exact):

```
<latest>  docs: add build history and git workflow reference  (on docs-workflow-and-history branch)
<latest>  test(sandtest): add deep integration test           (on main)
<latest>  chore: apply eol=lf normalization across tree       (on main)
<latest>  chore: add gitattributes to normalize line endings to LF (on main)
<latest>  feat: multi-language kernel extension                (squashed in one commit on main)
<earlier> docs: add build history for phases 1 through 5
<earlier> fix(errors): map json and tomllib decode errors to CONFIG_PARSE_ERROR
<earlier> feat(bridges): complete Phase 5 with PyO3 and JSON-RPC bridges
<earlier> feat(interfaces): complete Phase 4 with twelve Protocols and bridge envelope
<earlier> feat(storage): complete Phase 3 with SQLite, filesystem, graph, cache, and skill stores
<earlier> feat: complete Phase 1 foundation and Phase 2 schemas
<earlier> chore(pre-commit): make hooks phase-aware
<earlier> fix(core): rename UnknownCodeError, add translation layer, add Rust workspace manifests
<earlier> Full Docs updated
```

Full hashes are on `origin/main`. Working tree is clean at the time of this snapshot (after the docs branch work, but before the phase-9-design.md and BUILD_STATE.md commits — the user is committing those files in the current session).

### Commit strategy for Phase 9

**Branch first.** `git checkout -b phase-9-plugins` from `main`. All Phase 9 work happens on the branch.

**Commit per block.** After each block passes its block-boundary tests, commit:

```bash
git add -A
git commit -m "feat(plugins): <block description>"
```

Eight commits on `phase-9-plugins`. The exact commit messages will be determined per block in the next session.

**Push after each commit** so nothing is lost if the session crashes.

**PR and merge after Phase 9 is complete.** One PR containing eight commits. Full diff reviewable. `main` untouched until the PR merges.

---

## Where the next phase starts

Phase 9 — Plugins. 8 blocks, 36 files. Reference `docs/summaries/phase-9-design.md` for the complete specification.

**Block order:**

1. **B-042** — `schemas/plugin-manifest.json` verification (already drafted in current session, verify on disk)
2. **B-043** — Rust kernel runtime grammar loading (7 files)
3. **B-044** — Python plugin infrastructure (7 files, including `permissions.py`)
4. **B-045** — Bundled language plugins (10 files: 8 plugins + 2 inits)
5. **B-046** — Bundled tool plugins (3 files)
6. **B-047** — Bundled reporter plugin (2 files)
7. **B-048** — Behavioral contract tests (4 files)
8. **B-049** — Integration tests (2 files)

**What "done" means for Phase 9:**

1. `cargo build --workspace --release` succeeds with the new `grammar_loader.rs`
2. `PluginRegistry.discover([BundledSource(...), EntryPointSource()])` finds all bundled plugins and can find a fake third-party plugin
3. `_native.load_grammar(path)` loads a real grammar at runtime (test with a tree-sitter-ruby `.so` in a temp directory)
4. `_native.list_languages()` returns 9+ languages (8 built-in plus runtime-loaded)
5. Every bundled plugin passes its behavioral contract test
6. Audit hook blocks a plugin that declares `network: false` from opening a socket (test with a fake plugin)
7. Fault barrier times out a plugin that hangs (test with a fake slow plugin)
8. Version negotiation accepts `"1.0"`, warns on `"1.1"`, rejects `"2.0"`
9. The deep test's new plugin section proves end-to-end plugin invocation
10. `pytest tests/` still shows all pre-existing tests passing (563 + Phase 9 additions)

**What Phase 9 will NOT deliver (documented residuals):**

1. Permission enforcement against C extensions (Phase 13+)
2. Plugin process crash isolation (Phase 13)
3. End-to-end pipeline proof with agents (Phase 11 + 13)
4. MCP dynamic tool discovery (Phase 15)
5. User-facing plugin CLI (Phase 14)

The residual list is in `docs/summaries/phase-9-design.md` §9 and repeated in this BUILD_STATE's "Phase 9 does NOT claim" section.

---

## Phase 0 note

The following Phase 0 files are still deferred:

- `.github/workflows/ci.yml` — no CI yet
- `.github/workflows/docs.yml`, `.github/workflows/release.yml`, `.github/workflows/security.yml`
- `.devcontainer/`
- `docker/Dockerfile.python-sandbox`, `docker/Dockerfile.typescript-sandbox`
- `benchmarks/` fixtures and baselines
- `mkdocs.yml` (the docs site does not build yet)
- `examples/skills/`

30 files, no runtime impact. They land in one commit at the end of the build. Until then, `pytest tests/` runs on the local machine and pre-commit runs on commit. The CI gap means a merge to `main` does not automatically run tests.

The next time a Phase boundary test needs to run in a clean environment, that is when Phase 0's remaining work becomes urgent. For now, the local test suite is the source of truth.
