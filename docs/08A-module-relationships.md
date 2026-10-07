# 08A — Module Relationships

---

## 1. Purpose

This document defines how every module in CodeGuardian connects to every other module. Where `08-repository-structure.md` says where files live, this document says how they talk to each other.

It answers one question: **"If I change X, what else is affected?"**

The document has seven sections:

1. Python module dependency graph
2. Rust crate dependency graph
3. Interface → Implementation → Contract test map
4. Agent → Data contract map
5. Phase → Component map
6. Bridge map
7. Call flow diagrams

Every relationship is at the module level or interface level. No function signatures. No line numbers. Those change too often. This document stays stable.

---

## 2. Python Module Dependency Graph

The Python layer is organized by responsibility. Dependencies flow in one direction: entry points depend on core, core depends on interfaces, core does not depend on bundled plugins.

```mermaid
flowchart TB
    subgraph Entry["Entry Points"]
        Main[__main__.py]
        CLI[cli/main.py]
    end

    subgraph Interfaces["interfaces/"]
        I_Repo[repository.py]
        I_Apply[apply.py]
        I_Model[model.py]
        I_Sandbox[sandbox.py]
        I_Orch[orchestrator.py]
        I_Agent[agent.py]
        I_Tool[tool.py]
        I_Verifier[verifier.py]
        I_Reporter[reporter.py]
        I_Language[language.py]
        I_MCPC[mcp_client.py]
        I_MCPS[mcp_server.py]
    end

    subgraph Core["core/"]
        Orch[orchestrator/]
        Agents[agents/]
        Providers[providers/]
        Sandbox[sandbox/]
        MCP[mcp/]
        Plugins[plugins/]
        Bridges[bridges/]
        Storage[storage/]
        Reporting[reporting/]
        Config[config/]
        Errors[errors/]
        Telemetry[telemetry/]
    end

    subgraph Bundled["plugins_bundled/"]
        BLang[languages/]
        BTools[tools/]
        BReport[reporters/]
    end

    Main --> CLI
    CLI --> Orch
    CLI --> Config
    CLI --> Reporting

    Orch --> Agents
    Orch --> Providers
    Orch --> Sandbox
    Orch --> MCP
    Orch --> Plugins
    Orch --> Bridges
    Orch --> Storage
    Orch --> Reporting
    Orch --> Config
    Orch --> Telemetry

    Agents --> Bridges
    Agents --> Providers
    Agents --> Storage

    Providers --> I_Repo
    Providers --> I_Model
    Providers --> I_Apply

    Sandbox --> I_Sandbox
    MCP --> I_MCPC
    MCP --> I_MCPS
    Plugins --> I_Language
    Plugins --> I_Tool
    Plugins --> I_Verifier
    Plugins --> I_Reporter

    Orch --> I_Orch
    Agents --> I_Agent

    Plugins -.->|loads at runtime| Bundled
    Bridges --> Errors
    Storage --> Errors
    Orch --> Errors
```

**Enforced rules:**

| Rule | What It Means |
|---|---|
| **Core never imports bundled plugins** | `core/` has zero imports from `plugins_bundled/`. Plugins are loaded at runtime via entry points. |
| **Interfaces have no dependencies** | `interfaces/` imports nothing from `core/` or `plugins_bundled/`. |
| **Bridges are the only way to Rust** | Nothing in `core/` calls Rust directly. All Rust access goes through `bridges/`. |
| **Errors is a leaf** | `errors/` imports nothing from `core/`. Everything can import from `errors/`. |
| **Config is a leaf** | `config/` imports nothing from `core/`. Everything can import from `config/`. |

### 2.1 Core Module Internal Dependencies

Inside `core/`, dependencies flow from high-level orchestration to low-level utilities.

