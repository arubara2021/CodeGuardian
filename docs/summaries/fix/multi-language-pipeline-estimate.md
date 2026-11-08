# Multi-Language Pipeline — Estimate and Plan

**Snapshot date:** 2026-11-07
**Status:** Design and estimate. Not scheduled. Not started.
**Depends on:** Phase 12 v2 (complete), Phase 13 (orchestrator, not started)
**Delivers:** Pipeline support for every language the kernel parses, up from Python-only today.

---

## 1. Purpose

This document records the estimate, the architecture, and the build plan for extending the Phase 11 agent pipeline from Python-only to every language the kernel supports. It exists because the beast deep test proved, on real production code, that the kernel parses six languages at scale while the pipeline runs on one. That gap is the largest remaining architectural distance in the project.

The document has three audiences:

- A future planner who needs a size and a shape for the work.
- A future engineer who needs the file-by-file breakdown to estimate or split.
- A future reviewer who needs to know what the work explicitly does not do.

---

## 2. Current state

### 2.1 What works today

| Capability | Proven by | Languages |
|---|---|---|
| Kernel parse at production scale | gin (99 files, 242k nodes), vaultkit (118 files, 232k nodes), httpx (61 files, 141k nodes) | Python, Go, TypeScript, JavaScript, Java, Rust |
| Multi-hop blast on same-file chains | Synthetic 3-deep corpus, 6 languages with transitive=2 | Python, TypeScript, JavaScript, Java, Go, Rust |
| Full pipeline end-to-end | httpx 65 of 69 assertions pass, 4 environmental failures | Python only |
| Recon agent | All six repos | Every parsed language |
| Blast agent | httpx pipeline and multi-language chain | Every parsed language |

### 2.2 What does not work today

| Gap | Where | Effect |
|---|---|---|
| Source splicing | `writer.py::_splice_into_file` uses Python `ast.parse` | Writer cannot splice a Go, TypeScript, Java, or Rust function into a file |
| Characterization test generation | `_pipeline.py::_write_tool_characterization` and `tester.py` | Generates pytest files; no equivalent for `go test`, `jest`, `junit`, `cargo test` |
| Fault injection | `_fault_injection.py::inject_behavioral_drift` uses Python AST | Cannot inject drift into non-Python source |
| Test execution | `_pipeline.py` hardcodes `python3 -m pytest` | No per-language test runner invocation |
| Install commands | `_repos.py::RepoSpec` per repo | No `go mod download`, `mvn install`, `cargo build` |
| Prompts | `prompts/tester`, `prompts/writer`, `prompts/security_verifier` | Written for Python examples; no per-language variants |

### 2.3 Why the current design already supports the fix

Phase 4 built `interfaces/language.py::LanguagePlugin`. Phase 9 shipped eight implementations in `plugins_bundled/languages/`. The protocol declares:

```python
class LanguagePlugin(Protocol):
    name: str
    tree_sitter_grammar: str
    test_runner: str
    refactor_rules: list[str]

    def detect(self, path: Path) -> bool: ...
    def parse(self, path: Path) -> AstNode: ...
    def generate_test(self, context: ContextPack) -> str: ...
    def run_tests(self, sandbox: SandboxHandle, test_path: str) -> TestResult: ...
```

The pipeline does not use this interface. It hardcodes Python. The fix is not an architectural change; it is wiring the existing interface into the existing agents.

---

## 3. The three tiers

Three possible scopes, from smallest to largest. Tier C is the target.

| Tier | Languages | Total lines | Duration estimate |
|---|---|---|---|
| **A** | Go only | ~1,260 | 1 phase, ~1 week |
| **B** | Go, TypeScript, JavaScript, Java | ~2,980 | 2 phases, ~2 weeks |
| **C** | All six kernel languages + C++ | ~4,760 | 3 phases, ~3 weeks |

Tier C is the recommendation for the full upgrade. The tiers exist so that if schedule pressure forces a smaller scope, the plan degrades gracefully: Tier A closes the highest-value gap (Go cross-file resolution) at the lowest cost.

The remainder of this document details Tier C.

---

## 4. Tier C detailed breakdown

### 4.1 Total line estimate

| Category | Lines |
|---|---|
| `LanguagePlugin` protocol extensions | 60 |
| Language plugin implementations (7 languages) | 780 |
| Agent refactors | 110 |
| Prompts (5 language groups × 3 roles + shared parameterization) | 1,900 |
| Test templates (5 language groups) | 700 |
| `_repos.py` install commands | 60 |
| New tests | 750 |
| Documentation | 400 |
| **Total** | **4,760** |

