# Phase 11 — Agents: Design Settlement

Snapshot date: 2026-10-31
Status: Design settled. Six pre-fix files written and verified. Agents not yet written.
Supersedes: Phase_11.md dated 2026-10-31 (earlier snapshot).
Scope: 41 files total. Six pre-fix files already written. Thirty-five files remain.

---

## 1. What Phase 11 is

Phase 11 delivers nine callable agents. Each agent:

1. Receives a typed input.
2. Loads its prompt from the Phase 10 prompt system.
3. Calls a model through the injected `ModelProvider` (deterministic agents call the kernel instead).
4. Validates the response through `validate_output`.
5. Classifies its own outcome.
6. Records a provenance row for the call.
7. Returns a typed output.

What Phase 11 does NOT do:

| Not in scope | Phase |
|---|---|
| Orchestration across phases | 13 |
| Retry loop on test failure | 13 |
| Sandbox execution | 13 |
| Diff building and application | 13 |
| Consent gates and plan approval | 13 |
| CLI commands | 14 |
| MCP tool exposure | 15 |
| Reporting | 15 |

Agents are framework-neutral units. They call model providers, use tools, and write telemetry; they are side-effecting by design. They do not depend on any orchestration framework. Phase 11 has zero LangGraph imports.

---

## 2. What changed from the original plan, and why

The original plan (2026-10-30) described nine agents as thin wrappers around a model call. Research into the 2026 landscape showed that model-call wrappers are no longer competitive. The scaffolding around the model is now the differentiator.

Key findings:

| Finding | Impact |
|---|---|
| Same model, different scaffolding, 16.6 absolute points on SWE-bench | The agent layer matters as much as the model |
| ACI design alone gave 3.3x improvement | Structured tool interfaces beat free-form exploration |
| Context rot: models get worse as input grows | Progressive disclosure is not optional |
| Independence requires different evidence, not just different model family | Each verifier gets a different slice |
| Semantic error compounds sequentially | Failure containment is first-class |
| Multi-agent amplification: 17.2x uncoordinated vs 4.4x centralized | Coordination must be deterministic |
| Sample-and-select beats single-shot when selection is cheap | Writer produces N candidates, pruned deterministically |

The redesign applies these findings. Agents become compound: context-managed, classification-aware, provenance-recorded, deterministically gated.

---

## 3. The nine agents

| File | Role | Deterministic | Tools used | Output type |
|---|---|---|---|---|
| `recon.py` | recon | Yes | none | `ReconResult` |
| `blast.py` | blast | Yes | none | `BlastResult` |
| `analyst.py` | analyst | No | `list-symbols`, `code-skeleton`, `function-body`, `call-graph-slice` | `RefactorSpec` + `ContextPackOutput` |
| `tester.py` | tester | No | `function-body`, `code-skeleton` | `SourceOutput` |
| `writer.py` | writer | No | `function-body`, `call-graph-slice` | `SourceCandidates` |
| `correctness_verifier.py` | correctness_verifier | No | none | `VerdictOutput` |
| `security_verifier.py` | security_verifier | No | none | `VerdictOutput` |
| `contract_verifier.py` | contract_verifier | No | none | `VerdictOutput` |
| `skill_curator.py` | skill_curator | No | none | `SkillProposalOutput` |

Recon and Blast are deterministic because the cache is keyed by commit hash and the audit log hashes the CPG payload. Both assume byte-identical output.

---

## 4. The agent contract

Every agent implements `Agent[InputT, OutputT]` and declares five class attributes:

| Attribute | Type | Purpose |
|---|---|---|
| `is_deterministic` | `bool` | Phase 13 caches when true, retries when false |
| `requires_tools` | `tuple[str, ...]` | Phase 13 provisions only the tools listed; enforced at the barrier |
| `can_refuse` | `bool` | Phase 13 knows whether to expect a refusal |
| `prompt_role` | `str | None` | Role name for `render_prompt`; None for deterministic |
| `prompt_version` | `str | None` | Prompt version; None for deterministic |

The `Agent` protocol itself declares only `name`, `phase`, and `run`. The five attributes live on the concrete classes and are enforced by `test_agent_contracts.py`, not by the type system.

The `requires_tools` attribute is not advisory. `_runtime.fresh_barrier` checks every method call against it. A tool call to a method not listed in `requires_tools` raises `PLUGIN_PERMISSION_DENIED` before reaching the barrier. This is least-privilege enforcement at the tool boundary.

---

## 5. The RefactorSpec artifact

