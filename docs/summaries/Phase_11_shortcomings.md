# Phase 11 — Known Shortcomings and Fix Inventory

**Snapshot date:** 2026-11-03
**Purpose:** Every limitation identified during Phase 11 v1.1 delivery and
subsequent review, organized by when it should be fixed.

This document is the working checklist for Phase 11 v1.1.1, Phase 12,
Phase 13, and Phase 14. Each item names what is wrong, why it matters,
and what "fixed" looks like. Items are grouped by priority and by the
phase that should close them.

---

## How to read this document

| Column | Meaning |
|---|---|
| **Tier** | Priority group (1 is highest) |
| **Phase** | Where the fix belongs |
| **Status** | `open`, `partial`, `fixed-pending-verify`, `closed` |
| **Verify** | How to confirm the fix works |

Items marked `fixed-pending-verify` are in the working tree but the deep
test has not been re-run since the fix was applied.

---

## Tier 1 — v1.1.1 blockers (re-run before declaring v1.1 complete)

Three fixes were applied to the working tree but never verified by a
fresh deep-test run. The previous deep test (2026-11-02) was run before
these fixes were in place. Until a fresh run is green, v1.1 is not
provably complete.

### 1.1 Fault-injection label double-count

| Field | Value |
|---|---|
| **File** | `sandtest/deep_test.py`, `step_fault_injection` |
| **Status** | `fixed-pending-verify` |
| **What was wrong** | The label passed to `_run_char_test` said `"(expect FAIL)"`, which the helper recorded as a `[FAIL]`. The subsequent code correctly detected the injected drift. Both lines referred to the same event. |
| **Fix applied** | Label changed to `"tool value check on fault-injected code ran"`. Confirmed present via `grep`. |
| **Verify** | Run `CODEGUARDIAN_DEEP_CANDIDATES=1 python3 sandtest/deep_test.py`. The section should show `[PASS] tool value check on fault-injected code ran` and `[PASS] fault injection detected by value check — label=semantic-drift`. Zero `[FAIL]` on this section. |

### 1.2 Tester prompt does not mention Python literals

| Field | Value |
|---|---|
| **Files** | `prompts/tester/v1.0.0.md`, `prompts/tester/v1.1.0.md` |
| **Status** | `fixed-pending-verify` |
| **What was wrong** | The model `qwen3.8-27b` wrote `true` instead of `True`. The validator caught it, retried, got the same output again, and fell back to the tool test. The prompt did not explicitly forbid JavaScript literals. |
| **Fix applied** | Added two-line constraint to both prompts stating Python literals are required. Confirmed present via `grep -c "Python literals"`. |
| **Verify** | Run the deep test. The tester section should show `[PASS] tester output parses and has assertions` on the first or second attempt. If the model still emits JS literals, this is not a prompt problem — it is a model quality problem (see 4.1). |

### 1.3 Skill Curator prompt does not list `skill_type` values

| Field | Value |
|---|---|
| **Files** | `prompts/skill_curator/v1.0.0.md` |
| **Status** | `fixed-pending-verify` |
| **What was wrong** | The model wrote `skill_type: refactor` instead of `refactor_pattern`. The validator correctly rejected it, but the prompt had no explicit list of valid values, so the model was guessing. |
| **Fix applied** | Added explicit enumeration of the four valid values. Confirmed present via `grep -c "refactor_pattern"`. |
| **Verify** | Run the deep test. The skill curator sections should show `[PASS] skill curator (success) polarity` and `[PASS] skill curator (success) proposal in draft state`. Currently they show `[NOTE] ... proposed no skill: ("invalid skill_markdown: frontmatter 'skill_type' must be one of ...; got 'refactor'")`. That note must become a `[PASS]`. |

**Acceptance criteria for Tier 1:** a fresh deep test run reports `89 passed, 0 failed, 0 skipped, 2 xfailed`. Until then, v1.1 is not fully green.

---

## Tier 2 — Correctness bugs (fix in Phase 11 v1.1.1)

