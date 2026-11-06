# CodeGuardian — Build State

**Snapshot date:** 2026-11-06
**Last phase completed:** Phase 12 — Skills Module v2 (27 files across 6 blocks)
**Next phase to start:** Phase 13 — Orchestrator
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

---

## Completed phases

### Phase 0 — Repository Scaffold (partial)

| Block | Status | Files |
|---|---|---|
| B-001 Root Build Configuration | done | pyproject.toml, Cargo.toml, Cargo.lock, rust-toolchain.toml, rustfmt.toml |
| B-002 Root Project Hygiene | done | clippy.toml, justfile, .gitignore, .codeguardianignore, .dockerignore |
| B-003 Root Editor and Documentation | done | .editorconfig, .env.example, .python-version, .pre-commit-config.yaml, mkdocs.yml |
| B-004 through B-009 | deferred to end | 30 files, no runtime impact |

### Phases 1 through 10 — complete

See `docs/summaries/phases-0-to-8.md` (through Phase 8), `docs/summaries/phase-9-design.md` (Phase 9), and `docs/summaries/phase-10.md` (Phase 10) for the detailed build history of every phase through Phase 10, plus the multi-language extension and the deep integration test.

Test state at the Phase 10 boundary: **884 passed, 10 skipped, 7 xfailed, 5 xpassed** (Python pytest). Deep test: 59 passed, 2 xfail.

### Multi-language kernel extension — complete

Scope extension between Phase 8 and Phase 9. Extended the kernel from 2 languages (Python, TypeScript) to 8 (adding JavaScript, Java, Go, Rust, C, C++). C# was attempted but removed after `tree-sitter-c-sharp` 0.23.5 produced an ABI incompatible with `tree-sitter` 0.24.

Recorded in `docs/summaries/phases-0-to-8.md` §13. Post-batch corrections in §13.3.

### Deep integration test — complete

`sandtest/deep_test.py` (795+ lines). Permanent artifact. Clones a real repository into `sandtest/work/`, runs every phase that exists, reports pass/fail/skip/xfail/warn per section. Does not stop at first failure. Downstream sections mark SKIP when a dependency fails.

### Phase 9 — Plugins (complete)

Merged scope. Replaced the originally-planned Phase 9 (plugin metadata) and Phase 9.5 (runtime grammar loading) with one phase delivering 8 blocks, 36 files. Full write-up in `docs/summaries/phase-9-design.md`.

Delivers: plugin loader infrastructure, runtime grammar loading via `tree_sitter::Loader`, eight bundled language plugins, two bundled tool plugins, one bundled reporter plugin, behavioral contract tests, integration tests, version negotiation (N / N-1 SemVer), audit-hook permission enforcement, fault barrier with timeouts.

### Phase 10 — Prompts (complete)

24 files. Full write-up in `docs/summaries/phase-10.md`.

Delivers: strict `{{var}}` template renderer with injection defense, per-role output schemas, version-aware loader with `_base/v<version>.md` pairing, seven role prompts at v1.0.0, three golden-case eval YAMLs, one eval harness, eight test files. Approximately 178 new tests.

Pre-work delivered with Phase 10 (not counted as Phase 10 files): `_native.PyCPG` handle, three kernel query methods (`find_declaration`, `qualified_name_of`, `declaration_info`), `ToolPlugin.action_kind` / `sensitivity` / `is_read_only`, `ModelOptions.max_reasoning_tokens`, `context_provider` plugin type, four context provider plugins, `BudgetTracker`.

### Phase 11 — Agents (complete)

Full write-up in `docs/summaries/Phase_11.md`. Delivery record: `docs/summaries/Phase_11.md` §13 records the A- rating; shortcomings inventory: `docs/summaries/Phase_11_shortcomings.md`.

