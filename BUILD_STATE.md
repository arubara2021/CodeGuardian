# CodeGuardian — Build State

**Snapshot date:** 2026-11-08
**Last phase completed:** Phase 13 — Orchestrator (49 files across 10 blocks)
**Next phase to start:** Phase 14 — CLI
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

- **Maturin does not work with this workspace.** The `pyproject.toml` `[tool.maturin]` section originally had `features = ["extension-module"]`, which fails because the feature is enabled on the `pyo3` dependency rather than as a crate feature. The `features` line has been removed. The build path is `cargo build --workspace --release` followed by a manual `cp`. This is a known deviation. Do not re-add `features = ["extension-module"]` to `pyproject.toml`.
- **Line endings are normalized.** `.gitattributes` with `* text=auto eol=lf` is committed. WSL2 writing to a Windows filesystem caused mixed line endings that broke pre-commit for eight consecutive commit attempts during the multi-language phase. The permanent fix is in place.
- **Pre-commit is installed and active.** Every commit runs the hooks. `end-of-file-fixer`, `mixed-line-ending`, `ruff`, `ruff-format` will modify files on the first commit attempt if they find issues. Second attempt passes. This is normal pre-commit behavior, not a bug.
- **`.so` size is approximately 8.8 MB** after the multi-language work. The increase is the linked grammar object files.
- **`rust/crates/codeguardian-rpc/Cargo.toml` was accidentally overwritten** during the multi-language commit attempts and restored. Do not confuse it with root workspace content.
- **Shell discipline.** Every Python, pytest, ruff, and git command in this project runs in WSL. The PowerShell window shares the same physical directory but does not know WSL tooling; pasting bash commands there creates empty files and drops tokens into the working tree. During Phase 13 the working tree accumulated twenty-two zero-byte files from a stray PowerShell paste; they were removed. Close the PowerShell window during build work.

---

## Completed phases

### Phase 0 — Repository Scaffold (partial)

| Block | Status | Files |
|---|---|---|
| B-001 Root Build Configuration | done | pyproject.toml, Cargo.toml, Cargo.lock, rust-toolchain.toml, rustfmt.toml |
| B-002 Root Project Hygiene | done | clippy.toml, justfile, .gitignore, .codeguardianignore, .dockerignore |
| B-003 Root Editor and Documentation | done | .editorconfig, .env.example, .python-version, .pre-commit-config.yaml, mkdocs.yml |
| B-004 through B-009 | deferred to end | 30 files, no runtime impact |

### Phases 1 through 12 — complete

See `docs/summaries/phases-0-to-8.md` (through Phase 8), `docs/summaries/phase-9-design.md` (Phase 9), `docs/summaries/phase-10.md` (Phase 10), `docs/summaries/Phase_11.md` (Phase 11), and `docs/summaries/Phase_12.md` (Phase 12 v2) for the detailed build history of every phase through Phase 12.

Test state at the Phase 12 v2 boundary: **1547 passed, 14 skipped, 7 xfailed, 5 xpassed** (Python pytest).

### Phase 13 — Orchestrator (complete)

**Scope.** Phase 13 delivers the harness engine: a deterministic graph walker that resolves typed slots against an agent registry, consumes a two-tier evidence bus, runs a three-layer verification stack with a blast-band-scaled verifier loop, compiles bounded context from a run tree, enforces a cheap-first routing layer with blast-aware escalation, populates the four evidence columns, and enforces delegation capability attenuation.

**Files delivered (49 across 10 blocks):**

- **18 orchestrator source modules** in `python/codeguardian/core/orchestrator/`: `phase_graph.py`, `phase_graphs/{__init__,legacy}.py`, `registry.py`, `evidence.py`, `phase_gates.py`, `consent.py`, `checkpointing.py`, `checkpoint_supervisor.py`, `verification_prompt.py`, `loop.py`, `langgraph_orchestrator.py`, `deterministic_gate.py`, `verifier_stack.py`, `context_engine.py`, `sandbox_adapter.py`, `capability_attenuation.py`, `__init__.py`
- **4 telemetry source modules** in `python/codeguardian/core/telemetry/`: `__init__.py`, `metrics.py`, `tracing.py`, `langsmith.py`
- **1 migration**: `007_agent_registry.sql`
- **8 test files** in `tests/unit/core/`: `test_phase_graph.py`, `test_orchestrator_core.py`, `test_verification_stack.py`, `test_context_and_routing.py`, `test_evidence_columns.py`, `test_sandbox_adapter.py`, `test_phase_gates.py`, `test_orchestrator_e2e.py`
- **2 test-scoped adapters** in `sandtest/deep/`: `_sandbox_verify.py`, `_verifier_agents.py`
- **1 deep-test rewire**: `sandtest/deep/_pipeline.py` (full rewrite, orchestrator-driven)
- **3 doc updates**: `docs/public-api.md`, `docs/05-data-model.md`, `BUILD_STATE.md`
- **1 roadmap**: `docs/summaries/Phase_13_walls_and_handoff.md`

