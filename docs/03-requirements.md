# 03 — Requirements

> **Status:** Draft
> **Last updated:** 2026-10-04
> **Owner:** Project lead
> **Related docs:** `01-project-overview.md`, `02-goals-non-goals-metrics.md`, `04-architecture.md`, `07-tech-stack.md`

> **Note:** Language and framework choices are documented in `07-tech-stack.md`. Requirements here constrain but do not name them. Where a requirement implies a language property (speed, memory safety, dynamic loading), it is phrased as the property and traced to `07`.

---

## 1. How to Read This Document

- **FR** = Functional Requirement — what the system does.
- **NFR** = Non-Functional Requirement — how the system behaves.
- Every requirement has an **ID**, a **statement**, and an **acceptance criterion**.
- Acceptance criteria are pass/fail, not subjective.
- Requirements marked **[v1]** are in scope. Requirements marked **[v2]** are documented but deferred.

---

## 2. Functional Requirements

### 2.1 Repository Access (FR-R)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-R1** | The system must read code from a local filesystem path. | Given a local path, the system reads files without modifying them. |
| **FR-R2** | The system must clone and read code from a GitHub repository. | Given a GitHub URL, the system clones into the sandbox. |
| **FR-R3** | The system must clone and read code from a GitLab repository (SaaS and self-hosted). | Given a GitLab URL (any host), the system clones into the sandbox. |
| **FR-R4** | The Repository Provider must be read-only by default. | No provider exposes a write/apply method without an explicit, separate consent step. |
| **FR-R5** | The system must never write to the original working tree. | After any run, the original tree is byte-identical to before. |
| **FR-R6** | The system must support adding new providers without modifying core code. | A new provider passes contract tests and is loaded via plugin discovery. |
| **FR-R7** | **[v2]** The system must support Bitbucket and Azure DevOps. | Deferred. |

### 2.2 Project Reconnaissance (FR-N)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-N1** | The system must detect all languages present in a repository. | Detection covers all languages with a registered parser plugin. |
| **FR-N2** | The system must detect frameworks from imports, manifests, and config files. | Framework detection produces evidence (`file:line`). |
| **FR-N3** | The system must build a dependency graph (file-level and symbol-level). | The graph answers "what depends on X?" and "what does X depend on?" |
| **FR-N4** | The system must build a hierarchical index (directory → file → symbol). | The index answers "where is symbol Y?" |
| **FR-N5** | The system must extract declared versions from manifests. | Reads `pyproject.toml`, `package.json`, `pom.xml`, `go.mod`, etc. |
| **FR-N6** | The system must run the project's existing test suite in the sandbox (smoke test). | Test results are captured; failures do not abort reconnaissance. |
| **FR-N7** | The system must produce a Truth Report comparing doc claims against code reality. | Report lists each claim, its evidence, and a suggested correction. |
| **FR-N8** | The system must produce a Drift Report comparing declared vs. actual vs. available versions. | Report lists each drift with evidence. |
| **FR-N9** | Reconnaissance must output a deterministic, evidence-backed `recon.json`. | The file validates against its schema; every fact carries evidence. |
| **FR-N10** | Reconnaissance must be mandatory before any refactor. | No refactor phase executes without a completed `recon.json`. |

### 2.3 Context Gathering (FR-C)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-C1** | The system must build a minimal Context Pack per task. | Context Pack contains only the target and its required dependencies. |
| **FR-C2** | The Context Pack must respect a configurable token budget. | Pack never exceeds budget; overflow is handled by truncation or summarization. |
| **FR-C3** | The system must not send the full repository to any model. | Run logs show no full-repo payloads. |

### 2.4 Characterization Testing (FR-T)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-T1** | The system must generate characterization tests before any refactor. | A test file exists for the target before the refactor phase begins. |
| **FR-T2** | Characterization tests must pass on the unmodified code. | If they fail, the system stops and reports the original code as broken. |
| **FR-T3** | The system must not modify the target if the safety net fails. | No diff is produced when the safety net fails. |
| **FR-T4** | Test generation must target the language's native test framework. | Python → `pytest`, TS/JS → `vitest`/`jest`, Java → `JUnit`, etc. |

