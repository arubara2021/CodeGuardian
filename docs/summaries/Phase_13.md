# Phase 13 — Orchestrator: Design Settlement

**Snapshot date:** 2026-11-07
**Status:** Design settled. Implementation pending.
**Depends on:** Phase 11 (Agents, complete), Phase 12 v2 (Skills, complete)
**Deliverables:** 8 blocks, approximately 2,000–3,000 lines across code, tests, and documentation. No rewrites of working files except two explicitly approved Phase 11 correctness fixes.

---

## 1. What Phase 13 is

Phase 13 is the harness engine. Every prior phase built a component; this phase is the first whose acceptance criterion is that the components run as a coherent system. It wires the nine Phase 11 agents into a runnable pipeline, makes every Phase 12 v2 accessor fire at the right moment, enforces every phase gate, and turns the primitives that were tested in isolation — the verifier loop, the context budget, the routing layer, the evidence columns — into properties of a running system.

It is not a refactor of Phase 11. It is not a new agent. It is not a CLI (Phase 14), not an MCP server (Phase 15), and not a reporter (Phase 15). It is the layer that decides the order in which the pieces run, what each piece receives, and what happens when a piece fails.

The phase is large enough that it needs a settlement before any code exists, in the same way Phase 9 and Phase 12 v2 needed one. That settlement is this document. No code is written until this document is accepted. When code begins, the block order in §11 is followed exactly, with the same boundary-testing discipline used in every prior phase.

---

## 2. The two structural challenges Phase 13 must solve

The 2026 harness literature has converged on a precise vocabulary for what an orchestrator must do, and the vocabulary is worth adopting directly because it separates two problems that are easy to conflate. The openJiuwen paper (Huawei, November 2026) names them **Structural Composability** and **Runtime Adaptivity**, and argues that every modern harness must solve both.

**Structural Composability** is the ability to compose capabilities, reconfigure execution logic, and scale increasingly complex agent systems without repeatedly rebuilding orchestration. When Phase 14 or a future phase adds an agent, the orchestrator must not change. When a future phase adds a mode — greenfield, upgrade, or a test-only pipeline — the orchestrator must not change. When a capability that several agents share is extracted into a plugin, the orchestrator must not change. If any of those three events requires an edit to the orchestrator loop, the phase has failed the composability requirement.

**Runtime Adaptivity** is the ability to let evolving evidence — semantic diagnostics, execution outcomes, task progress, changing context relevance — dynamically influence later runtime decisions. When a verifier fails, the evidence should trigger a retry rather than a crash. When a test run returns a specific error, the evidence should shape the next prompt rather than being discarded. When the context budget fills past a threshold, the evidence should trigger compaction rather than a silent degradation. The orchestrator must consume evidence and change its behaviour accordingly, but — and this is the critical distinction — the *model* must never choose workflow transitions. The engine decides transitions; the evidence informs the engine.

These two challenges map one-to-one onto the user requirements that drove this phase. "Adding new agents must give me space" is Structural Composability. "Greenfield must work perfectly after v1" is the same problem, because greenfield is not a new code path — it is a different composition of the same parts. The full-test-agent case, where a future agent writes the project's complete test suite, is the same problem again: it registers on the agent plane, fills a slot, and the loop never learns its name.

---

## 3. Why prompted orchestration is rejected

Before describing what Phase 13 does, it is worth recording what it explicitly refuses to do, because the refusal is load-bearing.

The GraphBit paper (March 2026) studies agent frameworks where the model itself determines workflow transitions. It finds that these frameworks "often suffer from hallucinated routing, infinite loops, and non-reproducible execution." Its alternative is an engine-orchestrated framework that defines workflows explicitly and deterministically as a directed acyclic graph, with agents operating as typed functions and a Rust engine governing routing, state transitions, and tool invocation. On GAIA it reaches 67.6% with zero framework-induced hallucinations and 11.9 ms of engine overhead.