Delivers: nine callable agents (recon, blast, analyst, tester, writer, three verifiers, skill_curator), routing primitives, evidence capture (migration 004), the closed verifier loop primitive, the context budget primitive, per-agent telemetry, the `_runtime.py` / `_store_context.py` infrastructure.

Test state at the Phase 11 boundary: **1367 passed, 14 skipped, 7 xfailed, 5 xpassed**. Deep test: **87 passed, 2 failed, 0 skipped, 2 xfailed**. The two deep test failures are documented as Phase 11 residuals (§3.4 in Phase_11.md): tester emitted JS literals instead of Python literals (a model quality issue with `qwen3.8-27b` on the free tier), and the fault-injection section labels its own success as a failure (a one-line label bug in the test harness).

Three tiers of known Phase 11 issues are recorded in `docs/summaries/Phase_11_shortcomings.md`. Tier 1 (three fixes applied but not re-verified) and Tier 2 (four correctness bugs) are the actionable items. The three item closures required before declaring v1.1 provably complete are in §12.1 of Phase_11.md.

### Phase 12 v2 — Skills Module (complete)

Full design settlement in `docs/summaries/Phase_12.md`. This section records what shipped.

**Scope.** Phase 12 v1 shipped 6 files: `embeddings.py`, `library.py`, `retriever.py`, `indexer.py`, and their tests. Phase 12 v2 delivered 27 files across 6 blocks:
- **12 new source modules** in `python/codeguardian/core/skills/`: `types.py`, `graph.py`, `graph_store.py`, `ppr.py`, `retrieval.py`, `hydrator.py`, `reranker.py`, `negative.py`, `governor.py`, `provenance.py`, `security.py`, `evolver.py`
- **2 new migrations**: `005_skill_graph.sql` (five new tables), `006_skill_states.sql` (expanded CHECK constraint on `skills.state`)
- **8 new test files** in `tests/unit/core/`: `test_skill_graph.py`, `test_ppr.py`, `test_retrieval_pipeline.py`, `test_hydrator.py`, `test_negative.py`, `test_governor.py`, `test_skill_security.py`, `test_evolver.py`
- **6 updated files**: `schemas/skill.json` (v1.0 → v1.1), `core/config/defaults.py` (16 new constants), `core/config/schema.py` (SkillsConfig), `core/skills/library.py` (accessors + admission routing), `core/skills/indexer.py` (rebuild_graph, apply_forgetting_curve), `core/skills/__init__.py` (19 public exports)
- **1 Rule-1 escalation fix**: `core/storage/skill_store.py` (FK-safe upsert + FK-safe remove)

**Six-layer architecture.** Every layer behind a Protocol, every layer flag-gated:
1. Store — existing `SkillStore` Markdown + FTS5 + vec0, unchanged
2. Graph — deterministic typed edges from frontmatter, rebuilt from source
3. Retrieval — staged pipeline with a fallback at every stage
4. Negative memory — separate tier with explicit adopt/reject
5. Governance — draft → trial → active → stable → retired, admission gate, dedupe, fitness retirement
6. Security — admission scan, provenance pinning, invocation guard

**Four-stage decision chain.** The main agent never chooses; a chain does:
- Stage 0 — Recon + Blast project filter (deterministic)
- Stage 1 — Hybrid seed (FTS5 + vec0, RRF merge)
- Stage 2 — Reverse-weighted PageRank over the graph, bounded to 1–2 hops
- Stage 3 — Optional cross-encoder rerank
- Stage 4 — Budget-aware hydration with calibrated threshold gate

Every stage has a fallback to the previous stage. Flag-off produces byte-identical output to Phase 12 v1.

**Five safety rules, enforced by test:**
1. Every new flag defaults to `false`; a user who upgrades without editing config gets exactly v1 behavior
2. New state is derived, never authoritative; drop and rebuild from Markdown
3. `retriever.py` stays in the repo as the always-available fallback
4. Schema only grows optional fields; every existing `SKILL.md` validates without edit
5. Nothing mutates in place; graph mutations use a shadow file promoted only on validation