These are real defects in Phase 11 code. They do not block the deep test but they degrade the pipeline silently. They must be closed before Phase 12.

### 2.1 Analyst spec degrades silently when `function-body` returns invalid source

| Field | Value |
|---|---|
| **File** | `python/codeguardian/core/agents/analyst.py`, `_build_refactor_spec` |
| **Status** | `open` |
| **Symptom** | On the `prepend_scheme_if_needed` target with directive `"add type hints to every parameter and the return type"`, the analyst produced only `apply_directive`. It should have produced `parameter_annotation` for `url` and `new_scheme`, plus `return_annotation`. The spec was degraded. |
| **Root cause** | Unknown. Two candidates: (a) `function-body` returned a string that is not valid Python, so `ast.parse` failed and the annotation extraction was skipped; or (b) `ast.parse` succeeded but `tree.body[0]` was not a `FunctionDef`. Neither case raises an error. The spec is silently wrong. |
| **Why it matters** | The writer proceeds with a degraded spec. The refactor may be correct on the directive keyword but miss specific changes the directive asked for. The pipeline has no signal that the spec was incomplete. |
| **What "fixed" looks like** | (1) `_build_refactor_spec` adds a warning to `AnalystResult.warnings` when `_needs_body(directive)` is true but no annotations could be extracted. (2) The deep test asserts `len(analyst_result.spec.changes) > 1` for a directive that names specific change kinds. (3) A dedicated test in `test_agent_analyst.py` constructs a body that fails `ast.parse` and asserts the warning fires. |
| **Verify** | A deep test run on `prepend_scheme_if_needed` shows `spec changes > 1` (with `parameter_annotation` present) OR `warnings` contains `"could not extract annotations from body"`. |

### 2.2 Writer signature check silently passes when original source is invalid

| Field | Value |
|---|---|
| **File** | `python/codeguardian/core/agents/writer.py`, `_check_signature_drift` |
| **Status** | `open` |
| **Symptom** | `_check_signature_drift(original_source, candidate_source)` calls `ast.parse` on both. If either fails, it returns `(True, None)` — meaning "no drift." A candidate that drifted but whose original source did not parse is accepted. |
| **Why it matters** | The same root cause as 2.1: if `function-body` returns invalid source, the writer's signature check is bypassed. The writer can accept a candidate whose parameter names have changed. |
| **What "fixed" looks like** | When `ast.parse(original_source)` fails, `_check_signature_drift` returns `(False, "original source did not parse")`. The candidate is rejected. The writer records the rejection in `decision_log`. |
| **Verify** | A test that constructs a writer input whose original source is invalid asserts that every candidate is rejected with `pre_check=0` in `refusal_reason`. |

### 2.3 `function-body` failures are not surfaced

| Field | Value |
|---|---|
| **Files** | `analyst.py`, `tester.py`, `writer.py` |
| **Status** | `open` |
| **Symptom** | When `function-body` returns an error string (starts with `"(declaration "`, `"(file "`, `"(path "`, `"(graph "`, `"(cannot "`), the analyst tries `ast.parse` on it. The tester checks with `_is_not_found`. The writer checks with `_is_not_found`. But none of them logs the failure to telemetry or surfaces it in `warnings`. |
| **Why it matters** | A silent tool failure is invisible. The pipeline produces a degraded output with no signal that the tool failed. |
| **What "fixed" looks like** | Every agent that calls `function-body` adds a `warnings` entry when the response starts with a "not found" prefix. The `decision_log` records `{"step": "tool_call", "tool": "function-body", "verdict": "not_found", "reason": "<first 100 chars>"}`. |
| **Verify** | A deep test run on a missing function shows the warning in the analyst result and the decision log entry in `agent_calls`. |

### 2.4 Recon does not parse project metadata files