### 2.5 Refactoring (FR-F)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-F1** | The system must refactor the target according to an explicit directive. | Directive is captured in the audit log. |
| **FR-F2** | The system must produce a diff, not a mutation. | Output is a patch/diff, never an in-place write to the source. |
| **FR-F3** | The system must self-correct on test failure, up to 3 retries. | Retry count is logged; 4th failure stops the loop. |
| **FR-F4** | The system must report honest failure when retries are exhausted. | Failure report includes the error trace and the last attempted diff. |

### 2.6 Verification (FR-V)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-V1** | Refactored code must be verified against characterization tests in a sandbox. | Tests run in Docker; results are captured. |
| **FR-V2** | The system must prompt per run: "Verify with a second model? (y/N)". | Prompt appears every run; default is N. |
| **FR-V3** | When verification is enabled, a Correctness Verifier must run. | Verifier is a fresh model instance with no access to the original or writer's reasoning. |
| **FR-V4** | When verification is enabled, a Security Verifier must run. | Second fresh instance, separate lens. |
| **FR-V5** | Verifier verdicts must be recorded in the audit log. | Each verdict includes model ID, context hash, and result. |
| **FR-V6** | **[v2]** Additional verifiers (edge-case, simplicity) may be added as plugins. | Deferred. |

### 2.7 Consent & Apply (FR-A)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-A1** | No change is applied without explicit user approval. | Apply step requires an interactive confirmation or `--apply` flag. |
| **FR-A2** | The default mode is dry-run. | Without `--apply`, the system produces a diff and exits. |
| **FR-A3** | Consent must be recorded in the audit log with a timestamp. | Log entry includes user identity (or local session ID). |
| **FR-A4** | Applying changes must be atomic and reversible. | A rollback reference is recorded before apply. |

### 2.8 Output (FR-O)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-O1** | Every run must produce a diff. | Diff is written to the output directory or printed. |
| **FR-O2** | Every run must produce a PR/MR description. | Description includes summary, test evidence, and verifier verdicts. |
| **FR-O3** | Every run must produce a structured audit log entry. | Entry validates against the audit schema (`05-data-model.md`). |
| **FR-O4** | The system must support PR/MR creation on GitHub and GitLab. | Optional, gated behind explicit user action. |
| **FR-O5** | Output formats must be pluggable (JSON, Markdown, SARIF). | Adding a format requires no core change. |

### 2.9 Model & Provider Layer (FR-M)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-M1** | The system must be provider-agnostic across cloud LLM APIs. | Supports Anthropic, OpenAI, DeepSeek, Google, and OpenAI-compatible endpoints. |
| **FR-M2** | The system must support local models via OpenAI-compatible endpoints. | Verified with Ollama, vLLM, llama.cpp, LM Studio. |
| **FR-M3** | The user must be able to choose the model per role. | CLI/config allows per-role model selection. |
| **FR-M4** | The system must not force multi-model routing. | Single-model runs are the default. |
| **FR-M5** | The system must record model ID and version in the audit log for every call. | Log entry per model call. |

### 2.10 MCP Integration (FR-MCP)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-MCP1** | The system must consume external MCP tools. | At least 2 external MCP tools integrated in v1. |
| **FR-MCP2** | The system must expose itself as an MCP server. | A third-party MCP client can call CodeGuardian's refactor tool. |
| **FR-MCP3** | MCP tool availability must be configurable. | Users can enable/disable individual MCP tools. |

### 2.11 Plugin System (FR-P)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-P1** | Plugins must be discoverable at runtime without core modification. | Adding a plugin requires only installation, not a code change. |
| **FR-P2** | The system must support plugin types: language, provider, tool, verifier, reporter. | Each type has a defined interface and contract test. |
| **FR-P3** | Plugins must declare capabilities and permissions via a manifest. | Manifest validates against schema; undeclared capabilities are rejected. |
| **FR-P4** | Plugin failures must not crash the core. | Faulty plugins are isolated and reported. |