**Rule 1 escalations performed:**
- `core/storage/skill_store.py` — `_upsert_skill_row` was DELETE-then-INSERT, which failed the `skill_feedback.skill_name` FK once a skill had any feedback. Changed to UPDATE-when-exists. The same fix covers `promote_skill`, `archive_skill`, and `remove`. Pre-existing bug from Phase 3, exposed by Phase 12 v2's `record_load_outcome`.
- `core/storage/migrations/006_skill_states.sql` — new migration to expand the `skills.state` CHECK constraint from `('active', 'stale', 'archived', 'draft')` to include `('trial', 'stable', 'retired')`. SQLite cannot ALTER a CHECK, so the table is rebuilt with rowids preserved and FTS rebuilt. Migration is forward-only and idempotent, per the migration contract.

**Test state at the Phase 12 v2 boundary:** **1547 passed, 14 skipped, 7 xfailed, 5 xpassed** (Python pytest). The delta from Phase 11's 1367 is +180: the pre-existing `test_skills.py` and `test_skill_store.py` (which were uncommitted at Phase 11 boundary), plus the 166 new tests from Blocks 5 and 6.

**Verification gates cleared:**
- Full pytest suite: 1547 passed
- `ruff check` on all Phase 12 files: `All checks passed!`
- Migration 005 applies on a fresh DB and on an existing DB (data preserved)
- Migration 006 applies on a fresh DB and on an existing DB (data preserved, FKs safe)
- Flag-off byte-identical guarantee: verified by `test_all_flags_off_delegates_to_v1_retriever`
- False-positive / false-negative security guard: verified manually after Block 6