The original plan had the Analyst produce a file list, and the Writer work from a free-text directive. That design forces the Writer to guess which parameters get type hints, which return type is intended, and which constraints apply.

The Analyst now produces a structured `RefactorSpec`, carried in the `context-pack.json` artifact's `extensions.refactor_spec` field. No schema change.

```json
{
  "directive": "add type hints",
  "target": "sessions.Session.send",
  "changes": [
    {"kind": "parameter_annotation", "name": "request", "inferred_type": "Request"},
    {"kind": "return_annotation", "inferred_type": "Response"},
    {"kind": "keep_signature_shape", "reason": "3 callers pass positional args"}
  ],
  "constraints": [
    "do not change parameter order",
    "return type must remain compatible with Response.stream()"
  ]
}
```

Every `change` is inferred from the CPG and the caller evidence. Every `constraint` names a specific blast-radar finding.

The Tester reads the spec to know which behaviors to lock. The Writer reads the spec to know which edits are required.

---

## 6. Context management: progressive disclosure

Each LLM agent has four context providers available and starts with the cheapest. Escalation is on-demand.

| Provider | Cost | When to use |
|---|---|---|
| `list-symbols` | Very low | First look at a file |
| `code-skeleton` | Low | Need signatures but not bodies |
| `function-body` | High | Need the actual source |
| `call-graph-slice` | Medium | Need callers or callees |

Each agent holds a `BudgetTracker` constructed per run. Two caps: `max_calls` and `max_output_bytes`. When a cap is approached, the agent stops escalating. When the cap is exceeded, `check_call` raises `PLUGIN_TIMEOUT` with `details["budget"]`, and the agent returns a structured `budget_exhausted` refusal.

### Concurrency safety

The `PluginRegistry` returns the same `FaultBarrier` instance for a plugin on every `get_*` call. Two agents calling into the same plugin concurrently would race if they set `agent_mode` and `budget` on the shared barrier and then reset them afterward.

Phase 11 avoids that race by constructing a fresh `FaultBarrier` per agent call. `_runtime.py` exposes:

```python
with fresh_barrier(registry, "function-body", mode="plan", budget=tracker, allowed_tools=("provide",)) as barrier:
    result = await barrier.call_async("provide", input_dict)
```

`fresh_barrier`:

1. Reads `registry.get_registered(name, type)` to obtain the underlying `.plugin` instance.
2. Validates that every requested method is in `allowed_tools`. Raises `PLUGIN_PERMISSION_DENIED` on the first violation.
3. Wraps `.plugin` in a new `FaultBarrier` configured with the given `mode` and `budget`.
4. Yields the barrier. On exit, discards it.

The registry's own barrier is untouched. Concurrent agents cannot interfere.

The cost of constructing a barrier per call is one `ThreadPoolExecutor` reference and a dict lookup. `FaultBarrier` shares the process-wide executor. There is no new thread pool per call.

The audit hook still fires correctly: `FaultBarrier` sets the plugin name in a `ContextVar` before dispatching, and the hook reads it.

---

## 7. The Writer candidate model

The original plan produced one refactor. The redesign produces N candidates, prunes deterministically, and returns the survivors ranked by measurable signals.

**Generation:** N=3 by default. Same prompt, different seeds (temperature 0.2 with `seed` set in `ModelOptions`).

**Deterministic pre-check per candidate:**

1. Signature drift check (Python only): parse original and candidate with `ast`, compare parameter names, order, count, return type.
2. `ast.parse` on the whole file after splicing the candidate into the original file's location.
3. Directive compliance: does the candidate contain every change the `RefactorSpec` required?

Any candidate failing a check is dropped.

**Ranking of survivors:** two measurable signals, in order.

| Signal | Calculation | Direction |
|---|---|---|
| Directive compliance score | `changes_present / total_changes` in the candidate | Higher is better |
| Diff size | Total lines changed by the candidate versus the original | Smaller is better |

Primary sort: compliance score (descending). Secondary sort: diff size (ascending). Tertiary sort: creation order (stable).

The ranking is not a quality claim. It is a deterministic ordering by the two measurable signals available without a sandbox. Directive compliance measures whether the candidate did what the spec asked. Diff size measures how much it changed. A candidate that is fully compliant and minimally invasive is preferred. A candidate that is fully compliant and larger is second. A candidate that is partially compliant is ranked below any fully compliant candidate.

**Return:** `SourceCandidates` — a list of surviving candidates, ranked by the above signals. Each candidate carries its `compliance_score` and `diff_size` in the output so Phase 13 and the audit trail can see why the order is what it is.