**Architectural delivery.**

| Layer | Component | Status |
|---|---|---|
| Data model | `PhaseGraph`, `PhaseSlot`, `LEGACY_GRAPH` | Complete, validated |
| Dispatch | `AgentRegistry`, `AgentSpec`, `SlotResolver` | Complete, tag and schema checks |
| Walk | `loop.run_loop` | Complete, deterministic |
| Protocol | `LangGraphOrchestrator` | Complete, `Orchestrator` Protocol satisfied |
| Evidence | `EvidenceBus`, hot-tier events, cold-tier writes | Complete, two-tier split preserved |
| Gates | `phase_gates`, `consent` | Complete, entry and exit per slot |
| Checkpoints | `checkpointing`, `checkpoint_supervisor` | Complete, resume tested |
| Verification | `deterministic_gate`, `verifier_stack` | Complete, K-scaled by band |
| Context | `context_engine` | Complete, 60/85 thresholds |
| Sandbox | `sandbox_adapter` | Complete, backend-agnostic |
| Attenuation | `capability_attenuation` | Complete, parent ∩ agent ∩ skill |
| Telemetry | `MetricsCollector`, `TraceContext`, `LangSmithAdapter` | Complete, no-op fallbacks |
| Migration | `007_agent_registry` | Complete, seeded by the loop |
| Routing | `FallbackModelProvider` | Extended, budget and blast-aware |

**Test state at the Phase 13 boundary:** **1771 passed, 14 skipped, 7 xfailed, 5 xpassed** (Python pytest). Delta from Phase 12 v2's 1547 is +224: the eight new Phase 13 test files plus the Block 1 analyst and writer tests.

**Deep test results.**

`httpx` at 65 passed, 0 failed, 2 xfailed. The three pipelines walked all ten phases on all three bands. Fault injection ran and was correctly skipped for a target whose return value was not in the mutable-type set. The block-band approval contract fired. Checkpoints resumed. The verifier retry fired. The audit chain verified. Telemetry recorded 9/9 roles with provenance hashes.

`scrapy` at 57 passed, 1 failed, 2 skipped, 1 warning, 2 xfailed. The failure is a fault-injection test-assertion detail (`test failed but no test_value_* in FAILED lines`); the smoke test caught the injected drift, the check did not accept the smoke-only catch. The two skips are honest environment limits: the block-band target exceeded `qwen3.8-27b`'s context window, and the block-band approval assertion consequently could not fire. The warning is scrapy's own Twisted/Zope incompatibility, not a pipeline problem. Twenty of twenty pipeline phases passed on the proceed and review bands.

**Rule 1 escalations in Phase 13 (eight, all approved):**

| File | Nature |
|---|---|
| `core/agents/analyst.py` | §2.1 warn on unparseable body; §2.3 record function-body not-found in decision_log; 413 becomes a refusal |
| `core/agents/writer.py` | §2.2 reject candidates when original source does not parse; §2.3 record function-body not-found |
| `core/agents/tester.py` | §2.3 record function-body not-found; rate-limit and unavailability become refusals |
| `core/providers/fallback_router.py` | Additive: `BudgetEnvelope`, `blast_band_override`, `escalation_signals`, tier-mapping helper |
| `core/orchestrator/deterministic_gate.py` | Replace non-runtime-checkable `isinstance` against `WritableSandboxExecutor` with `hasattr` (latent bug) |
| `core/orchestrator/phase_gates.py` | `verifier_entry` checks `sandbox_verify` result, not a `diff` slot that never runs; `skill_curator_exit` accepts refusal as a legitimate outcome |
| `core/orchestrator/phase_graphs/legacy.py` | Distinguishing capability tag per verifier slot so each resolves to its own agent |
| `core/orchestrator/loop.py` | `_seed_registered_agents` from the resolved graph; keeps the seed out of SQL |
| `core/storage/sqlite_store.py` | `check_same_thread=False` on the connection to support the worker-thread dispatch |