Phase 13 adopts the engine-orchestrated position without qualification. The model's output is evidence. It is never a transition. When the Analyst produces a RefactorSpec, the spec tells the orchestrator what the Writer should receive, not which phase comes next. When a verifier returns a verdict, the verdict tells the VerifierLoop whether to retry, not which agent to invoke. The phase graph is data; the engine walks it; the model never sees a routing decision.

This is the same principle that `docs/modularity-audit.md` §7 already mandated before Phase 13 began: the orchestrator must interpret a `PhaseGraph` object rather than a hardcoded sequence. The 2026 literature confirms the choice.

---

## 4. The three extension planes

The architecture that solves Structural Composability has been arrived at independently by several 2026 systems, and the consensus shape is three extension planes that are orthogonal to each other. Phase 13 adopts that shape.

### 4.1 The capability plane

The capability plane holds the shared infrastructure that every agent can use: tools, context providers, hooks, and commands. HarnessDesk's design expresses the intent cleanly: "a plugin registered once is usable across every agent, and an agent added once can use every plugin." Tools and context providers are not per-agent. They are shared.

In CodeGuardian, the capability plane is where `ast_grep`, `code_analysis`, `code_skeleton`, `function_body`, `call_graph_slice`, and `list_symbols` live. It is also where the sandbox executor adapter lives, once Phase 13 block 7 lands. A new capability registered on this plane becomes available to every agent that declares it in its capability requirements; no agent changes, and the orchestrator never learns the capability's name.

The capability plane is the plane whose plugins are *reversibly registered*. DeepSeek Harness's "everything is a plugin" architecture makes this point most clearly: model adapters, the tool registry, the session log, and even the agent loop itself are replaceable plugins, organized on a Cordis meta-framework whose defining property is spatiotemporal composability. The point of reversible registration is that a plugin that declares a dependency on `[tools, http]` stays pending until those dependencies exist and unloads if one disappears; the system does not have to be restarted to swap a capability.

Phase 13 does not need hot-swap in v1, so capability-plane registrations are reversible but not hot-swapped at runtime. They are registered once at startup and unregistered at shutdown. The reversible *shape* is preserved because it costs nothing to preserve and because a future phase that does want hot-swap will not have to re-architect the plane.

### 4.2 The agent plane

The agent plane holds the executable agents. Raven's Heterogeneous Agent Registry (September 2026) is the reference design. An available agent is identified by a registered name and a capability description. Its registry entry binds that name to one of four execution backends — the in-process Raven loop, a command-line process, an Agent Client Protocol connection, or an OpenAI-compatible model API — and records three capability tags that constrain planning: *stateful* (can continue an earlier conversation), *file-capable* (can read local paths), and *progress-reporting* (streams its intermediate activity). The Host Agent receives the roster with these tags and selects among the entries using the current context, "without a separately trained routing classifier or assumed oracle capability scores."

Phase 13 adopts a stronger form of this. Where Raven uses three boolean tags, Phase 13 uses **typed capability contracts**. Each agent registry entry declares four things:

1. A name.
2. A set of capability tags (the boolean constraints Raven uses).
3. An **input schema** describing the typed data the agent accepts.
4. An **output schema** describing the typed data the agent produces.
5. An execution backend.

Slot resolution then checks structural schema compatibility first and capability tags second. This is the Skillware contribution — separating *what an agent wants to do* from *how the task is executed*, with a typed contract that a deterministic resolver can check. It means a new agent that produces a new artifact type does not need the slot to be renamed; it needs the slot's schema to say it accepts that type. It also means an agent cannot accidentally satisfy a slot by having a similar name; it must satisfy the slot's contract.

Agent-plane registrations are **immutable**. An agent carries session state, and immutable registration is what makes the state model predictable. A future phase that wants hot-swap of agents can introduce a session-draining protocol; Phase 13 does not.

### 4.3 The phase graph plane

The phase graph plane is where the mode lives. A `PhaseGraph` is a data object describing an ordered set of slots, the transitions between them, and the entry point. `legacy.py` defines one; a future `greenfield.py` defines another; a future `upgrade.py` defines a third. The orchestrator reads a `PhaseGraph` and walks it. It has no `if mode == "greenfield"` branch anywhere, and adding a fourth mode is a new file, not a fork.

