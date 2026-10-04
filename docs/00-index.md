# CodeGuardian — Documentation Index

This folder contains the complete documentation set for **CodeGuardian**, an autonomous agent that safely refactors legacy Python code by writing characterization tests first, verifying changes in an isolated Docker sandbox, and producing an auditable proof that behavior was preserved.

The docs are written so that a new developer, reviewer, or agent can understand the project from documentation alone — no prior conversation required.

---

## How to read these docs

There are four reading paths depending on who you are:

| If you are…                        | Start here                        | Then read                      |
| ---------------------------------- | --------------------------------- | ------------------------------ |
| **New to the project**             | `01-project-overview.md`          | `02`, `04`, `12`               |
| **Implementing the system**        | `04-architecture.md`              | `05`, `06`, `07`, `08`, `09`   |
| **Reviewing safety & correctness** | `15-verification-architecture.md` | `11`, `14`, `16`               |
| **An agent onboarding**            | `../skills.md`                    | Then `00-index.md`, `04`, `08` |
| **A stakeholder / reviewer**       | `01-project-overview.md`          | `02`, `12`, `13`               |

---

## Document map

### Vision & Scope

| File                            | What it covers                                                                                 |
| ------------------------------- | ---------------------------------------------------------------------------------------------- |
| `01-project-overview.md`        | What CodeGuardian is, the problem it solves, who it's for, one-sentence pitch, business value. |
| `02-goals-non-goals-metrics.md` | Goals, non-goals, and the measurable success metrics for v1.                                   |
| `03-requirements.md`            | Functional and non-functional requirements, v1 scope boundaries, explicit out-of-scope items.  |

### Design

| File                  | What it covers                                                                                      |
| --------------------- | --------------------------------------------------------------------------------------------------- |
| `04-architecture.md`  | System components, agent roles (Analyst, Tester, Writer, Judge), data flow, Mermaid diagrams.       |
| `05-data-model.md`    | Entities, relationships, schema, storage choices — audit log, context pack, repo map, run traces.   |
| `06-api-contracts.md` | GitHub API usage, model provider APIs, the agent's own CLI/API surface, versioning, error handling. |
| `07-tech-stack.md`    | Languages, frameworks, libraries, and the rationale for each choice (Python + Rust hybrid).         |

### Implementation

| File                            | What it covers                                                                    |
| ------------------------------- | --------------------------------------------------------------------------------- |
| `08-repository-structure.md`    | Directory layout, naming conventions, code style, commit conventions.             |
| `09-environment-setup.md`       | Prerequisites, dependencies, configuration (env vars, API keys), local dev setup. |
| `10-testing-cicd-deployment.md` | Testing strategy (unit, integration, e2e), CI pipeline, deployment plan.          |

### Operations & Safety

| File                                       | What it covers                                                                 |
| ------------------------------------------ | ------------------------------------------------------------------------------ |
| `11-security-performance-observability.md` | Sandbox security, performance budgets, logging, tracing, metrics, audit trail. |

### Strategy & Planning

| File                                | What it covers                                                                     |
| ----------------------------------- | ---------------------------------------------------------------------------------- |
| `12-roadmap.md`                     | Milestones (M1–M4), task breakdown, dependencies, timeline.                        |
| `13-risks-assumptions-decisions.md` | Risk register, assumptions, open questions, decision log with dates and rationale. |

### Differentiation

| File                              | What it covers                                                                                     |
| --------------------------------- | -------------------------------------------------------------------------------------------------- |
| `14-model-strategy.md`            | The model cascade: which models for which roles, cost analysis, fallbacks, budget estimates.       |
| `15-verification-architecture.md` | The independent Judge, behavioral equivalence, self-review failure mitigation, test oracle design. |
| `16-context-engineering.md`       | Repo Map, Context Pack, sliding window, RAG, token budgeting for large codebases.                  |

### Agent Operating Manual

| File           | What it covers                                                                                                                                                                        |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `../skills.md` | The operating manual for the agent that builds this project: mission, scope boundaries, glossary, workflow, conventions, quality bar, onboarding order, escalation rules, checklists. |

---

## Convention used in these docs

- **Mermaid** is used for all diagrams (architecture, data flow, ER, sequence).
- **Tables** are used for comparisons, decision matrices, and requirement lists.
- **Open questions** are flagged inline as `> **Open Question:**` so they are easy to grep.
- **Assumptions** are flagged inline as `> **Assumption:**`.
- **Decisions** are logged in `13-risks-assumptions-decisions.md` with a date and rationale.

---

## Status

> **Assumption:** This documentation set is in early draft. Sections marked with open questions or assumptions are pending input. Nothing here should be treated as final until the decision log in `13` is closed for v1.

---

## Related files outside this folder

- `../README.md` — project front door: what CodeGuardian is, how to install, how to run.
- `../skills.md` — agent cookbook for anyone (human or AI) building this project.