```mermaid
flowchart TB
    subgraph High["High-Level"]
        Orch[orchestrator/]
        Agents[agents/]
    end

    subgraph Mid["Mid-Level"]
        Providers[providers/]
        Sandbox[sandbox/]
        MCP[mcp/]
        Plugins[plugins/]
        Reporting[reporting/]
    end

    subgraph Low["Low-Level"]
        Bridges[bridges/]
        Storage[storage/]
    end

    subgraph Leaf["Leaf"]
        Config[config/]
        Errors[errors/]
        Telemetry[telemetry/]
    end

    Orch --> Agents
    Orch --> Providers
    Orch --> Sandbox
    Orch --> MCP
    Orch --> Plugins
    Orch --> Reporting
    Agents --> Providers
    Agents --> Bridges
    Agents --> Storage
    Providers --> Config
    Providers --> Errors
    Sandbox --> Errors
    MCP --> Errors
    Plugins --> Errors
    Bridges --> Errors
    Bridges --> Telemetry
    Storage --> Errors
    Reporting --> Errors
    Orch --> Telemetry
    Orch --> Config
```

**Rule:** Dependencies flow downward. A low-level module never imports a high-level module.

---

## 3. Rust Crate Dependency Graph

The Rust layer is a workspace with seven crates. Each crate has one responsibility. Dependencies flow inward.

```mermaid
flowchart TB
    subgraph Bindings["Bindings Layer"]
        PyO3[codeguardian-pyo3]
    end

    subgraph Core["Core Crates"]
        Kernel[codeguardian-kernel]
        Blast[codeguardian-blast]
        Sandbox[codeguardian-sandbox]
        Audit[codeguardian-audit]
        Policy[codeguardian-policy]
    end

    subgraph Shared["Shared Layer"]
        RPC[codeguardian-rpc]
    end

    subgraph External["External Dependencies"]
        TreeSitter[tree-sitter]
        Tokio[tokio]
        Serde[serde]
        PyO3Lib[pyo3]
        Flatgraph[flatgraph-style layout]
    end

    PyO3 --> Kernel
    PyO3 --> Blast
    PyO3 --> Policy
    PyO3 --> PyO3Lib

    Kernel --> TreeSitter
    Kernel --> Flatgraph

    Blast --> Kernel

    Sandbox --> Tokio
    Sandbox --> Serde

    Audit --> Tokio
    Audit --> Serde

    Policy --> Serde

    RPC --> Serde

    Sandbox --> RPC
    Audit --> RPC
```

### 3.1 Crate Responsibilities

| Crate | Responsibility | Depends On | Used By |
|---|---|---|---|
| **codeguardian-kernel** | Tree-sitter parsing, CPG construction, incremental updates | tree-sitter, flatgraph layout | blast, pyo3 |
| **codeguardian-blast** | Caller resolution, contract detection, blast score | kernel | pyo3 |
| **codeguardian-sandbox** | Sandbox backends, warm pool, quotas | tokio, serde, rpc | rpc consumers |
| **codeguardian-audit** | Append-only log, hash chain | tokio, serde, rpc | rpc consumers |
| **codeguardian-policy** | Policy enforcement, permissions | serde | pyo3 |
| **codeguardian-rpc** | JSON-RPC protocol | serde | sandbox, audit |
| **codeguardian-pyo3** | PyO3 bindings | pyo3, kernel, blast, policy | Python layer |

### 3.2 Crate ↔ Python Module Mapping

| Rust Crate | Python Bridge | Python Consumer |
|---|---|---|
| codeguardian-kernel | PyO3 | `core/bridges/pyo3_bridge.py` |
| codeguardian-blast | PyO3 | `core/bridges/pyo3_bridge.py` |
| codeguardian-policy | PyO3 | `core/bridges/pyo3_bridge.py` |
| codeguardian-sandbox | JSON-RPC | `core/bridges/jsonrpc_bridge.py` |
| codeguardian-audit | JSON-RPC | `core/bridges/jsonrpc_bridge.py` |
| codeguardian-rpc | Shared protocol | Both bridges |
| codeguardian-pyo3 | Built into the Python package | Loaded at import |

---

## 4. Interface → Implementation → Contract Test Map

Every interface has at least one implementation and one contract test. This table shows the full mapping.