| Field | Value |
|---|---|
| **Files** | `python/codeguardian/core/agents/recon.py` |
| **Status** | `open` |
| **Symptom** | `pyproject.toml`, `Cargo.toml`, `package.json`, `go.mod`, `pom.xml` are detected as present (`configs_present`) but their contents are not read into the artifact. Downstream agents do not know the project's declared Python version, framework versions, lint config, type-check config, or test configuration. |
| **Why it matters** | These files declare the project's actual constraints. An Analyst writing type hints for a Python 3.11 project must use `X | Y`, not `Union[X, Y]`. A Writer targeting a Django 4.2 codebase must respect `QuerySet` semantics. A Tester for a project with `testpaths = ["tests"]` must write to the right directory. Without the declared truth, the agents guess. |
| **What "fixed" looks like** | Recon reads the top five formats (`pyproject.toml`, `Cargo.toml`, `package.json`, `go.mod`, `pom.xml`) with the appropriate stdlib parser. The artifact's `extensions.declared` field contains a structured object per language: `{requires_python, dependencies, lint_config, type_check_config, test_config}`. The Analyst and Writer prompts receive the relevant subset as variables. |
| **Verify** | A test asserts that a `pyproject.toml` with `requires-python = ">=3.11"` produces `extensions.declared.python.requires_python == ">=3.11"`. A deep test on `psf/requests` shows `extensions.declared.python.dependencies` containing `urllib3`, `certifi`, `chardet`, `idna`. |

---

## Tier 3 — Wiring gaps (Phase 13 unlocks)

These are modern features that work in isolation, are tested, but the deep test does not supply the inputs they need. **The fix is orchestration, not agent code.**

### 3.1 Sandbox adapter for the deep test

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | A `SandboxExecutor` adapter wrapping `(BubblewrapBackend, handle)`. The deep test constructs the backend and handle but does not adapt them to the `SandboxExecutor` protocol. |
| **Unlocks** | 6 features: Tester run_tests, Tester mutation gate, Writer splice-verify, Correctness verifier execution, Contract verifier execution, Security semgrep/bandit. |
| **What "fixed" looks like** | A `BubblewrapExecutorAdapter` class (approximately 20 lines) in `sandtest/` implements `run()` and `write_file()`. The deep test constructs it once per sandbox and passes it to every agent input that accepts `sandbox_executor`. |
| **Verify** | Deep test run shows `[PASS] tester run_tests executed in sandbox`, `[PASS] writer splice-verified candidate`, `[PASS] correctness verifier executed the check`. |

### 3.2 Semgrep and bandit installation in the sandbox

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | The deep test's `step_sandbox_install` installs `pytest` but not `semgrep` or `bandit`. Even with 3.1 fixed, `_binary_present()` would return False and the scanners would silently skip. |
| **What "fixed" looks like** | The install step adds `pip install semgrep bandit` after pytest. Both binaries are on PATH inside the sandbox. |
| **Verify** | `_binary_present(executor, "semgrep")` returns True. The security verifier's `external_findings` field is populated with real semgrep output. |

### 3.3 `ContextBudget` fill-percent tracking

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | `ContextBudget` exists. `tracked_model_call` accepts `context_budget=`. No agent passes one. Fill-percent tracking and compaction never fire. |
| **What "fixed" looks like** | Each agent constructs a `ContextBudget` from `CapabilityProfile.context_window` at the start of `run()` and passes it to every `tracked_model_call`. The budget is queried between calls; at 60% compaction fires; at 85% the agent refuses with `context_overflow` and a snapshot. |
| **Verify** | A test constructs a low-window budget, drives it past 85%, asserts the agent returns `status="refused"` with `snapshot` in the refusal reason. |

### 3.4 `VerifierLoop` invocation

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | `_loop.py` exists, is tested, but no caller uses it. The deep test's apply → verify → rollback flow is linear. |
| **What "fixed" looks like** | The deep test wraps its apply → verify section in `VerifierLoop.run(generate=writer_stage, verify=verify_stage)`. On verifier fail, the writer is re-invoked with `previous_attempt`, `test_failure`, and `previous_diff`. Each round rolls back before the next. Bounded at K=2. |
| **Verify** | A test injects a bad candidate on round 1, confirms the writer is re-invoked, confirms round 2 succeeds or halts at K. |

