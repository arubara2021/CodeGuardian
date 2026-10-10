# 03 — Requirements

---

## 1. Purpose

This document defines the functional and non-functional requirements for CodeGuardian. It translates the goals and metrics from `02-goals-non-goals-metrics.md` into concrete, testable statements.

Every requirement has an identifier, a statement, and an acceptance criterion. Acceptance criteria are pass/fail. Requirements marked **[v1]** are in scope for the first release. Requirements marked **[v1.5]** or **[v2]** are documented for traceability and deferred.

Language and framework choices are documented separately. Requirements here constrain but do not name them. Where a requirement implies a language property — such as speed, memory safety, or dynamic loading — it is phrased as the property.

---

## 2. How to Read This Document

- **FR** = Functional Requirement. What the system does.
- **NFR** = Non-Functional Requirement. How the system behaves.
- **AC** = Acceptance Criterion. The pass/fail test for the requirement.

The non-functional requirements are organized according to the ISO/IEC 25010 product quality model, which groups software quality into eight characteristics: functional suitability, performance efficiency, compatibility, usability, reliability, security, maintainability, and portability[reference:0]. The security requirements are informed by the NIST Secure Software Development Framework (SSDF), which structures secure development practices into four groups: Prepare the Organization, Protect the Software, Produce Well-Secured Software, and Respond to Vulnerabilities[reference:1].

---

## 3. Functional Requirements

### 3.1 Repository Access

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-R1** | The system must read code from a local filesystem path. | Given a local path, the system reads files without modifying them. |
| **FR-R2** | The system must clone and read code from a GitHub repository. | Given a GitHub URL, the system clones into the sandbox. |
| **FR-R3** | The system must clone and read code from a GitLab repository, including self-hosted instances. | Given a GitLab URL on any host, the system clones into the sandbox. |
| **FR-R4** | The Repository Provider must be read-only by default. | No provider exposes a write or apply method during agent execution. |
| **FR-R5** | The system must clone the full repository before any analysis begins. | The clone completes before the first phase starts. |
| **FR-R6** | The system must never write to the original working tree during agent execution. | After any run, the original tree is byte-identical to before. |
| **FR-R7** | The system must support adding new providers without modifying core code. | A new provider passes contract tests and is loaded via plugin discovery. |
| **FR-R8** | The system must not proceed to the next phase until the previous phase's exit condition is satisfied. | Each phase has a documented entry and exit condition. No phase runs out of order. |
| **FR-R9** | The system must not write any file in the original repository until every phase has passed and the user has explicitly approved. | The Apply Provider is never instantiated during agent execution. It runs only after consent. |
| **FR-R10** | **[v2]** The system must support Bitbucket and Azure DevOps. | Deferred. |

### 3.2 Reconnaissance

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-N1** | The system must detect all languages present in a repository. | Detection covers all languages with a registered parser plugin. |
| **FR-N2** | The system must detect frameworks from imports, manifests, and configuration files. | Framework detection produces evidence for every finding. |
| **FR-N3** | The system must build a Code Property Graph that merges syntax, control flow, and data dependence into a single model. | The graph contains AST, CFG, and PDG edges between nodes[reference:2]. |
| **FR-N4** | The system must build a dependency graph at file level and symbol level. | The graph answers what depends on this and what does this depend on. |
| **FR-N5** | The system must build a hierarchical index mapping directories to files to symbols. | The index answers where a symbol is located. |
| **FR-N6** | The system must extract declared versions from manifests. | Reads configuration and lock files across supported languages. |
| **FR-N7** | The system must run the project's existing test suite in the sandbox as an instrumented smoke test. | Test results are captured. Call frequency, call ordering, and data shape transformations are recorded. |
| **FR-N8** | The system must produce a Truth Report comparing documentation claims against code reality. | Each claim, its evidence, and a suggested correction are listed. |
| **FR-N9** | The system must produce a Drift Report comparing declared, installed, and available versions. | Each drift is listed with evidence. |
| **FR-N10** | The system must produce a deterministic, evidence-backed reconnaissance manifest. | The file validates against its schema. Every fact carries a file and line reference. |
| **FR-N11** | Reconnaissance must be mandatory before any refactor. | No refactor phase executes without a completed reconnaissance manifest. |
| **FR-N12** | The Code Property Graph must be built incrementally. | The first build is full. Subsequent updates after a file change affect only the changed portion. |