Language group counting is five, not seven, because TypeScript and JavaScript share most work and C and C++ share most work:

1. Go
2. TypeScript + JavaScript
3. Java
4. Rust
5. C + C++

### 4.2 Distribution by category

| Category | Share | Notes |
|---|---|---|
| Prompts | 40% | Largest single category. Editorial work, iterative. |
| Language plugins | 16% | Mechanical, predictable. |
| Tests | 16% | Coverage of the new code. |
| Test templates | 15% | Per-language test scaffolding. |
| Agent refactors | 2% | Small one-time change. |
| Documentation | 8% | This document plus usage docs. |
| Protocol extensions | 1% | Small one-time change. |
| Repo configuration | 1% | Per-repo install commands. |

**The single most important planning fact:** prompts are 40% of the total and are the bottleneck. Code is mechanical; prompts are editorial.

---

## 5. Protocol extensions

Four new methods on `LanguagePlugin`. All `interfaces/language.py`.

```python
def splice_function(
    self,
    file_source: str,
    function_name: str,
    new_source: str,
) -> str | None:
    """Return the file source with the named function replaced.

    Returns None when the function is not found or the spliced result
    does not parse. The caller records the None as a splice failure.
    """

def characterization_test_template(
    self,
    module: str,
    function: str,
    args: tuple,
    expected_type: str,
    expected_value_json: str,
) -> str:
    """Return a characterization test file for the given target.

    The returned string must be a complete test file that the language's
    test runner can execute. It must contain at least one test that
    passes on the unmodified code and fails when the function's return
    value differs.
    """

def inject_behavioral_drift(
    self,
    file_source: str,
    function_name: str,
) -> str | None:
    """Return the file source with the function's returns wrapped.

    Returns None when the injection is not possible for the function
    (no return values, unsupported return type). The caller records the
    None as a skip.
    """

def run_tests_command(
    self,
    test_filename: str,
) -> list[str]:
    """Return the argv that runs the given test file.

    Executed inside the sandbox by the pipeline. Examples:
    ["go", "test", "-run", "^Test...", "./..."],
    ["python3", "run_pytest.py", "..."],
    ["cargo", "test", "--test", "..."],
    """
```

Sixty lines total across the interface and docstrings. One-time cost.

---

## 6. Per-language plugin implementations

Each language plugin gains the four methods above. Implementation is smaller than it sounds because the operations are textual, not semantic.

### 6.1 Go

| Method | Lines | Approach |
|---|---|---|
| `splice_function` | 35 | Locate `func <name>(` by regex, walk braces to closing `}`, replace |
| `characterization_test_template` | 40 | Go's `testing.T` is straightforward: `func TestX(t *testing.T) { got := fn(args); if !cmp.Equal(got, want) { t.Errorf(...) } }` |
| `inject_behavioral_drift` | 25 | Wrap return expressions in a helper function call |
| `run_tests_command` | 10 | `["go", "test", "-run", f"^{prefix}", "./..."]` |

Total: **~110 lines**.

### 6.2 TypeScript / JavaScript (shared)

| Method | Lines | Approach |
|---|---|---|
| `splice_function` | 30 | Locate `function <name>(` or `export function <name>(`, walk braces |
| `characterization_test_template` | 45 | `jest` or `vitest` template with `toEqual` |
| `inject_behavioral_drift` | 25 | Wrap returns |
| `run_tests_command` | 10 | `["npx", "jest", test_filename]` |

TypeScript-specific: type annotations in the test template (~20 extra lines).

Total: **~130 lines** (TS + JS share 80%).

### 6.3 Java