### 3.5 Skill Curator mining, dedupe, and eval gates

| Field | Value |
|---|---|
| **Phase** | Phase 12 (skill library supplies run history) |
| **Status** | `open` |
| **What is missing** | `SkillCuratorInput` accepts `recent_runs`, `existing_skills`, and `eval_cases`. The deep test passes none of them. Mining is bypassed, dedupe is trivially unique, eval gate is skipped. |
| **What "fixed" looks like** | Phase 12's skill library exposes a `get_recent_runs(count)` and `list_existing_skills()`. The orchestrator passes both to `SkillCuratorInput`. Mining gate fires; proposals cite source runs; dedupe catches duplicates. |
| **Verify** | After three similar runs, the curator proposes a skill citing three `run_id`s. Duplicate names are rejected. |

### 3.6 Evidence columns are not populated by most agents

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | Migration 004 added `tool_chain`, `decision_log`, `model_reasoning`, `evidence_receipt` to `agent_calls`. Only the two sandbox-enabled verifiers pass `evidence_receipt`. **No agent passes `tool_chain`, `decision_log`, or `model_reasoning`.** Three of four columns stay NULL. |
| **What "fixed" looks like** | Every agent that calls tools writes `tool_chain`. Every agent with a pre-check writes `decision_log`. Every agent whose provider returns a reasoning trace writes `model_reasoning`. The `record_call` calls in each agent gain the four arguments. |
| **Verify** | After a deep test run, query `agent_calls` and confirm no row has `tool_chain=NULL` for an agent that called tools. |

### 3.7 Blind prediction fields are computed but not surfaced

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | Verifier results carry `blind_prediction`, `informed_verdict`, and `disagreement`. The deep test's `step_three_verifiers` does not print them. |
| **What "fixed" looks like** | The deep test prints `[INFO] verifier <name>: blind=<X>, informed=<Y>, disagreement=<bool>`. Disagreements are tracked across runs. |
| **Verify** | Deep test output shows the three fields per verifier. A security case with `blind=pass`, `informed=fail`, `disagreement=True` appears when a pre-scan match contradicts a model pass. |

### 3.8 Writer `status="partial"` never exercised

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | The writer supports `status="partial"` for block targets with `requires=("human_approval",)`. The deep test now picks a review-level target, so the partial path never fires. |
| **What "fixed" looks like** | A test in `test_agent_writer.py` constructs a writer input with `blast.recommendation_adjusted="block"` and asserts `status="partial"` and `requires=("human_approval",)`. This test exists — the gap is a *runtime* exercise, not a test. A deep test run with a block target (behind a flag) exercises the path. |
| **Verify** | A `CODEGUARDIAN_DEEP_TARGET=block` flag picks a block-level target and confirms the writer returns partial rather than refusing. |

---

## Tier 4 — Kernel and engine gaps

These require changes outside Phase 11. They are listed here so the fix-list is complete.

### 4.1 G-16 cross-file blast gap

| Field | Value |
|---|---|
| **Phase** | Future kernel phase |
| **Status** | `open` |
| **Symptom** | Imported function callers called with a bare name (`from utils import foo; foo()`) are not resolved to a Call edge. The blast agent under-reports callers for these targets. |
| **Mitigation in v1.1** | Blast emits `unresolved_call_sites` and `caller_search_complete`. The recommendation shifts one band toward caution when the search is incomplete. |
| **What "fixed" looks like** | The kernel's call graph resolves imported-bare-name calls. A test on `psf/requests` blast over `requests.utils.get_environ_proxies` finds its callers in `sessions.py`. |
| **Verify** | `tests/e2e/test_blast_radius.py::test_blast_on_function_target_imported` flips from xfail to pass. |

### 4.2 `transitive_path` reconstruction