The 2026 field has converged on this composition approach wherever multi-mode pipelines exist. Squirrel auto-detects project state and adapts its eight-phase pipeline accordingly; Vibe-loop runs greenfield and brownfield pipelines over one loop. The failure mode this avoids is well-documented: static role assignment forces the orchestrator to branch on mode, and every new mode grows the branch. `modularity-audit.md` §7 recorded the requirement before Phase 13 began: if the orchestrator is hardcoded as an eight-phase pipeline, v2 will require a full pipeline refactor; if it interprets a `PhaseGraph`, v2 adds new graph definitions only.

Phase 13 ships one graph — `legacy` — and the graph data model. A future phase adds graphs, not code.

### 4.4 Why the three planes are orthogonal

The three planes are orthogonal in the precise sense that a change on one plane never requires a change on another. Adding an agent (agent plane) uses capabilities that already exist on the capability plane and fills a slot that already exists in the phase graph plane. Adding a capability (capability plane) becomes available to every agent that declares it, and does not require the phase graph to change. Adding a mode (phase graph plane) reuses agents and capabilities that already exist and does not require new registrations.

This orthogonality is what makes the "adding a new agent does not require rebuilding the orchestrator" requirement true. The full-test-agent that a future phase might add registers `{name: project_test_writer, capability_tags: [file-capable, progress-reporting], input_schema: RefactorSpec, output_schema: TestSuite, backend: in-process}`. A `legacy` graph gets an optional `project_test_suite` slot wired to run only when the directive asks for it. A future `greenfield` graph makes that slot required. The orchestrator loop sees a slot name, resolves it against the registry by schema, and dispatches. It never learns the agent's class name.

---

## 5. Slot resolution

Slot resolution is the single function that decides which agent fills which slot. It is the piece that makes the registry meaningful. Phase 13 implements it as a deterministic function with three checks, in order.

The first check is **capability tag satisfaction**. The slot declares required tags (for example, `[file-capable]` for a slot that must read the clone). The agent's registry entry declares the tags it provides. Every required tag must be present. This is the boolean check Raven uses.

The second check is **input schema compatibility**. The slot declares an input schema (for example, `RefactorSpec`). The agent's registry entry declares the input schema it accepts. The resolver checks that the agent's schema can accept what the slot produces. This is a structural check, not a name check. A slot that produces a `RefactorSpec` can be filled by any agent whose input schema is `RefactorSpec` or a structural supertype of it.

The third check is **output schema compatibility**. The slot declares an output schema (for example, `Diff`). The agent's output schema must be structurally compatible with what the slot produces.

If all three checks pass, the slot is resolved. If more than one agent satisfies the slot, the resolver chooses deterministically by a priority field on the registry entry (Phase 13 uses a small integer; higher wins, ties broken by registration order). If zero agents satisfy the slot, the orchestrator raises a typed error at graph-start time, not mid-run. This is the fail-fast property that makes a missing agent a configuration problem rather than a runtime surprise.

The resolver never looks at agent class names. It looks at schemas and tags. This is what makes the "adding a new agent does not require rebuilding the orchestrator" requirement true.

---

## 6. Runtime Adaptivity: the two-tier evidence bus

The second structural challenge — letting evolving evidence influence later decisions without letting the model choose transitions — has a specific solution in Phase 13, and the solution is to split the evidence into two tiers with different consumers.

### 6.1 The hot tier

The hot tier is a typed event stream that the engine consumes **synchronously** to make immediate decisions: retry, escalate, abort, rollback. It is small, it is typed, and it is consumed in the orchestrator's decision loop. Examples of hot-tier events:

- `VerifierFailed(verifier, verdict, reason)` — the VerifierLoop receives this and decides whether to retry.
- `TestRunFailed(exit_code, stderr_tail)` — the pipeline receives this and passes it to the next Writer invocation as `previous_attempt` feedback.
- `ContextBudgetExceeded(fill_percent, snapshot)` — the ContextBudget receives this and refuses further calls or triggers compaction.
- `ToolCallFailed(tool, code, reason)` — the orchestrator logs this and decides whether to fall back to a different tool.
- `BlastRecommendationAdjusted(band, reason)` — the orchestrator receives this and decides whether the Writer needs human approval.