```mermaid
flowchart LR
    subgraph Interfaces["interfaces/"]
        I1[repository.py]
        I2[apply.py]
        I3[model.py]
        I4[sandbox.py]
        I5[orchestrator.py]
        I6[agent.py]
        I7[tool.py]
        I8[verifier.py]
        I9[reporter.py]
        I10[language.py]
        I11[mcp_client.py]
        I12[mcp_server.py]
    end

    subgraph Impl["core/ Implementations"]
        Impl1[providers/local_repository.py<br/>providers/github_repository.py<br/>providers/gitlab_repository.py]
        Impl2[providers/apply_provider.py]
        Impl3[providers/anthropic_model.py<br/>providers/openai_model.py<br/>providers/deepseek_model.py<br/>providers/openai_compatible.py]
        Impl4[sandbox/bubblewrap_backend.py<br/>sandbox/seatbelt_backend.py<br/>sandbox/firecracker_backend.py<br/>sandbox/docker_backend.py]
        Impl5[orchestrator/langgraph_orchestrator.py]
        Impl6[agents/recon.py<br/>agents/blast.py<br/>agents/analyst.py<br/>agents/tester.py<br/>agents/writer.py<br/>agents/correctness_verifier.py<br/>agents/security_verifier.py<br/>agents/contract_verifier.py<br/>agents/skill_curator.py]
        Impl7[plugins_bundled/tools/*]
        Impl8[plugins_bundled/verifiers/*]
        Impl9[reporting/json_reporter.py<br/>reporting/sarif_reporter.py<br/>reporting/markdown_reporter.py]
        Impl10[plugins_bundled/languages/python/<br/>plugins_bundled/languages/typescript/]
        Impl11[mcp/client.py]
        Impl12[mcp/server.py]
    end

    subgraph Tests["tests/contracts/"]
        T1[test_repository_contract.py]
        T2[test_apply_contract.py]
        T3[test_model_contract.py]
        T4[test_sandbox_contract.py]
        T5[test_orchestrator_contract.py]
        T6[test_agent_contract.py]
        T7[test_tool_contract.py]
        T8[test_verifier_contract.py]
        T9[test_reporter_contract.py]
        T10[test_language_contract.py]
        T11[test_mcp_client_contract.py]
        T12[test_mcp_server_contract.py]
    end

    I1 --> Impl1
    I2 --> Impl2
    I3 --> Impl3
    I4 --> Impl4
    I5 --> Impl5
    I6 --> Impl6
    I7 --> Impl7
    I8 --> Impl8
    I9 --> Impl9
    I10 --> Impl10
    I11 --> Impl11
    I12 --> Impl12

    Impl1 --> T1
    Impl2 --> T2
    Impl3 --> T3
    Impl4 --> T4
    Impl5 --> T5
    Impl6 --> T6
    Impl7 --> T7
    Impl8 --> T8
    Impl9 --> T9
    Impl10 --> T10
    Impl11 --> T11
    Impl12 --> T12
```

### 4.1 Full Mapping Table

| Interface | Implementations | Contract Test |
|---|---|---|
| `RepositoryProvider` | `LocalProvider`, `GitHubProvider`, `GitLabProvider` | `test_repository_contract.py` |
| `ApplyProvider` | `ApplyProvider` | `test_apply_contract.py` |
| `ModelProvider` | `AnthropicProvider`, `OpenAIProvider`, `DeepSeekProvider`, `OpenAICompatibleProvider` | `test_model_contract.py` |
| `SandboxBackend` | `BubblewrapBackend`, `SeatbeltBackend`, `FirecrackerBackend`, `DockerBackend` | `test_sandbox_contract.py` |
| `Orchestrator` | `LangGraphOrchestrator` | `test_orchestrator_contract.py` |
| `Agent` | `Recon`, `Blast`, `Analyst`, `Tester`, `Writer`, `CorrectnessVerifier`, `SecurityVerifier`, `ContractVerifier`, `SkillCurator` | `test_agent_contract.py` |
| `ToolPlugin` | Bundled tools, user-installed tools | `test_tool_contract.py` |
| `VerifierPlugin` | Bundled verifiers, user-installed verifiers | `test_verifier_contract.py` |
| `ReporterPlugin` | `JSONReporter`, `SARIFReporter`, `MarkdownReporter` | `test_reporter_contract.py` |
| `LanguagePlugin` | `PythonPlugin`, `TypeScriptPlugin`, user-installed plugins | `test_language_contract.py` |
| `MCPClient` | `MCPClient` | `test_mcp_client_contract.py` |
| `MCPServer` | `MCPServer` | `test_mcp_server_contract.py` |

