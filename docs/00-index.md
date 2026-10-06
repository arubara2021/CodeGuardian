# 00 — Documentation Index

> **Status:** Draft
> **Last updated:** 2026-10-06
> **Related docs:** all documents in this folder

---

## 1. What This Folder Contains

This folder contains the complete documentation set for **CodeGuardian**, an open-source, safety-first refactoring agent that understands any codebase, writes characterization tests before touching anything, verifies every change in an isolated sandbox with independent cross-model review, and produces a tamper-evident audit trail.

The docs are written so that a new developer, reviewer, contributor, or agent can understand the project from documentation alone — no prior conversation required.

---

## 2. How to Read These Docs

There are five reading paths depending on who you are.

| If you are… | Start here | Then read |
|---|---|---|
| **New to the project** | `01-project-overview.md` | `02`, `04`, `12` |
| **Implementing the system** | `04-architecture.md` | `05`, `06`, `07`, `08`, `09` |
| **Reviewing safety and correctness** | `15-verification-architecture.md` | `11`, `14`, `16` |
| **An agent onboarding** | `../skills.md` | Then `00-index.md`, `04`, `08` |
| **A stakeholder or reviewer** | `01-project-overview.md` | `02`, `12`, `13` |

---

## 3. Document Map

### Vision and Scope

| File | What it covers |
|---|---|
| `01-project-overview.md` | What CodeGuardian is, the problem it solves, who it is for, the one-sentence pitch, business value, positioning. |
| `02-goals-non-goals-metrics.md` | Goals, non-goals, success metrics, and anti-metrics. |
| `03-requirements.md` | Functional and non-functional requirements, v1 scope boundaries, explicit out-of-scope items. |

### Design

| File | What it covers |
|---|---|
| `04-architecture.md` | System components, agent roles, phase pipeline, plugin architecture, sandbox backends, language boundary between Python and Rust, data flow, deployment topology. |
| `05-data-model.md` | Entities, relationships, schemas, storage architecture, lifecycle, versioning. |
| `06-api-contracts.md` | Internal and external interfaces: repository providers, model providers, MCP tools, the agent's own CLI and MCP server, error handling, versioning. |
| `07-tech-stack.md` | Languages, frameworks, libraries, and the rationale for each choice. |

### Implementation

| File | What it covers |
|---|---|
| `08-repository-structure.md` | Directory layout, naming conventions, code style, commit conventions, modularity rules. |
| `09-environment-setup.md` | Prerequisites, dependencies, configuration, local development setup. |
| `10-testing-cicd-deployment.md` | Testing strategy (unit, integration, end-to-end, contract), CI pipeline, deployment plan. |

### Operations and Safety

| File | What it covers |
|---|---|
| `11-security-performance-observability.md` | Threat model, sandbox security, performance budgets, logging, tracing, metrics, audit trail. |

### Strategy and Planning

| File | What it covers |
|---|---|
| `12-roadmap.md` | Milestones, task breakdown, dependencies, timeline. |
| `13-risks-assumptions-decisions.md` | Risk register, open questions, decision log with rationale. |

### Differentiation

| File | What it covers |
|---|---|
| `14-model-strategy.md` | Model roles, model selection, provider abstraction, local model support, per-role configuration. |
| `15-verification-architecture.md` | The independent verifiers, behavioral equivalence, self-review failure mitigation, test oracle design. |
| `16-context-engineering.md` | Repo Map, Context Pack, sliding window, retrieval, token budgeting for large codebases. |

### Agent Operating Manual

| File | What it covers |
|---|---|
| `../skills.md` | The operating manual for the agent that builds this project: mission, scope boundaries, glossary, workflow, conventions, quality bar, onboarding order, escalation rules, checklists. |

---

## 4. Conventions Used in These Docs

- **Mermaid** is used for all diagrams — architecture, data flow, entity relationships, sequences, state machines.
- **Tables** are used for comparisons, decision matrices, requirement lists, and component inventories.
- **Open questions** are flagged inline as `> **Open Question:**` so they can be found by search.
- **Decisions** are logged in `13-risks-assumptions-decisions.md` with a date and rationale.
- **Requirement IDs** follow the format `FR-<area><number>` for functional and `NFR-<area><number>` for non-functional.
- **Schema IDs** are versioned independently and published at stable URLs under `codeguardian.dev/schemas/`.

---

## 5. The Project in Brief

CodeGuardian is a multi-phase agent workflow that operates on one target (file, function, or module) at a time. The pipeline runs seven phases:

1. **Reconnaissance** — detects languages, frameworks, dependencies, entrypoints, runs existing tests in a sandbox, and reconciles documentation against reality.
2. **Context Gathering** — reads the target file and builds a minimal Context Pack from the dependency graph.
3. **Safety Net** — writes characterization tests that lock in the current behavior. Verifies the tests pass on the old code before any change.
4. **Refactor** — applies a specific directive to modernize or improve the code.
5. **Sandbox Verification** — runs the new code against the tests inside an isolated sandbox. On failure, reads the error trace, rewrites, and retries up to three times.
6. **Independent Verification** — two verifiers inspect the result. Each is a fresh model instance with no access to the original code or the writer's reasoning.
7. **Output** — produces a diff, a pull or merge request description, and a structured audit log.

Nothing touches the user's real code without explicit consent. The agent never writes to the original working tree. It clones or copies into the sandbox, does its work there, and only applies changes when the user approves.

---

## 6. What Makes the Project Distinct

| Dimension | General coding agent | CodeGuardian |
|---|---|---|
| **Order of operations** | May refactor first, then test | Must write characterization tests first |
| **Safety gate** | Tests are optional | Tests must pass on old code before any change |
| **Isolation** | May run on the host machine | Always runs in an isolated sandbox |
| **Consent** | Writes to the working tree freely | Never touches real code without explicit approval |
| **Scope** | Whole repository, any language | Target-level, any language, tiered reliability |
| **Evidence** | Produces new code | Produces proof behavior did not change |
| **Self-correction** | Retries until it works | Retries at most three times, then stops and reports |
| **Verification** | Same model reviews its own work | Independent verifiers on a different model family |
| **Audit trail** | None | Structured provenance for every run |
| **Cost control** | Unbounded | Model-agnostic, per-run verification prompt |
| **Repository support** | Usually one provider | Local, GitHub, and GitLab |
| **Contribution** | Closed | Fully open source, plugin-based |

---

## 7. The Language and Sandbox Tiers

CodeGuardian parses any language Tree-sitter supports (over 100). It is honest about reliability.

| Tier | Languages | Reliability |
|---|---|---|
| **High** | Python, TypeScript, JavaScript | Production-ready. Full pipeline. |
| **Best-effort** | Java, Go, Rust, C, C++, C#, Ruby, PHP, Kotlin, Swift, Elixir, and others | Supported. Lower success rate. The CLI warns before proceeding. |

Sandbox execution is tiered by deployment context.

| Context | Sandbox Backend | Purpose |
|---|---|---|
| **Local CLI** | Bubblewrap on Linux, Seatbelt on macOS | Fast, lightweight, single-user |
| **Cloud / multi-tenant** | Firecracker microVMs | Hardware-level isolation for untrusted code |
| **CI / Windows / fallback** | Docker | Portability where other backends are unavailable |

---

## 8. The Agent Model

CodeGuardian uses a centralized orchestrator with six specialized agents. Five use language models. One is deterministic.

| Agent | Type | Responsibility |
|---|---|---|
| **Recon Agent** | Deterministic (Tree-sitter) | Detects languages, frameworks, dependencies, entrypoints. |
| **Analyst** | LLM | Reads the target and builds the Context Pack. |
| **Tester** | LLM | Writes characterization tests. |
| **Writer** | LLM | Refactors or implements code according to the directive. |
| **Correctness Verifier** | LLM | Independently checks behavioral equivalence. |
| **Security Verifier** | LLM | Independently checks for introduced vulnerabilities. |
| **Skill Curator** | LLM | Extracts reusable patterns from successful runs, on user approval. |

The default configuration runs all roles on a single model. Cross-model verification is prompted per run and is the recommended setting for production use.

---

## 9. Modes of Operation

| Mode | Starting Point | Safety Net | Status |
|---|---|---|---|
| **Legacy** | Existing code with unknown behavior | Characterization tests | v1 |
| **Greenfield** | A specification or requirement | Specification tests | v1.5 |

Both modes use the same sandbox, verifiers, audit trail, and consent model. Only the prompts and phase configuration differ.

---

## 10. Related Files Outside This Folder

- `../README.md` — project front door: what CodeGuardian is, how to install, how to run.
- `../skills.md` — agent operating manual for anyone building this project.

---

## 11. Document Status

This documentation set is a working draft. It is complete enough to begin implementation from, and it is updated as decisions are made. Each document declares its own status at the top.

The authoritative record of design decisions is `13-risks-assumptions-decisions.md`. When a decision changes, that document is updated first, and any affected document is updated to match.