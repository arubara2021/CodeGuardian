# Phase 13 Harness — v2 Roadmap

**Snapshot date:** 2026-11-08
**Status:** Design settled. Implementation pending.
**Scope:** The harness engine delivered by Phase 13. Not the CLI (Phase 14), not the MCP server or the reporters (Phase 15). Those are separate phases with their own settlements.
**Deliverables:** Seven work streams across six increment windows. Estimated four to six months of focused implementation for one person working with AI assistance.

---

## 1. What this document is

This is the v2 plan for the harness engine. It records what Phase 13 delivered, what walls remain, and the concrete work that lifts each one. It is not a restatement of the Phase 13 settlement; that document is complete and its acceptance criteria are met. It is the record of what comes after.

The document is organized around seven walls and seven work streams. Each wall names a limitation that the harness has today and the reason that limitation exists. Each work stream names the change that removes the wall, the components to build, and the acceptance criterion that proves the wall is gone. The section on architectural invariants records the constraints that must not break while the walls come down, because the value of Phase 13 is not in any single feature — it is in the invariants that let the harness grow without becoming a different harness.

The seven walls are ordered by leverage, not by difficulty. LSP integration is first because it changes what the writer can propose, not just how fast it proposes it. Multi-language comes second because it multiplies the reach of the same architecture. The remaining five are refinements that make the harness more resilient, more measurable, and more honest about what it does.

---

## 2. What Phase 13 delivered

Phase 13 shipped 49 files across ten blocks. The orchestrator is the graph walker, the registry is the slot resolver, the loop dispatches the agents, and every other component is a collaborator the loop uses when the phase calls for it.

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

The deep test ran on `httpx` at 65 passed, 0 failed, 2 xfailed. On `scrapy` at 57 passed, 1 failed (a test-assertion detail, not a pipeline defect), 2 skipped (honest environment limits), 1 warning, 2 xfailed. The pipeline walked all ten phases on the proceed and review bands of both repos. The unit suite sits at 1,771 passed, 14 skipped, 7 xfailed, 5 xpassed.

What this proves is that the architecture works. What it does not prove is that the architecture is complete. The seven walls are the gap between "works" and "works on the projects people actually have."

---

## 3. The seven walls

### 3.1 Wall one — no semantic layer

The writer proposes a splice. The kernel finds the symbol's byte range. The adapter replaces the bytes. This is structural editing: the harness knows where the symbol starts and ends, and it can swap one textual range for another. It is not semantic editing. When a rename touches ten references across five files, the harness has no mechanism to propagate the rename with the guarantee a language server provides.

The 2026 field has converged on a two-layer model. Tree-sitter handles syntax: fast, language-agnostic, sufficient for finding where a node begins and ends. LSP handles semantics: rename, find-references, code actions, diagnostic validation. NREKI pairs Tree-sitter's syntax layer with LSP's semantic layer and uses LSP code actions to auto-heal diagnostics. Ricecoder's LSP-first architecture falls back through LSP to configured rules to built-in Tree-sitter to generic text matching, in that order. The `agent-lsp` project (2026) exposes 65 LSP tools across 30 CI-verified languages behind a single JSON-RPC interface, which is the operational proof that the abstraction is implementable at scale.

Phase 13 has the first layer. It does not have the second.

**What this means in practice.** A Python refactor works because Python is the language the kernel was designed for first, and because Python's AST module gives the rewriter a byte-accurate path. A TypeScript rename of a symbol that appears in a barrel file fails silently: the writer edits the definition, the harness splices the definition, the test imports the barrel, and the barrel still points at the old name. The test passes because nothing calls the symbol through the definition path. The failure is discovered only when a user tries to call the symbol from a different module.

This is the wall the LSP layer removes. Until it does, the harness is honest to say it supports Python and quietly limited everywhere else.

### 3.2 Wall two — routing is tiered but not step-level

The `FallbackModelProvider` routes by role: analyst to frontier, tester to standard, verifiers to their configured tiers. The routing is correct at the run level. It is coarse at the step level. A tester in its first attempt may need a cheap model and succeed. The same tester in its second attempt, after a schema violation, may need a frontier model to reason about what went wrong. The current router cannot express that difference without a new entry in `ESCALATION_TRIGGERS`.

The 2026 routing literature has moved past run-level tiers. Harness-Native Agentic Routing makes the routing decision a native primitive of the harness: the router selects either a single best-fit model for cost-effective execution or multiple complementary models for ensemble-style accuracy, conditioned on the current step's evidence. The difference from Phase 13 is that "conditioned on the current step's evidence" replaces "conditioned on the role's tier." The model is chosen per dispatch, not per role.

