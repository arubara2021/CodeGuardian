# CodeGuardian — Phase 10: Prompts

**Snapshot date:** 2026-10-25
**Status:** Complete
**Test count:** 884 passing, 10 skipped, 7 xfailed, 5 xpassed
**Previous phase boundary:** Phase 9 + Phase 7 hardening + Phase 10 infrastructure

---

## 1. What Phase 10 is

Phase 10 delivers the seven prompt files every LLM role loads before
calling a model. It also delivers the infrastructure the prompts depend
on: a strict template renderer, per-role output schemas, a version-aware
loader, and a golden-case evaluation harness.

The BUILD_ORDER original plan was seven files, one per role, with a
single phase-boundary test. That plan did not survive first contact with
the design questions. A prompt whose output is not machine-actionable is
decorative. A prompt that cannot refuse a task the model cannot complete
is a hallucination generator. A prompt whose base rules live in seven
duplicated places is unmaintainable.

Phase 10 shipped twenty-four files, not seven. The gap is the
infrastructure that makes the seven prompts usable and testable.

---

## 2. What the BUILD_ORDER said vs what shipped

| Original plan | Actual delivery |
|---|---|
| `prompts/{analyst,tester,writer,correctness_verifier,security_verifier,contract_verifier,skill_curator}/v1.0.0.md` | Seven role prompts plus a versioned `_base/v1.0.0.md` holding rules every role inherits |
| Phase-boundary test: "every prompt renders with test variables" | Six infrastructure files, ten per-role test files, three golden-case YAML files, and one eval harness |
| Total: 7 files | Total: 24 files |
| Test count target: unspecified | Test count delivered: 42 new infrastructure tests, 32 + 44 + 62 = 138 per-role tests |

The deviation is authorized in the same sense as Phase 9's merged
scope: the phase could not deliver what the design required without the
infrastructure. The prompts alone would have been a set of markdown
files with no way to prove they render, no way to prove their output
shape, and no way to measure whether a change to a prompt improved or
regressed the role's behavior.

---

## 3. The five deviations, in order of consequence

### Deviation 1 — `_base/v1.0.0.md` is a versioned pair with each role

Every role prompt pairs with a base prompt of the same version. The
loader resolves `(role, version)` by reading both files.

**Why:** the base prompt holds rules that must be identical across all
seven roles: output discipline, authoritative-source rule, refusal
protocol, no-hallucinated-references rule, reasoning budget, training
cutoff reminder, deliberate narrowness reminder. Duplicating those in
seven prompts means seven places to update when a rule changes. A pair
of files makes the shared rules one source of truth.

**Cost:** bumping a role prompt's version requires the same bump on the
base. That is deliberate. The base rules and the role contract are
allowed to change together, but they are not allowed to drift apart.

### Deviation 2 — Strict template renderer with injection defense

The renderer substitutes `{{name}}` placeholders from a mapping. It
rejects three failure modes: missing variables, unbalanced braces, and
values that contain a reference to another variable in the same mapping.

**Why:** the injection check matters most. A variable whose value
contains `{{other_name}}` where `other_name` is also a key would, if
rendered twice, substitute text the template author never declared.
Rejecting is the only safe choice. The check is single-pass: a value
containing `{{unknown}}` where `unknown` is not a key passes through
unchanged, because it cannot cause a substitution.