**Downstream use:** Phase 13 sends the first candidate to the verifiers. If verification fails, the next candidate goes next. Verifiers never see more than one candidate per attempt.

For non-Python targets, the signature drift check is skipped. Directive compliance and syntax validity still apply.

---

## 8. Verifier evidence slicing, deterministic pre-scan, and parallel execution

The three verifiers run in parallel with different evidence slices. Each runs a deterministic pre-scan before its model call.

### Evidence slices

| Verifier | Evidence slice |
|---|---|
| Correctness | Diff + characterization test result (pass or fail with the specific assertion) |
| Security | Diff + original security-relevant patterns |
| Contract | Diff + one `ContractAssertion` per input item |

Each verifier sees only its slice. None sees the original code, the writer's reasoning, or the other verifiers' slices.

### Deterministic pre-scan

Before each model call, the verifier computes a structured pre-scan the model must address:

| Verifier | Pre-scan content |
|---|---|
| Correctness | Diff stats (lines added/removed/files/hunks); `ast.parse` result per Python hunk |
| Security | Diff stats; regex matches for a documented security pattern list (see below) |
| Contract | Diff stats; which assertions have corresponding lines in the diff |

Security pre-scan pattern list:

- `eval(` or `exec(` introduced in an added line
- `subprocess(` with `shell=True` in an added line
- `os.system(` in an added line
- String-formatted SQL (`execute(f"...` or `execute("..." % ...)`)
- `pickle.loads(` on untrusted input (added line, no `# noqa` comment nearby)
- `yaml.load(` without `Loader=SafeLoader`
- `md5`, `sha1`, `DES`, `RC4`, `ECB` cryptographic primitives
- Hardcoded strings matching API key, JWT, or password patterns

Each match has an ID. The pre-scan output lists matches as `{id, pattern, line_number, snippet}`.

The verifier's prompt receives the pre-scan output as part of its evidence. The prompt requires: if the model returns `pass` but the pre-scan flagged a pattern, the model must explicitly address each match in a `findings` entry with severity `info` explaining why the pattern is benign in this context. If it does not, Phase 13 records a `pre_scan_conflict` and escalates to the user.

The pre-scan does not determine the verdict. The model does. But the model cannot ignore the pre-scan.

### Parallelism

The three verifiers have zero dependencies on each other. They run with `asyncio.gather`. Wall-clock time drops from `t1 + t2 + t3` to `max(t1, t2, t3)`.

### Verdict vs. failure

A verifier that returns `fail` did not fail operationally. It succeeded at its job. `fail` is a verdict about the code, not a failure of the agent. Section 9 separates the two taxonomies.

---

## 9. Two taxonomies: FailureCategory and VerdictReason

### FailureCategory — why did the agent's execution fail

Recorded in `agent_calls.failure_category`. Populated only when the agent did not complete its task. NULL on successful runs.

| Category | Retry policy |
|---|---|
| `localization` | Retry with expanded context |
| `generation` | Retry with verifier feedback |
| `schema` | Retry once with the violation named |
| `contract` | Retry with the specific violation named |
| `timeout` | Retry once, then fall back |
| `context_overflow` | Retry with truncated context |
| `model_unavailable` | Fall back to next model |

### VerdictReason — why did a verifier reject the writer's output

Recorded in `agent_calls.verdict_reason`. Populated only when a verifier returned `fail`. NULL on all other runs.

| Reason | Phase 13 action |
|---|---|
| `behavior_drift` | Send back to Writer with the specific test failure |
| `vulnerability_introduced` | Escalate to the user; do not auto-retry |
| `contract_broken` | Send back to Writer with the assertion named |
| `inconclusive` | Send back to Analyst for expanded context |

### Why two taxonomies

Mixing them into a single enum puts two incompatible meanings in one column. A verifier that returns `fail` with reason `behavior_drift` did its job correctly; its `failure_category` is NULL. An agent that times out did not do its job; its `failure_category` is `timeout`. Phase 13's retry logic cannot be correct if it cannot distinguish these.

---

## 10. Replay provenance, not replay

Every agent call writes one row to `agent_calls` with provenance columns:

| Column | Purpose |
|---|---|
| `prompt_hash` | SHA-256 of the rendered prompt |
| `context_hash` | SHA-256 of the context pack |
| `output_hash` | SHA-256 of the agent's output |
| `model` | Provider/model identifier |
| `model_options` | JSON of `ModelOptions` |
| `failure_category` | One of seven, or NULL |
| `verdict_reason` | One of four, or NULL |

### What these prove

- Whether two runs used the same prompt.
- Whether two runs used the same context.
- Which model handled the call.
- What parameters were set.