The hot tier is what makes the phase "adaptive" in the openJiuwen sense. The evidence is what the engine reads to decide the next transition. It is not what the model reads; the model receives the context that the engine assembles in response to the evidence, not the raw events.

### 6.2 The cold tier

The cold tier is the audit and telemetry layer: the `agent_calls` table, the `decision_log` column, the `model_reasoning` column, the `evidence_receipt` column. It is queried **asynchronously**, after the run or by the observability loop. It is not on the hot path.

The separation matters because it stops two failure modes at once. If the engine reads the audit table on the hot path, the audit schema becomes a hot-path dependency and every schema change risks breaking the engine. If the engine does not read the audit table at all, the observability loop loses the ability to correlate decisions with outcomes. The two-tier split gives both.

The cold tier is also where the harness-evolution loop lives. The AHE paper (May 2026) demonstrates the value of this: ten iterations of decision-observability-driven evolution lift Terminal-Bench 2 pass@1 from 69.7% to 77.0%, surpassing the human-designed Codex-CLI at 71.9%. The frozen evolved harness transfers to SWE-bench Verified at 12% fewer tokens than the seed, and delivers +5.1 to +10.1 percentage points cross-family across three alternate model families. Ablations localize the gain to tools, middleware, and long-term memory rather than the system prompt — factual harness structure transfers, prose-level strategy does not.

Phase 13 does not implement the evolution loop; that is a future phase. It implements the cold tier in the form the loop needs: every run writes a decision log with a self-declared prediction ("this candidate will pass the verifier" or "this target is safe to refactor"), and the next run's outcome verifies that prediction. The decision-observability pillar is the prerequisite the future loop needs.

---

## 7. Context architecture

Phase 13 must solve the context inflation problem that long-horizon coding agents face, and the 2026 literature has a specific answer.

The Spine paper (October 2026) describes the failure mode precisely. Agents record interactions sequentially into a growing history, which causes context inflation and performance degradation. Trimming or summarising the history without explicit subtask boundaries discards information that unfinished subtasks need. Recursive subagent delegation isolates completed subtasks through child agents by assigning every subtask boundary to delegation, which conflates subtask granularity with executing-agent choice and adds execution and coordination overhead.

Spine's fix is an interface and runtime that aligns model context with subtask nesting and completion. An agent declares explicit subtask boundaries through **work scopes**, allowing the same agent to execute nested subtasks inline while reserving delegation for independent parallel tasks. The runtime organises execution in a **SpineTree**, a persistent tree recording subtask relations, local interactions, completion states, and returned results. Before each model call, it compiles the tree into model context just in time, **retaining unfinished subtasks' interactions and replacing completed subtasks' details with their results**, while preserving prefix-cache reuse.

The measured effect on SWE-Milestone with GPT-5.6 Sol is +11.8% score and −32.4% total cost versus native Codex. It reaches a comparable score to Recursive Subagent at **73.7% lower total cost**. It also improves average ProgramBench test-pass rate by 17.3%, and on the Pi host it delivers +9.5 to +17.5% SWE-Milestone improvement with Gemini 3.8 Flash and Qwen 3.8 27B.

Phase 13 adopts a light form of Spine. Each pipeline phase is a subtask node. The node records its input, its output, its completion state, and its interactions. Before each model call, the orchestrator compiles a **run context** from the tree: completed phases contribute a one-line summary and their typed output; the current phase contributes its full interaction history and its input; pending phases contribute nothing. The result is assembled just in time and passed to the model.

The full SpineTree with prefix-cache reuse is more than Phase 13 needs. Phase 13 compiles per phase, not per call. The prefix-cache optimisation is a future refinement. What Phase 13 does implement is the work-scope boundary — every phase has an explicit boundary, and the boundary is what makes compaction safe.