**Phase 13 residuals recorded:**

- The deep test's `sandbox_verify` and verifier slots are filled by test-scoped adapters in `sandtest/deep/`, not by production agents. A future phase that promotes them to `core/orchestrator/` swaps the registration and retires the two test files.
- `_SharedVerifierRunner` runs the verifier stack with `k_override=1`. The K-round retry path is exercised by the unit test `test_verifier_retry`, not by the deep test.
- The `project_test_suite` slot remains unregistered; the loop skips it because it is optional.
- The scrapy fault-injection assertion (`test failed but no test_value_* in FAILED lines`) needs the widened `as_check` predicate that accepts `test_smoke_*` as evidence of drift detection. The unit test for the module does not exercise this path; the deep test does.

**What Phase 13 explicitly does NOT deliver:** CLI (Phase 14), MCP server and reporters (Phase 15), the LSP semantic layer, multi-language pipeline, step-level routing, benchmark numbers. All are recorded in `docs/summaries/Phase_13_walls_and_handoff.md`.

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
| Phase 8 hardening + Phase 9 | 704 | 10 | 7 | 5 | 59 pass, 2 xfail |
| Phase 10 complete | 884 | 10 | 7 | 5 | 59 pass, 2 xfail |
| Phase 11 complete | 1367 | 14 | 7 | 5 | 87 pass, 2 fail, 0 skip, 2 xfail |
| Phase 12 v2 complete | 1547 | 14 | 7 | 5 | unchanged from Phase 11 |
| **Phase 13 complete** | **1771** | **14** | **7** | **5** | **httpx 65/0/2; scrapy 57/1/2 (1 fail, 2 skip, 1 warn)** |

**Pytest command:** `PYTHONPATH=python python3 -m pytest tests/ -q`
**Current result:** `1771 passed, 14 skipped, 7 xfailed, 5 xpassed`

**Ruff:** `ruff check python/codeguardian/ tests/ sandtest/` → `All checks passed!`

**Deep test:** last runs are recorded above. The Phase 13 orchestrator drives the pipeline on both `httpx` and `scrapy`. The scrapy fault-injection assertion is the one open test detail; the pipeline itself ran 20/20 phases on the proceed and review bands.

**Breakdown of the 7 pytest xfails:** unchanged from Phase 11. C/C++ declarator gap (G-15) plus cross-file resolution incompleteness (G-16).

**Breakdown of the 5 pytest xpasses:** unchanged from Phase 11. Five of the eight cross-file tests happen to resolve; `strict=False` counts them as passed.

---

## Deviations from BUILD_ORDER

### Phase 1 through Phase 12 deviations

Recorded in `docs/summaries/phases-0-to-8.md` §10–§13, `docs/summaries/phase-9-design.md` §2.3, `docs/summaries/phase-10.md` §9, `docs/summaries/Phase_11.md` §3, and `docs/summaries/Phase_12.md` §12. All resolved, none open.

### Phase 13 deviations

| # | What | Why |
|---|---|---|
| 1 | Phase 13 grew from the plan's ~35 files to 49 | The plan was written before the orchestrator's supporting infrastructure (telemetry package, two test-scoped agents) was fully scoped. The additions are additive; no planned file was dropped. |
| 2 | `legacy.py` gained distinguishing verifier tags | The three verifier slots shared identical capability tags in the initial plan. `SlotResolver` could not resolve them to three distinct agents without a distinguishing tag. The change is one tag per slot; the loop is unchanged. |
| 3 | `loop.py` gained `_seed_registered_agents` | The initial plan seeded `registered_agents` in SQL. Hardcoding the nine roles and their capability tags in the migration duplicated data that lives in `LEGACY_GRAPH`. The seed moved to the loop, where the resolved graph is the source of truth. |
| 4 | `sqlite_store.py` gained `check_same_thread=False` | The orchestrator's `_run_coroutine_sync` dispatches the loop to a worker thread when a loop is already running. SQLite's default thread affinity rejects that. Modern Python's SQLite is serialized mode; the flag is safe. |
| 5 | Two test-scoped agents live in `sandtest/deep/` instead of `core/orchestrator/` | The production `SandboxVerifyAgent` and `VerifierStackAgent` are not part of Phase 13's scope. The deep test needs agents to fill the two slots; placing them in the test harness keeps the production surface honest. |
| 6 | `sandbox_adapter.py` sets no `_ACTIVE_PERMISSIONS` contextvar | The build order named a variable that does not exist. The Phase 9 audit hook reads `_ACTIVE` and `_ACTIVE_NAME` through `plugin_active()`. The attenuation module uses the real mechanism. |
| 7 | Migration 007 ends with `PRAGMA foreign_keys = OFF` | The first draft ended with `ON`, contradicting migration 006's convention and the project default. The role FK is declarative; the registry enforces names at registration time. |