### 3.3 Blast Radius Analysis

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-B1** | The system must compute direct callers for any target. | Direct callers are listed with evidence. |
| **FR-B2** | The system must compute transitive callers for any target. | The full backward closure of the call graph is computed. |
| **FR-B3** | The system must detect contract violations, not just type errors. | Callers whose semantic assumptions break are flagged. |
| **FR-B4** | The system must identify coverage gaps for callers on the changed path. | Callers with zero test coverage are listed. |
| **FR-B5** | The system must compute a calibrated blast score for any target. | The score is a weighted severity, not a raw count. |
| **FR-B6** | The system must produce a proceed, review, or block recommendation for every target. | Every target receives a recommendation. No target proceeds without one. |
| **FR-B7** | The system must build a runtime contract map from the instrumented smoke test. | Call frequency, ordering, and data shape transformations are captured. |
| **FR-B8** | The system must not proceed past a block recommendation without explicit user override. | Block recommendations halt the pipeline unless the user overrides. |

### 3.4 Context Gathering

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-C1** | The system must build a minimal Context Pack per task. | The Context Pack contains only the target and its required dependencies. |
| **FR-C2** | The Context Pack must expand to include callers and their contract assertions when the blast score requires it. | High blast scores produce expanded Context Packs. |
| **FR-C3** | The Context Pack must respect a configurable token budget. | The pack never exceeds the budget. Overflow is handled by truncation or summarization. |
| **FR-C4** | The system must not send the full repository to any model. | Run logs show no full-repository payloads. |

### 3.5 Safety Net

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-T1** | The system must generate characterization tests before any refactor. | A test file exists for the target before the refactor phase begins. |
| **FR-T2** | Characterization tests must pass on the unmodified code. | If they fail, the system stops and reports the original code as broken. |
| **FR-T3** | The system must generate caller tests when the blast score requires it. | Callers in the batch have tests covering the changed path. |
| **FR-T4** | The system must generate contract assertions for every caller in the batch. | Every caller has at least one contract assertion. |
| **FR-T5** | Test generation must target the language's native test framework. | Python uses pytest. TypeScript and JavaScript use vitest or jest. Java uses JUnit. Go uses go test. |
| **FR-T6** | The system must not modify the target if the safety net fails. | No diff is produced when the safety net fails. |

### 3.6 Refactor

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-F1** | The system must refactor the target according to an explicit directive. | The directive is captured in the audit log. |
| **FR-F2** | The system must update callers in the same coherent change when the blast score requires it. | High blast scores produce diffs that include caller updates. |
| **FR-F3** | The system must produce a diff, not a mutation. | Output is a patch or diff, never an in-place write to the source. |
| **FR-F4** | The system must self-correct on test failure up to a hard retry limit. | The retry count is logged. Exhaustion stops the loop. |
| **FR-F5** | The system must report honest failure when retries are exhausted. | The failure report includes the error trace and the last attempted diff. |

### 3.7 Verification

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-V1** | Refactored code must be verified against characterization tests in a sandbox. | Tests run in the sandbox. Results are captured. |
| **FR-V2** | The system must prompt per run to verify with a second model. | The prompt appears every run. The default is no. |
| **FR-V3** | When verification is enabled, a Correctness Verifier must run. | The verifier is a fresh model instance with no access to the original or the writer's reasoning. |
| **FR-V4** | When verification is enabled, a Security Verifier must run. | A second fresh instance with a separate lens. |
| **FR-V5** | When verification is enabled, a Contract Verifier must run. | A third fresh instance that checks every caller's contract. |
| **FR-V6** | Verifier verdicts must be recorded in the audit log. | Each verdict includes model identity, context hash, and result. |
| **FR-V7** | **[v2]** Additional verifiers may be added as plugins. | Deferred. |