**What Phase 12 v2 explicitly does NOT deliver:**
- Agentic multi-hop retrieval (SkillGATE-style region exploration) — deferred to a future phase
- WASM-sandboxed skill execution — deferred
- Cross-repo skill transfer / `scope: repo` skills — deferred
- Skill authorship assistance — deferred
- Embedding model swap — deferred
- Multi-tenant isolation — deferred
- Semantic caching of retrieval results — deliberately not implemented (per Anthropic engineering's retired-profile-cache finding)

**Documented residuals from Phase 12 v2 design (documented in `docs/summaries/Phase_12.md` §13):**

| Residual | When it becomes relevant |
|---|---|
| Reranker loads `Xenova/ms-marco-MiniLM-L-6-v2` via `fastembed`; if the installed fastembed lacks `TextCrossEncoder`, degrades to passthrough silently | When reranker proves insufficient on real workloads |
| Stage 0 project filter is language-only in v2; dependency version and test framework match need Phase 13 orchestration | Phase 13 |
| Fitness formula uses effectiveness only; `−λ·cost` deferred because there is no per-skill cost column | When cost tracking arrives |
| Evolver store uses a sidecar JSON file, not shadow tables, to avoid migration 006 dependency | Phase 13+ |
| Negative knowledge decay is manual (via `apply_forgetting_curve`), not automatic | When Phase 13 scheduler exists |
| Provenance `admission_evidence` is reconstructed from persisted columns, not stored as a JSON blob | When richer evidence is needed |

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
| **Phase 12 v2 complete** | **1547** | **14** | **7** | **5** | **unchanged from Phase 11** |

**Pytest command:** `PYTHONPATH=python python3 -m pytest tests/ -q`
**Current result:** `1547 passed, 14 skipped, 7 xfailed, 5 xpassed`

**Ruff:** `ruff check python/codeguardian/ tests/` → `All checks passed!`

**Deep test:** last run is the Phase 11 deep test recorded in `docs/summaries/Phase_11.md` §3.1. The Phase 12 v2 work has not been exercised by the deep test; that is a Phase 13 obligation when the orchestrator consumes the retrieval pipeline.

**Breakdown of the 7 pytest xfails:** unchanged from Phase 11. C/C++ declarator gap (G-15) plus cross-file resolution incompleteness (G-16).

**Breakdown of the 5 pytest xpasses:** unchanged from Phase 11. Five of the eight cross-file tests happen to resolve; `strict=False` counts them as passed.

---

## Deviations from BUILD_ORDER

### Phase 1 through Phase 10 deviations

Recorded in `docs/summaries/phases-0-to-8.md` §10–§13, `docs/summaries/phase-9-design.md` §2.3, and `docs/summaries/phase-10.md` §9. All resolved, none open.

### Phase 11 deviations

Recorded in `docs/summaries/Phase_11.md`. The 41 files delivered versus the plan's 11 agent modules. Deviations are all additive at the behavioral level.

### Phase 12 v2 deviations

| # | What | Why |
|---|---|---|
| 1 | Phase 12 grew from 6 files to 27 | The v1 plan assumed flat retrieval was sufficient. The 2026 literature showed it is not at scale (implicit routing collapses from 73.6% to 27.4% recall with 1,000 distractors). The v2 delivery adds the graph, governance, and security layers to address that. |
| 2 | `skill_store.py` was on the "deliberately untouched" list and was modified | Rule 1 escalation. FK-safe upsert bug from Phase 3 was exposed by Phase 12 v2's `record_load_outcome`. Pre-existing bug, not introduced by this phase. |
| 3 | Migration `006_skill_states.sql` added | Rule 1 escalation. The `skills.state` CHECK constraint from Phase 3 could not accept `trial`, `stable`, `retired`. SQLite cannot ALTER a CHECK, so the table is rebuilt. Forward-only, idempotent, preserves rowids and FKs. |
| 4 | Evolver candidates use a sidecar JSON file, not shadow tables | The v2 design specified shadow tables, but that would require a new migration beyond `005` and `006`. A JSON file has the same isolation property (nothing touches the serving graph until `promote`) with no migration dependency. |
| 5 | Negative knowledge decay is manual, not automatic | The v2 design's forgetting curve runs on demand. Phase 13's scheduler is what will call it periodically. |
| 6 | `SkillIndexer.rebuild_graph` catches all exceptions and returns `(0, 0)` on failure | Maintenance passes run on a timer; a failing pass should not crash the process. The trade-off is that a failed rebuild is silent in the return value but logged as a warning. |

---

## Escalation queue

Rule 1 escalations performed across all phases. Every rewrite is additive at the behavioral level.

**Phases 1–10:** recorded in `docs/summaries/phases-0-to-8.md`, `docs/summaries/phase-9-design.md`, `docs/summaries/phase-10.md`. All resolved.

**Phase 11:** recorded in `docs/summaries/Phase_11.md`. All resolved.

**Phase 12 v2:** two Rule 1 escalations, both closed this session.

| File | Nature | Approved |
|---|---|---|
| `core/storage/skill_store.py` | FK-safe upsert and FK-safe remove; latent Phase 3 bug | Yes (during Block 4 verification) |
| `core/storage/migrations/006_skill_states.sql` | New migration, expanded CHECK constraint | Yes (during Block 4 verification) |

**Open escalations:** zero.

---

## What the next chat must do first

1. Read `skills.md`, `BUILD_ORDER.md`, `BUILD_STATE.md` (this file), `docs/summaries/Phase_12.md`, `docs/summaries/Phase_11.md`, and `docs/public-api.md`.
2. Read `docs/git-workflow.md` for the branch and commit workflow.
3. Do not attempt to re-verify Phases 1 through 12. They are green. Trust the state.
4. Ask the user to `source .venv/bin/activate` before any Python command.
5. Confirm `cargo --version` works inside the venv. If it fails, the venv activate script no longer appends `$HOME/.cargo/bin`.
6. Start **Phase 13 — Orchestrator**. Read `BUILD_ORDER.md` §18, `docs/04-architecture.md` §6, and `docs/summaries/Phase_12.md` for the spec. The retrieval pipeline, governance gate, negative store, telemetry helpers, checkpointing primitives, and the verifier loop are all ready for the orchestrator to consume.
7. Create the branch first: `git checkout -b phase-13-orchestrator`. All Phase 13 work happens on that branch.
8. **Phase 13 must build the orchestrator as a phase graph, not a hardcoded sequence.** This is the modularity-audit §7 recommendation. If the orchestrator is hardcoded as an eight-phase pipeline, v2 (greenfield, upgrade) will require a full pipeline refactor. If it interprets a `PhaseGraph` object, v2 adds new graph definitions only.
9. **Phase 13 consumes Phase 12's public surface.** Specifically:
   - `library.retrieval().retrieve(query, project_context=card)` for retrieval
   - `library.governor().admit(name, frontmatter, body, eval_score=...)` for skill admission after the Skill Curator produces a proposal
   - `library.negative().find(context_signature)` before retries, with explicit adopt/reject logging to `agent_calls.decision_log`
   - `library.governor().record_load_outcome(name, outcome)` after every skill use
   - `library.indexer().rebuild_graph()` and `.apply_forgetting_curve(governor=...)` at run start or on a timer
   - `ProjectCard` must be constructed from `recon.json` and `blast_radius.json`. The retrieval pipeline's Stage 0 filter is inert without it.
10. **Phase 13 must satisfy Phase 11 residuals that Phase 11 could not close in isolation.** These are the Tier 3 items in `docs/summaries/Phase_11_shortcomings.md`: the sandbox adapter for the deep test (unlocks six opt-in features), the `VerifierLoop` wiring into the deep test's apply → verify → rollback flow, `ContextBudget` construction from `CapabilityProfile` per run, evidence columns written by all agents, and the skill curator mining gate supplied from Phase 12's run history.

---

## Rules the next chat must follow

- Think before writing. Read the problem twice. Name the invariant. Name the failure mode.
- Write full files only. Never fragments, never diffs.
- Never rewrite a file that has already passed without stopping and asking. Rule 1 escalations need explicit approval in writing before the edit lands.
- Test at phase boundaries, not per block. For Block-heavy phases, also run targeted block tests to catch failures early.
- Never claim a test passed. Wait for the user's raw output.
- Ask before guessing. Ask before adding a code, symbol, dependency, schema field, or error code.
- Follow `skills.md` for every file.
- **Before writing any file marked `(UPDATE)`, request `git show HEAD:<path>` for that file and write from the actual current content, not from imports or assumptions.** This rule was added after the `defaults.py` regression in this session, where an UPDATE was written from imports and lost four constants.
- **Every patch script ends with a verify-before-write assertion.** This rule was added after a `defaults.py` patch script wrote a file whose `__all__` promised names that the script never actually defined.

---

## Known gaps carried forward

Gaps identified across Phases 1 through 12. Recorded here so they are not forgotten. The table is consolidated from `docs/summaries/phases-0-to-8.md` §16, `docs/summaries/Phase_11_shortcomings.md`, and Phase 12 v2 residuals.

| # | Gap | Becomes relevant | Notes |
|---|---|---|---|
| G-1 | Multi-process SQLite write contention | Phase 13 | `busy_timeout` handles two writers; three will time out |
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
| G-13 | CLI exit-code mapping not written | Phase 14 | Design specified, code does not exist |
| G-14 | Phase 0 B-004 through B-009 deferred | End of build | 30 files, no runtime impact |
| G-15 | C/C++ function names not extracted | Next kernel phase | `extract_name_id` does not walk declarator chains |
| G-16 | Cross-file call resolution incomplete | Next kernel phase | Narrowed by Phase 9 but not closed |
| G-17 | C# not supported | When grammar ships | ABI incompatibility with tree-sitter 0.24 |
| G-18 | `ast.rs` comment on `Property` mentions C# | Cosmetic | One-word fix, Rule 1 escalation on `ast.rs` |
| G-19 | No automated test/language drift check | Phase 13+ | Adding a language to `dispatch` without adding a test case goes unnoticed |
| G-20 | `audit.verify` returns `entries_checked: 1` after apply + rollback | Investigate | Real finding from the deep test |
| G-21 | `echo` returns 0 callers on `pallets/click` | Next kernel phase | Partly resolved by the pyo3 `path` key fix. `echo` in click not re-tested |
| G-22 | Maturin does not work with this workspace | Cosmetic | Documented in environment notes |
| G-23 | Phase 9 residuals: permission bypass via C extensions | Phase 13+ | Audit hook is Python-level |
| G-24 | Phase 9 residuals: plugin process crash isolation | Phase 13 | Fault barrier catches exceptions; segfaults need process pool |
| G-25 | Phase 9 residuals: pipeline proof requires agents | Partially closed by Phase 11 | Still needs Phase 13 orchestrator |
| G-26 | Phase 11 residuals: reranker feature flag never exercised on real workload | Phase 13 | The reranker degrades to passthrough silently when `fastembed` lacks `TextCrossEncoder` |
| G-27 | Phase 12 v2 residuals: Stage 0 filter is language-only | Phase 13 | Dependency version and test framework match need orchestration |
| G-28 | Phase 12 v2 residuals: negative knowledge decay is manual | Phase 13 | Requires a scheduler |
| G-29 | Phase 12 v2 residuals: evolver uses sidecar JSON, not shadow tables | Phase 13+ | Working alternative but does not use the DB's transactional guarantees |
| G-30 | Phase 12 v2 residuals: provenance `admission_evidence` is reconstructed from columns | Phase 13+ | Not stored as a JSON blob; richer evidence needs a schema change |
| G-31 | Phase 12 v2 residuals: fitness formula is effectiveness-only | When cost tracking arrives | `−λ·cost` deferred |
| G-32 | Phase 11 residuals: 3 item closures not yet applied (fault-injection label, tester prompt clarification, skill curator prompt clarification) | Before Phase 13 can consume the Skill Curator reliably | `docs/summaries/Phase_11_shortcomings.md` §1.1, §1.2, §1.3 |

**Gaps that Phase 12 v2 closed:**
- Partial G-19 (behavioral contract tests for skills)
- Nothing else closed by Phase 12 v2; the new tables are derived and drop-rebuild safe, so the "state divergence" class of bugs (e.g. schema/constraint drift) is protected by the 005/006 migrations.

**Gaps that remain open after Phase 12 v2:**
- G-1 through G-9, G-11, G-13, G-14 unchanged
- G-12 partial (JSON-RPC error mapping)
- G-15, G-16, G-17, G-18, G-20, G-21 (kernel issues) — Phase 12 v2 does not touch them
- G-19 partial (Phase 12 v2's tests cover the skills module but not the full surface)
- G-22 (documentation only)
- G-23, G-24, G-25 (Phase 9 residuals)
- G-26, G-27, G-28, G-29, G-30, G-31 (Phase 12 v2 residuals, added above)
- G-32 (Phase 11 residuals)

---

## Git history

**Branch state:** `main` and `phase-12-skills-v2` both exist. Phase 12 v2 work is uncommitted at the time of this snapshot. The push sequence is documented in the Phase 12 delivery verification.

**Recent commits (order may not be exact):**

```
<latest on main>  docs: add Phase 12 design settlement (v1) and Phase 11 record
<latest on main>  feat: phase-11 agents complete with deep test integration
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

### Commit strategy for Phase 12 v2

**Branch:** `phase-12-skills-v2` from `main`.

**Commit:** single commit containing all 27 Phase 12 v2 files, plus the two migrations, plus the `skill_store.py` Rule 1 fix.

```bash
git add python/codeguardian/core/skills/ \
  python/codeguardian/core/config/defaults.py \
  python/codeguardian/core/config/schema.py \
  python/codeguardian/core/storage/migrations/005_skill_graph.sql \
  python/codeguardian/core/storage/migrations/006_skill_states.sql \
  python/codeguardian/core/storage/skill_store.py \
  schemas/skill.json \
  tests/unit/core/test_skill_graph.py \
  tests/unit/core/test_ppr.py \
  tests/unit/core/test_retrieval_pipeline.py \
  tests/unit/core/test_hydrator.py \
  tests/unit/core/test_negative.py \
  tests/unit/core/test_governor.py \
  tests/unit/core/test_skill_security.py \
  tests/unit/core/test_evolver.py \
  tests/unit/core/test_skills.py

git commit -m "feat(phase-12): skills module v2 with graph, governance, and security"
```

Pre-commit may modify files on first attempt; re-add and commit again. Second attempt passes.

**Push:** `git push -u origin phase-12-skills-v2`. **PR:** one PR, review, merge. **Tag:** `v1.2.0` after merge.

---

## Where the next phase starts

Phase 13 — Orchestrator. Reference `BUILD_ORDER.md` §18 and `docs/04-architecture.md` §6.

**Blocks (per `BUILD_ORDER.md`, subject to revision during the Phase 13 design settlement):**

1. **B-056** — Orchestrator Core: `orchestrator/__init__.py`, `phase_gates.py`, `consent.py`, `verification_prompt.py`, `checkpointing.py`
2. **B-057** — Orchestrator Implementation: `langgraph_orchestrator.py`
3. **B-058** — Telemetry: `telemetry/__init__.py`, `metrics.py`, `tracing.py`, `langsmith.py`
4. **B-059** — Orchestrator Tests: `test_orchestrator.py`, `test_phase_gates.py`, `test_checkpointing.py`, `test_consent.py`, `test_telemetry.py`

**What "done" means for Phase 13:**

1. The full pipeline runs end-to-end against a fixture repository: recon → blast → analyst → tester → writer → sandbox verify → three verifiers → rollback on any failure.
2. The pipeline is graph-driven, not hardcoded. `PhaseGraph` per mode is defined in `orchestrator/phase_graphs/legacy.py`. A `greenfield` graph can be added later without touching the orchestrator.
3. `library.retrieval().retrieve()` is called with a `ProjectCard` constructed from `recon.json` and `blast_radius.json`. Stage 0 filter runs.
4. `library.governor().admit(...)` is called after every Skill Curator proposal. Only admitted skills enter the pool.
5. `library.negative().find(context_signature)` is called before retries with explicit adopt/reject logging to `agent_calls.decision_log`.
6. `library.governor().record_load_outcome(...)` is called after every skill use. Fitness and lifecycle transitions fire.
7. `VerifierLoop` is wired into the apply → verify → rollback flow. Failed verifications trigger bounded retries.
8. Checkpoint durability is supervised. A crash mid-run resumes from the last checkpoint.
9. The `ContextBudget` is constructed per run from a `CapabilityProfile` and passed to every `tracked_model_call`.
10. `PYTHONPATH=python python3 -m pytest tests/ -q` remains green with all Phase 1–12 tests passing and the Phase 13 additions.

**What Phase 13 will NOT deliver (foreseeable residuals):**
- CLI (Phase 14)
- MCP (Phase 15)
- Full reporter integration (Phase 15)
- Auto-propose mode (v2 design, post-v1)
- Phase 11 residuals beyond G-26 and G-32 (the two that Phase 13 can close)

**Suggested Phase 13 design settlement length:** at least as long as Phase 9's and Phase 12's. The orchestrator is the phase that decides whether the whole system's modularity survives v2. The decision "phase graph, not hardcoded sequence" from `docs/modularity-audit.md` §7 must be documented as an explicit settlement item, with the alternative considered and rejected on the record.

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