| Field | Value |
|---|---|
| **Phase** | Future kernel phase |
| **Status** | `open` |
| **Symptom** | `RankedCaller.transitive_path` is always empty. The kernel's `compute_blast` API does not expose per-edge graph access. |
| **What "fixed" looks like** | `compute_blast` returns the path from the target to each transitive caller. `RankedCaller.transitive_path` is populated with `[target, intermediate, ..., caller]`. |
| **Verify** | A blast on a multi-hop target exposes the path. |

### 4.3 Decorators missing from `function-body`

| Field | Value |
|---|---|
| **Phase** | Phase 10 or future context provider phase |
| **Status** | `open` |
| **Symptom** | The CPG's `start_byte` for a Python function points at the `def` keyword, not the `@` lines above it. `function-body` returns the function without its decorators. |
| **Why it matters** | A function decorated with `@functools.cache` or `@pytest.mark.parametrize` shows different behavior with the decorator than without. The agent sees the wrong body. |
| **Mitigation** | `code-skeleton` includes decorators for Python. An agent that needs them reads the skeleton. |
| **What "fixed" looks like** | `function-body` returns the function plus its `decorator_list` lines. The CPG `start_byte` for a decorated function points at the first `@`. |
| **Verify** | A test on a decorated function asserts the returned body starts with `@`. |

### 4.4 G-4 cache key does not include kernel version

| Field | Value |
|---|---|
| **Phase** | Phase 3 (blocks Phase 13) |
| **Status** | `open` |
| **Symptom** | The cache key is `(repo_id, commit_hash, cache_type)`. Kernel version is not included. A cache entry produced by kernel version X may be reused by version Y with different parse behavior. |
| **What "fixed" looks like** | The cache key becomes `(repo_id, commit_hash, kernel_version, parser_config_hash, cache_type)`. Old entries become unreachable on kernel upgrade. |
| **Verify** | A test upgrades the kernel version and asserts old cache entries are not returned. |

### 4.5 Type checker integration

| Field | Value |
|---|---|
| **Phase** | Phase 13 or 14 |
| **Status** | `open` |
| **Symptom** | mypy or pyright per language is not run. Type errors in refactored code are caught only by the characterization test and the model verifiers. |
| **What "fixed" looks like** | The correctness verifier runs `mypy` on the target file when Python and mypy is installed. `pyright` for TypeScript. Findings become part of the evidence receipt. |
| **Verify** | A refactor that introduces a type error is caught by the type checker even when the characterization test passes. |

### 4.6 Multi-function refactor support

| Field | Value |
|---|---|
| **Phase** | Phase 13+ |
| **Status** | `open` |
| **Symptom** | The writer sees one function body plus the file's skeleton. Refactors that move logic between functions (extract, inline) need to see both bodies. |
| **What "fixed" looks like** | `WriterInput` gains `neighbor_function_bodies: dict[str, str]` populated by the caller based on the RefactorSpec's change kinds. |
| **Verify** | An extract-function refactor works on a target where the extracted code calls a sibling function. |

---

## Tier 5 — Model and prompt quality

### 5.1 Tester model quality

| Field | Value |
|---|---|
| **Phase** | Phase 14 (real-model evaluation) |
| **Status** | `open` |
| **Symptom** | `qwen3.8-27b` on Groq free tier emits JavaScript literals when writing Python test files. The pipeline handles this correctly (retry, then fallback) but the tester's own output never survives. |
| **Root cause** | Model quality on a specific prompt shape. Not a prompt bug. The prompt now explicitly asks for Python literals; if the model still fails, the model is not capable of the task. |
| **What "fixed" looks like** | The tester's model is either (a) a stronger model, or (b) a model that reliably follows syntax constraints. Tier 5.1 resolution is a routing decision: use `frontier`-tier for tester, not `standard`. |
| **Verify** | Deep test with a stronger tester model shows `[PASS] tester output parses and has assertions` and `[PASS] tester agent test green on old code`. |

### 5.2 Writer no-op avoidance — prompt quality