The evidence that drives the choice is already in the harness. The `EvidenceBus` carries `VerifierFailed`, `TestRunFailed`, `ToolCallFailed`, and `BlastRecommendationAdjusted` events. The `BudgetEnvelope` tracks pressure. The `decision_log` records the previous attempt's failure. A step-level router reads all of these before selecting the next model. Phase 13's router reads only the role and the blast band.

**What this means in practice.** A run that hits `MODEL_RATE_LIMITED` on the writer's first attempt escalates to the frontier tier for the retry. That works. A run where the writer's first attempt produced a plausible candidate that failed a correctness verifier does not escalate, because the failure is not in the writer's `ESCALATION_TRIGGERS`. The router cannot see that "the verifier said the candidate has a semantic mismatch" is a reason to send the next attempt to a stronger writer. The information exists; the router does not consume it.

### 3.3 Wall three — the pipeline is Python-only

The kernel parses eight languages. The blast engine resolves callers on eight languages. The pipeline — analyst, tester, writer, and three verifiers — runs on one.

This is not a design oversight. It is the honest current scope. The pipeline's job is to produce a refactor: read the code, write a characterization test, propose a candidate, verify the candidate, apply or roll back. Each of those steps has a language-specific implementation detail. The characterization test is a pytest file for Python, a JUnit file for Java, a `#[test]` block for Rust. The splice is byte-range replacement with Python `ast` for Python, `ast-grep` or `Splice` for other languages. Fault injection wraps return expressions with a helper that mutates the value in a way the test catches — the mutation rules differ per language.

The 2026 benchmark is multilingual. SWE-Bench ProMax evaluates 170 instances across Python, Java, TypeScript, Go, C, C++, and Rust, averaging 11.5 modified files and 261.6 lines per refactor. The best frontier model (GPT-5.2) resolves 41.2% of instances overall, with per-language rates ranging from 13.0% on Go to 48.3% on C++. CodeGuardian cannot participate in that benchmark until its pipeline reaches at least one language beyond Python.

**What this means in practice.** A user hands CodeGuardian a Java repository. The kernel parses it. The blast engine reports the callers. The analyst produces a spec. The tester cannot produce a characterization test because there is no JUnit adapter. The writer cannot splice a candidate because there is no byte-range rewriter for Java. The pipeline stops at the tester with a refusal, which is honest but not useful.

The wall is not a defect in the architecture. It is the architecture doing what it was designed to do: refusing to run a phase it cannot complete correctly. The fix is to add the four adapters per language behind existing plugin interfaces, not to change the orchestrator.

### 3.4 Wall four — no fallback harness

Every long-running system needs a degraded mode. When the primary model provider is unavailable, when the sandbox cannot spawn, when the LSP server crashes, the harness should continue in a reduced form and report what it lost, not halt.

The 2026 Harness Resilience paper names six properties a resilient harness must have: portability of context, tool abstraction, reproducible workflows, fallback harnesses, audit trails, and human recovery procedures. Phase 13 has two of the six (tool abstraction via the capability plane, audit trails via the hash chain) and partial coverage of a third (reproducibility via checkpoints, though the workflow is not deterministic in the model-call sense).

The missing property that matters most is the fallback harness. When every model provider is unavailable, Phase 13's run fails. There is no path where the run completes with a reduced result. The deep test already demonstrates the problem at a smaller scale: when `scrapy`'s import fails, the pipeline cannot run, and the deep test reports `[SKIP]` because the harness cannot continue.

**What this means in practice.** A user runs CodeGuardian with a Groq API key. Midway through the writer phase, Groq rate-limits the key and the secondary key, and the tertiary key. The `FallbackModelProvider` tries each in turn, all rate-limited. The run stops. The checkpoint preserves what was completed. The user has to wait for the rate limit to reset and resume.

A resilient harness would have a third fallback: a deterministic writer that emits template-based candidates, checks them with the deterministic gate, and applies the ones that pass. The result is weaker than a frontier model's output, but it is better than stopping. The run completes with `status="partial"` and a `degradations=[("writer", "model_unavailable")]` field that reports what happened.

### 3.5 Wall five — no benchmark number

CodeGuardian has no published benchmark score. The deep test proves the pipeline runs. It does not prove the pipeline solves problems at a rate comparable to the field.