### 3.8 Consent and Apply

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-A1** | No change is applied without explicit user approval. | The apply step requires interactive confirmation or an explicit apply flag. |
| **FR-A2** | The default mode is dry-run. | Without an apply flag, the system produces a diff and exits. |
| **FR-A3** | Consent must be recorded in the audit log with a timestamp. | The log entry includes the scope of consent and the session identity. |
| **FR-A4** | Applying changes must be atomic and reversible. | A rollback reference is recorded before apply. |
| **FR-A5** | No file in the original repository is written until every phase has passed and the user has approved. | The apply step is the first and only moment the original repository is touched. |

### 3.9 Output

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-O1** | Every run must produce a diff. | The diff is written to the output directory or printed. |
| **FR-O2** | Every run must produce a pull or merge request description. | The description includes a summary, the blast radius report, test evidence, and verifier verdicts. |
| **FR-O3** | Every run must produce a structured audit log entry. | The entry validates against the audit schema. |
| **FR-O4** | The system must support pull or merge request creation on GitHub and GitLab. | Optional. Gated behind explicit user action. |
| **FR-O5** | Output formats must be pluggable. | Adding a format requires no core change. |

### 3.10 Model and Provider Layer

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-M1** | The system must be provider-agnostic across cloud language model APIs. | Supports multiple providers and any OpenAI-compatible endpoint. |
| **FR-M2** | The system must support local models via OpenAI-compatible endpoints. | Verified with at least one local runtime. |
| **FR-M3** | The user must be able to choose the model per role. | The CLI and configuration allow per-role selection. |
| **FR-M4** | The system must not force multi-model routing. | Single-model runs are the default. |
| **FR-M5** | The system must record model identity and version in the audit log for every call. | Log entry per model call. |

### 3.11 MCP Integration

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-MCP1** | The system must consume external MCP tools. | At least two external MCP tools are integrated in v1. |
| **FR-MCP2** | The system must expose itself as an MCP server. | A third-party MCP client can call CodeGuardian's refactor tool. |
| **FR-MCP3** | MCP tool availability must be configurable. | Users can enable or disable individual MCP tools. |

### 3.12 Plugin System

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-P1** | Plugins must be discoverable at runtime without core modification. | Adding a plugin requires only installation, not a code change. |
| **FR-P2** | The system must support plugin types: language, provider, tool, verifier, reporter, sandbox backend. | Each type has a defined interface and contract test. |
| **FR-P3** | Plugins must declare capabilities and permissions via a manifest. | The manifest validates against its schema. Undeclared capabilities are rejected. |
| **FR-P4** | Plugin failures must not crash the core. | Faulty plugins are isolated and reported. |

### 3.13 Sandbox Backends

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-SB1** | The system must support multiple sandbox backends behind a single interface. | At least two backends are implemented. |
| **FR-SB2** | The sandbox backend must be selectable at configuration or CLI level. | The user can choose the backend. |
| **FR-SB3** | All backends must enforce network isolation, CPU quotas, and memory quotas. | Policy tests verify enforcement. |
| **FR-SB4** | The sandbox must be destroyed after each run. | No sandbox persists after the pipeline completes. |
| **FR-SB5** | The workspace must be reset between targets. | A second target does not see the first target's workspace. |

### 3.14 CLI

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-CLI1** | The system must provide a CLI as the primary interface. | All core operations are available from the CLI. |
| **FR-CLI2** | The CLI must render reconnaissance, blast radius, plan, diff, and verification views. | All five views are implemented and readable. |
| **FR-CLI3** | The CLI must warn the user when targeting a best-effort language. | The warning appears before any refactor begins. |
| **FR-CLI4** | The CLI must show a live progress view during long operations. | Progress is visible for reconnaissance, testing, refactoring, and verification. |
| **FR-CLI5** | **[v2]** A full-screen TUI may be provided for interactive exploration. | Deferred. |

---

## 4. Non-Functional Requirements

The non-functional requirements are organized according to the ISO/IEC 25010 product quality model, which defines eight quality characteristics: functional suitability, performance efficiency, compatibility, usability, reliability, security, maintainability, and portability[reference:3].