| Method | Lines | Approach |
|---|---|---|
| `splice_function` | 40 | Locate method signature, walk braces (Java's braces are balanced but methods can be nested in classes) |
| `characterization_test_template` | 50 | JUnit 5 with `assertEquals` |
| `inject_behavioral_drift` | 30 | Wrap returns in helper call |
| `run_tests_command` | 15 | `["mvn", "test", f"-Dtest={class}"]` |

Total: **~135 lines**.

### 6.4 Rust

| Method | Lines | Approach |
|---|---|---|
| `splice_function` | 40 | Locate `fn <name>(`, walk braces |
| `characterization_test_template` | 50 | `#[test]` attribute with `assert_eq!` |
| `inject_behavioral_drift` | 25 | Wrap returns |
| `run_tests_command` | 10 | `["cargo", "test", f"--test={name}"]` |

Total: **~125 lines**.

### 6.5 C / C++ (shared)

| Method | Lines | Approach |
|---|---|---|
| `splice_function` | 40 | Declarator chain matters here. Locate by function name, walk braces |
| `characterization_test_template` | 50 | Simple `main` with `assert` per function |
| `inject_behavioral_drift` | 30 | Wrap returns |
| `run_tests_command` | 15 | Compile then run |

C++-specific: templates complicate splice but the general pattern (walk braces) still works.

Total: **~135 lines** (C + C++ share 85%).

### 6.6 Plugin subtotal

| Language group | Lines |
|---|---|
| Go | 110 |
| TypeScript + JavaScript | 130 |
| Java | 135 |
| Rust | 125 |
| C + C++ | 135 |
| Shared helper utilities | 145 |
| **Total** | **780** |

The shared helpers (extracting function boundaries, matching braces) account for a substantial fraction. All five language groups reuse them.

---

## 7. Agent refactors

The three Python-specific agents get refactored to call the plugin. Small change; large effect.

| File | Change | Lines |
|---|---|---|
| `writer.py::_splice_into_file` | Replace `ast.parse` with `plugin.splice_function` | 15 |
| `tester.py::_generate_test` | Replace `_pytest_template` with `plugin.characterization_test_template` | 15 |
| `_fault_injection.py::inject_behavioral_drift` | Replace AST walk with `plugin.inject_behavioral_drift` | 10 |
| `_pipeline.py` (test execution) | Replace hardcoded `python3 -m pytest` with `plugin.run_tests_command` | 20 |
| `_pipeline.py` (fault injection) | Pass plugin to fault-injection module | 10 |
| `_pipeline.py` (launcher) | Write a language-appropriate launcher | 20 |
| Plugin registry access throughout | Call `registry.get_language(language)` where hardcoded Python is today | 20 |
| **Total** | | **110** |

The refactor is additive: every method takes a `language` parameter, looks up the plugin, delegates. Existing tests pass because the Python plugin implements the same interface the hardcoded logic did.

---

## 8. Prompts

### 8.1 Structure

The seven prompt roles divide into two groups:

| Prompt type | Roles | Why |
|---|---|---|
| **Parameterized** | Analyst, Correctness Verifier, Contract Verifier, Skill Curator | Reasoning is language-neutral. Examples differ; the model is smart enough to generalize from Python examples to `{{language}}` examples if the structure is clear. |
| **Per-language** | Tester, Writer, Security Verifier | Syntax, idioms, and attack surfaces differ enough that the prompt must be written for the language. |

**Three prompt files per language group.** Five language groups, so **fifteen new prompt files**.

### 8.2 Estimate

| Prompt | Per-language lines | Files | Total |
|---|---|---|---|
| Analyst | Shared prompt + `{{language}}` variable + per-language examples block | 1 shared + 5 example blocks | 200 |
| Tester | 150 per language | 5 | 750 |
| Writer | 150 per language | 5 | 750 |
| Security Verifier | 60 per language (attack surfaces are shorter) | 5 | 300 |
| Correctness Verifier | Shared | — | 0 |
| Contract Verifier | Shared | — | 0 |
| Skill Curator | Shared | — | 0 |
| **Total** | | | **2,000** |

Conservative estimate: **1,900 lines** if the Tester and Writer prompts share structure across related languages (TS/JS, C/C++).

### 8.3 Prompt engineering is the bottleneck

This is the single most important fact for planning:

- A prompt file at 150 lines takes **one hour to write** and **several hours to tune** against real refactors.
- A golden eval set is needed per prompt to measure quality: five roles × five languages × 10 cases = **250 curated golden cases**.
- The eval harness exists (Phase 10). Running it against each new prompt and iterating is the majority of the work.

The line count above does not include the eval cases. Those are another **~300 lines of YAML** per language group, **~1,500 lines total**.

---

## 9. Test templates

Separate from prompts. These are the test files the pipeline writes for the tool characterization (not the tester's characterization).

| Language | Template lines | Notes |
|---|---|---|
| Go | 150 | Includes imports, package clause, assertion helpers |
| TypeScript | 180 | `jest`, imports, `toEqual` |
| JavaScript | 130 | Same as TS, minus types |
| Java | 180 | JUnit 5, imports, class wrapper |
| Rust | 160 | `#[test]`, `assert_eq!`, module wrapper |
| C | 140 | `main`, `assert`, includes |
| C++ | 160 | Same plus namespaces |

**Total: ~1,100 lines**, but with sharing across groups: **~700 lines**.

---

## 10. Repository configuration

`_repos.py` gains install commands per repo:

```python
"gin": RepoSpec(
    ...
    install_commands=("go mod download",),
    test_command=("go", "test", "./..."),
),

"vaultkit": RepoSpec(
    ...
    install_commands=("npm ci",),
    test_command=("npx", "jest"),
),

"logger-internals-java": RepoSpec(
    ...
    install_commands=("mvn", "install", "-DskipTests"),
    test_command=("mvn", "test"),
),

"secs": RepoSpec(
    ...
    install_commands=("cargo", "build",),
    test_command=("cargo", "test"),
),
```

Sixty lines including the new `test_command` field on `RepoSpec`.

---

## 11. What does not change

Explicit list so the scope is not over-read.

| Component | Why unchanged |
|---|---|
| Kernel | Already supports all six languages via `LanguageModule` |
| Recon agent | Delegates to `_native.build_graph`; language-neutral |
| Blast agent | Delegates to `_native.compute_blast`; language-neutral |
| Orchestrator (Phase 13) | Language-neutral by design |
| Audit log | Language-neutral |
| Telemetry | Language-neutral |
| Checkpoints | Language-neutral |
| Consent gate | Language-neutral |
| Verifier infrastructure (evidence slicing, pre-scan, veto) | Language-neutral; only prompts change |
| Skills retrieval (Phase 12 v2) | Language-neutral; skills are keyed by language not implementation |
| Sandbox backends | Language-agnostic; the sandbox runs the runner command the plugin returns |

---

## 12. Build order

Six blocks. Each block is one coherent increment with its own tests.

### Block 1 — Protocol and shared utilities

| File | Purpose |
|---|---|
| `interfaces/language.py` | Add four methods with docstrings |
| `plugins_bundled/languages/_shared.py` | New: brace-walk helpers, function boundary detection |

**Test:** protocol addition compiles; existing eight plugins satisfy the new methods with stubs; the shared helpers have unit tests.

### Block 2 — Python plugin (validation)

Before touching other languages, the Python plugin gets the four methods implemented. This validates that the interface is sufficient.

| File | Purpose |
|---|---|
| `plugins_bundled/languages/python/plugin.py` | Implement four methods by moving logic from `writer.py`, `tester.py`, `_fault_injection.py` |

**Test:** run the existing `httpx` deep test. It should produce identical results to today.

### Block 3 — Agent refactors

| File | Purpose |
|---|---|
| `writer.py` | Call `plugin.splice_function` |
| `tester.py` | Call `plugin.characterization_test_template` |
| `_fault_injection.py` | Call `plugin.inject_behavioral_drift` |
| `_pipeline.py` | Call `plugin.run_tests_command` |

**Test:** `httpx` still produces identical results. The refactor is behavior-preserving by construction.

### Block 4 — Go plugin and prompt

| File | Purpose |
|---|---|
| `plugins_bundled/languages/go/plugin.py` | Implement four methods |
| `plugins_bundled/languages/go/test_template.tmpl` | Test template |
| `prompts/tester/v1.0.0_go.md` | Go tester prompt |
| `prompts/writer/v1.0.0_go.md` | Go writer prompt |
| `prompts/security_verifier/v1.0.0_go.md` | Go security prompt |
| `prompts/_examples/go/` | Per-language examples for parameterized prompts |
| `prompts/*/eval/golden_go.yaml` | Golden cases for Go |

**Test:** run the beast against `gin` with a new pipeline section added. Target: full pipeline succeeds on one or more Go targets.

### Block 5 — TypeScript, JavaScript, Java plugins and prompts

Repetition of Block 4 across three languages.

**Test:** `vaultkit` and `logger-internals-java` with pipeline sections.

### Block 6 — Rust, C, C++ plugins and prompts

Repetition of Block 4 across three languages. C and C++ ship at `best-effort` tier with the G-15 declarator-chain gap documented.

**Test:** `secs` with pipeline section. C/C++ use synthetic fixtures because no real production C/C++ repo under 200 files exists in the corpus.

---

## 13. Testing strategy

### 13.1 Phase-boundary tests

Every block has its own isolation test, chain test, and regression test. Same discipline as Phases 1–12.

### 13.2 Beast sections

Each language repo gains a `pipeline` section in its `RepoSpec`. The beast's `_run_one_repo` gains a check: a repo whose sections include `"pipeline"` must produce at least one full pipeline run through rollback on its primary language.

### 13.3 Golden-case measurement

Every new prompt has a golden eval set. The eval harness runs each prompt against ten curated cases and reports a pass rate. A prompt with a pass rate below 70% is not shipped.

### 13.4 Cross-language proof

A single beast run with `--repo all` (after the tier-C work) proves six languages, seven repos, full pipeline on each. That is the acceptance test.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Prompt quality lags on non-Python languages | Ship language plugins first, run the pipeline with fallback to smoke test only, then iterate prompts |
| Non-Python test frameworks are complicated (JUnit, jest, cargo) | Start with the simplest runner per language; extend later |
| Fault injection is language-specific in subtle ways | Skip injection for languages where it does not reliably catch drift; smoke test is a valid fallback |
| Repair of broken refactors is harder in compiled languages (compile step required to know if the refactor works) | The sandbox's install command compiles the code; if the refactor does not compile, the verification stage catches it |
| C/C++ declarator chain gap (G-15) limits what the pipeline can do | Ship C and C++ at best-effort tier with the gap documented; the pipeline still proves end-to-end on C/C++ but does not claim production readiness |
| Prompt engineering takes more time than expected | Budget the prompt work at two-thirds of the total, not the estimated 40%; tune down after the first language if iteration is expensive |

---

## 15. Dependencies

| Phase | Status | Why it blocks |
|---|---|---|
| Phase 11 (Agents) | Complete | Provides the seven agents the pipeline runs |
| Phase 12 v2 (Skills) | Complete | The Skill Curator writes proposals; retrieval is language-neutral |
| Phase 13 (Orchestrator) | Not started | The pipeline orchestration this work extends is built in Phase 13 |
| Phase 15 (MCP and Reporters) | Not started | Reporters must handle multi-language diffs; not strictly blocking but should land in the same window |

**Phase 13 must come first.** This work extends the pipeline the orchestrator wires up. Doing it before the orchestrator means rewriting against a moving target.

---

## 16. Acceptance criteria

The work is done when:

1. All four `LanguagePlugin` methods are implemented and tested on every bundled language plugin.
2. `writer.py`, `tester.py`, `_fault_injection.py`, and `_pipeline.py` call the plugin interface and have no hardcoded language logic.
3. Each of the five language groups has a Tester prompt, Writer prompt, and Security Verifier prompt, each with a golden eval set that scores at least 70% pass rate.
4. Each of the five language groups has a test template that produces a passing characterization test on unmodified code.
5. The beast runs `--repo gin`, `--repo vaultkit`, `--repo logger-internals-java`, and `--repo secs` with the pipeline section enabled, and each produces at least one full pipeline run through rollback on its primary language.
6. The multi-language test suite (`pytest tests/unit/core/test_language_contract.py`) passes for every plugin with the four new methods tested.
7. Full pytest suite remains green: baseline 1547 passed grows to approximately 1547 + 750 = 2297, with zero regressions.
8. Documentation is updated in `docs/04-architecture.md`, `docs/06-api-contracts.md`, `docs/08A-module-relationships.md`, and `docs/public-api.md`.

---

## 17. What this does not deliver

| Not delivered | Reason |
|---|---|
| Full multi-file refactoring (extract function across files, inline across modules) | Requires whole-program analysis the pipeline does not perform today |
| Type-checker integration (mypy, pyright, tsc, javac static checks) | Separate concern; the compile step catches syntax, not semantics |
| Language-specific linting (gofmt, rustfmt, prettier) | Post-refactor formatting is a Phase 14 concern |
| Per-language security scanning (semgrep, bandit equivalents) | Phase 15 will integrate per-language scanners; the pipeline only invokes the plugin's test runner |
| Framework-aware refactoring (Django templates, React components, Spring beans) | The pipeline handles plain source; framework awareness is future work |
| C# support | ABI incompatibility with tree-sitter 0.24 is unresolved (G-17) |

---

## 18. One-paragraph summary

Extending the pipeline from Python to all six kernel languages costs approximately 4,760 lines across five language groups, of which **40% is prompts, not code**. The plugin interface already exists (Phase 4, Phase 9); the pipeline does not use it. The work is: four protocol methods, five plugin implementations, four small agent refactors, fifteen prompt files, five test templates, and per-repo install commands. The build order proceeds language-by-language so that each language proves the pattern before the next one starts. Prompts are the bottleneck, not code. Phase 13 must land first. The full delivery proves the pipeline end-to-end on gin (Go), vaultkit (TypeScript), logger-internals-java (Java), secs (Rust), and synthetic C/C++ fixtures. That closes the gap between "the kernel parses six languages" and "the pipeline runs on six languages," which is the single largest remaining architectural distance in the project.