This matters more than it sounds. Phase 13's architectural claims — blast radius before the model call, three-layer verification, capability attenuation, evidence split — are correct as design decisions, and each is defensible on the 2026 literature. But a claim without a number is a hypothesis. The 2026 field has coalesced around two benchmarks: SWE-Bench ProMax for multilingual refactoring and TUA-Bench for terminal-agent behavior. Claude Code, Codex CLI, OpenHands, and the other production harnesses all report on one or both.

**What this means in practice.** A user comparing CodeGuardian to Aider or Cline has no data to make the comparison. The architectural advantages are real, but the user cannot weigh them against a 10-point difference in resolve rate. The measurement is not a vanity metric. It is the input to the user's decision about whether to use the tool.

The wall lifts with an SWE-Bench ProMax adapter on the existing deep test infrastructure. The benchmark's Docker containers already have dependencies installed, so the install-layer fragility that plagues the deep test on arbitrary repos does not apply. The pipeline runs against a controlled environment, and the result is comparable to the field.

### 3.6 Wall six — the cold tier is thin

Phase 13 split evidence into two tiers. The hot tier is the typed event stream the loop consumes synchronously to decide whether to retry, escalate, abort, or roll back. The cold tier is the SQLite-backed record: `agent_calls` rows, `decision_log` entries, `evidence_receipt` records.

The split is correct. The cold tier is underused.

Every `decision_log` entry carries a self-declared prediction: "this candidate will pass the verifier," "this target is safe to refactor." The harness writes those predictions. It does not verify them. The 2026 AHE paper demonstrates what happens when the harness verifies its own predictions: ten iterations of decision-observability-driven evolution lift Terminal-Bench 2 pass@1 from 69.7% to 77.0%, surpassing a human-designed reference harness at 71.9%. The gains localize to tools, middleware, and long-term memory, not to the system prompt. Factual harness structure transfers; prose-level strategy does not.

Phase 13 has the input to that loop. It does not have the loop.

**What this means in practice.** A user runs CodeGuardian a hundred times. Ninety times, the analyst's decision that "this target is refactorable" is correct. Ten times it is not. The `decision_log` records all hundred predictions and all hundred outcomes. No code reads them together. The pattern — which kinds of targets the analyst is wrong about — is in the database and unreachable.

The wall lifts with a decision-observability loop that reads the previous N runs' predictions and outcomes and reports which patterns consistently predict success. It is not machine learning. It is counting.

### 3.7 Wall seven — test-harness fragility (not a product wall)

The `scrapy` deep-test run fails on install-layer issues: the Twisted/Zope import incompatibility and the block-band target that exceeds the model's context window. Neither is a product problem. Neither is a harness problem. Both are the deep test trying to clone-and-install somebody else's project into a sandbox that was never designed to install arbitrary Python dependencies at this scale.

A production user does not clone and install their own project. They have a working venv with the correct dependency versions and a test command that runs. CodeGuardian analyzes the code; it does not set up the environment.

The wall matters because it obscures the other six. A reader looking at the `scrapy` run sees failures and assumes the harness is broken. It is not. The harness ran the pipeline on 20 of 20 phases across the proceed and review bands; the failures are in the setup, not the pipeline.

The fix is not to make the install layer general. The fix is to make it honest: state clearly in the deep test's documentation that the setup layer is per-repo configuration, not a general installer, and that a production user's environment is already working. The general fix — reading the repo's `.github/workflows/*.yml` and using its install steps — belongs to a future phase.

---

## 4. The seven work streams

### 4.1 LSP integration

The semantic layer is the first wall to lift because it changes what the writer can propose. Structural editing produces candidates that pass the verifiers on the target function and fail in production because a caller in another module still uses the old signature. Semantic editing produces candidates that are correct by construction.

The `agent-lsp` reference architecture (2026) exposes LSP behind a single JSON-RPC interface, with per-language servers spawned as warm sidecars. The key design decision is the warm runtime: a single `agent-lsp`-style process routes `.ts` to `tsserver`, `.go` to `gopls`, `.rs` to `rust-analyzer`. The server stays indexed across sessions. First session indexes the workspace; subsequent sessions are instant. This eliminates the cold-start penalty that makes sequential LSP calls too slow for an agent loop.

The second design decision is speculative editing. `simulate_edit` previews changes in memory, computes the diagnostic delta, and reports whether the edit is safe. The writer proposes a candidate; the LSP simulates it; the diagnostics confirm or reject; only confirmed candidates reach the sandbox. Fewer sandbox cycles, faster verification.