### 4.1 Maintainability

This is the core maintainability contract. Every item is testable. It exists to ensure that changing one part of the system does not ripple outward, that updates do not break unrelated modules, and that adding extensions never requires modifying the core.

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-MT1** | Every core component must be defined by an abstract interface. | Interfaces exist for provider, language, tool, verifier, reporter, sandbox backend, and orchestrator. |
| **NFR-MT2** | The core must not import concrete implementations. | Static analysis shows zero core-to-plugin imports. |
| **NFR-MT3** | Adding a language, provider, tool, verifier, reporter, or sandbox backend must not require a core change. | Acceptance test: add a dummy plugin. No core files change. |
| **NFR-MT4** | Every interface must have a contract test suite. | Each plugin runs against the shared contract suite. |
| **NFR-MT5** | Interfaces must be semantically versioned. | Each interface declares a version. The core supports the current version and the previous version. |
| **NFR-MT6** | The core must contain no global mutable state. | No module-level mutable singletons. Verified by lint rule. |
| **NFR-MT7** | Every dependency must be injectable. | Core classes accept dependencies via constructor. |
| **NFR-MT8** | Public APIs must be explicit and small. | Every module declares its public surface. |
| **NFR-MT9** | A change to one module must not require changes to unrelated modules. | Locality-of-change test: modify module X. No other module's tests fail. |
| **NFR-MT10** | Imports must not cascade. | Moving or renaming an internal module does not require edits outside it. |
| **NFR-MT11** | Upgrades must be safe. | Updating a plugin or dependency must not break the core. Contract tests catch regressions. |

### 4.2 Performance Efficiency

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-PE1** | Code parsing must be substantially faster than a pure-interpreted baseline. | Benchmark on a moderate repository. |
| **NFR-PE2** | Reconnaissance on a moderate repository must complete within a reasonable interactive window. | Timed on reference hardware. |
| **NFR-PE3** | Blast radius computation must return interactively for any target. | Timed per target. |
| **NFR-PE4** | Refactor of a moderately sized target, including tests and verification, must complete within a reasonable window. | Timed end-to-end. |
| **NFR-PE5** | Sandbox start must be imperceptible on the local backend. | Measured per run. |
| **NFR-PE6** | CLI first response must be immediate. | Measured on interactive commands. |
| **NFR-PE7** | Incremental graph updates must be visibly faster than a full rebuild. | Timed comparison on the same repository. |
| **NFR-PE8** | Cross-boundary calls must be batched per phase, not per operation. | Architecture test verifies batching. |

### 4.3 Compatibility

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-CO1** | The system must run on Linux, macOS, and Windows. | CI matrix covers all three. |
| **NFR-CO2** | The system must interoperate with multiple cloud language model providers. | Verified against at least two providers. |
| **NFR-CO3** | The system must interoperate with local model runtimes via OpenAI-compatible endpoints. | Verified against at least one local runtime. |
| **NFR-CO4** | The system must interoperate with multiple repository providers. | Verified against local, GitHub, and GitLab. |
| **NFR-CO5** | The system must expose and consume the Model Context Protocol. | Verified as both client and server. |

### 4.4 Usability

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-US1** | CLI output must be readable at narrow terminal widths. | No layout breaks below 80 columns. |
| **NFR-US2** | CLI must support no-color and machine-readable output modes. | Both modes verified. |
| **NFR-US3** | Prompts must have safe defaults. | Pressing Enter selects the safe option. |
| **NFR-US4** | The user must be warned at the point of action when a target carries high risk. | Blast radius warnings appear before any change. |
| **NFR-US5** | The system must surface documentation drift without blocking. | The Truth Report is shown. The user may proceed. |

### 4.5 Reliability

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-RE1** | Errors must be typed and categorized. | Error taxonomy is documented and enforced. |
| **NFR-RE2** | Failures must never leave partial mutations. | Atomic apply and rollback are verified. |
| **NFR-RE3** | Failures must produce a clear, actionable report. | The report includes cause, evidence, and suggested next step. |
| **NFR-RE4** | Plugin failures must be isolated from the core. | A faulty plugin does not crash the run. |
| **NFR-RE5** | The system must recover gracefully from a sandbox crash. | A sandbox crash produces a failure report, not a host crash. |
| **NFR-RE6** | The system must not corrupt the audit log on unexpected termination. | The append-only log remains readable and hash-chain valid after a crash. |