**Rule:** Adding a new implementation requires it to pass the existing contract test. No exceptions. This is what keeps the plugin architecture honest.

---

## 5. Agent → Data Contract Map

Every agent reads specific schemas and produces specific schemas. This table shows the full data flow per agent.

```mermaid
flowchart LR
    subgraph Schemas["Schemas"]
        S1[recon.json]
        S2[runtime-contract-map.json]
        S3[blast-radius.json]
        S4[context-pack.json]
        S5[contract-assertions.json]
        S6[diff.patch]
        S7[verification.json]
        S8[audit-entry.json]
        S9[skill.md]
        S10[checkpoint.json]
    end

    subgraph Agents["Agents"]
        A_Recon[Recon Agent]
        A_Blast[Blast Radius Agent]
        A_Analyst[Analyst]
        A_Tester[Tester]
        A_Writer[Writer]
        A_CV[Correctness Verifier]
        A_SV[Security Verifier]
        A_CtV[Contract Verifier]
        A_Curator[Skill Curator]
    end

    A_Recon -->|produces| S1
    A_Recon -->|produces| S2

    S1 --> A_Blast
    S2 --> A_Blast
    A_Blast -->|produces| S3

    S1 --> A_Analyst
    S3 --> A_Analyst
    A_Analyst -->|produces| S4

    S4 --> A_Tester
    A_Tester -->|produces| S5

    S4 --> A_Writer
    S5 --> A_Writer
    A_Writer -->|produces| S6

    S6 --> A_CV
    A_CV -->|produces| S7

    S6 --> A_SV
    A_SV -->|produces| S7

    S6 --> A_CtV
    S5 --> A_CtV
    A_CtV -->|produces| S7

    S6 --> A_Curator
    S7 --> A_Curator
    A_Curator -->|produces| S9

    A_Recon -->|writes| S8
    A_Blast -->|writes| S8
    A_Analyst -->|writes| S8
    A_Tester -->|writes| S8
    A_Writer -->|writes| S8
    A_CV -->|writes| S8
    A_SV -->|writes| S8
    A_CtV -->|writes| S8
```

### 5.1 Agent Input/Output Table

| Agent | Reads | Produces | Writes to Audit |
|---|---|---|---|
| **Recon Agent** | Repository | `recon.json`, `runtime-contract-map.json`, CPG metadata | Yes |
| **Blast Radius Agent** | `recon.json`, `runtime-contract-map.json`, target | `blast-radius.json` | Yes |
| **Analyst** | `recon.json`, `blast-radius.json`, target | `context-pack.json` | Yes |
| **Tester** | `context-pack.json` | `contract-assertions.json`, test files | Yes |
| **Writer** | `context-pack.json`, `contract-assertions.json`, directive | `diff.patch` | Yes |
| **Correctness Verifier** | `diff.patch` only | `verification.json` (correctness) | Yes |
| **Security Verifier** | `diff.patch` only | `verification.json` (security) | Yes |
| **Contract Verifier** | `diff.patch`, `contract-assertions.json` | `verification.json` (contract) | Yes |
| **Skill Curator** | `diff.patch`, `verification.json`, run trajectory | `SKILL.md` proposals | Yes |

**Rule:** Verifiers read the diff only. They never read the original file, the Context Pack, or the Writer's reasoning. This is the independence guarantee.

---

## 6. Phase → Component Map

Each of the eight phases activates a specific set of components. Components not listed are dormant during that phase.

