# Changelog

All notable changes to the CodeGuardian project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Phase 14: Decoupled UIs (Headless Daemon, Rust TUI, VS Code Extension).
- Phase 15: MCP Server and Reporters.
- Phase 16: Full Assembly and release automation.
- Harness v2: LSP semantic layer, step-level routing, multi-language pipeline.

---

## [1.3.0] - 2026-11-08
### Added
- **Phase 13: Orchestrator (Harness Engine)**
  - A deterministic, graph-walking orchestration engine (`loop.py`) that resolves typed slots against a heterogeneous agent registry.
  - `PhaseGraph` data model to define execution modes (starting with `LEGACY_GRAPH`).
  - A three-layer verification stack (`deterministic_gate` -> `deterministic pre-scan` -> `LLM verifier loop`) with K-scaled retries based on blast radius.
  - A two-tier evidence bus (`EvidenceBus`) separating synchronous hot-tier events from asynchronous cold-tier telemetry.
  - Capability attenuation at dispatch (`capability_attenuation.py`), enforcing the intersection of parent, agent, and skill permissions.
  - `ContextEngine` for bounded run-tree context compilation and 60/85 budget threshold enforcement.
  - New telemetry package (`core/telemetry/`) with `MetricsCollector`, `TraceContext`, and `LangSmithAdapter`.
  - Migration `007_agent_registry.sql`: Creates `registered_agents`, adds a foreign key to `agent_calls`, and adds `checkpoints.heartbeat_at`.

### Changed
- The deep test (`sandtest/deep_test.py`) now drives the `LangGraphOrchestrator` rather than calling agents directly. Test-scoped agents (`_sandbox_verify.py`, `_verifier_agents.py`) fill slots not yet owned by production code.
- `fallback_router.py` extended with `BudgetEnvelope`, `blast_band_override`, and `escalation_signals` (Rule 1 escalation).
- `sqlite_store.py` connection opened with `check_same_thread=False` to support orchestrator's worker-thread dispatch (Rule 1 escalation).

### Fixed
- Latent `isinstance` bug in `deterministic_gate.py` against a non-runtime-checkable Protocol (Rule 1 escalation).
- `phase_gates.py` verifier entry now correctly checks for `sandbox_verify` output, and skill curator exit accepts a refusal as a valid outcome.
- Analyst, Writer, and Tester agents now correctly surface function-body failures to decision logs and handle rate limits/context overflows as graceful refusals.

---

## [1.2.0] - 2026-11-06
### Added
- **Phase 12 v2: Skills Module (Lifecycle-Managed Procedural Memory)**
  - Typed skill graph (`skill_graph_nodes`, `skill_graph_edges`) derived deterministically from Markdown frontmatter.
  - Staged retrieval pipeline (`retrieval.py`) featuring hybrid seed, reverse-weighted PPR, cross-encoder reranking, and budget-aware hydration.
  - Separate negative-knowledge tier (`negative.py`) with explicit adopt/reject protocols.
  - Governance gate (`governor.py`) with a `draft -> trial -> active -> stable -> retired` lifecycle state machine, dedupe, and fitness-driven retirement.
  - Security layer (`security.py`) treating skills as untrusted supply-chain input, with an admission scanner and permission manifest enforcement.
  - Migrations `005_skill_graph.sql` and `006_skill_states.sql`.

### Changed
- `schemas/skill.json` upgraded to v1.1 (additive optional fields, expanded state enum).
- `SkillStore` fixed to use UPDATE-when-exists instead of DELETE-then-INSERT to prevent foreign key violations (Rule 1 escalation).

---

## [1.1.0] - 2026-11-02
### Added
- **Phase 11 v1.1: Agents**
  - Nine typed agents (Recon, Blast, Analyst, Tester, Writer, Correctness/Security/Contract Verifiers, Skill Curator).
  - Bounded verifier repair loop primitive (`VerifierLoop`).
  - `ContextBudget` for model window tracking.
  - Routing primitives (`CapabilityProfile`, `BudgetEnvelope`, `TierRubric`, `ObjectiveEscalation`, `ModelGateway`).
  - Migrations `002` through `004` adding the `agent_calls` table and evidence columns (`tool_chain`, `decision_log`, `model_reasoning`, `evidence_receipt`).
- **Phase 10: Prompts**
  - Strict template renderer with injection defense, version-aware loader, and per-role output schemas.
  - Golden-case evaluation harness.

### Changed
- Deep test successfully completes end-to-end refactoring loop against `psf/requests`.

---

## [1.0.0] - 2026-10-19
### Added
- **Phase 0-9: Core Architecture**
  - **Phase 1 (Foundation):** Error taxonomy, retry engine, configuration system.
  - **Phase 2 (Schemas):** JSON Schema definitions for all boundary artifacts.
  - **Phase 3 (Storage):** WAL-mode SQLite, binary CPG store, Markdown-based skill store, LRU cache.
  - **Phase 4 (Interfaces):** 12 Protocols defining component boundaries.
  - **Phase 5 (Bridges):** PyO3 and JSON-RPC bridges for Python-Rust communication.
  - **Phase 6 (Kernel):** Tree-sitter parsing, AST, CFG, PDG, CPG merge, and query interface.
  - **Phase 7 (Engines):** Blast radius computation, Bubblewrap/Docker sandboxing, hash-chained audit log, policy engine, `rpcd` daemon.
  - **Phase 8 (Providers):** Git providers (Local, GitHub, GitLab), ApplyProvider (consent-gated), Model providers (Anthropic, OpenAI, DeepSeek, OpenAI-compatible), and Python sandbox backends.
  - **Phase 9 (Plugins):** Runtime grammar loading (`tree-sitter` loader), unified plugin discovery, fault barrier with timeouts, and `sys.addaudithook` permission enforcement.
- **Multi-language extension:** Kernel expanded to 8 languages (Python, TypeScript, JavaScript, Java, Go, Rust, C, C++).

---

## [0.1.0] - 2026-10-08
### Added
- Initial repository scaffold.
- Root build configuration (`pyproject.toml`, `Cargo.toml`).
- Project hygiene and documentation configuration.