### What these do not prove

- They cannot **reconstruct** a run. The actual prompt and context are not stored by default.
- They cannot **re-execute** a run deterministically. Hosted models do not commit to determinism for the same seed.

### How to use them

When a run fails, the provenance record shows whether the failure is reproducible (same hash) and whether the inputs changed between runs. Investigating further requires re-sending the same prompt, reconstructed from the run's stored artifacts, and comparing. Phase 13 builds that comparison tool.

Phase 11 records provenance. Phase 13 builds reconstruction.

---

## 11. Deterministic policy gate

Every tool call goes through `FaultBarrier.call_async` or `FaultBarrier.call`, constructed by `_runtime.fresh_barrier`. Three checks run before dispatch:

1. Plugin not disabled.
2. `action_kind` allowed for the current `agent_mode` (via `check_action_allowed`).
3. Budget not exceeded.

Plus one check before the barrier is constructed:

0. `requires_tools` allows the requested method. A tool call to a method not in the agent's declared list raises `PLUGIN_PERMISSION_DENIED` before the barrier exists.

The model cannot talk its way past these gates. They are enforced in Python code, not in a prompt instruction.

The policy engine from Phase 7 is not wired into Phase 11 tool calls. Those checks live at the sandbox boundary (rpcd) and the plugin boundary (audit hook). Both are already enforced.

---

## 12. The Recon agent

Deterministic. Wraps the kernel. Produces an artifact conforming to `recon.json`.

**Process:**

1. Enumerates files under the clone root via the repository provider.
2. Detects languages by file extension and priority order.
3. Builds the CPG via `_native.build_graph`.
4. Extracts the dependency graph from `Import` and `ImportFrom` nodes.
5. Builds the hierarchical index.
6. Fills remaining schema fields with skipped or empty values.
7. Sets `extensions.report_statuses` (see below).
8. Returns `ReconResult` with all eleven required fields populated.

**The eleven required fields:**

| Field | Value |
|---|---|
| `schema_version` | `"1.1"` |
| `repo` | URL, commit hash, detected_at |
| `languages` | Detected with counts and percentages |
| `structural` | CPG path, node count, edge count, built_at |
| `dependency_graph` | Real import graph from AST |
| `hierarchical_index` | Real declaration groupings |
| `entrypoints` | Scripts, top-level functions, route decorators (best-effort) |
| `versions` | Declared/actual/available (skipped if none detected) |
| `smoke_test` | `{"status": "skipped", "passed": 0, "failed": 0, "errored": 0}` |
| `truth_report` | Empty array |
| `drift_report` | Empty array |

**Optional `frameworks` field:** empty array.

**Report statuses in `extensions`:** every analysis carries an explicit status so a downstream consumer cannot mistake "not run" for "found nothing".

```json
"extensions": {
  "report_statuses": {
    "dependency_graph": "complete",
    "hierarchical_index": "complete",
    "entrypoints": "partial",
    "versions": "partial",
    "frameworks": "complete",
    "smoke_test": "skipped",
    "truth_report": "skipped",
    "drift_report": "skipped"
  }
}
```

A `skipped` status means the analysis was not performed. A `complete` status means it was performed and its result is authoritative. A `partial` status means it was performed with known limitations.

---

## 13. The Blast agent

Deterministic. Wraps `_native.compute_blast` and re-shapes the output. Produces an artifact conforming to `blast-radius.json`.

**Process:**

1. Calls `_native.compute_blast`.
2. Computes per-caller score contribution and ranks callers.
3. Extracts `ContractAssertion` objects from raw violations, each with a `test_expression` the Tester must assert.
4. Tags coverage gaps.
5. Computes `unresolved_call_sites` and `caller_search_complete` (see below).
6. Applies calibrated recommendation (see below).
7. Returns `BlastResult`.

### Caller search completeness

The kernel's call graph is exact for same-file calls and best-effort for cross-file calls. G-16 documents the gap: imported functions called as `utils.parse()` may be missing from the caller list.

The Blast agent makes this visible:

- `unresolved_call_sites`: the count of call sites in the CPG that produced an unresolved edge.
- `caller_search_complete`: boolean, true iff `unresolved_call_sites == 0`.
- Recommendation shift: if `caller_search_complete` is false, shift the recommendation one band toward caution.

| Original recommendation | Adjusted (incomplete) |
|---|---|
| `proceed` | `review` |
| `review` | `block` |
| `block` | `block` |

The user can override. The tool reports the adjustment so the override is a conscious decision, not a hidden default.

### Fields added to `BlastResult`