```mermaid
flowchart TB
    subgraph P0["Phase 0 — Reconnaissance"]
        P0_R[Recon Agent]
        P0_K[Code Intelligence Kernel]
        P0_C[CPG]
        P0_S[Sandbox Engine]
        P0_A[Audit Writer]
    end

    subgraph P1["Phase 1 — Blast Radius"]
        P1_B[Blast Radius Agent]
        P1_Bl[Blast Radius Engine]
        P1_C[CPG]
        P1_A[Audit Writer]
    end

    subgraph P2["Phase 2 — Context"]
        P2_A[Analyst]
        P2_M[Model Provider]
        P2_Au[Audit Writer]
    end

    subgraph P3["Phase 3 — Safety Net"]
        P3_T[Tester]
        P3_M[Model Provider]
        P3_S[Sandbox Engine]
        P3_Au[Audit Writer]
    end

    subgraph P4["Phase 4 — Refactor"]
        P4_W[Writer]
        P4_M[Model Provider]
        P4_Au[Audit Writer]
    end

    subgraph P5["Phase 5 — Sandbox Verify"]
        P5_S[Sandbox Engine]
        P5_Au[Audit Writer]
    end

    subgraph P6["Phase 6 — Independent Verify"]
        P6_CV[Correctness Verifier]
        P6_SV[Security Verifier]
        P6_CtV[Contract Verifier]
        P6_M[Model Provider]
        P6_Au[Audit Writer]
    end

    subgraph P7["Phase 7 — Output"]
        P7_R[Report Generator]
        P7_Au[Audit Writer]
        P7_St[Storage]
    end
```

### 6.1 Full Phase Component Table

| Phase | Active Agents | Active Engines | Bridges Used |
|---|---|---|---|
| **0 — Reconnaissance** | Recon | Code Intelligence Kernel, Sandbox Engine | PyO3, JSON-RPC |
| **1 — Blast Radius** | Blast Radius | Blast Radius Engine | PyO3 |
| **2 — Context** | Analyst | — | — |
| **3 — Safety Net** | Tester | Sandbox Engine | JSON-RPC |
| **4 — Refactor** | Writer | — | — |
| **5 — Sandbox Verify** | — | Sandbox Engine | JSON-RPC |
| **6 — Independent Verify** | Correctness, Security, Contract Verifiers | — | — |
| **7 — Output** | Report Generator | — | JSON-RPC (audit) |

### 6.2 Phase Gate Enforcement

Each phase has an entry condition and an exit condition. The orchestrator enforces them.

```mermaid
flowchart LR
    P0[Phase 0] -->|recon.json + CPG| P1[Phase 1]
    P1 -->|blast-radius.json| P2[Phase 2]
    P2 -->|context-pack.json| P3[Phase 3]
    P3 -->|tests pass on old code| P4[Phase 4]
    P4 -->|diff.patch| P5[Phase 5]
    P5 -->|tests pass in sandbox| P6[Phase 6]
    P6 -->|verifier verdicts| P7[Phase 7]
    P7 -->|user consent| Apply[Apply]
```

No phase runs until the previous phase's exit condition is satisfied.

---

## 7. Bridge Map

Two bridges connect Python and Rust. The choice is determined by trust and isolation requirements.

```mermaid
flowchart TB
    subgraph Python["Python Layer"]
        Orch[Orchestrator]
        Agents[Agent Runtime]
        Storage[Storage]
    end

    subgraph PyO3["PyO3 Bridge"]
        PB1[In-process]
        PB2[Batched per phase]
        PB3[Zero-copy data transfer]
        PB4[GIL released during Rust calls]
    end

    subgraph JSONRPC["JSON-RPC over stdio"]
        JB1[Out-of-process]
        JB2[Isolated]
        JB3[Survives Python compromise]
        JB4[Batched per phase]
    end

    subgraph Rust["Rust Layer"]
        Kernel[Code Intelligence Kernel]
        Blast[Blast Radius Engine]
        Policy[Policy Engine]
        Sandbox[Sandbox Engine]
        Audit[Audit Log Writer]
    end

    Orch --> PyO3
    Agents --> PyO3
    PyO3 --> Kernel
    PyO3 --> Blast
    PyO3 --> Policy

    Orch --> JSONRPC
    JSONRPC --> Sandbox
    JSONRPC --> Audit
```

### 7.1 Bridge Selection Table

| Zone | Bridge | Why | Batching Rule |
|---|---|---|---|
| **Code Intelligence Kernel** | PyO3 | Trusted, high-volume, latency-sensitive | Batch per phase. Parse 500 files in one call, not 500 calls. |
| **Blast Radius Engine** | PyO3 | Trusted, high-volume, latency-sensitive | Batch per query type. |
| **Policy Engine** | PyO3 | Trusted, frequent calls | Batch per phase. |
| **Sandbox Engine** | JSON-RPC | Untrusted input. Must survive Python compromise. | Batch per phase. |
| **Audit Log Writer** | JSON-RPC | Tamper-evidence requires isolation. Python must not have a file handle to the log. | Batch per phase. |