### 2.12 CLI / TUI (FR-CLI)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **FR-CLI1** | The system must provide a CLI as the primary interface. | All core operations are available from the CLI. |
| **FR-CLI2** | The CLI must render reconnaissance, plan, diff, and verification views. | All four views are implemented and readable. |
| **FR-CLI3** | The CLI must warn the user when targeting a best-effort language. | Warning appears before any refactor begins. |
| **FR-CLI4** | The CLI must show a live progress view during long operations. | Progress is visible for recon, testing, refactoring, and verification. |
| **FR-CLI5** | **[v2]** A TUI may be provided for interactive exploration. | Deferred. |

---

## 3. Non-Functional Requirements

### 3.1 Modularity & Extensibility (NFR-M)

This is the core maintainability contract. Every item is testable.

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-M1** | Every core component must be defined by an abstract interface. | Interfaces exist for provider, language, tool, verifier, reporter, orchestrator. |
| **NFR-M2** | The core must not import concrete implementations. | Static analysis shows zero core → plugin imports. |
| **NFR-M3** | Adding a language/provider/tool/verifier/reporter must not require a core PR. | Acceptance test: add a dummy plugin, no core files change. |
| **NFR-M4** | Every interface must have a contract test suite. | Each plugin runs against the shared contract suite. |
| **NFR-M5** | Interfaces must be semantically versioned. | Each interface declares a version; core supports N and N-1. |
| **NFR-M6** | The core must contain no global mutable state. | No module-level mutable singletons; verified by lint rule. |
| **NFR-M7** | Every dependency must be injectable. | Core classes accept dependencies via constructor. |
| **NFR-M8** | Public APIs must be explicit and small. | Every module declares its public surface (`__all__` / `pub`). |
| **NFR-M9** | A change to one module must not require changes to unrelated modules. | Locality-of-change test: modify module X, no other module's tests fail. |
| **NFR-M10** | Imports must not cascade. | Moving or renaming an internal module does not require edits outside it. |
| **NFR-M11** | Upgrades must be safe. | Updating a plugin or dependency must not break the core; contract tests catch regressions. |

### 3.2 Performance (NFR-P)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-P1** | Code parsing must be at least 10x faster than a pure-Python baseline. | Benchmark on a 50k-line repo. *(Traces to `07` — implies a compiled parser.)* |
| **NFR-P2** | Reconnaissance on a 50k-line repo must complete in ≤5 minutes. | Timed on reference hardware. |
| **NFR-P3** | Refactor of a 500-line file (with tests + verification) must complete in ≤10 minutes. | Timed end-to-end. |
| **NFR-P4** | Sandbox cold-start must be ≤15 seconds. | Measured per run. |
| **NFR-P5** | CLI first response must be ≤500 ms. | Measured on interactive commands. |

### 3.3 Security (NFR-S)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-S1** | Untrusted code must never execute on the host. | All execution is inside Docker; verified by policy tests. |
| **NFR-S2** | The sandbox must be network-isolated except during a controlled dependency-resolution phase. | Network policy enforced; verified by tests. |
| **NFR-S3** | The sandbox must enforce CPU and memory quotas. | Quotas configurable; exceeded runs are terminated. |
| **NFR-S4** | Secrets (API keys) must never be written to logs or audit entries. | Log redaction verified by tests. |
| **NFR-S5** | The sandbox runner must be memory-safe against untrusted input. | *(Traces to `07` — implies a memory-safe language for the runner.)* |
| **NFR-S6** | The system must never mutate real code without consent. | Covered by FR-A1, enforced architecturally. |

### 3.4 Observability (NFR-O)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-O1** | Every phase must emit structured logs. | Logs are machine-parseable and correlate by run ID. |
| **NFR-O2** | Every run must produce a structured audit entry. | Validates against the audit schema. |
| **NFR-O3** | Every model call must record prompt hash, model ID, context hash, and output hash. | Present in audit entry. |
| **NFR-O4** | Metrics (duration, retries, cost) must be captured per phase. | Available in the run summary. |
| **NFR-O5** | Logs must be exportable in a standard format (JSONL). | Export verified. |