- `unresolved_call_sites: int`
- `caller_search_complete: bool`
- `recommendation_raw: str` — before calibration
- `recommendation_adjusted: str` — after calibration
- `calibration_reason: str | None` — non-None when the shift applied

The re-shaped output makes the incomplete-analysis risk visible rather than silent.

---

## 14. The Analyst agent

Model-backed. Converts reconnaissance evidence into a `RefactorSpec` and a context pack.

**Process:**

1. Reads the blast result and the recon hierarchical index.
2. `list-symbols` on the target file.
3. `code-skeleton` on relevant callers (score contribution > threshold).
4. `function-body` on the target or critical helpers.
5. `call-graph-slice` if the blast flagged a contract violation.
6. Composes the `RefactorSpec` deterministically from tool output.
7. Calls the model with the spec draft and collected context. The model confirms or refuses.
8. If the model returns a spec different from what the tools support, uses the tool-supported version and logs a warning.
9. Returns `RefactorSpec` and `ContextPackOutput`.

The `RefactorSpec` is grounded in tool output, not invented by the model. The model catches contradictions and composes the human-readable reason.

---

## 15. The Tester agent

Model-backed. Writes a characterization test file that passes on the unmodified code.

**Process:**

1. Reads the `RefactorSpec`. Every `change` implies a behavior to lock.
2. Reads the target function body.
3. Writes a test file covering every branch, boundary, and exception path, plus one assertion per `ContractAssertion`.
4. `ast.parse` the test file.
5. Runs an assertion count post-check (see below).
6. Returns the test file.

### Assertion count post-check

For every `def test_*` function in the output:

- Must contain at least one `assert` statement.
- If the test uses `pytest.raises`, it counts as an assertion.

If any test function has zero assertions, the Tester self-retries once with the specific deficiency named in the retry prompt.

The post-check catches the weakest failure mode: a test file that parses, imports, and runs, but asserts nothing.

### Retry on later failure

If Phase 13 reports the test failed on old code, the Tester is re-invoked with `v1.1.0` and the failure message.

---

## 16. The Writer agent

Detailed in section 7. Key points:

- Reads the `RefactorSpec`.
- Generates N=3 candidates.
- Deterministic pre-check drops broken candidates.
- Survivors ranked by directive compliance score and diff size.
- Returns ranked survivors with scores in the output.
- On retry, uses `v1.1.0` and the verifier's specific failure.

---

## 17. The three verifier agents

Detailed in section 8. Key points:

- Each gets a different evidence slice.
- Each runs a deterministic pre-scan.
- They run in parallel.
- A verifier that returns `fail` records `verdict_reason` and leaves `failure_category` NULL.
- A verifier that times out or hits a schema violation records `failure_category` and leaves `verdict_reason` NULL.
- If the model's verdict does not address a pre-scan match, Phase 13 escalates as `pre_scan_conflict`.

---

## 18. The Skill Curator agent

Model-backed. Reviews a completed refactor — successful or failed — and proposes a reusable procedure.

**Two modes:**

- **On success:** positive skill (`polarity: positive`).
- **On failure:** negative skill (`polarity: negative`) with the failure mode named.

**Every proposal is a draft.** The `state` field on the output is `draft`, carried in the `extensions` bag. Phase 12's skill library quarantines drafts until evidence and review.

**Output:** `SkillProposalOutput` with `skill_name`, `skill_markdown`, `confidence`, `polarity`, and `state: "draft"`.

---

## 19. Cross-cutting modules

**`telemetry.py`:**

- Defines `FailureCategory` (7 values) and `VerdictReason` (4 values).
- Provides `record_call(...)` that writes one `agent_calls` row.
- Computes `prompt_hash`, `context_hash`, `output_hash`.
- Maps each category to its retry policy string.
- All nine agents call `record_call` once at the end of `run()`, in a `finally` block.

**`_runtime.py`:**

- `fresh_barrier(registry, name, *, mode, budget, allowed_tools)`: constructs a new `FaultBarrier` around the plugin instance; enforces `allowed_tools` before yielding; does not mutate the registry's barrier.
- `tracked_model_call(provider, prompt, model, options, role)`: wraps the model call, measures latency, returns the response and timing.
- `classify_failure(exc)`: maps a `CodeGuardianError` to a `FailureCategory` or returns `None`.

Both modules are agent-internal. Neither is imported outside `core/agents/`.

---

## 20. Database schema: migration 002 and 003

Migration `002_agent_calls.sql` (already written and verified) creates `agent_calls` with 16 columns:

`call_id`, `run_id`, `phase_id`, `target_symbol`, `role`, `prompt_role`, `prompt_version`, `model_id`, `tokens_in`, `tokens_out`, `latency_ms`, `outcome`, `refusal_code`, `retry_count`, `output_hash`, `created_at`.

Migration `003_agent_provenance.sql` (new) adds five columns to `agent_calls`:

- `prompt_hash TEXT`
- `context_hash TEXT`
- `failure_category TEXT`
- `verdict_reason TEXT`
- `model_options TEXT`

All additively via `ALTER TABLE ADD COLUMN`. No column renamed, no CHECK extended. `failure_category` and `verdict_reason` are not CHECK-constrained at the database level; the valid sets live in `telemetry.py`.

---

## 21. Pre-fix files and test boundary

Six pre-Phase-11 files were written and verified:

| File | Gap closed | Verified |
|---|---|---|
| `python/codeguardian/core/storage/migrations/002_agent_calls.sql` | Migration 002 | Migration applies, 16 columns, 3 indexes |
| `prompts/_base/v1.1.0.md` | Retry-version base | Loads, contains injection-defense and prior-attempt sections |
| `prompts/writer/v1.1.0.md` | Writer retry | Renders with retry variables |
| `prompts/tester/v1.1.0.md` | Tester retry | Renders with retry variables |
| `python/codeguardian/core/agents/agent_eval.py` | Non-verifier eval | Loads analyst golden cases; runs end to end; rejects verifier roles |
| `tests/unit/core/test_phase11_fixes.py` | Verification | 11 tests pass |

Test count at the pre-fix boundary: **895 passing**, up from 884.

The five gaps from the pre-Phase-11 audit that were not resolved by a separate module are resolved inside agent files:

| Gap | Resolution location |
|---|---|
| Budget into barrier | `_runtime.fresh_barrier` |
| No `signature_of` | `writer.py` uses `ast` on `function_source` output |
| `reason_code` | Each verifier pre-validates before `validate_output` |
| Recon missing fields | `recon.py` fills all eleven |
| Agent Protocol attributes | `test_agent_contracts.py` asserts them |

---

## 22. File inventory

Forty-one files in Phase 11. Six already written. Thirty-five remain.

### Already written (6)

```
python/codeguardian/core/storage/migrations/002_agent_calls.sql
prompts/_base/v1.1.0.md
prompts/writer/v1.1.0.md
prompts/tester/v1.1.0.md
python/codeguardian/core/agents/agent_eval.py
tests/unit/core/test_phase11_fixes.py
```

### To write (35)

Agent modules (11):

```
python/codeguardian/core/agents/__init__.py
python/codeguardian/core/agents/recon.py
python/codeguardian/core/agents/blast.py
python/codeguardian/core/agents/analyst.py
python/codeguardian/core/agents/tester.py
python/codeguardian/core/agents/writer.py
python/codeguardian/core/agents/correctness_verifier.py
python/codeguardian/core/agents/security_verifier.py
python/codeguardian/core/agents/contract_verifier.py
python/codeguardian/core/agents/skill_curator.py
python/codeguardian/core/agents/telemetry.py
```

Runtime helpers (1):

```
python/codeguardian/core/agents/_runtime.py
```

Migration (1):

```
python/codeguardian/core/storage/migrations/003_agent_provenance.sql
```

Golden cases (4):

```
prompts/analyst/eval/golden.yaml
prompts/tester/eval/golden.yaml
prompts/writer/eval/golden.yaml
prompts/skill_curator/eval/golden.yaml
```

Adversarial suites (3):

```
prompts/correctness_verifier/eval/adversarial.yaml
prompts/security_verifier/eval/adversarial.yaml
prompts/contract_verifier/eval/adversarial.yaml
```

Fixtures (2):

```
tests/fixtures/agents/recon_cases.yaml
tests/fixtures/agents/blast_cases.yaml
```

Unit tests (12):

```
tests/unit/core/test_agent_contracts.py
tests/unit/core/test_agent_recon.py
tests/unit/core/test_agent_blast.py
tests/unit/core/test_agent_analyst.py
tests/unit/core/test_agent_tester.py
tests/unit/core/test_agent_writer.py
tests/unit/core/test_agent_verifiers.py
tests/unit/core/test_agent_skill_curator.py
tests/unit/core/test_agent_telemetry.py
tests/unit/core/test_agent_golden.py
tests/unit/core/test_adversarial_verifiers.py
tests/unit/core/test_determinism.py
```

End-to-end test (1):

```
tests/e2e/test_blast_radius.py
```