| Field | Value |
|---|---|
| **Phase** | Phase 11 v1.1.1 |
| **Status** | `fixed-pending-verify` |
| **What was wrong** | The model returned the input function unchanged because "identical observable behavior" was ambiguous. The writer's `_pre_check` accepted it (diff_size=0). |
| **Fix applied** | (1) `_pre_check` rejects `diff_size == 0`; (2) both writer prompts clarify that "identical observable behavior" applies to runtime, not source text. |
| **Verify** | Deep test run: if the model produces a no-op, the writer refuses with `pre_check=1`. If the model produces a real change, `diff_size > 0`. |

### 5.3 Prompt clarifications pending runtime verification

| Field | Value |
|---|---|
| **Phase** | Phase 11 v1.1.1 (verify) |
| **Status** | `fixed-pending-verify` |
| **Summary** | The three prompt clarifications from Tier 1 need a deep-test run to confirm they work with the actual models. If the models still fail, the fix is a model swap, not more prompt engineering. |

---

## Tier 6 — Memory and context architecture

These are not bugs. They are structural choices. Whether to change them depends on whether the product needs the capability.

### 6.1 No cross-run memory

| Field | Value |
|---|---|
| **Phase** | Phase 12 (skill library) and Phase 13 (decision replay) |
| **Status** | `open` by design |
| **What it means** | Each run is stateless. The next run on the same repo re-clones, re-parses, re-runs every agent. It has no knowledge that it has seen this code before. |
| **What "fixed" looks like** | Phase 12's skill library is populated from prior runs. When the Analyst plans a refactor, relevant skills are retrieved and injected into the prompt. Phase 13's decision log is replayable — a run's decisions can be inspected and re-applied. |
| **Verify** | After three similar runs, the fourth run's Analyst prompt contains retrieved skills. A run's decision log can be exported and replayed. |

### 6.2 Cross-agent memory limited to typed dataclasses

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` by design |
| **What it means** | Each agent sees only the typed input dataclass of what came before. The Analyst does not know what Blast rejected. The Tester does not know what Analyst warned about. |
| **Why** | Independence. The 2026 research shows evidence slicing and typed hand-offs produce better verdicts than shared state. |
| **What "fixed" looks like** | `AnalystInput` gains `blast_warnings: tuple[str, ...]` (warnings the blast agent produced). `TesterInput` gains `analyst_warnings: tuple[str, ...]`. The information is additive, not shared. Independence is preserved because each agent still receives its own slice. |
| **Verify** | A test confirms that a blast warning reaches the analyst input and appears in the analyst's prompt. |

### 6.3 Cross-model context loss on model switch

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` by design |
| **What it means** | When the user switches models, the previous model's reasoning trace, confidence, and rejected candidates are not passed to the new model. |
| **What "fixed" looks like** | Migration 004's `model_reasoning` column is populated. When a retry uses a different model, `model_reasoning` from the previous attempt is included in the prompt under a "prior attempt reasoning" section. |
| **Verify** | A test switches models on retry and confirms the new prompt contains the previous model's reasoning. |

### 6.4 Silent tool failures

| Field | Value |
|---|---|
| **Phase** | Phase 11 v1.1.1 (see Tier 2.3) |
| **Status** | `open` |
| **What it means** | Tool failures (return values starting with `"(declaration "` etc.) are not surfaced. The agent produces a degraded output with no signal. |
| **What "fixed" looks like** | See 2.3. Same fix. |

### 6.5 Spec-vs-code verification gap

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What it means** | No verifier checks whether the refactored code actually implements the RefactorSpec. The correctness verifier runs the test. The contract verifier checks assertions. Neither reads the spec. |
| **What "fixed" looks like** | The correctness verifier's input gains `spec: RefactorSpec`. Its pre-scan checks whether every change in the spec appears in the diff. A spec-vs-diff mismatch downgrades a model pass to `uncertain`. |
| **Verify** | A test constructs a diff that omits one required change and asserts the verifier flags the mismatch. |