The three-tier memory architecture from GraphBit complements this. Phase 13's run memory has three tiers: ephemeral scratch (the current phase's working context, discarded when the phase completes), structured state (the checkpoint and the run tree, persisted to SQLite), and external connectors (the repository, the CPG, the skills library). GraphBit's ablation shows each tier contributes measurably to the result; Phase 13 implements all three, but the connectors remain thin — the orchestrator reads from them, it does not cache them.

---

## 8. The verification stack

The verification study from October 2026 compares 17 verification designs across seven frontier coding models on Terminal-Bench and SWE-bench Verified. Its findings are the most detailed guidance available, and Phase 13 adopts them almost directly.

The study evaluates four layers of the agent lifecycle: initial instructions, mid-turn reminders, completion gates, and independent review. The findings:

- **Initial instructions and mid-turn reminders** yield modest, model-dependent average gains on the coding benchmarks, without substantially increasing average output token use. These are cheap but unreliable.
- **Completion gates** can yield larger gains but may substantially increase cost. Use sparingly.
- **Independent review by a critic** offers a more favourable accuracy-cost tradeoff: +5.9 to +9.5 percentage points on Terminal-Bench and +3.9 to +6.6 on SWE-bench Verified at 1.13–1.25 times and 1.00–1.04 times baseline cost, respectively.
- **Additional review rounds or independent critics** further improve average Terminal-Bench accuracy by up to +3.5 percentage points.

Phase 13's verification stack reflects this. It has three layers.

### 8.1 The deterministic gate (zero tokens)

Before any LLM verifier runs, the diff passes through deterministic checks: `git diff --check` for whitespace errors, the language's type checker if available, the language's linter if available, and the characterization test run in the sandbox. Any failure bounces back to the Writer immediately at zero token cost. This is the RLP Desk principle applied: the machine checks what the machine can check, and only what survives reaches an LLM.

Phase 13 does not implement the full RLP Desk campaign-gate and execution-replay machinery. It implements the two deterministic checks that pay for themselves immediately — `git diff --check` and the characterization test. Type checker and linter integration are Phase 14 or 15 work, gated on per-language toolchain availability.

### 8.2 The deterministic pre-scan (zero tokens)

The security verifier and the contract verifier already implement deterministic pre-scans in Phase 11 — regex lists for known vulnerabilities, PDG-based detection for contract violations. Phase 13 runs these pre-scans on every diff before any LLM verifier is invoked, and it surfaces the pre-scan results in the hot tier so the VerifierLoop can use them as evidence.

The pre-scan's conclusion can also veto a model verdict, which Phase 11 already implements as `apply_veto`. Phase 13 wires that veto into the VerifierLoop's decision.

### 8.3 The LLM verifier loop

Only what survives the deterministic gate and the deterministic pre-scan reaches the LLM verifiers. The three verifiers run in parallel: correctness, security, and contract. Their verdicts are collected by the VerifierLoop, which is the primitive Phase 11 built and Phase 12 v2 tested but which nothing has yet called.

The VerifierLoop is the piece that makes the verification study's numbers real. The study reports the gain from *independent review followed by revision*, not from review alone. Phase 13's loop runs `generate → verify → revise` with a bounded K (default 2), passing the verifier's reason as `previous_attempt` feedback on the revise step, rolling back the diff between rounds so the Writer starts from the original file. The 2026 study reports a separate measurement that reinforces this: a repair loop that kept only the latest code and test results matched one keeping the full history while using **one-third the input tokens**. Phase 13's loop keeps only the latest, matching the token-efficient form.

The loop's K is not fixed at 2. It scales with blast band. A `proceed` target runs K=1 (one verification round, no retry unless it fails). A `review` target runs K=2. A `block` target runs K=2 and escalates to human approval on the second failure. This is verification depth as a function of blast score, which no other harness computes because no other harness computes blast score.

---

## 9. The routing layer

The Phase 11 routing primitives — `CapabilityProfile`, `BudgetEnvelope`, `TierRubric`, `ObjectiveEscalation`, `ModelGateway` — were tested in isolation. Phase 13 wires them into `fallback_router.py`, and the wiring is informed by the 2026 model-routing research.