### 7.2 Cross-Boundary Call Flow

```mermaid
sequenceDiagram
    participant Orch as Orchestrator (Python)
    participant PyO3 as PyO3 Bridge
    participant Kernel as Code Intelligence Kernel (Rust)
    participant JSONRPC as JSON-RPC Bridge
    participant Sandbox as Sandbox Engine (Rust)
    participant Audit as Audit Writer (Rust)

    Orch->>PyO3: parse_files([500 paths])
    PyO3->>Kernel: parse_files(paths)
    Kernel-->>PyO3: [ASTs]
    PyO3-->>Orch: [ASTs]

    Orch->>JSONRPC: sandbox.create(config)
    JSONRPC->>Sandbox: create(config)
    Sandbox-->>JSONRPC: sandbox_id
    JSONRPC-->>Orch: sandbox_id

    Orch->>JSONRPC: audit.write([entries])
    JSONRPC->>Audit: write(entries)
    Audit-->>JSONRPC: {written: true}
    JSONRPC-->>Orch: {written: true}
```

**Rule:** Cross the boundary once per phase, not once per operation.

---

## 8. Call Flow Diagrams

Four key flows. Each shows the full sequence of modules involved.

### 8.1 Reconnaissance Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Orch as Orchestrator
    participant Repo as RepositoryProvider
    participant Recon as Recon Agent
    participant Kernel as Code Intelligence Kernel
    participant Sandbox as Sandbox Engine
    participant Audit as Audit Writer
    participant Storage

    User->>CLI: codeguardian recon --url <repo>
    CLI->>Orch: run_recon(url)
    Orch->>Repo: clone(url, sandbox)
    Repo-->>Orch: sandbox_path
    Orch->>Recon: phase_0(sandbox_path)
    Recon->>Kernel: parse_files(paths)
    Kernel-->>Recon: ASTs
    Recon->>Kernel: build_graph(paths)
    Kernel-->>Recon: cpg_metadata
    Recon->>Sandbox: run_smoke_test()
    Sandbox-->>Recon: test_results
    Recon->>Recon: build_runtime_contract_map
    Recon->>Recon: generate_truth_report
    Recon->>Recon: generate_drift_report
    Recon-->>Orch: recon.json
    Orch->>Storage: write(recon.json)
    Orch->>Audit: write(audit_entry)
    Orch-->>CLI: recon_result
    CLI-->>User: dashboard
```

### 8.2 Blast Radius Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Orch as Orchestrator
    participant Blast as Blast Radius Agent
    participant Engine as Blast Radius Engine
    participant Kernel as Code Intelligence Kernel
    participant Audit as Audit Writer

    User->>CLI: codeguardian blast --target <file>
    CLI->>Orch: run_blast(target)
    Orch->>Blast: phase_1(target)
    Blast->>Kernel: query_graph(cpg_path)
    Kernel-->>Blast: graph_handle
    Blast->>Engine: compute_blast_radius(target)
    Engine-->>Blast: report
    Blast->>Engine: detect_contract_violations(target, callers)
    Engine-->>Blast: violations
    Blast->>Engine: identify_coverage_gaps(callers)
    Engine-->>Blast: gaps
    Blast->>Engine: compute_blast_score(violations, gaps)
    Engine-->>Blast: score
    Blast-->>Orch: blast-radius.json
    Orch->>Audit: write(audit_entry)
    Orch-->>CLI: blast_result
    CLI-->>User: blast_radius_view
```