### 6.6 Internally consistent errors slip through

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` by design |
| **What it means** | If the Analyst produces a spec that is internally consistent but factually wrong (wrong parameter name, missing change), the Tester and Writer trust the spec. The pipeline produces a coherent refactor of the wrong thing. |
| **What "fixed" looks like** | Two mechanisms: (1) the spec-vs-diff check from 6.5 catches missing changes; (2) the Analyst's warning system from 2.3 surfaces upstream tool failures. Neither catches a spec that is plausible and internally consistent. Full prevention requires cross-checking the spec against the CPG, which is Phase 13 work. |
| **Verify** | A test with a deliberately wrong spec (parameter name changed) is caught by the correctness verifier's spec-vs-diff check. |

---

## Tier 7 — Documentation

### 7.1 `_store_context.py` not documented in design

| Field | Value |
|---|---|
| **Phase** | Phase 11 v1.1.1 |
| **Status** | `open` |
| **What is missing** | `_store_context.py` exists but is not mentioned in `Phase_11.md`'s file inventory or design section. |
| **Fix** | Add it to the Agent modules list and describe it in the cross-cutting modules section. |
| **Verify** | `Phase_11.md` mentions `_store_context.py` by name and explains its purpose. |

### 7.2 `test_agents.py` deleted

| Field | Value |
|---|---|
| **Phase** | Phase 11 v1.1 |
| **Status** | `closed` |
| **What was wrong** | The file was 0 bytes — an accidental artifact. |
| **Fix** | Deleted in v1.1 block 1. |

### 7.3 `Phase_11.md` was stale

| Field | Value |
|---|---|
| **Phase** | Phase 11 v1.1 |
| **Status** | `closed` |
| **Fix** | Fully rewritten to reflect v1.1. |

---

## Tier 8 — Deferred design decisions (Phase 13)

### 8.1 Block 2b — `fallback_router.py` wiring

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | The routing primitives from Block 2 (`CapabilityProfile`, `BudgetEnvelope`, `TierRubric`, `ObjectiveEscalation`, `ModelGateway`) are tested but inert. `fallback_router.py` still uses the old fallback-list logic. |
| **Why deferred** | Wiring them requires changing `fallback_router.py`, which is a Phase 8 file. The user chose to defer this to avoid touching Phase 0–10 during Phase 11. |
| **What "fixed" looks like** | `FallbackModelProvider._build_plan` calls `select_profile()` from `routing.py` instead of `_order_models`. `metadata.json` is converted to a `CapabilityProfile` list, or a parallel `profiles.yaml` is introduced. The `BudgetEnvelope` is consulted before every call. `ObjectiveEscalation` drives retries. |
| **Verify** | A test with `max_cost_usd=0.5` runs a pipeline and confirms the total spent does not exceed the cap. A `standard`-tier call at 80% budget downgrades to `cheap`. |

### 8.2 `metadata.json` tier vocabulary mismatch

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | `metadata.json` uses `frontier`/`mid`/`local`. `routing.py` uses `cheap`/`standard`/`frontier`/`apex`. Both use the word "frontier" with different meanings. |
| **What "fixed" looks like** | A single vocabulary. `metadata.json` entries are converted to `CapabilityProfile` with the 4-tier vocabulary. `gpt-oss-120b` ($0.15/Mtok) becomes `standard`; `claude-opus-4-6` ($15/Mtok) becomes `apex`. |
| **Verify** | A test asserts `select_profile` picks `gpt-oss-120b` for a `standard` role and `claude-opus-4-6` for an `apex` role. |

### 8.3 `ProviderGateways` not used

| Field | Value |
|---|---|
| **Phase** | Phase 13 |
| **Status** | `open` |
| **What is missing** | `DirectGateway`, `OpenRouterGateway`, and `LiteLLMGateway` exist but no caller instantiates them. |
| **What "fixed" looks like** | The orchestrator selects a gateway based on user config. A Groq-free-tier user gets `DirectGateway({"groq": adapter})`. An OpenRouter user gets `OpenRouterGateway(adapter, profiles)`. |
| **Verify** | A test switches gateway config and confirms the pipeline uses the selected provider. |

---

## Tier 9 — Real-model evaluation (Phase 14)

These are not defects. They are the metrics that must be measured before the pipeline is declared production-ready.

### 9.1 Four quality metrics

| Field | Value |
|---|---|
| **Phase** | Phase 14 |
| **Status** | `open` |
| **Metrics** | False approval rate (<5%), false refusal rate (<10%), contract assertion recall (>85%), cost per successful refactor (<$0.50 mid, <$2.00 frontier) |
| **Why it matters** | The architecture is proven. Whether it produces good refactors at scale is not measured. |

### 9.2 Ground truth corpus

| Field | Value |
|---|---|
| **Phase** | Phase 14 |
| **Status** | `open` |
| **What is missing** | A curated set of ~50 known refactors (correct and buggy) from SWE-bench Pro, partner repos, and CodeGuardian's own history. Model agreement is not acceptable ground truth. |

### 9.3 Holdout set

| Field | Value |
|---|---|
| **Phase** | Phase 14 |
| **Status** | `open` |
| **What is missing** | ~50 examples not used to tune prompts, versions, or model selection. Held out across the entire Phase 14 evaluation. Any adjustment after seeing holdout results invalidates the holdout. |

---

## Tier 10 — Beyond the current roadmap

These are design directions, not fixes. They would be new products, not new versions.

### 10.1 Full agentic loop with sandbox tool execution

The current design gives the model fixed tools. A different design gives the model a `sandbox.run()` tool and lets it decide what commands to run. That is more autonomous, more expensive, harder to audit, and treats the sandbox as a first-class tool rather than a harness resource. Not for v1.x.

### 10.2 Long-term memory

The current design has no cross-run memory. A memory layer would retain decisions, patterns, and refactoring history across runs. Phase 12's skill library is the closest analogue; a full memory layer is further out.

### 10.3 Cross-harness compatibility

`SKILL.md` is now adopted by 9 of 11 production harnesses. CodeGuardian's Skill Curator emits `SKILL.md`-conformant output. A future version could import skills from other harnesses and export to them. Not in scope today.

### 10.4 Semantic verification beyond characterization tests

The pipeline proves behavior preservation *on the test*. It does not prove *semantic equivalence*. Differential fuzzing, property-based verification, and formal methods are all directions to explore. Not in scope today.

---

## Summary table

| Tier | Count | Blocks v1.1 green? | Blocks Phase 12? | Blocks Phase 13? |
|---|---|---|---|---|
| 1 | 3 | Yes | Yes | Yes |
| 2 | 3 | Yes (for correctness) | Yes | Yes |
| 3 | 8 | No | No | Yes |
| 4 | 6 | No | No | Partial |
| 5 | 3 | Partial | No | No |
| 6 | 6 | No | Partial | Yes |
| 7 | 3 | Yes | Yes | Yes |
| 8 | 3 | No | No | Yes |
| 9 | 3 | No | No | No |
| 10 | 4 | No | No | No |

**Total open items: 42.** Of these:
- **6 close in Phase 11 v1.1.1** (Tiers 1, 2, 5, 7)
- **11 close in Phase 12** (parts of Tiers 3, 6)
- **19 close in Phase 13** (Tiers 3, 4, 6, 8)
- **3 close in Phase 14** (Tier 9)
- **4 remain beyond the roadmap** (Tier 10)

---

## Process rules

When fixing an item:

1. Update its status to `in-progress` on the working copy.
2. Change it to `fixed-pending-verify` when the code change lands.
3. Change it to `closed` only after the verification step passes.
4. Record the commit hash next to the status.
5. Never mark an item `closed` without running its Verify step.

When an item is discovered that is not in this list:

1. Add it under the appropriate tier.
2. If it is a Tier 1 or Tier 2 item, stop current work and address it first.
3. If it is Tier 3 or later, note it and continue.

This document is the source of truth for Phase 11 v1.1's completeness. `Phase_11.md` describes what shipped. This file describes what did not.

---

*End of document.*