**Cost:** every prompt that passes a source-code variable (the Tester's
and Writer's `{{characterization_test}}` or `{{diff}}`) must escape any
`{{...}}` sequence in the source before passing it to the renderer. In
practice, source code almost never contains `{{`, but a diff might if
the diff itself is about template literals. That is documented as the
caller's responsibility.

### Deviation 3 — Every role has a `refused` output shape

The three verifiers return JSON with a `verdict` field of `pass`,
`fail`, `uncertain`, or `refused`. The Tester and Writer return either
source code or a single line beginning `REFUSED:`. The Analyst returns
JSON with a `files` array, or an empty array with a `reason` beginning
`refused:`. The Skill Curator returns JSON with `(none)` as the
`skill_name` sentinel when no reusable pattern exists.

**Why:** a prompt that requires a strict shape will make a model try to
squeeze a novel situation into the wrong field, or hallucinate a value
because it cannot say "this does not fit." Refusal is the exit door.
Without it, the strict output traps the model in a box.

**Cost:** every consumer of every role's output must handle the
refusal case. The verifier pipeline treats `refused` as neither pass
nor fail. The Writer's output is checked for the `REFUSED:` prefix
before it is parsed as source. The Analyst's empty `files` array is
treated as a phase-level refusal, not an empty context pack.

### Deviation 4 — Golden-case evaluation harness

`python/codeguardian/core/prompts/eval_harness.py` loads curated test
cases from `prompts/<role>/eval/golden.yaml`, renders the prompt for
each case, calls a model through an injected callable, validates the
model's output against the schema, and compares the verdict against the
expected verdict.

**Why:** a prompt is code. Changing it without measuring its effect is
a hope, not a discipline. The harness measures the effect. It runs
manually — not in CI — because it requires real model calls. The
intended workflow is: change a prompt, run the harness, compare the
pass rate before and after, revert if it went down.

**Cost:** three golden-case YAML files, ten cases each for the three
verifier roles. The cases are curated by hand, not generated. Adding a
case is a manual edit.

### Deviation 5 — Three additional pre-phase items

Phase 10 could not ship the prompts without three things the design
did not explicitly ask for:

- **`_native.PyCPG`** — a Python handle wrapping a deserialized Code
  Property Graph. Without it, every context provider call would
  re-deserialize the graph at 4.4 MB per call, roughly 200 ms per call.
  With a handle, the graph is deserialized once per phase.
- **Three new query methods on the kernel** — `find_declaration`,
  `qualified_name_of`, `declaration_info` — so `PyCPG` can answer
  "what is the source of the function named X in file Y" without
  exposing the raw query surface.
- **`ToolPlugin` gained `action_kind`, `sensitivity`, `is_read_only`** —
  the agent approval layer needs to know what a tool does, not just
  what it takes. Without the fields, a `plan`-mode agent could call a
  write tool.

These are recorded in Phase 10's session as pre-work, not as Phase 10
files. They belong to the same session because the prompts could not be
tested without them.

---

## 4. The twenty-four Phase 10 files

### Infrastructure (4 files)

| File | Purpose |
|---|---|
| `python/codeguardian/core/prompts/renderer.py` | Strict `{{var}}` parser, missing-variable detection, injection guard |
| `python/codeguardian/core/prompts/schema.py` | Per-role output validators (verdict, source, context-pack, skill) |
| `python/codeguardian/core/prompts/versioning.py` | `(role, version)` pair loader with `_base` resolution and frontmatter parsing |
| `python/codeguardian/core/prompts/__init__.py` | Public surface plus `PromptResult` and `render_prompt` |

### Base prompt (1 file)

| File | Purpose |
|---|---|
| `prompts/_base/v1.0.0.md` | Shared rules every role inherits: output discipline, authoritative source, refusal protocol, no hallucinated references, reasoning budget, training cutoff, deliberate narrowness |

### Role prompts (7 files)

| File | Mode | Tools | Output shape |
|---|---|---|---|
| `prompts/analyst/v1.0.0.md` | `plan` | Four read-only | JSON: `files` array + `reason` |
| `prompts/tester/v1.0.0.md` | `auto-edit` | Four read-only | Source code or `REFUSED:` |
| `prompts/writer/v1.0.0.md` | `auto-edit` | Four read-only | Source code or `REFUSED:` |
| `prompts/skill_curator/v1.0.0.md` | `plan` | None | JSON: `skill_name`, `skill_markdown`, `confidence` |
| `prompts/correctness_verifier/v1.0.0.md` | `plan` | None | JSON verdict |
| `prompts/security_verifier/v1.0.0.md` | `plan` | None | JSON verdict |
| `prompts/contract_verifier/v1.0.0.md` | `plan` | None | JSON verdict |

### Golden cases (3 files)

| File | Purpose |
|---|---|
| `prompts/correctness_verifier/eval/golden.yaml` | Ten cases: variable rename, type hints, return-type drift, exception removal, comprehension, side-effect reorder, helper extraction, off-by-one, None-check addition, truncated diff |
| `prompts/security_verifier/eval/golden.yaml` | Ten cases: parameterize SQL, f-string SQL, eval introduction, input validation, password logging, path sanitization, shell injection, CSRF check, crypto upgrade, too-short diff |
| `prompts/contract_verifier/eval/golden.yaml` | Ten cases: returns-none, add default param, remove param, add attribute, exception change, rename with callers, return-type change, keyword-only param, side-effect reorder, contradictory assertions |

### Eval harness (1 file)

| File | Purpose |
|---|---|
| `python/codeguardian/core/prompts/eval_harness.py` | Loads golden cases, runs them through the prompt + injected model callable, validates output, compares against expected verdict, returns an `EvalReport` |

### Tests (8 files)

| File | Count |
|---|---|
| `tests/unit/core/test_prompt_renderer.py` | 42 |
| `tests/unit/core/test_prompt_analyst.py` | 16 |
| `tests/unit/core/test_prompt_skill_curator.py` | 16 |
| `tests/unit/core/test_prompt_tester.py` | 22 |
| `tests/unit/core/test_prompt_writer.py` | 22 |
| `tests/unit/core/test_prompt_correctness_verifier.py` | ~20 |
| `tests/unit/core/test_prompt_security_verifier.py` | ~20 |
| `tests/unit/core/test_prompt_contract_verifier.py` | ~20 |

Total new tests: approximately 178.

---

## 5. Design decisions with rationale

### The base prompt is versioned per role

**Decision:** `prompts/_base/v<version>.md` pairs with `prompts/<role>/v<version>.md`.

**Alternative considered:** a single unversioned base prompt.

**Why versioned:** an unversioned base would silently change every role prompt when edited. A versioned base makes the pairing explicit. Bumping one requires bumping the other, which is the correct friction.

### The renderer rejects injection

**Decision:** a value containing `{{other_name}}` where `other_name` is a key in the same mapping raises `InjectionError`.

**Alternative considered:** allow the substitution and rely on the caller to escape.

**Why reject:** the caller is the pipeline. A pipeline bug that passes an unescaped source code variable to the renderer is a real risk. Rejecting the substitution fails loudly, at render time, rather than silently producing a prompt with an unintended variable expansion.

### Verifiers have no tools

**Decision:** the three verifiers cannot read the original code, the callers, or the test file. They see only the diff and, for the contract verifier, the assertions.

**Alternative considered:** give verifiers the same four tools the other roles have.

**Why no tools:** the independent verification guarantee. A verifier that can read the original code inherits the context the Writer had. The verifier's value is that it does not share the Writer's mental model. Giving it the tools would break that property.

### The Analyst returns an empty array to refuse

**Decision:** an empty `files` array with a `reason` beginning `refused:` is the Analyst's refusal shape.

**Alternative considered:** a `verdict` field like the verifiers have.

**Why empty array:** the Analyst's output is a context pack. A context pack with no files is not "pass" (the Writer has nothing to write), not "fail" (no error occurred), and not "uncertain" (the Analyst has reached a verdict). It is "I cannot do this." An empty array with a reason is the honest shape.

### The eval harness is a manual tool

**Decision:** the golden cases run manually, not in CI.

**Alternative considered:** run them in CI with a canned fake model.

**Why manual:** a canned fake model proves the harness works, not that the prompt works. The test suite already covers the harness with fakes. Real measurement requires real model calls, and real model calls are not deterministic enough for CI.

---

## 6. Test count evolution

| Boundary | Passing | Skipped | Xfailed | Xpassed |
|---|---|---|---|---|
| Phase 9 closeout | 704 | 10 | 7 | 5 |
| After Phase 10 infrastructure (renderer, schema, versioning) | 746 | 10 | 7 | 5 |
| After analyst and skill_curator | 776 | 10 | 7 | 5 |
| After tester and writer | 814 | 10 | 7 | 5 |
| After three verifiers and eval harness | 884 | 10 | 7 | 5 |

**Test command:** `PYTHONPATH=python python3 -m pytest tests/ -q`
**Result:** `884 passed, 10 skipped, 7 xfailed, 5 xpassed`

The 10 skipped and 7/5 xfail/xpass are unchanged from Phase 9. They are the C/C++ declarator gap and the cross-file resolution tests.

---

## 7. Known limitations

### The eval harness needs a model callable

`run_golden(cases, model_callable, ...)` requires a callable that takes a prompt and returns the model's response. Building that callable against a real provider is Phase 11 work — it requires the agent that knows which model to call for which role. Today the harness runs against a fake.

### The golden cases are curated, not generated

Ten cases per verifier is enough to catch regressions in prompt changes. It is not enough to prove the verifier detects all classes of defect. Adding cases is manual work. The pass rate is a signal, not a guarantee.

### The base prompt does not cover every role equally

The base prompt holds rules that apply to all seven roles. Some rules are more relevant to verifiers (independence, no tools) and some to code-producing roles (preservation contract, source output discipline). The base holds the shared subset; each role adds its specific rules.

### `PyCPG` relaxes the "no Python-visible state" rule

The Phase 6 kernel held no Python-visible state. `PyCPG` changes that: Python holds a reference to a deserialized graph. The handle is immutable, exposes six read-only methods, and is discarded at the end of the phase. The relaxation is deliberate and documented. It is not the same as shared mutable state.

### Two of the four context providers have a documented edge

`function-body` does not include decorators on Python functions because the CPG's `start_byte` points at the `def` keyword, not the `@` lines above it. An AI that needs the exact decorator should read the code skeleton (which includes them for Python) or read the file directly.

---

## 8. What Phase 11 inherits

- **Seven tested prompts** ready to load via `render_prompt(role, version, variables)`.
- **Per-role output schemas** ready to validate model output before the pipeline acts on it.
- **A version-aware loader** that pairs each role prompt with its base.
- **A golden-case harness** that Phase 11 can run before promoting a prompt change.
- **The action-kind model** on every tool plugin, so Phase 11 agents can enforce agent-mode gating.
- **The `_native.PyCPG` handle** that Phase 11 agents will hold during their phase and pass to context provider plugins.
- **The `cpg_available` context manager** that sets the handle and clone root for the phase.
- **The `BudgetTracker`** that Phase 11 agents will construct per phase and pass to the fault barrier.

Everything Phase 11 needs to call a model with a prompt and validate the output exists. The missing piece is Phase 11 itself — the seven agent classes that load their prompts, call their models, validate their outputs, and hand their results to the next phase.

---

## 9. Deviations from BUILD_ORDER, recorded

| # | Deviation | Reason |
|---|---|---|
| 1 | Phase 10 grew from 7 files to 24 | Infrastructure required by the prompt design |
| 2 | `_base/v1.0.0.md` added as a versioned pair | Shared rules live in one place |
| 3 | Strict renderer with injection check | Prompt values can contain adversarial content |
| 4 | `refused` output shape for every role | Strict schemas without refusal trap the model |
| 5 | Golden-case harness added | Prompt changes need measurement, not intuition |
| 6 | `_native.PyCPG` added | Without it, context providers re-deserialize per call |
| 7 | Three kernel query methods added | `PyCPG` needs them |
| 8 | `ToolPlugin` gained three fields | The agent approval layer needs action_kind |
| 9 | `ModelOptions` gained `max_reasoning_tokens` | Reasoning models need a separate cap |
| 10 | `context_provider` plugin type added | The four context providers need a home |

All ten are recorded in this document. The next session's `BUILD_STATE.md`
update should reference them.

---

## 10. Related documents

- `docs/summaries/phases-0-to-8.md` — build history through Phase 8
- `docs/summaries/phase-9-design.md` — Phase 9 design settlement
- `docs/summaries/v2-auto-propose.md` — v2 design spec
- `docs/modularity-audit.md` — modular and non-modular decisions
- `BUILD_ORDER.md` — phase plan, Phase 10 section
- `BUILD_STATE.md` — current state and known gaps
- `prompts/_base/v1.0.0.md` — the shared prompt rules
- `python/codeguardian/core/prompts/` — renderer, schema, versioning, eval harness