---

## Escalation queue

Rule 1 escalations performed across all phases. Every rewrite is additive at the behavioral level.

**Phases 1–12:** recorded in the phase summaries. All resolved.

**Phase 13:** eight Rule 1 escalations, all closed.

| File | Nature | Approved |
|---|---|---|
| `core/agents/analyst.py` | §2.1 and §2.3 fixes; 413 becomes refusal | Yes |
| `core/agents/writer.py` | §2.2 and §2.3 fixes | Yes |
| `core/agents/tester.py` | §2.3 fix; rate-limit becomes refusal | Yes |
| `core/providers/fallback_router.py` | Additive budget, blast override, escalation signals | Yes |
| `core/orchestrator/deterministic_gate.py` | Latent `isinstance` bug fixed | Yes |
| `core/orchestrator/phase_gates.py` | `verifier_entry` and `skill_curator_exit` corrected | Yes |
| `core/orchestrator/phase_graphs/legacy.py` | Distinguishing verifier tags | Yes |
| `core/orchestrator/loop.py` | `_seed_registered_agents` added | Yes |
| `core/storage/sqlite_store.py` | `check_same_thread=False` | Yes |

**Open escalations:** zero.

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md` (this file), `docs/summaries/Phase_13.md`, `docs/summaries/Phase_13_walls_and_handoff.md`, and `docs/public-api.md`.
2. Read `docs/git-workflow.md` for the branch and commit workflow.
3. Do not attempt to re-verify Phases 1 through 13. They are green. Trust the state.
4. Ask the user to `source .venv/bin/activate` before any Python command.
5. Confirm `cargo --version` works inside the venv. If it fails, the venv activate script no longer appends `$HOME/.cargo/bin`.
6. Start **Phase 14 — CLI**. Read `BUILD_ORDER.md` §19 and `docs/04-architecture.md` §7.
7. Create the branch first: `git checkout -b phase-14-cli`. All Phase 14 work happens on that branch.
8. **Phase 14 consumes Phase 13's public surface.** Specifically:
   - `LangGraphOrchestrator.run(config)`, `.resume(run_id)`, `.pause(run_id)`, `.get_state(run_id)`
   - The `RunConfig`, `RunResult`, `RunState` dataclasses from `codeguardian.interfaces.orchestrator`
   - `LegacyGraph` as the default `PhaseGraph`
   - The `CheckpointSupervisor` for the `resume` subcommand
9. **Phase 14 must address the residuals the deep test surfaced.** G-33 (analyst 413 surfaces as a SKIP in the deep test; the CLI must translate it into a user-facing refusal). Per-phase progress output so an 11-minute run does not look frozen. The `recovery.md` from the roadmap is a Phase 14 concern if it is not built into the orchestrator first.
10. **Phase 14 must not change the orchestrator, the loop, the registry, the evidence bus, or the verifier stack.** Those are the structural invariants; they survive any CLI.

---

## Rules the next chat must follow

- Think before writing. Read the problem twice. Name the invariant. Name the failure mode.
- Write full files only. Never fragments, never diffs.
- Never rewrite a file that has already passed without stopping and asking. Rule 1 escalations need explicit approval in writing before the edit lands.
- Test at phase boundaries, not per block. For Block-heavy phases, also run targeted block tests to catch failures early.
- Never claim a test passed. Wait for the user's raw output.
- Ask before guessing. Ask before adding a code, symbol, dependency, schema field, or error code.
- Follow `skills.md` for every file.
- **Before writing any file marked `(UPDATE)`, request `git show HEAD:<path>` for that file and write from the actual current content, not from imports or assumptions.** This rule was added after the `defaults.py` regression in the Phase 12 session, where an UPDATE was written from imports and lost four constants.
- **Every patch script ends with a verify-before-write assertion.** This rule was added after a `defaults.py` patch script wrote a file whose `__all__` promised names that the script never actually defined.
- **Run every bash command in WSL.** The PowerShell window creates empty files and drops tokens into the working tree. Twenty-two such files were cleaned up during Phase 13.
- **The deep test does not install the user's project.** A production user has a working environment; the deep test clones and installs, and its install fragility is a harness limitation, not a product limitation. Do not confuse the two.

---

## Known gaps carried forward

Gaps identified across Phases 1 through 13. Recorded here so they are not forgotten. The table is consolidated from `docs/summaries/phases-0-to-8.md` §16, `docs/summaries/Phase_11_shortcomings.md`, Phase 12 v2 residuals, and Phase 13 residuals.

| # | Gap | Becomes relevant | Notes |
|---|---|---|---|
| G-1 | Multi-process SQLite write contention | Phase 14+ | `busy_timeout` handles two writers; three will time out |
| G-2 | No retention for metadata tables | Phase 16 | Tables grow forever |
| G-3 | No SQLite backup | Before v1 ships | Audit log is safe; database is not |
| G-4 | Cache keys do not include schema version | When schema changes | Stale cache hits |
| G-5 | No model provenance on skill embeddings | When embedding model switches | Vectors become incomparable |
| G-6 | CPG header has no compression flag | Optional | 200 MB graph is 200 MB on disk |
| G-7 | Cleanup runs as background thread | Optional | Crashes with the process |
| G-8 | 64 KB cache threshold is a magic number | Optional | Not configurable |
| G-9 | `@typed` decorator discipline not enforced | Phase 16 | No lint rule |
| G-10 | Rust `RpcServer` never exercised against real input | Closed by Phase 7 | rpcd exercises it via JSON-RPC |
| G-11 | `test_config_secrets.py` does not exist | Phase 1 backlog | Deferred by user decision |
| G-12 | Rust error-to-Python mapping at bridge boundary | Partially closed | JSON-RPC still maps all errors to `SYS_INTERNAL` |
| G-13 | CLI exit-code mapping not written | **Phase 14** | Design specified, code does not exist |
| G-14 | Phase 0 B-004 through B-009 deferred | End of build | 30 files, no runtime impact |
| G-15 | C/C++ function names not extracted | Next kernel phase | `extract_name_id` does not walk declarator chains |
| G-16 | Cross-file call resolution incomplete | Next kernel phase | Narrowed by Phase 9 but not closed |
| G-17 | C# not supported | When grammar ships | ABI incompatibility with tree-sitter 0.24 |
| G-18 | `ast.rs` comment on `Property` mentions C# | Cosmetic | One-word fix, Rule 1 escalation on `ast.rs` |
| G-19 | No automated test/language drift check | Phase 14+ | Adding a language to `dispatch` without adding a test case goes unnoticed |
| G-20 | `audit.verify` returns `entries_checked: 1` after apply + rollback | Investigate | Real finding from the deep test |
| G-21 | `echo` returns 0 callers on `pallets/click` | Next kernel phase | Partly resolved by the pyo3 `path` key fix. `echo` in click not re-tested |
| G-22 | Maturin does not work with this workspace | Cosmetic | Documented in environment notes |
| G-23 | Phase 9 residuals: permission bypass via C extensions | Future | Audit hook is Python-level |
| G-24 | Phase 9 residuals: plugin process crash isolation | Future | Fault barrier catches exceptions; segfaults need process pool |
| G-25 | Phase 9 residuals: pipeline proof requires agents | **Closed by Phase 13** | The orchestrator runs the full pipeline on `httpx` and `scrapy` |
| G-26 | Phase 11 residuals: reranker feature flag never exercised on real workload | Future | The reranker degrades to passthrough silently when `fastembed` lacks `TextCrossEncoder` |
| G-27 | Phase 12 v2 residuals: Stage 0 filter is language-only | Phase 14+ | Dependency version and test framework match need orchestration |
| G-28 | Phase 12 v2 residuals: negative knowledge decay is manual | Phase 14+ | Requires a scheduler |
| G-29 | Phase 12 v2 residuals: evolver uses sidecar JSON, not shadow tables | Future | Working alternative but does not use the DB's transactional guarantees |
| G-30 | Phase 12 v2 residuals: provenance `admission_evidence` is reconstructed from columns | Future | Not stored as a JSON blob; richer evidence needs a schema change |
| G-31 | Phase 12 v2 residuals: fitness formula is effectiveness-only | When cost tracking arrives | `−λ·cost` deferred |
| G-32 | Phase 11 residuals: tester prompt clarification, skill curator prompt clarification | **Phase 14** | §1.2 and §1.3 of `Phase_11_shortcomings.md`; the fault-injection label was fixed in Phase 13 |
| **G-33** | **Analyst context-window overflow surfaces as a `[SKIP]` in the deep test** | **Phase 14** | The CLI must translate it into a user-facing refusal, not a silent skip. The deep test's skip is honest for the harness; a real user needs a message. |
| **G-34** | **No LSP layer** | **v2** | The kernel parses 8 languages; semantic rename, find-references, and code-action refactoring rely on tree-sitter ranges. The highest-leverage multi-language work. Recorded in `docs/summaries/Phase_13_walls_and_handoff.md` §4.1. |
| **G-35** | **Scrapy fault-injection assertion rejects smoke-only drift detection** | **Phase 14 or patch** | The `as_check` predicate in `_fault_injection.py` requires `test_value_*` in the failure output. A target whose value test is not written (non-comparable return) has only `test_smoke_*`; the widened predicate must accept it. The unit test does not exercise this path. |

**Gaps that Phase 13 closed:**

- G-25 (pipeline proof requires agents) — the orchestrator runs the pipeline end-to-end.
- Partial G-13 (CLI exit codes) — the orchestrator's public surface is stable; the CLI consumes it.

**Gaps that remain open after Phase 13:**

- G-1 through G-12, G-14 unchanged
- G-13 partial (CLI exists at the orchestrator level; exit-code mapping is Phase 14)
- G-15 through G-24, G-26 through G-32 unchanged
- G-33, G-34, G-35 added above

---

## Git history

**Branch state:** `main` and `phase-13-orchestrator` both exist. Phase 13 work is uncommitted at the time of this snapshot. The push sequence is documented in the Phase 13 delivery verification.

**Recent commits (order may not be exact):**

```
<latest on main>  docs: add Phase 13 v2 roadmap and Phase 12 v2 residuals
<latest on main>  feat: phase-13 orchestrator with graph walker and verifier stack
<earlier>         docs: add Phase 12 design settlement (v1) and Phase 11 record
<earlier>         feat: phase-11 agents complete with deep test integration
<earlier>         docs: add build history for phases 1 through 10
<earlier>         feat: phase-10 prompts with renderer, schema, eval harness
<earlier>         feat: phase-9 plugins with runtime grammar loading
<earlier>         feat: multi-language kernel extension (8 languages)
<earlier>         feat: phase-8 providers, model routing, sandbox backends
<earlier>         feat: phase-7 rust engines (blast, sandbox, audit, policy)
<earlier>         feat: phase-6 rust kernel (parser, CPG, queries)
<earlier>         feat: phase-5 bridges (PyO3, JSON-RPC)
<earlier>         feat: phase-4 interfaces (12 Protocols, bridge envelope)
<earlier>         feat: phase-3 storage (SQLite, filesystem, graph, cache, skills)
<earlier>         feat: phase-1 foundation and phase-2 schemas
<earlier>         chore(pre-commit): make hooks phase-aware
```

### Commit strategy for Phase 13

**Branch:** `phase-13-orchestrator` from `main`.

**Commit:** single commit containing all 49 Phase 13 files, plus the eight Rule-1 escalations and the deep-test rewires.

```bash
git add python/codeguardian/core/orchestrator/ \
  python/codeguardian/core/telemetry/ \
  python/codeguardian/core/storage/migrations/007_agent_registry.sql \
  python/codeguardian/core/storage/sqlite_store.py \
  python/codeguardian/core/providers/fallback_router.py \
  python/codeguardian/core/agents/analyst.py \
  python/codeguardian/core/agents/writer.py \
  python/codeguardian/core/agents/tester.py \
  sandtest/deep/_pipeline.py \
  sandtest/deep/_sandbox_verify.py \
  sandtest/deep/_verifier_agents.py \
  sandtest/deep_test.py \
  tests/unit/core/test_phase_graph.py \
  tests/unit/core/test_orchestrator_core.py \
  tests/unit/core/test_verification_stack.py \
  tests/unit/core/test_context_and_routing.py \
  tests/unit/core/test_evidence_columns.py \
  tests/unit/core/test_sandbox_adapter.py \
  tests/unit/core/test_phase_gates.py \
  tests/unit/core/test_orchestrator_e2e.py \
  tests/unit/core/test_agent_analyst.py \
  tests/unit/core/test_agent_writer.py \
  docs/public-api.md \
  docs/05-data-model.md \
  docs/summaries/Phase_13_walls_and_handoff.md \
  BUILD_STATE.md