### 8.3 Refactor with Verification Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Orch as Orchestrator
    participant Analyst
    participant Tester
    participant Writer
    participant Sandbox as Sandbox Engine
    participant CV as Correctness Verifier
    participant SV as Security Verifier
    participant CtV as Contract Verifier
    participant Audit as Audit Writer

    User->>CLI: codeguardian run --target <file> --directive "<goal>"
    CLI->>Orch: run(target, directive)
    Orch->>User: "Approve plan? (y/N)"
    User->>Orch: y

    Orch->>Analyst: phase_2(context)
    Analyst-->>Orch: context-pack.json

    Orch->>Tester: phase_3(context-pack)
    Tester-->>Orch: tests + contract-assertions.json

    Orch->>Sandbox: run_tests(old_code)
    Sandbox-->>Orch: pass

    Orch->>Writer: phase_4(context-pack, directive)
    Writer-->>Orch: diff.patch

    Orch->>Sandbox: phase_5_verify(diff)
    alt tests pass
        Sandbox-->>Orch: pass
    else tests fail
        Sandbox-->>Orch: fail + error_trace
        Orch->>Writer: retry(diff, error_trace)
        Writer-->>Orch: new_diff
    end

    Orch->>User: "Verify with a second model? (y/N)"
    User->>Orch: y

    par Parallel Verifiers
        Orch->>CV: phase_6_correctness(diff)
        CV-->>Orch: verdict
    and
        Orch->>SV: phase_6_security(diff)
        SV-->>Orch: verdict
    and
        Orch->>CtV: phase_6_contract(diff, assertions)
        CtV-->>Orch: verdict
    end

    Orch->>Audit: write(all_entries)
    Orch-->>CLI: run_result
    CLI-->>User: "Apply changes? (y/N)"
```

### 8.4 Apply Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Orch as Orchestrator
    participant Apply as ApplyProvider
    participant Repo as Repository
    participant Audit as Audit Writer
    participant Storage

    User->>CLI: y
    CLI->>Orch: apply(diff, consent)
    Orch->>Apply: apply(repo_url, diff, consent)
    Apply->>Audit: write(consent_record)
    Apply->>Repo: apply_diff(diff)
    Repo-->>Apply: applied
    Apply->>Storage: record_rollback_ref
    Apply-->>Orch: apply_result
    Orch-->>CLI: applied
    CLI-->>User: success
```

---

## 9. The "What Breaks If I Change X" Map

This table answers the question directly.

| If you change… | These modules are affected |
|---|---|
| **A schema in `schemas/`** | Every agent that reads or produces it. Every contract test that validates it. |
| **An interface in `interfaces/`** | Every implementation. Every contract test. |
| **A core module** | Modules that import it. Module-level dependency graph in §2 shows the downstream set. |
| **A Rust crate** | Crates that depend on it. Python modules that call it through a bridge. |
| **The orchestrator** | Every phase. Every agent. The CLI. |
| **A model provider** | Every agent that uses a model. The verification prompt logic. |
| **A sandbox backend** | The sandbox selector. Every phase that runs code in the sandbox. |
| **The audit writer** | Every phase. The tamper-evidence guarantee. |
| **A plugin** | Only that plugin. The core is unaffected. |
| **The verification prompt** | The orchestrator. Phase 6. |

### 9.1 Locality-of-Change Guarantee

The modularity rules from `03` and `08` ensure that:

```mermaid
flowchart TB
    subgraph Change["You change…"]
        C1[One plugin]
        C2[One provider implementation]
        C3[One sandbox backend]
        C4[One verifier]
        C5[One reporter]
    end

    subgraph Unaffected["Unaffected"]
        U1[Core engine]
        U2[Orchestrator]
        U3[Other plugins]
        U4[Other providers]
        U5[Other sandbox backends]
        U6[Other verifiers]
        U7[Other reporters]
    end

    C1 --> U1
    C2 --> U1
    C3 --> U1
    C4 --> U1
    C5 --> U1

    C1 -.->|does not affect| U3
    C2 -.->|does not affect| U4
    C3 -.->|does not affect| U5
    C4 -.->|does not affect| U6
    C5 -.->|does not affect| U7
```

**Rule:** A change to one plugin does not require a change to the core, another plugin, or any other plugin type. This is enforced by the interface boundaries and the contract tests.

---

## 10. Related Documents

- `03-requirements.md` — modularity requirements (NFR-MT1 through MT11)
- `04-architecture.md` — components described here
- `05-data-model.md` — schemas referenced in the agent-data map
- `06-api-contracts.md` — interfaces and contract tests
- `07-tech-stack.md` — bridge technologies
- `08-repository-structure.md` — directory layout
- `10-testing-cicd-deployment.md` — how contract tests run in CI