### 4.6 Security

The security requirements are informed by the NIST Secure Software Development Framework, which organizes secure development practices into four groups: Prepare the Organization, Protect the Software, Produce Well-Secured Software, and Respond to Vulnerabilities[reference:4].

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-SE1** | Untrusted code must never execute on the host. | All execution goes through the sandbox backend. Verified by policy tests. |
| **NFR-SE2** | The sandbox must be network-isolated except during a controlled dependency-resolution phase. | Network policy is enforced. Verified by tests. |
| **NFR-SE3** | The sandbox must enforce CPU and memory quotas. | Quotas are configurable. Exceeded runs are terminated. |
| **NFR-SE4** | Secrets must never be written to logs or audit entries. | Log redaction is verified by tests. |
| **NFR-SE5** | The sandbox runner must be memory-safe against untrusted input. | The runner is implemented in a memory-safe language. |
| **NFR-SE6** | The system must never mutate real code without consent. | Enforced architecturally. Verified by policy tests. |
| **NFR-SE7** | The audit log must be tamper-evident. | Each entry includes a hash chain link. Modification breaks the chain. |
| **NFR-SE8** | Plugin permissions must be enforced. | A plugin cannot access resources not declared in its manifest. |
| **NFR-SE9** | The system must protect software components from tampering and unauthorized access. | Plugin manifests are validated. Sandbox backends are verified. |

### 4.7 Portability

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-PO1** | The system must run with cloud APIs or fully offline with local models. | Offline mode is verified with a local model. |
| **NFR-PO2** | The sandbox must support language-specific environments. | At least two language environments are supported in v1. |
| **NFR-PO3** | The system must support multiple sandbox backends across platforms. | At least one backend works on each supported operating system. |
| **NFR-PO4** | The skill library must be portable and rebuildable from its source files. | The library is reconstructable from Markdown. |

### 4.8 Functional Suitability

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-FS1** | The system must provide the functions specified in the functional requirements. | Every functional requirement has at least one passing test. |
| **NFR-FS2** | The system must provide accurate results for the functions it performs. | Caller resolution, contract detection, and blast scoring meet their accuracy criteria. |
| **NFR-FS3** | The system must provide complete results for the functions it performs. | Every target receives a blast radius report. Every run produces a diff, a description, and an audit entry. |

---

## 5. Traceability

Requirements with strong language implications are traced to the technology stack document.

| Requirement | Property Implied | Resolved In |
|---|---|---|
| **NFR-PE1** | Substantially faster parsing than a pure-interpreted baseline | Technology stack: compiled parser |
| **NFR-SE5** | Memory-safe sandbox runner | Technology stack: memory-safe language |
| **NFR-MT3** | Runtime plugin discovery | Technology stack: dynamic loading support |
| **NFR-MT6** | No global mutable state | Technology stack: language with strong ownership model |
| **NFR-PE8** | Batched cross-boundary calls | Architecture: bridge design |
| **NFR-PE7** | Incremental graph updates | Architecture: Code Property Graph construction |
| **FR-M2** | OpenAI-compatible local model endpoints | Architecture: provider abstraction |

---

## 6. Related Documents

- `01-project-overview.md` — vision, problem, positioning, and core guarantees.
- `02-goals-non-goals-metrics.md` — goals, non-goals, success metrics, and anti-metrics.
- `04-architecture.md` — system design that satisfies these requirements.
- `05-data-model.md` — schemas for the reconnaissance manifest, blast radius report, audit log, and context pack.
- `07-tech-stack.md` — language and framework decisions.
- `08-repository-structure.md` — modularity conventions.
- `10-testing-cicd-deployment.md` — how requirements are verified in CI.
- `13-risks-assumptions-decisions.md` — decision log.