git commit -m "feat(phase-13): orchestrator with graph walker, verifier stack, and telemetry"
```

Pre-commit may modify files on first attempt; re-add and commit again. Second attempt passes.

**Push:** `git push -u origin phase-13-orchestrator`. **PR:** one PR, review, merge. **Tag:** `v1.3.0` after merge.

---

## Where the next phase starts

Phase 14 — CLI. Reference `BUILD_ORDER.md` §19 and `docs/04-architecture.md` §7.

**Blocks (per `BUILD_ORDER.md`, subject to revision during the Phase 14 design settlement):**

1. **B-060** — CLI Foundation: `cli/__init__.py`, `cli/main.py`, `cli/context.py`, `cli/errors.py`
2. **B-061** — CLI Commands: `cli/commands/run.py`, `cli/commands/resume.py`, `cli/commands/pause.py`, `cli/commands/state.py`, `cli/commands/skill.py`
3. **B-062** — CLI Output: `cli/output/json_out.py`, `cli/output/human_out.py`, `cli/output/progress.py`
4. **B-063** — CLI Views: `cli/views/run_view.py`, `cli/views/skill_view.py`
5. **B-064** — CLI Tests: `test_cli_commands.py`, `test_cli_output.py`, `test_cli_views.py`

**What "done" means for Phase 14:**

1. `codeguardian run --repo <url> --target <path>:<symbol> --directive <text>` runs the pipeline through the orchestrator and produces a `RunResult`.
2. `codeguardian resume <run_id>` resumes from the last checkpoint using `CheckpointSupervisor`.
3. `codeguardian pause <run_id>` sets the run's status to paused.
4. `codeguardian state <run_id>` returns the current `RunState`.
5. `codeguardian skill list`, `skill show`, `skill promote`, `skill retire`, `skill rejected`, `skill graph` read from the Phase 12 v2 governor.
6. Every command supports `--json` for machine-readable output.
7. Progress is streamed per phase so an 11-minute run does not look frozen.
8. The G-33 residual is addressed: a context-window overflow is reported as a user-facing refusal, not a silent skip.
9. `PYTHONPATH=python python3 -m pytest tests/ -q` remains green with all Phase 1–13 tests passing and the Phase 14 additions.

**What Phase 14 will NOT deliver (foreseeable residuals):**
- MCP server and client (Phase 15)
- Reporters (Phase 15)
- Full assembly script (Phase 16)
- Multi-language pipeline (v2; see `docs/summaries/Phase_13_walls_and_handoff.md`)
- LSP semantic layer (v2)

**Suggested Phase 14 design settlement length:** shorter than Phase 13's. The CLI is a thin surface over the orchestrator; the design questions are about output shape, progress presentation, and error translation, not about architecture. A document of Phase 9's length is sufficient.

---

## Phase 0 note

The following Phase 0 files are still deferred:

- `.github/workflows/ci.yml` — no CI yet
- `.github/workflows/docs.yml`, `.github/workflows/release.yml`, `.github/workflows/security.yml`
- `.devcontainer/`
- `docker/Dockerfile.python-sandbox`, `docker/Dockerfile.typescript-sandbox`
- `benchmarks/` fixtures and baselines
- `mkdocs.yml` (the docs site does not build yet)
- `examples/skills/` (the two example SKILL.md files are 0 bytes; `skills/README.md` is 0 bytes; `scripts/migrate.py` is 0 bytes)

33 files, no runtime impact. They land in one commit at the end of the build, or earlier if a phase boundary test needs a clean environment.

Until then, `pytest tests/` runs on the local machine and pre-commit runs on commit. The CI gap means a merge to `main` does not automatically run tests.