The Ohio State thesis on tiered routing reports a result that reverses the standard cost-quality assumption. On PinchBench, a four-tier router with cheap-first default and three escalation paths scores **0.7618 at $0.3844** against an all-premium baseline of **0.7410 at $2.3717** — an **83.8% cost reduction with a 0.021 absolute score improvement**. The tier-1 model alone scores **0.8245**, which is higher than the all-premium baseline. Quality is non-monotonic in cost; cheap-first is the correct default, and escalation is a failure-recovery mechanism, not a quality cascade.

Phase 13 adopts this. The default routing is cheap-first: the routing layer selects the cheapest model that satisfies the role's `TierRubric` constraints (minimum tier, required capabilities). Escalation fires only on typed evidence — a `MODEL_RATE_LIMITED` or a verifier failure or a specific quality signal — not on the model's self-reported confidence. The `BudgetEnvelope` is consulted before every call; at 80% of the budget the tier drops one step, at 95% the orchestrator pauses and reports rather than overspending.

The routing layer also gains one CodeGuardian-specific extension that the field does not have: **blast-aware tier override**. A target with `recommendation_adjusted == "block"` gets the frontier-tier model for the Writer and three independent verifiers regardless of the user's cost setting. A `proceed` target gets the standard tier. The harness makes a safety-versus-cost tradeoff based on graph analysis, not on a static policy.

---

## 10. Capability attenuation

The 2026 security literature names capability attenuation as the largest unsolved white space in agentic systems. The OWASP Agentic Top 10 for 2026 lists over-privileged skills and identity/privilege abuse as core risks. The Monotonic Narrowing paper generalises capability attenuation into four invariants: delegation attenuation (authority narrows monotonically across delegation chains), governance attenuation (policy artifacts can only strengthen; weakening requires higher-order authorization), disclosure attenuation (audit views reveal only the minimum necessary subset), and exception attenuation (temporary escalation is bounded by pre-committed ceilings with hard time-to-live).

Phase 13 implements the first of these — delegation attenuation — because it is the one the orchestrator is uniquely positioned to enforce. Every subagent call receives a **strict subset** of the parent's permissions. A skill-scoped call can never hold a permission the skill did not declare. A verifier call holds only read permissions. The audit chain records the attenuation at each hop.

The enforcement mechanism is the Phase 9 `sys.addaudithook` and the Phase 12 v2 skill permission manifest, composed. When the orchestrator dispatches to a subagent, it constructs the subagent's effective permission set by intersecting the parent's set with the subagent's declared set, and it sets the context variable that the audit hook reads. A subagent cannot hold a permission its parent did not hold; a skill-scoped call cannot hold a permission the skill's manifest did not declare; a verifier call cannot write.

The audit chain records each intersection as a decision-log entry, so a later investigation can reconstruct the exact permission set that was active at every hop.

Phase 13 does not implement governance attenuation, disclosure attenuation, or exception attenuation. Those are future phases; the delegation form is the one that has an immediate caller in the current orchestrator.

---

## 11. The block plan

Eight blocks. Each block is a coherent increment with its own boundary tests. The order matters because later blocks depend on earlier ones, and because the Phase 11 correctness fixes must land before the orchestrator can exercise them.

### Block 1 — Phase graph data model and Phase 11 correctness fixes

This block establishes the `PhaseGraph` and `PhaseSlot` dataclasses, defines the `legacy` graph as a constant, and lands the three Phase 11 correctness fixes (§2.1 analyst spec degradation, §2.2 writer signature check bypass, §2.3 silent function-body failure) that the orchestrator exercises during normal runs. The three fixes are small and independent; landing them here means the orchestrator does not have to work around them.

**Boundary test:** the `legacy` graph validates against the schema; the three fixes each have a targeted test in their own module's test file; the full pytest suite remains green.

### Block 2 — Orchestrator core loop and checkpoint supervisor