**Total: 41 files (6 written, 35 remaining).**

---

## 23. Test strategy

**Block boundary (after each block of 3–5 files):** run only the affected tests. Isolated, fast.

**Phase boundary (after all 35 files):** full suite + deep test + integration test that exercises Analyst, Tester, Writer, one verifier against a fixture repository.

**Determinism tests (not in CI):** each LLM agent runs twice with the same input, fake model, temperature 0. Outputs must match. Proves our side is deterministic given the same model output; does not prove the model itself is deterministic.

**Golden-case tests (fake model, CI):** each role's golden cases run against a deterministic fake. Proves the harness works and the prompt renders correctly. Does not establish model quality.

**Adversarial tests (fake model, CI):** five traps per verifier. Proves the harness catches a fake model that ignores the defense. Real adversarial resistance is measured in the real-model evaluation.

Expected test count at the phase boundary: 895 → approximately 1035. Delta of 140 tests.

---

## 24. Real-model evaluation (Phase 14 constraint)

Phase 11 provides the inputs and the design. Phase 14 builds the harness and runs it. This section records the requirements Phase 14 must meet.

### Metrics and thresholds

| Metric | Ground truth source | Threshold |
|---|---|---|
| False approval rate | Bad refactor got `pass`, caught by tests | < 5% over 100 runs |
| False refusal rate | Good refactor got `refused`, characterized test passes on both | < 10% over 100 runs |
| Suggestion precision | Human-reviewed ground truth corpus (not model agreement) | > 70% |
| Contract assertion recall | Blast-extracted assertions verifier correctly checks | > 85% |
| Cost per successful refactor (mid-tier) | Measured per run | < 0.50 USD |
| Cost per successful refactor (frontier) | Measured per run | < 2.00 USD |
| Latency per successful refactor | Wall-clock | < 3 minutes (mid-tier) |
| Refusal honesty | Refusals where the task was impossible | > 80% |

### Ground truth requirements

Suggestion precision must be measured against a curated corpus of ~50 known refactors (correct or buggy), from:

- SWE-bench Pro subset where a known fix exists
- Human-reviewed examples from partner repositories
- Curated examples from CodeGuardian's own refactor history

Model agreement is not an acceptable ground truth. Two models agreeing does not mean the finding is correct.

### Holdout set

A held-out set of ~50 examples not used to tune prompts, prompt versions, or model selection. Held out across the entire Phase 14 evaluation, not per-role. Any adjustment to the design after seeing the holdout results invalidates the holdout.

### Repetition and uncertainty

Each case runs 3 times. Report mean and 95% confidence interval. A result whose confidence interval crosses the threshold is treated as not meeting the threshold.

### Coverage

Stratify by language (at least 3 languages) and by change kind (syntactic, semantic, contract, signature). Report results per stratum, not just aggregate.

---

## 25. Deviations from the original BUILD_ORDER plan

| Original | Actual | Reason |
|---|---|---|
| 3 blocks, 10 files | 4 blocks, 41 files total | Research-driven redesign plus pre-fix files plus migration 003 |
| Agents as thin wrappers | Compound units | 2026 scaffolding finding |
| Single Writer candidate | N=3 candidates, deterministic pre-check, ranked by measurable signals | Sample-and-select with defensible ranking |
| Sequential verifiers | Parallel, evidence-sliced, deterministic pre-scan | Independence + wall-clock + pre-scan |
| Free-text directive | Structured RefactorSpec | Spec-driven grounding |
| Single failure enum | Two taxonomies: FailureCategory and VerdictReason | Operational failure vs verdict about code |
| "Replayable" | "Replay provenance" | Hashes prove input match, not reconstruction |
| Shared barrier mutation | Fresh barrier per agent call | Concurrency-safe |
| No `requires_tools` enforcement | Enforced at `fresh_barrier` | Least privilege at tool boundary |
| Caller search gap silent | `unresolved_call_sites` + `caller_search_complete` + calibrated recommendation | Visibility of incomplete analysis |
| Empty reports unqualified | `extensions.report_statuses` per analysis | Not-run vs nothing-found distinction |
| Skill proposals active by default | Proposals in `draft` state | No unproven lessons in rotation |
| Fake-model-only eval | Fake-model tests + real-model thresholds + ground truth requirements + holdout set | Machinery vs quality distinction |
| Ranking by creation order | Ranking by directive compliance and diff size | Measurable signals |

Every deviation is additive. No existing design decision is reversed.

---

## 26. Known limitations