The third design decision is semantic rename. When the writer proposes a signature change, the LSP `rename` operation propagates it through every reference, barrel file, and re-export. The result is structurally correct: the definition, the internal references, the external references, and the re-export all move together or none of them do.

**Components to build.**

A `LanguageServerClient` capability plugin on the capability plane. The client exposes five operations: `prepare_rename(path, line, column)`, `rename(path, line, column, new_name)`, `find_references(path, line, column)`, `diagnostics(path)`, and `simulate_edit(path, edit)`. Each operation is a JSON-RPC call to the language server, wrapped in the existing `FaultBarrier` for timeouts and exception isolation.

A warm runtime manager that owns one sidecar per language, keeps them indexed, and routes incoming requests by file extension. The manager is constructed once per run and torn down at the end of the run. First run pays the indexing cost; subsequent runs reuse the indexed workspace.

A `SemanticWriter` agent that wraps the existing `WriterAgent` and calls `simulate_edit` before proposing a candidate. If the simulation shows new diagnostics, the candidate is rejected before the sandbox. If the simulation shows no new diagnostics, the candidate is proposed as before.

**Acceptance criterion.** A TypeScript rename that touches the definition, three internal references, and a barrel re-export completes without any of the six failing. A Go rename that touches a method, its interface implementation, and two callers in other packages completes without any of the four failing.

**Dependencies.** None on other work streams. The LSP client is a new capability; the writer's existing prompt does not change.

**Estimated time.** Four to six weeks for the client, the warm runtime, and TypeScript plus Go adapters. Java and Rust add two to three weeks each. C/C++ adds four to six weeks and depends on the kernel gaps G-15 and G-16 closing first.

### 4.2 Dynamic orchestration

The second wall to lift is the routing layer's coarseness. Phase 13's router selects a model by role and blast band. A step-level router reads the current attempt's evidence — the previous verifier's failure, the current `BudgetEnvelope` pressure, the schema violation from two rounds back — and selects a model for that specific attempt.

Harness-Native Agentic Routing (2026) makes this the routing primitive. The router's inputs are the phase, the evidence bus state, and the budget. Its outputs are a chosen model and an optional ensemble. The router never escalates on the model's self-reported confidence; it escalates only on objective signals — a verifier returned fail, a schema validator rejected the output, a pre-scan flagged a contract violation.

The second refinement is delegated model selection. When the orchestrator dispatches to a subagent, it chooses the model for that child session, not just the tier. The resolved model is stored on the child session so the transcript and the execution agree about what actually ran. This is already partially true in Phase 13 via `ContextBudget`, which is constructed per run from a `CapabilityProfile`. The refinement is to make the per-dispatch selection explicit in the router's API and in the `decision_log`.

The third refinement is model-free continuations. Routine automation — scheduled checks, webhook normalization, mechanical updates — runs without model latency or token cost. A script delegates only the exceptional or interpretive part to a model. For the harness, this means the discovery and ranking phases can run with zero model calls, and the analyst, tester, writer, and verifiers delegate only the parts that need reasoning. This is already true in Phase 13's design; the refinement is to make it visible in the telemetry and the routing decisions.

**Components to build.**

A `StepRouter` class that replaces the current `_build_plan` in `FallbackModelProvider`. The new router accepts the phase name, the `EvidenceBus` state, the `BudgetEnvelope`, and the previous attempt's failure. It returns a single model or an ensemble.

An ensemble mode in `FallbackModelProvider.complete` that runs two or three models in parallel and selects the candidate the verifier accepts. The ensemble mode is opt-in via a keyword-only parameter on `complete`.

A `decision_log` entry for every routing choice: `{"step": "routing", "phase": ..., "chosen_model": ..., "reason": ...}`. The entry is cold-tier, written through `record_call`.

**Acceptance criterion.** A run with a `BudgetEnvelope` of $0.50 completes without overspend, routing each phase to the cheapest model that satisfies its capability contract, and escalating only on objective signals. A run that hits a verifier failure on the writer's first attempt escalates to the frontier tier for the retry, and the escalation is recorded in the `decision_log` with the specific signal that triggered it.

**Dependencies.** None on other work streams. The router's API can change without changing the loop, because the loop calls `complete` the same way in both versions.

**Estimated time.** Three to four weeks for the step-level router, the ensemble mode, and the decision-log entries. The routing primitives already exist; the work is in composing them differently.

### 4.3 Multi-language pipeline

The third wall is the largest in scope. The kernel handles eight languages; the pipeline handles one. The fix is four adapters per language, behind existing plugin interfaces, with no orchestrator change.