This block implements the loop that walks a `PhaseGraph`, resolves each slot against the registry, invokes the agent, collects the output, and advances. It also implements the checkpoint supervisor from `modularity-audit.md` §6.6: a heartbeat column on the `checkpoints` table, a stale-run detection query, and a resume path that re-enters the pipeline from the last complete checkpoint.

**Boundary test:** the loop walks `legacy` on a fixture repository with stub agents; the checkpoint supervisor resumes a run that was killed mid-phase; the audit chain records every transition.

### Block 3 — Verifier stack

This block implements the three-layer verification stack from §8: deterministic gate, deterministic pre-scan, LLM verifier loop. It wires the `VerifierLoop` from Phase 11 into the apply → verify → rollback flow with the blast-band-scaled K.

**Boundary test:** a bad candidate fails the deterministic gate and bounces back without an LLM call; a candidate that fails the LLM verifier enters the loop; the loop rolls back between rounds; the loop halts at K and reports.

### Block 4 — Context engine

This block implements the run-tree context compilation from §7, the `ContextBudget` per-run construction from `CapabilityProfile.context_window`, and the three-tier memory wiring.

**Boundary test:** a long pipeline run compiles a bounded context per phase; the `ContextBudget` fires at 60% compaction and 85% refusal; the checkpoint captures the run tree.

### Block 5 — Routing layer

This block wires the Phase 11 routing primitives into `fallback_router.py`, with the cheap-first default, the escalation paths, the `BudgetEnvelope` enforcement, and the blast-aware override.

**Boundary test:** a run with `max_cost_usd=0.5` does not exceed the cap; a `standard`-tier call at 80% budget downgrades; a block-band target escalates to frontier tier regardless of budget.

### Block 6 — Evidence columns and decision log

This block populates `tool_chain`, `decision_log`, `model_reasoning`, and `evidence_receipt` on every `agent_calls` row that should carry them. It also implements the cold-tier prediction contract: every decision log entry carries a self-declared prediction, and the observability loop (deferred) can verify it against the next run's outcome.

**Boundary test:** a full pipeline run leaves no row with `tool_chain=NULL` for an agent that called a tool; a decision-log entry from a Writer candidate predicts "will pass verifier" and the prediction is stored.

### Block 7 — Sandbox adapter and capability attenuation

This block implements the `SandboxExecutor` adapter that unlocks the six Phase 11 opt-in features (tester `run_tests`, mutation gate, writer splice-verify, correctness and contract executable checks, semgrep/bandit), and the delegation attenuation from §10.

**Boundary test:** the tester's `run_tests` fires inside the sandbox; a subagent call holds only the intersection of parent and agent permissions; the audit chain records the intersection.

### Block 8 — Integration tests and deep-test update

This block adds the orchestrator tests and updates the deep test to run through the orchestrator rather than the pipeline directly.

**Boundary test:** the full pipeline runs end-to-end on `httpx`; the checkpoint supervisor resumes a killed run; the verifier loop retries on a bad candidate; the beast reports the same counts it reported before (plus any new assertions).

---

## 12. Anti-patterns the settlement forbids

Four patterns are explicitly forbidden, each with a documented cause.

**Prompted orchestration.** If the model's output ever becomes a transition decision, the phase has failed. The engine decides transitions; the model produces evidence. GraphBit's finding is unambiguous: prompted routing produces hallucinated transitions, infinite loops, and non-reproducible execution.

**Mode branching inside the loop.** Any `if mode == "greenfield"` or equivalent inside the orchestrator loop makes the loop non-composable. The mode must select the graph before the loop starts, and the loop must be blind to it.

**Self-review without execution.** The 2026 verification study shows that asking the model to review its own code without running it gives zero gain. The deterministic gate runs first; the LLM verifier runs on what survives; there is no path where the model's opinion about its own work substitutes for a check.

**Agent class names in the graph.** If a slot's name encodes an agent class name, a second agent that satisfies the same slot cannot be added without editing the graph. The graph names slots; the registry names agents.

A fifth implicit anti-pattern is worth recording: **letting the audit schema become a hot-path dependency**. The two-tier evidence split from §6 exists specifically to prevent this. If the engine reads the audit table on the hot path, every schema change risks breaking the engine; the split keeps the hot path and the cold path independent.