**G-16 cross-file blast gap.** Imported function callers are under-reported. Phase 11 makes this visible via `unresolved_call_sites` and `caller_search_complete`, and adjusts the recommendation, but does not fix the underlying kernel. A future kernel phase closes it.

**No static type checking.** The design does not run a type checker against refactored code. mypy/pyright dependency plus per-language tooling (8 languages) is a Phase 13 or Phase 14 addition. Until then, type errors in the refactored code are caught only by the characterization test and the model verifiers.

**No dedicated security scanner.** Security pre-scan uses regex patterns against a documented list. It is not a substitute for semgrep, bandit, or similar. Adding a real scanner is Phase 13 or Phase 14.

**Writer signature check is Python-only.** Non-Python targets skip the `ast`-based signature drift check.

**No candidate reconstruction.** Provenance hashes are recorded; reconstruction is Phase 13.

**Recon smoke_test and truth_report are empty.** Phase 12 or Phase 13 fills them.

**Skill polarity is a Phase 12 concern.** Phase 11 emits drafts; Phase 12 quarantines and promotes.

**Real-model evaluation is Phase 14.** Phase 11 provides inputs and thresholds. Phase 14 builds the harness.

**Cache key does not include tool versions.** G-4 from `BUILD_STATE.md`. A cache entry produced by kernel version X may be reused by kernel version Y with different parse behavior. Fix is a Phase 3 change: add `kernel_version` and `parser_config_hash` to the cache key. Out of Phase 11 scope; flagged as a pre-Phase-13 requirement.

**Model choices are provisional until Phase 14.** `registry.json` model selections are not validated against the real-model evaluation because that evaluation runs in Phase 14. Phase 13 uses them but records them as provisional and warns the user.

---

## 27. What Phase 11 hands forward

| To | What |
|---|---|
| Phase 12 | Skill polarity (positive/negative); every proposal in `draft` state; report_statuses shape for extending recon's truth and drift reports |
| Phase 13 | Nine callable agents with declared attributes; dual taxonomy for retry selection; provenance hashes for the comparison tool; `RefactorSpec` shape; parallel verifier pattern; calibrated blast recommendation; pre-scan evidence format for escalation; `requires_tools` enforcement pattern |
| Phase 14 | Telemetry rows for `codeguardian eval`; golden case inputs; test data for the real-model evaluation harness; ground truth requirements; holdout set requirement |
| Phase 15 | Adversarial suite inputs for MCP tool response |

**Phase 13 constraints recorded for later phases:**

- Model choices in `registry.json` are provisional until Phase 14 real-model evaluation completes. Phase 13 must record them as provisional and warn the user.
- Cache key including kernel version is a pre-Phase-13 requirement (G-4). Phase 13 cannot guarantee cache correctness across kernel version changes until this is fixed.
- Type checker and security scanner are Phase 13 or Phase 14 additions. Until then, refactor verification relies on the characterization test and the model verifiers.

---

## 28. What Phase 11 does NOT do

| Not in scope | Reason | Phase |
|---|---|---|
| Orchestration | Unit vs sequence | 13 |
| Retry loop | Needs sandbox | 13 |
| Running the test | Sandbox | 13 |
| Applying the diff | Consent | 13 |
| Plan approval, apply consent | User-facing | 13 |
| Checkpointing and resume | Pipeline state | 13 |
| LangGraph integration | Orchestration | 13 |
| Replay reconstruction tool | Provenance here | 13 |
| Type checker | Dependency + per-language tooling | 13 |
| Security scanner | Dependency | 13 |
| Cache key fix | Phase 3 change | Pre-13 |
| CLI commands | CLI surface | 14 |
| Real-model evaluation harness | Design recorded here | 14 |
| MCP tool exposure | MCP surface | 15 |
| Semantic recon (Truth Report, Drift Report populated) | Model + sandbox | 12 or later |
| Skill injection into future prompts | Skill library | 12 |

---

## 29. Related documents

- `docs/summaries/phases-0-to-8.md` — build history through Phase 8
- `docs/summaries/phase-9-design.md` — Phase 9 design settlement
- `docs/summaries/phase-10.md` — Phase 10 completion record
- `docs/modularity-audit.md` — modular and non-modular decisions
- `BUILD_ORDER.md` — phase plan
- `BUILD_STATE.md` — current state and known gaps
- `prompts/_base/v1.0.0.md` — shared prompt rules, first version
- `prompts/_base/v1.1.0.md` — shared prompt rules, adds injection defense and prior-attempt feedback
- `python/codeguardian/core/prompts/` — renderer, schema, versioning, eval harness
- `python/codeguardian/core/agents/agent_eval.py` — non-verifier eval harness