The four adapters are the LSP client (already covered in 4.1), the test framework adapter, the rewriter, and the fault injector. The LSP client is shared with the semantic layer work; the other three are per-language.

The test framework adapter's job is to write a characterization test that locks the current behavior of the target function. That is language-specific. Python uses pytest. TypeScript uses Jest or Vitest. Java uses JUnit 5. Go uses `go test`. Rust uses `cargo test`. C and C++ use GoogleTest or Catch2. The adapter implements three methods: `generate_test(source, symbol)`, `test_file_path(target)`, and `run(sandbox, test_path)`. The tester agent's prompt stays the same; only the adapter changes.

The rewriter's job is to replace a byte range in the source with new text, re-parse with Tree-sitter, and reject the edit if the result does not parse. `ast-grep` covers JavaScript, TypeScript, Python, Ruby, Java, C#, and Rust. The `Splice` crate covers seven languages with byte-accurate, AST-validated refactoring operations. For languages neither covers, a small Python-side rewriter built on the kernel's Tree-sitter ranges is the fallback.

The fault injector's job is to wrap every return expression with a call to `_cg_inject(value)`, re-parse the source, and reject the injection if the result does not parse. The mutation rules differ per language: an int becomes int + 1, a string gains a suffix, a bool is inverted, a container gains an extra element. These rules are the same in every language; only the AST walk that finds the return expressions is language-specific.

**Language order.** TypeScript first, because `ast-grep` covers rewriting and `tsserver` is stable. Go second, for the same reasons with `gopls`. Java third, because `jdt.ls` needs workspace configuration and JUnit has a heavier setup. Rust fourth, because `rust-analyzer`'s indexing is slow but its rename is precise. C/C++ last, because the kernel gaps G-15 and G-16 have to close before the pipeline can support them.

**Components to build per language.**

A `TestFrameworkAdapter` plugin with the three methods above. One file per language, roughly 150–250 lines.

A `Rewriter` plugin with a single `splice(file_source, symbol_range, candidate_source) -> str | None` method. One file per language, roughly 80–150 lines, unless `ast-grep` covers the language in which case the plugin is a thin wrapper.

A `FaultInjector` plugin with a single `wrap_returns(file_source, function_name) -> str | None` method. One file per language, roughly 100–200 lines.

The orchestrator, the registry, the loop, and the verifier stack are unchanged. The new agents are registered with the same `AgentSpec` mechanism Phase 13 introduced; the capability tags include the language name so a Java writer fills the writer slot only on a Java target.

**Acceptance criterion.** A refactor on TypeScript, Go, Java, and Rust passes the same verifier stack that Python passes today, with the language-appropriate characterization test, the language-appropriate rewriter, and the language-appropriate fault injector. The deep test gains a per-language section for each new language, with a per-language pass count.

**Dependencies.** The LSP client from 4.1 for all five languages. The kernel gaps G-15 and G-16 for C/C++ only.

**Estimated time.** Two to three weeks per language for the first three (TypeScript, Go, Java), three to four weeks for Rust, four to six weeks for C/C++ once the kernel gaps close. Total: fifteen to nineteen weeks.

### 4.4 Production resilience

The fourth wall is the absence of a fallback harness. A resilient harness continues in a reduced form when the primary path fails, and reports what it lost. Phase 13's harness does not.

The 2026 Harness Resilience paper's six properties are the reference. Phase 13 has tool abstraction and audit trails. It does not have a fallback harness, portability of context across providers, or a human recovery procedure.

The fallback harness is the highest-leverage property. When every model provider is unavailable, the run should not stop. The analyst should return a template-based spec, the tester should return the tool's characterization test, the writer should return template-based candidates, and the verifiers should rely on deterministic pre-scan only. The result is weaker than a frontier model's output, but it completes. The run's status is `"partial"` and the degradation is recorded.

Portability of context means a run paused on Provider A can resume on Provider B without re-prompting for the same context. The checkpoint's `state_json` is already provider-agnostic; the refinement is a `resume_from_provider` field that the resume path honors.

The human recovery procedure is a `recovery.md` written into the run's artifact directory on any failure. The file names what failed, what the checkpoint contains, what the user can do — resume, discard, retry with a larger model, narrow the target — in plain language.

**Components to build.**

A `DeterministicFallback` agent for each of the three model-backed pipeline agents (analyst, tester, writer). Each fallback is a small class that implements the same input and output types as its model-backed counterpart and returns a template-based result. The analyst's fallback returns a spec with `change_kind: apply_directive` and no detailed change types. The tester's fallback returns the tool's characterization test verbatim. The writer's fallback returns the target's existing source unchanged, which the deterministic gate rejects, so the run stops at the writer with a clear refusal.