### 3.5 Portability (NFR-PT)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-PT1** | The system must run on Linux, macOS, and Windows. | CI matrix covers all three. |
| **NFR-PT2** | The system must run with cloud APIs or fully offline with local models. | Offline mode verified with a local model. |
| **NFR-PT3** | The sandbox must support language-specific base images. | At least Python + TypeScript base images in v1. |

### 3.6 Testability (NFR-T)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-T1** | Every core module must have unit tests. | Coverage ≥80% on core. |
| **NFR-T2** | Every interface must have contract tests. | Shared suite runs against all implementations. |
| **NFR-T3** | The full pipeline must have an end-to-end test. | E2E test refactors a sample file end-to-end. |
| **NFR-T4** | Tests must be runnable without network access. | Offline test mode verified. |

### 3.7 Error Handling (NFR-E)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-E1** | Errors must be typed and categorized (user, system, model, sandbox). | Error taxonomy documented and enforced. |
| **NFR-E2** | Failures must never leave partial mutations. | Atomic apply/rollback verified. |
| **NFR-E3** | Failures must produce a clear, actionable report. | Report includes cause, evidence, and suggested next step. |
| **NFR-E4** | Plugin failures must be isolated from the core. | Faulty plugin does not crash the run. |

### 3.8 Cost (NFR-C)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-C1** | Cloud-model cost per 500-line refactor must be ≤$2.00 (single-model default). | Token accounting per run. |
| **NFR-C2** | Cost must be visible in the run summary. | Summary includes token count and estimated cost. |
| **NFR-C3** | Local-model runs must report $0 cloud cost. | Reported as zero. |

### 3.9 Accessibility & UX (NFR-UX)

| ID | Requirement | Acceptance Criterion |
|---|---|---|
| **NFR-UX1** | CLI output must be readable at 80-column terminals. | No layout breaks below 80 cols. |
| **NFR-UX2** | CLI must support `--no-color` and `--json` modes. | Both modes verified. |
| **NFR-UX3** | Prompts must have safe defaults. | Enter alone selects the safe option. |

---

## 4. Traceability Matrix (Preview)

Requirements with strong language implications are traced to `07-tech-stack.md`.

| Requirement | Property Implied | Resolved In |
|---|---|---|
| **NFR-P1** | 10x faster parsing than pure Python | `07` — compiled parser |
| **NFR-S5** | Memory-safe sandbox runner | `07` — memory-safe language |
| **FR-P1** | Runtime plugin discovery | `07` — dynamic loading support |
| **NFR-M6** | No global mutable state | `07` — language with strong ownership model |
| **FR-M2** | OpenAI-compatible local model endpoints | `07` — provider abstraction |

Full matrix in `07-tech-stack.md`.

---

## 5. Open Questions

> **Open Question:** Are the performance targets (NFR-P1–P5) final, or provisional?
> **Open Question:** Is the security verifier's scope defined (SAST-style, dependency check, or pattern-based)?
> **Open Question:** Should the audit log include a cryptographic hash chain in v1, or is structured JSONL sufficient?
> **Open Question:** What is the minimum supported terminal for the CLI (Windows Terminal, iTerm2, etc.)?
> **Open Question:** Are NFR-T1 coverage targets enforced in CI, or advisory?

---

## 6. Assumptions

> **Assumption:** All requirements marked **[v1]** are in scope for the 2027-01-15 ship date.
> **Assumption:** Requirements marked **[v2]** are documented for traceability but not implemented in v1.
> **Assumption:** Acceptance criteria are pass/fail and testable.
> **Assumption:** Language choices are deferred to `07-tech-stack.md`.
> **Assumption:** Contract tests are the enforcement mechanism for the plugin architecture.

---

## 7. Related Documents

- `01-project-overview.md` — vision and positioning
- `02-goals-non-goals-metrics.md` — goals and success metrics
- `04-architecture.md` — how these requirements are realized
- `05-data-model.md` — schemas for `recon.json`, audit log, context pack
- `07-tech-stack.md` — language and framework decisions
- `08-repository-structure.md` — modularity conventions
- `10-testing-cicd-deployment.md` — how requirements are verified in CI
- `13-risks-assumptions-decisions.md` — decision log