---

## 13. Acceptance criteria

Phase 13 is done when:

1. The `legacy` phase graph validates against the schema, and the orchestrator walks it on a fixture repository with stub agents, producing a checkpoint and an audit entry per phase.
2. The checkpoint supervisor resumes a run killed mid-phase and reaches completion.
3. The verifier stack runs deterministic gate, pre-scan, and LLM loop in that order, and the LLM loop retries a failing candidate with the verifier's reason as `previous_attempt` feedback.
4. The `ContextBudget` fires at 60% compaction and 85% refusal on a synthetic long run.
5. The routing layer enforces a `BudgetEnvelope` cap of $0.50 on a synthetic run and downgrades a tier at 80%.
6. Every `agent_calls` row from a full pipeline run has `tool_chain`, `decision_log`, and `model_reasoning` populated where applicable; `evidence_receipt` populated for every verifier.
7. A subagent call holds only the intersection of parent and agent permissions; the audit chain records the intersection.
8. The deep test runs the full pipeline through the orchestrator on `httpx` and produces the same baseline counts as the pre-Phase-13 run (65 passed, 4 environmental failures, 2 xfailed), plus any new assertions.
9. `PYTHONPATH=python python3 -m pytest tests/ -q` remains green with no regressions.
10. `docs/public-api.md`, `docs/05-data-model.md`, and `BUILD_STATE.md` are updated to record the orchestrator surface, the two new tables or columns, and the phase completion.

---

## 14. What Phase 13 does not deliver

| Not delivered | Target phase |
|---|---|
| CLI | Phase 14 |
| MCP server and client | Phase 15 |
| Reporters | Phase 15 |
| Harness-evolution loop (AHE) | Future |
| Full SpineTree with prefix-cache reuse | Future |
| Governance attenuation, disclosure attenuation, exception attenuation | Future |
| Per-language pipeline support | Future phase (see `docs/summaries/multi-language-pipeline-estimate.md`) |
| The greenfield graph | Future phase (the data model supports it; the graph is one file) |
| The upgrade graph | Future phase |
| Hot-swap of agents or capabilities | Future |
| Type checker and linter integration in the deterministic gate | Phase 14 or 15 |

---

## 15. Handoff to Phases 14–16

### To Phase 14 (CLI)

The CLI reads the orchestrator's public surface: `run`, `resume`, `pause`, `get_state`. The `PhaseGraph` is a config input the CLI exposes; the `legacy` graph is the default. The CLI's `skill` subcommands read the Phase 12 v2 governor and graph; the orchestrator does not change.

### To Phase 15 (MCP and Reporters)

The MCP server wraps the orchestrator's `run` method; the reporters read the run artifacts and the audit log. The orchestrator does not change.

### To Phase 16 (Full Assembly)

The integration script grows one section for Phase 13: it walks the `legacy` graph on a fixture, asserts the checkpoint and audit chain, and verifies that a killed run resumes.

---

## 16. One-paragraph summary

Phase 13 is the harness engine: a deterministic graph walker that resolves typed slots against a heterogeneous agent registry, consumes a two-tier evidence bus, runs a three-layer verification stack with a blast-band-scaled verifier loop, compiles bounded context from a run tree, enforces a cheap-first routing layer with blast-aware escalation, populates the four evidence columns, and enforces delegation capability attenuation. The orchestrator is the smallest piece; the agents, capabilities, and modes live in registries and data files. Adding an agent is a registration; adding a mode is a graph file; adding a shared capability is a plugin. The loop is the last thing you touch, not the first. The phase is estimated at 2,000–3,000 lines across eight blocks, and it is the phase that turns the ten structural advantages CodeGuardian already has — blast radius before model call, four-tier memory, tamper-evident audit, cross-family independence, published failure inventory, governed memory, phase-graph extensibility, multi-language path, per-phase residual inventory, deterministic deep test — from architectural claims into measured properties of a running system.

---

*End of document.*