A `resume_from_provider` field on the `Checkpoint` dataclass. The `CheckpointSupervisor.resume` method checks the field and passes it to `tracked_model_call` as an override.

A `recovery.md` writer that runs in the `LangGraphOrchestrator._finalize_run` method on any non-success status.

**Acceptance criterion.** A run survives a mid-phase `MODEL_UNAVAILABLE` on all providers, continues with the deterministic fallbacks, and completes with `status="partial"` and a `degradations` list that names which phases ran in fallback mode. The `recovery.md` file exists in the artifact directory and names the failure.

**Dependencies.** None on other work streams. The fallback agents use the same `AgentSpec` mechanism as the model-backed agents.

**Estimated time.** Three to four weeks for the three fallback agents, the checkpoint field, and the recovery writer.

### 4.5 Benchmarking

The fifth wall is the absence of a benchmark number. The 2026 field reports on SWE-Bench ProMax for multilingual refactoring and TUA-Bench for terminal-agent behavior. CodeGuardian reports on neither.

SWE-Bench ProMax (2026) evaluates 170 instances across Python, Java, TypeScript, Go, C, C++, and Rust. Average 11.5 source files and 261.6 lines per refactor. Each instance ships in a Docker container with dependencies installed, so the setup layer that plagues the deep test on arbitrary repos does not apply. The benchmark harness checks out the pre-refactor commit, runs the pipeline, applies the diff, and runs the test suite.

The adapter reuses the deep test's infrastructure. The `_pipeline.py` module already runs the full pipeline against a cloned repo. The adapter replaces the per-repo `RepoSpec` discovery with an SWE-Bench instance loader, replaces the install commands with "the container already has them," and replaces the report with a per-language resolve rate.

The ablation is what proves which Phase 13 components contribute. Run with and without the verifier stack. Run with and without blast-aware routing. Run with and without the LSP semantic layer (once 4.1 lands). The ablation table is what turns the architectural claims into measured properties.

**Components to build.**

An `SWEBenchProMaxAdapter` class in `sandtest/benchmarks/`. The adapter takes an instance ID, loads the Docker container, clones the pre-refactor commit, runs the pipeline, applies the diff, runs the test suite, and reports pass or fail.

A benchmark report generator that aggregates results across the 170 instances and produces a per-language resolve rate and a cost per instance.

An ablation driver that runs the benchmark four times with the four toggles in different configurations and reports the deltas.

**Acceptance criterion.** A published per-language resolve rate and cost per instance on SWE-Bench ProMax, with an ablation table. The numbers are comparable to GPT-5.2's baseline (41.2% overall, per-language rates ranging from 13.0% on Go to 48.3% on C++), adjusted for the fact that CodeGuardian is a harness running on a chosen model rather than a raw model.

**Dependencies.** The multi-language pipeline from 4.3 for the non-Python languages. The LSP semantic layer from 4.1 for the ablation.

**Estimated time.** Two to three weeks for the adapter, the report generator, and the ablation driver, once the pipeline reaches the languages the benchmark covers.

### 4.6 Evidence and observability

The sixth wall is the thinness of the cold tier. Phase 13 writes `agent_calls` rows, `decision_log` entries, and `evidence_receipt` records. It does not read them back. The decision-observability loop that the AHE paper demonstrates is not present.

The AHE paper's result is the reason this matters. Ten iterations of decision-observability-driven evolution lift Terminal-Bench 2 pass@1 from 69.7% to 77.0%, surpassing a human-designed reference harness at 71.9%. The gains localize to tools, middleware, and long-term memory, not to the system prompt. Phase 13 has the input to this loop: every decision_log entry carries a self-declared prediction. It does not have the loop.

The second refinement is OpenTelemetry integration. The 2026 OpenTelemetry GenAI semantic conventions define standard attribute names for LLM spans: `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`. A `MetricsCollector` implementation that emits these spans is a third implementation of the existing protocol, with no change to the collector's interface. The spans correlate to the `agent_calls` rows via the `run_id` and `span_id` the `TraceContext` already carries.

**Components to build.**

An `OTelMetricsCollector` class that implements the existing `MetricsCollector` protocol and emits OTel spans. A soft import of the `opentelemetry` package; the collector degrades to no-op when the package is not installed.

A `DecisionObservabilityReport` generator that reads the previous N runs' `decision_log` entries and their outcomes, groups them by the pattern the decision contained, and reports which patterns predicted success and which did not.

A `LangSmithCorrelation` field on the `agent_calls` row that links the row to the LangSmith trace ID the `LangSmithAdapter` recorded.

**Acceptance criterion.** A run produces an OTel trace that correlates every model call, tool call, and phase transition, queryable by `run_id`. The decision-observability report names the top three patterns that predict success and the top three that predict failure.

**Dependencies.** None on other work streams. The OTel collector is a new implementation of the existing protocol; the decision-observability report reads the existing tables.

**Estimated time.** Two to three weeks for the OTel collector and the decision-observability report.

### 4.7 The phasing

| Window | Work | Time | Depends on |
|---|---|---|---|
| v2.0 | LSP client. Warm runtime. TypeScript and Go adapters. Semantic rename for the writer. | 4–6 weeks | None |
| v2.1 | Java and Rust adapters. Test framework adapters. Rewriter adapters. Fault injector adapters. | 4–6 weeks | v2.0 |
| v2.2 | C/C++ adapters. Kernel gaps G-15 and G-16 closed. | 4–6 weeks | v2.1 |
| v2.3 | Step-level router. Ensemble mode. Fallback harness. | 3–4 weeks | v2.0 |
| v2.4 | SWE-Bench ProMax adapter. Ablation driver. Published numbers. | 2–3 weeks | v2.1 |
| v2.5 | OTel collector. Decision-observability report. | 2–3 weeks | v2.3 |

The two long tails are the multi-language pipeline (v2.0 through v2.2, fifteen to nineteen weeks) and the routing and resilience work (v2.3, three to four weeks). The two shorter tails are benchmarking and observability, both of which depend on the pipeline and the router being in place.

The one ordering constraint worth naming: the LSP client must precede the TypeScript and Go adapters, and it should precede the Java and Rust adapters. The LSP client is the shared foundation for all five languages; building it once is the reason the multi-language work is feasible.

---

## 5. Architectural invariants

Five invariants must not break while the seven walls come down. They are the reason Phase 13's architecture is worth extending; breaking any of them turns the harness into a different harness.

**Invariant one — the loop does not branch on mode, agent, or language.** The loop reads a `PhaseGraph`, resolves each slot against the registry, invokes the agent, runs the gate, writes the checkpoint, advances. It never learns an agent's name, a language's name, or a mode's name. A Java writer is an agent that declares `{llm, coding, code-producer, java}` and fills the writer slot. The loop dispatches it exactly as it dispatches the Python writer. The semantic layer, the test adapter, the rewriter, and the fault injector are all capability-plane plugins. The loop does not know they exist.

**Invariant two — the model never chooses a transition.** The model's output is evidence. It informs the engine's decision. It is never the decision. A step-level router reads the evidence to choose a model, not to choose a phase. The phase transitions are still determined by the graph. A verifier's verdict still determines whether the verifier stack retries, not which phase runs next. The line between "the model produces evidence" and "the engine decides transitions" is what makes the harness deterministic.

**Invariant three — the evidence split survives.** The hot tier is the typed event stream the loop consumes synchronously. The cold tier is the SQLite record. A future phase that adds a benchmark harness, a decision-observability report, or an OTel collector reads from cold, never hot. The hot path is synchronous and must stay small. The cold path is asynchronous and can grow.

**Invariant four — the seed lives in the loop.** `loop._seed_registered_agents` reads from the resolved graph and writes to `registered_agents`. A future migration must not add a hardcoded seed. Adding a new agent to the graph adds it to `registered_agents` on the next run without a migration. Adding a new language to the pipeline adds it to `registered_agents` the same way.

**Invariant five — the capability plane is where new capabilities live.** The LSP client, the test framework adapter, the rewriter, and the fault injector are all plugins. They are discovered through the existing `PluginRegistry`, wrapped in the existing `FaultBarrier`, audited by the existing `sys.addaudithook`. Adding a new language is a new set of plugin files, not a change to the orchestrator. This is the Structural Composability property from `Phase_13.md` §2, and it is the reason the multi-language work is feasible at all.

---

## 6. Acceptance criteria

The v2 roadmap is complete when each work stream's acceptance criterion is satisfied and the full suite is green. The seven criteria are:

1. **LSP integration.** A TypeScript rename and a Go rename both complete without any of the reference sites failing. The LSP client satisfies the `FaultBarrier` timeout contract and the audit hook logs every LSP call.
2. **Dynamic orchestration.** A run with a `BudgetEnvelope` of $0.50 completes without overspend. A verifier failure on the writer's first attempt escalates to the frontier tier for the retry, and the escalation is recorded in the `decision_log`.
3. **Multi-language pipeline.** A refactor on TypeScript, Go, Java, and Rust passes the verifier stack with language-appropriate characterization tests, rewriters, and fault injectors. The deep test reports a per-language pass count.
4. **Production resilience.** A run survives a mid-phase `MODEL_UNAVAILABLE` on all providers, continues with the deterministic fallbacks, and completes with `status="partial"` and a `degradations` list. The `recovery.md` file exists in the artifact directory.
5. **Benchmarking.** A published per-language resolve rate and cost per instance on SWE-Bench ProMax, with an ablation table that shows the contribution of the verifier stack, blast-aware routing, and the LSP semantic layer.
6. **Evidence and observability.** A run produces an OTel trace that correlates every model call, tool call, and phase transition. The decision-observability report names the top three patterns that predict success and the top three that predict failure.
7. **All five invariants hold.** A new agent, a new language, and a new mode can be added without changing the loop, the registry, the evidence bus, or the checkpoint supervisor.

The acceptance criteria do not name a test count. The counts follow from the work. The criterion is that each wall is gone in a way that a reader can verify, not that a specific number of tests exists.

---

## 7. References

The sources cited in this document, in the order they appear. Every claim traces to one of them.

1. **NREKI** (2026). Tree-sitter for syntax, LSP for semantics, LSP code actions for auto-healing diagnostics.
2. **Ricecoder** (2026). LSP-first architecture with fallback through LSP to configured rules to tree-sitter to generic text.
3. **agent-lsp** (2026). 65 LSP tools across 30 CI-verified languages, single JSON-RPC interface.
4. **Thoughtworks Technology Radar 2026.** LSP-based code intelligence as the emerging standard for AI code tools.
5. **SWE-Bench ProMax** (2026). 170 instances across Python, Java, TypeScript, Go, C, C++, Rust. Average 11.5 source files, 261.6 lines per refactor. GPT-5.2 at 41.2% overall, per-language rates 13.0%–48.3%.
6. **Harness-Native Agentic Routing** (2026). Step-level model selection conditioned on current evidence, not role tier.
7. **Harness Resilience** (2026). Six properties: portability of context, tool abstraction, reproducible workflows, fallback harnesses, audit trails, human recovery procedures.
8. **AHE** (May 2026). Decision-observability-driven evolution. Ten iterations lift Terminal-Bench 2 pass@1 from 69.7% to 77.0%. Gains localize to tools, middleware, long-term memory.
9. **OpenTelemetry GenAI semantic conventions** (2026). `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`.
10. **Splice** crate (2026). Byte-accurate, AST-validated refactoring operations for seven languages.
11. **ast-grep** (2026). Structural search and rewriting for JavaScript, TypeScript, Python, Ruby, Java, C#, Rust.
12. **The source-code study of eleven production coding harnesses** (2026). No agent runtime imports a general-purpose agentic framework. All use hand-rolled async loops and deterministic retrieval.
13. **The verification study** (October 2026). Context management and verification gates are where harness gains concentrate.

The related internal documents, for a reader who wants the Phase 13 context:

- `docs/summaries/Phase_13.md` — the Phase 13 design settlement.
- `docs/summaries/Phase_12.md` Part II — the skill library and retrieval pipeline that the orchestrator consumes.
- `docs/summaries/Phase_11.md` — the nine agents and the verifier loop primitive.
- `docs/modularity-audit.md` §7 — the phase-graph requirement that Phase 13 satisfied.
- `docs/04-architecture.md` §6 — the orchestrator's place in the overall system.

---

## 8. One-paragraph summary

v2 adds the LSP semantic layer so the writer can propose edits that are correct by construction, makes routing step-level and capability-native so the router reads the current attempt's evidence instead of the role's tier, extends the pipeline to TypeScript, Go, Java, Rust, C, and C++ through four adapters per language behind existing plugin interfaces, hardens the harness against provider failure so a run survives a mid-phase model outage, publishes a per-language benchmark number on SWE-Bench ProMax with an ablation that shows what each Phase 13 component contributes, and thickens the cold tier with OpenTelemetry spans and a decision-observability loop. None of it changes the orchestrator loop, the registry, the evidence split, or the seed location. That is the point. The architecture is already the shape v2 needs; v2 fills it in.

*End of document.*
