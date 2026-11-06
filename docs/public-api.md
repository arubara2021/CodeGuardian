# CodeGuardian — Public API Reference

Every public symbol exported from every module written so far. This file
is a contract, not documentation. It lists what exists and its signature.
For behavior, read the source or its docstring.

The next chat must consult this file before asking for a source file to
modify. If a symbol is not listed here, it is not public.

**Snapshot:** end of Phase 12 v2.

**Test count:** 1547 passing, 14 skipped, 7 xfailed, 5 xpassed.

**Structure:** Two parts.

- **Part I — Phases 0 through 5.** Retained verbatim from the pre-Phase-6
  version of this file. Accurate for its era. Kept for cross-reference,
  and because a future reader often wants to see the shape of the early
  packages before the later phases layered on top of them.
- **Part II — Phase 6 through Phase 12 v2 additions.** Documents every
  module, symbol, and constant added since Part I was written. This part
  is authoritative for the current state.

---

# Part I — Phases 0 through 5

## `codeguardian` (package root)

```python
from codeguardian import version, __version__

__version__: str  # "1.0.0"
version: module   # the codeguardian.version module
```

---

## `codeguardian.version`

```python
from codeguardian.version import __version__, get_version

__version__: str  # "1.0.0"
get_version() -> str
```

---

## `codeguardian.core.errors`

```python
from codeguardian.core.errors import (
    Backoff,
    CodeGuardianError,
    DEFAULT_MARKER,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
    UnknownCodeError,
    category_of,
    compute_delay,
    redact,
    retry_async,
    retry_policy_of,
    retry_sync,
    translate,
    typed,
)
```

### Enums

```python
class Backoff(str, Enum):
    NONE = "none"
    FIXED = "fixed"
    EXPONENTIAL = "exponential"

class ErrorCategory(str, Enum):
    USER = "user"
    SYSTEM = "system"
    MODEL = "model"
    SANDBOX = "sandbox"
    PLUGIN = "plugin"
    NETWORK = "network"
    AUTH = "auth"

class ErrorCode(str, Enum):
    # Members are listed in codeguardian/core/errors/codes.py.
    # Categories (Repository, Apply, Model, Sandbox, MCP, Plugin,
    # Grammar, Network, Config, Storage, Internal) are described in
    # docs/06-api-contracts.md §15.
    # Part II records the codes added since this snapshot was taken.
```

### Data classes

```python
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    retryable: bool
    max_retries: int
    backoff: Backoff
```

### Exceptions

```python
class UnknownCodeError(KeyError):
    def __init__(self, code: str) -> None: ...
    code: str

class CodeGuardianError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        evidence: str | None = None,
    ) -> None: ...

    code: ErrorCode
    message: str
    details: dict[str, Any]
    evidence: str | None
    category: ErrorCategory
    retry_policy: RetryPolicy

    def __str__(self) -> str: ...
```

### Constants

```python
DEFAULT_MARKER: str = "[REDACTED]"
```

### Functions

```python
category_of(code: ErrorCode | str) -> ErrorCategory
retry_policy_of(code: ErrorCode | str) -> RetryPolicy
compute_delay(attempt, policy, *, rng=None) -> float
retry_sync(fn, *args, retry_categories=None, on_retry=None, sleep=time.sleep, **kwargs) -> R
retry_async(fn, *args, retry_categories=None, on_retry=None, sleep=asyncio.sleep, **kwargs) -> R
translate(exc: BaseException) -> CodeGuardianError
typed(fn: Callable[P, R]) -> Callable[P, R]
redact(value: Any, *, marker: str = DEFAULT_MARKER) -> Any
```

Behaviour, error mapping, and idempotence guarantees are in the docstrings of `retry.py`, `translation.py`, and `redaction.py`.

---

## `codeguardian.core.config`

```python
from codeguardian.core.config import (
    VALID_API_KEY_SOURCES, VALID_LOG_LEVELS, VALID_MODEL_ROLES,
    VALID_MODES, VALID_PROVIDER_NAMES, VALID_SANDBOX_BACKENDS,
    CODEGUARDIAN_CONFIG_ENV, CODEGUARDIAN_HOME_ENV,
    DEFAULT_ARTIFACTS_RETENTION_DAYS, DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_FILENAME, DEFAULT_DATABASE,
    DEFAULT_GRAPH_RETENTION_DAYS, DEFAULT_LOG_LEVEL, DEFAULT_MODE,
    DEFAULT_NETWORK_DURING_DEPS, DEFAULT_SANDBOX_BACKEND,
    DEFAULT_SANDBOX_CPU_CORES, DEFAULT_SANDBOX_MEMORY_MB,
    DEFAULT_SANDBOX_PROCESS_LIMIT, SERVICE_NAME,
    Config, ExclusionsConfig, GeneralConfig, MCPConfig,
    MCPServerConfig, ModelRoleConfig, ModelsConfig, ProviderConfig,
    ProvidersConfig, SandboxConfig, StorageConfig,
    delete_api_key, get_api_key, load_config, set_api_key, validate,
)
```

Data classes: `GeneralConfig`, `ModelRoleConfig`, `ModelsConfig`, `ProviderConfig`, `ProvidersConfig`, `StorageConfig`, `SandboxConfig`, `MCPServerConfig`, `MCPConfig`, `ExclusionsConfig`, `Config`. All `@dataclass(frozen=True, slots=True)`. Field lists are in `core/config/schema.py`.

Functions:

```python
validate(raw: Mapping[str, Any] | None) -> Config
load_config(path=None, env=None) -> Config
get_api_key(provider: str) -> str | None
set_api_key(provider: str, value: str) -> None
delete_api_key(provider: str) -> None
```

Part II adds `SkillsConfig` and 16 new `DEFAULT_SKILLS_*` constants.

---

## `codeguardian.core.storage`

```python
from codeguardian.core.storage import (
    CPGHeader, Cache, CleanupSummary, FilesystemStore, GraphStore,
    MigrationResult, MigrationRunner, SkillStore, SQLiteStore,
    run_cleanup,
)
```

Exports and methods are documented in §`SQLiteStore` through §`SkillStore` of the Phase-5 snapshot. **Part II records two behaviour changes to `SkillStore`** (internal upsert pattern and `remove` semantics) that emerged in Phase 12 v2.

---

## `codeguardian.interfaces`

Twelve Protocols. Re-exported dynamically at import time, declared statically in a `TYPE_CHECKING` block. Member list is the union of the `__all__` of every module in `interfaces/`. Part II adds one new Protocol (`ContextProvider`) and three new fields on `ToolPlugin`.

---

## `codeguardian.core.bridges`

```python
from codeguardian.core.bridges import (
    AUDIT_EXPORT, AUDIT_VERIFY, AUDIT_WRITE,
    RpcError, RpcNotification, RpcRequest, RpcResponse,
    SANDBOX_CREATE, SANDBOX_DESTROY, SANDBOX_EXECUTE, SANDBOX_HEALTH,
)

from codeguardian.core.bridges.pyo3_bridge import PyO3Bridge, is_native_available
from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
```

Phase-5 PyO3Bridge exposed only `echo`. **Part II records the growth of `_native` across Phases 6, 7, 9, and 10.**

---

## Rust crates (Phase-5 snapshot)

At Phase 5, `codeguardian-rpc` had real content, `codeguardian-pyo3` had `echo`, `codeguardian-kernel` was docstring-only, and four crates were stubs. **Part II records what each crate contains after Phase 7.**

---

# Part II — Phase 6 through Phase 12 v2 additions

Each section lists the exports added in that phase. Signatures are transcribed where the writer is confident; where a signature would need source verification, the section says so and refers to source. This matches the convention Part I already uses for DTO fields.

---

## Phase 6 — Kernel bindings on `_native`

`codeguardian._native` gained three PyO3 functions.

```python
# codeguardian._native  (imported as `from codeguardian import _native`)

def kernel_version() -> str: ...
    # Returns the kernel's semantic version string.

def build_graph(sources: list[tuple[str, str, str]]) -> bytes: ...
    # sources is [(path, language, content), ...].
    # Returns the serialized Code Property Graph bytes.
    # GIL is released during the call.

def graph_summary(data: bytes) -> dict: ...
    # Reads a serialized CPG and returns:
    # {"node_count": int, "edge_count": int,
    #  "file_count": int, "incremental": bool}

class KernelError(Exception):
    code: str       # "PARSE_FAILED", "CHECKSUM_MISMATCH", "INVALID_MAGIC",
                    # "TREE_TOO_DEEP", "FILE_NOT_FOUND",
                    # "BLAST_TARGET_NOT_FOUND", "BLAST_TARGET_AMBIGUOUS"
    message: str
    details: dict
```

Every `_native` function raises `KernelError` on failure. The Python side is responsible for translating a `KernelError.code` into a `CodeGuardianError` code. The pattern in `pyo3/kernel.rs` translates every kernel variant. The JSON-RPC path (Phase 7) translates every failure to `SYS_INTERNAL`.

Rust-side content is documented in `docs/summaries/phases-6-to-7.md` §10. The kernel crate now contains: `parser.rs`, `ast.rs`, `symbol_table.rs`, `call_graph.rs`, `cfg.rs`, `pdg.rs`, `cpg.rs`, `incremental.rs`, `query.rs`, plus the language modules under `langs/` (populated in the multi-language extension after Phase 8 — see below).

### Multi-language extension (between Phase 8 and Phase 9)

The kernel's `Language` enum grew from 2 variants (Python, TypeScript) to 8 (adding JavaScript, Java, Go, Rust, C, C++). `NodeKind` grew from 55 to 82 variants. This is recorded in `docs/summaries/phases-0-to-8.md` §13. It is a Rust internal; the Python-facing `_native.build_graph` signature did not change.

---

## Phase 7 — Engine bindings on `_native` and the `rpcd` daemon

`_native` gained four PyO3 functions, and a new Rust binary (`codeguardian-rpcd`) was introduced.

```python
# codeguardian._native  (continued)

def compute_blast(
    cpg_bytes: bytes,
    target_path: str,
    target_qualified_name: str,
) -> dict: ...
    # Returns:
    # {
    #   "target_node_id": int,
    #   "direct_callers": list[dict],
    #   "transitive_callers": list[dict],
    #   "transitive_truncated": bool,
    #   "contract_violations": list[dict],
    #   "coverage_gaps": list[dict],
    #   "blast_score": int,      # 0..100
    #   "recommendation": str,   # "proceed" | "review" | "block"
    # }
    # GIL is released during the call.
    # Raises KernelError with code BLAST_TARGET_NOT_FOUND or
    # BLAST_TARGET_AMBIGUOUS on resolution failure.

def check_sandbox_spec(
    language: str,
    memory_mb: int,
    cpu_cores: int,
    process_limit: int,
    max_memory_mb: int,
    max_cpu_cores: int,
    max_process_limit: int,
    allowed_languages: list[str],
) -> dict: ...
    # Returns a policy verdict dict. Allowed means no violation.
    # Policy is fail-closed: an empty allow list denies every language.

def check_plugin_permissions(
    plugin_name: str,
    filesystem: str,        # "none" | "sandbox_only" | "read_only" | "full"
    network: bool,
    process_spawn: bool,
    env_read: list[str],
    allowed_filesystem: str,
    allow_network: bool,
    allow_process_spawn: bool,
    allowed_env_keys: list[str],
) -> dict: ...
    # Filesystem levels are ordered. "does the request exceed the grant?"
    # is a single comparison.

def detect_host_limits() -> dict: ...
    # Reads /proc/meminfo, /proc/cpuinfo, /proc/sys/kernel/pid_max.
    # Returns {"total_memory_mb": int, "total_cpu_cores": int,
    #          "max_processes": int}. Fails on platforms without /proc.
```

### `rpcd` binary

Location: `rust/crates/codeguardian-rpcd/`. Spawned by `JSONRPCBridge`. Speaks JSON-RPC 2.0 over stdio. Two-worker tokio runtime.

Seven handlers, registered at startup:

| Method | Purpose |
|---|---|
| `sandbox.health` | Report available backends |
| `sandbox.create` | Create a sandbox handle |
| `sandbox.destroy` | Destroy a sandbox handle |
| `sandbox.execute` | Run a command inside a sandbox |
| `audit.write` | Append an audit entry |
| `audit.verify` | Verify a hash chain |
| `audit.export` | Export an audit log |

Every sandbox and audit operation crosses the Python-Rust boundary via these seven methods. Python never holds a direct handle to the log file or the bwrap process.

---

## Phase 8 — Providers and sandbox backends

### `codeguardian.core.providers`

```python
from codeguardian.core.providers import (
    # Repositories
    LocalProvider, GitHubProvider, GitLabProvider,
    # Apply
    ApplyProvider,
    # Models
    AnthropicProvider, OpenAIProvider, DeepSeekProvider,
    OpenAICompatibleProvider,
    ModelRegistry, FallbackModelProvider,
    RegistryModel, RoleConfig,
)
```

| Class | Implements | Notes |
|---|---|---|
| `LocalProvider` | `RepositoryProvider` | Real `git` subprocess. `_resolve_relative` walks `.resolve()` then `.relative_to(root)` to reject escapes. |
| `GitHubProvider` | `RepositoryProvider` | Subclass of `LocalProvider`, URL-prefix validation. |
| `GitLabProvider` | `RepositoryProvider` | Subclass of `LocalProvider`, GitLab prefixes. |
| `ApplyProvider` | `ApplyProvider` | Consent-gated. `sha256(diff) == consent.diff_hash` before any write. |
| `AnthropicProvider` | `ModelProvider` | Anthropic Messages API. |
| `OpenAIProvider` | `ModelProvider` | OpenAI Chat Completions. |
| `DeepSeekProvider` | `ModelProvider` | Subclass of `OpenAIProvider`. |
| `OpenAICompatibleProvider` | `ModelProvider` | Groq, Ollama, vLLM, llama.cpp, LM Studio. Requires `base_url`; API key optional. |
| `ModelRegistry` | — | Discovery-driven. `discover(provider)` calls the adapter's `list_models()`. |
| `FallbackModelProvider` | `ModelProvider` | Interprets `model` as a role name or `provider/model` string. Applies family exclusion, tier selection, cooldowns, scoring. |

`apply_provider.py` exposes one additional method on the class beyond the interface: `apply` returns `ApplyResult` with a `rollback_ref` field carrying the pre-apply HEAD commit.

### `codeguardian.core.sandbox`

```python
from codeguardian.core.sandbox import (
    BubblewrapBackend, DockerBackend,
    SeatbeltBackend, FirecrackerBackend,
    select_backend,
)
```

| Class | Implements | v1 status |
|---|---|---|
| `BubblewrapBackend` | `SandboxBackend` | Working, Linux default. |
| `DockerBackend` | `SandboxBackend` | Working, fallback. |
| `SeatbeltBackend` | `SandboxBackend` | Stub — `create` raises `SANDBOX_CREATE_FAILED`. |
| `FirecrackerBackend` | `SandboxBackend` | Stub — same. |
| `select_backend(config)` | — | Returns a backend **name**, not an instance. Stubs are never auto-selected. |

Phase 8's `SandboxBackend.execute` signature changed from Phase 4's: `execute(handle, command: Sequence[str], *, env, cwd, timeout_ms, stdin)`. This was a Rule-1 escalation recorded in `docs/summaries/phases-8.md` §4.

---

## Phase 9 — Plugins

### `codeguardian.core.plugins`

```python
from codeguardian.core.plugins import (
    PluginRegistry, PluginSpec,
    PluginSource, BundledSource, EntryPointSource,
    RegisteredPlugin, FaultBarrier,
    PluginManifest, Compatibility, check_interface_version,
    BudgetTracker,
)
```

| Symbol | Type | Purpose |
|---|---|---|
| `PluginRegistry` | Class | Central registry. Classmethod `discover(sources)`. Lookups: `get_language`, `get_tool`, `get_reporter`, `get_verifier`, `list_plugins`, `load_runtime_grammar`. |
| `PluginSpec` | Frozen dataclass | `{source, manifest_dict, plugin_class_loader, package_path}`. |
| `PluginSource` | Protocol | `__iter__` yields `PluginSpec`. |
| `BundledSource` | Class | Walks `plugins_bundled/` directories. |
| `EntryPointSource` | Class | Uses `importlib.metadata.entry_points`. |
| `RegisteredPlugin` | Frozen dataclass | Wraps a loaded instance. Carries the `disabled` flag. |
| `FaultBarrier` | Class | Exception isolation + per-type timeout. |
| `PluginManifest` | Frozen dataclass | Validated manifest. `validate(raw)` raises `PLUGIN_MANIFEST_INVALID`. |
| `Compatibility` | Literal type | `"current" | "newer_minor" | "prior_major" | "incompatible"`. |
| `check_interface_version` | Function | `(plugin_version, kernel_version) -> Compatibility`. Raises on incompatible. |
| `BudgetTracker` | Class | Per-phase tool call and output byte caps. Added as Phase 10 pre-work but exported from `core.plugins`. |

### `_native` additions

```python
def load_grammar(language_name: str, library_path: str) -> None: ...
    # Loads a tree-sitter grammar shared library at runtime.
    # Raises KernelError with GRAMMAR_LIBRARY_NOT_FOUND,
    # GRAMMAR_SYMBOL_NOT_FOUND, GRAMMAR_ABI_INCOMPATIBLE, or
    # GRAMMAR_ALREADY_LOADED.

def list_languages() -> list[str]: ...
    # Union of built-in and runtime-loaded languages.
```

### `codeguardian.plugins_bundled`

```
plugins_bundled/
├── context_providers/    # added in Phase 10
│   ├── call_graph_slice/plugin.py
│   ├── code_skeleton/plugin.py
│   ├── function_body/plugin.py
│   └── list_symbols/plugin.py
├── languages/
│   ├── python/plugin.py
│   ├── typescript/plugin.py
│   ├── javascript/plugin.py
│   ├── java/plugin.py
│   ├── go/plugin.py
│   ├── rust/plugin.py
│   ├── c/plugin.py
│   └── cpp/plugin.py
├── reporters/
│   └── sarif_reporter/plugin.py
└── tools/
    ├── ast_grep/plugin.py
    └── code_analysis/plugin.py
```

Each `plugin.py` declares a class and a class-level `__manifest__` dict.

### Schema change

`schemas/plugin-manifest.json` went from v1.1 to v1.2. Four new optional fields: `capabilities`, `blast_scope`, `kernel_extensible`, `grammar_path`. Reader supports N-1.

---

## Phase 10 — Prompts, PyCPG, and context providers

### `codeguardian.core.prompts`

```python
from codeguardian.core.prompts import (
    render_prompt, validate_output, PromptResult,
    SkillProposalOutput,
    # Per-role output dataclasses beyond SkillProposalOutput are in
    # core/prompts/schema.py.
)
```

| Symbol | Type | Purpose |
|---|---|---|
| `render_prompt` | Function | `(role, version, variables) -> PromptResult`. Strict `{{var}}` substitution. Raises on missing variables and on variable values that reference other variables in the same mapping. |
| `validate_output` | Function | `(role, data) -> dataclass`. Raises `CodeGuardianError` on schema violation. |
| `PromptResult` | Frozen dataclass | `{rendered: str, role: str, version: str, variables: Mapping}`. |
| `SkillProposalOutput` | Frozen dataclass | `{skill_name, skill_markdown, confidence}`. |

The eval harness lives at `core/prompts/eval_harness.py` and exposes `run_golden(role, cases, model_callable, ...)` and `load_golden(role)` returning `EvalReport`.

### `_native` additions for PyCPG

```python
class _native.PyCPG:
    """Python handle wrapping a deserialized CPG. Avoids re-deserialization."""
    # Constructed from serialized bytes.
    # Methods (added in Phase 10; full surface in source):
    #   find_declaration(...) -> int | None
    #   qualified_name_of(...) -> str | None
    #   declaration_info(...) -> dict | None

def find_declaration(cpg_bytes: bytes, file_path: str, symbol: str) -> int | None: ...
def qualified_name_of(cpg_bytes: bytes, node_id: int) -> str | None: ...
def declaration_info(cpg_bytes: bytes, node_id: int) -> dict | None: ...
```

The `_native` module now provides both stateless (bytes-in/out) and stateful (`PyCPG`) access. The context providers use `PyCPG` so a phase deserializes once and queries many times.

### `codeguardian.interfaces.context_provider`

New Protocol added in Phase 10:

```python
class ContextProvider(Protocol):
    name: str
    def provide(self, cpg: PyCPG, target: str, **kwargs) -> str: ...
```

Four implementations ship: `code-skeleton`, `function-body`, `call-graph-slice`, `list-symbols`.

### `ToolPlugin` fields added in Phase 10

The Protocol at `interfaces/tool.py` gained three fields:

```python
class ToolPlugin(Protocol):
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    action_kind: str      # e.g. "read", "write", "network"
    sensitivity: str      # e.g. "low", "medium", "high"
    is_read_only: bool
    async def call(self, input: dict) -> dict: ...
```

These support the agent approval layer. A `plan`-mode agent cannot invoke a `write`-kind tool.

### `ModelOptions` field added in Phase 10

`interfaces/model.py` gained `max_reasoning_tokens: int | None`. Reasoning models (gpt-oss family) need a separate cap because their internal reasoning consumes the general `max_tokens` budget before any visible output.

### Prompts on disk

```
prompts/
├── _base/
│   ├── v1.0.0.md      # every role inherits
│   └── v1.1.0.md
├── analyst/
│   ├── v1.0.0.md
│   ├── v1.1.0.md
│   └── eval/golden.yaml
├── tester/            # v1.0.0, v1.1.0, eval/golden.yaml
├── writer/            # v1.0.0, v1.1.0, eval/golden.yaml
├── skill_curator/     # v1.0.0, eval/golden.yaml
├── correctness_verifier/    # v1.0.0, eval/golden.yaml, eval/adversarial.yaml
├── security_verifier/       # v1.0.0, eval/golden.yaml, eval/adversarial.yaml
└── contract_verifier/       # v1.0.0, eval/golden.yaml, eval/adversarial.yaml
```

---

## Phase 11 — Agents, routing, telemetry, and verifier loop

### `codeguardian.core.agents`

```python
from codeguardian.core.agents import (
    # Deterministic
    ReconAgent, BlastAgent,
    # Model-backed
    AnalystAgent, TesterAgent, WriterAgent,
    CorrectnessVerifierAgent, SecurityVerifierAgent,
    ContractVerifierAgent, SkillCuratorAgent,
    # Inputs and results
    ReconInput, ReconResult,
    BlastInput, BlastResult,
    AnalystInput, AnalystResult,
    TesterInput, TesterResult,
    WriterInput, WriterResult,
    CorrectnessVerifierInput, CorrectnessVerifierResult,
    SecurityVerifierInput, SecurityVerifierResult,
    ContractVerifierInput, ContractVerifierResult,
    SkillCuratorInput, SkillCuratorResult,
    # Skill curator helpers
    RecentRun, SkillEvalCase,
)
```

Every agent implements `interfaces/agent.py::Agent[InputT, OutputT]` — the Protocol did not change. Each agent class has class-level attributes: `name`, `phase`, `is_deterministic`, `requires_tools`, `can_refuse`, `prompt_role`, `prompt_version`.

### `codeguardian.core.agents.telemetry`

```python
from codeguardian.core.agents.telemetry import (
    record_call, try_record_call,
    FailureCategory, VerdictReason,
)

record_call(store, *, role, outcome, failure_category, verdict_reason,
            target_symbol, prompt, output, tokens_in, tokens_out,
            latency_ms, model_id, prompt_role, prompt_version,
            model_options, ...) -> None
try_record_call(store_or_none, **kwargs) -> None
```

`FailureCategory` (Literal, 7 values): `localization`, `generation`, `schema`, `contract`, `timeout`, `context_overflow`, `model_unavailable`.

`VerdictReason` (Literal, 4 values): `behavior_drift`, `vulnerability_introduced`, `contract_broken`, `inconclusive`.

The two are stored in separate columns. A verifier that returns `fail` did its job — its `failure_category` is `NULL`. An agent that times out did not — its `verdict_reason` is `NULL`.

### `codeguardian.core.agents.context_budget`

```python
from codeguardian.core.agents.context_budget import ContextBudget
```

`ContextBudget` tracks fill percentage of a model context window. Requests compaction past 60%, refuses with a snapshot past 85%. Passed through `tracked_model_call` via `context_budget=`.

### `codeguardian.core.agents._loop`

```python
from codeguardian.core.agents._loop import VerifierLoop
```

`VerifierLoop` accepts a generator coroutine and a verifier coroutine. Runs up to `max_attempts` rounds, passes the previous verification result as feedback, halts on a generator refusal, calls an optional observer per round. Used by Phase 13.

### `codeguardian.core.providers.routing`, `.budget`, `.gateways`

Phase 11 added routing primitives in `core/providers/`:

```python
from codeguardian.core.providers.routing import (
    CapabilityProfile, TierRubric, ObjectiveEscalation,
    select_profile,
)

from codeguardian.core.providers.budget import (
    BudgetEnvelope, PressureLevel,
)

from codeguardian.core.providers.gateways import (
    DirectGateway, OpenRouterGateway, LiteLLMGateway,
    ModelGateway,
)
```

Field lists and method signatures are in the source. These primitives are tested in isolation at Phase 11; Phase 13 wires them into `fallback_router.py`.

### `codeguardian.core.agents.agent_eval`

```python
from codeguardian.core.agents.agent_eval import run_golden as run_agent_golden, load_golden as load_agent_golden
```

Distinct from `core.prompts.eval_harness`. `core.prompts.eval_harness` scores the prompt rendering. `core.agents.agent_eval` scores the agent's overall pass/fail against golden cases.

### Migrations added

| File | Purpose |
|---|---|
| `002_agent_calls.sql` | New `agent_calls` table |
| `003_agent_provenance.sql` | Adds `prompt_role`, `prompt_version`, `model_options_json` |
| `004_agent_provenance_v2.sql` | Adds `tool_chain`, `decision_log`, `model_reasoning`, `evidence_receipt` |

All four migrations run at Phase 11 boundary.

### Deep test

`sandtest/deep_test.py` was rewritten during Phase 11. Its public behavior (running against `psf/requests` and reporting pass/fail/skip/xfail per section) is unchanged; the internals add a section per agent and a check per prompt call.

---

## Phase 12 v2 — Skills

Phase 12 v1 shipped 6 files. Phase 12 v2 shipped 27 files plus two Rule-1 escalations. The design settlement is in `docs/summaries/Phase_12.md` Part II.

### `codeguardian.core.skills`

```python
from codeguardian.core.skills import (
    # v1 — unchanged surface
    EmbeddingProvider, SkillLibrary, SkillIndexer, SkillRetriever,
    RetrievedSkill,
    # v2 — new modules
    SkillGovernor, NegativeKnowledgeStore,
    SkillGraph, SkillEvolver,
    RetrievalPipeline,
    # v2 — shared types
    SkillNode, SkillEdge, EdgeKind,
    RetrievalBundle, RetrievalItem,
    NegativeRecord, AdmissionVerdict,
    ProvenancePin, ProjectCard,
)
```

That is 19 exported names. Every one is a direct re-export of a symbol defined in a submodule.

### `SkillLibrary` — new methods and behavior

Existing v1 methods (`add_skill`, `promote_skill`, `archive_skill`, `get_skill`, `list_skills`, `record_feedback`) are unchanged in signature and behavior **except** for `add_skill`, which gains gate routing when `admission_strict=True`.

New methods:

```python
class SkillLibrary:
    # ... existing v1 methods ...

    def governor(self) -> SkillGovernor: ...
    def negative(self) -> NegativeKnowledgeStore: ...
    def graph(self) -> SkillGraph: ...
    def evolver(self) -> SkillEvolver: ...
    def retrieval(self) -> RetrievalPipeline: ...

    def set_skill_state(self, name: str, state: str) -> bool: ...

    @property
    def config(self) -> SkillsConfig: ...
```

Each accessor is a lazy factory — the first call constructs the dependent object; subsequent calls return the cached instance.

`add_skill` behavior with `admission_strict=True`:

1. Parse frontmatter, generate embedding, write via `SkillStore.add`.
2. Call `governor().admit(name, frontmatter, body)`.
3. If the verdict is not accepted:
   - Call `SkillStore.remove(name)` to delete the just-written file and DB rows.
   - Raise `CodeGuardianError(STORAGE_SCHEMA_INVALID)` with the verdict's reason and findings.

If `admission_strict=False` (the default), `add_skill` behaves exactly as in Phase 12 v1.

### `SkillGovernor`

```python
class SkillGovernor:
    def __init__(self, store: SQLiteStore, library: SkillLibrary, config: SkillsConfig) -> None: ...

    def admit(
        self,
        skill_name: str,
        frontmatter: dict,
        body: str,
        *,
        eval_score: float | None = None,
    ) -> AdmissionVerdict: ...

    def transition(self, skill_name: str, target_state: str) -> bool: ...

    def retire(self, skill_name: str, reason: str = "") -> bool: ...

    def record_load_outcome(
        self,
        skill_name: str,
        outcome: str,
        *,
        task_description: str | None = None,
    ) -> None: ...

    def apply_retirement(self) -> list[str]: ...

    def get_state(self, skill_name: str) -> str | None: ...
```

`admit` runs the five checks in order: security scan, strict warning promotion, eval score, dedupe, active cap. Every call writes one row to `skill_admission_log`. Every rejection is logged with the reason and findings.

`transition` enforces the state machine: `draft → trial → active → stable → retired`. `stale` and `archived` (v1 states) are treated as aliases for `retired` on read. Illegal transitions return `False`; they do not raise.

`record_load_outcome` records the feedback through `library.record_feedback` and then reads the DB row to check whether a transition should fire. The transition logic uses only DB values because `record_feedback` updates the DB row and not the Markdown file — reading the Markdown here would see stale counters.

### `NegativeKnowledgeStore`

Store-bound wrapper around the module-level functions in `core/skills/negative.py`.

```python
class NegativeKnowledgeStore:
    def __init__(self, store: SQLiteStore) -> None: ...
    def add(self, record: NegativeRecord) -> None: ...
    def find(
        self,
        context_signature: str,
        *,
        min_confidence: float = 0.1,
        limit: int = 5,
    ) -> tuple[NegativeRecord, ...]: ...
    def adopt(self, failure_id: str) -> None: ...
    def reject(self, failure_id: str) -> None: ...
    def delete(self, failure_id: str) -> bool: ...
    def apply_forgetting_curve(
        self,
        *,
        decay_factor: float = 0.9,
        reference_days: int = 30,
        min_confidence: float = 0.1,
        max_per_skill: int = 20,
    ) -> int: ...
```

The underlying module (`core.skills.negative`) also exports: `add_record`, `get_record`, `find_relevant`, `list_all`, `adopt`, `reject`, `delete_record`, `apply_forgetting_curve`. Callers that hold a library should prefer the store-bound accessor.

### `SkillGraph`

Store-bound wrapper around `core.skills.graph_store`.

```python
class SkillGraph:
    def __init__(self, store: SQLiteStore, skills_dir: Path) -> None: ...
    def nodes(self) -> tuple[SkillNode, ...]: ...
    def edges(self) -> tuple[SkillEdge, ...]: ...
    def neighbors(
        self,
        node_id: str,
        *,
        kind: EdgeKind | None = None,
        direction: str = "out",   # "out" | "in" | "both"
    ) -> tuple[str, ...]: ...
    def rebuild(
        self,
        embeddings: dict[str, list[float]] | None = None,
        *,
        semantic_threshold: float = 0.85,
    ) -> tuple[int, int]: ...
    def node_count(self) -> int: ...
    def edge_count(self) -> int: ...
```

The underlying module (`core.skills.graph_store`) also exports: `upsert_node`, `upsert_edge`, `get_node`, `get_all_nodes`, `get_all_edges`, `neighbors`, `count_nodes`, `count_edges`, `drop_and_rebuild`.

The builder module (`core.skills.graph`) exposes: `build_edges(frontmatters, embeddings=None, *, semantic_threshold=0.85) -> (list[SkillEdge], list[str])`, `detect_cycles(edges) -> tuple[tuple[str, ...], ...]`, `capability_node_id(name) -> str`, `CAPABILITY_PREFIX = "cap:"`.

### `SkillEvolver`

Store-bound wrapper around `core.skills.evolver`.

```python
class SkillEvolver:
    def __init__(self, store: SQLiteStore) -> None: ...
    def mine(
        self,
        *,
        min_cooccurrence: int = 3,
        outcome_filter: str = "success",
    ) -> list[SkillEdge]: ...
    def write(self, path: Path, candidates: list[SkillEdge]) -> int: ...
    def list(self, path: Path) -> list[SkillEdge]: ...
    def promote(self, path: Path, *, min_weight: float = 0.5) -> int: ...
    def discard(self, path: Path) -> bool: ...
```

The underlying module also exports: `mine_candidates`, `write_candidates`, `list_candidates`, `promote_candidates`, `discard_candidates`.

Phase 12 v2 stores candidate edges in a sidecar JSON file, not shadow tables. See `docs/summaries/Phase_12.md` §13 for the recorded residual.

### `RetrievalPipeline`

```python
class RetrievalPipeline:
    def __init__(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: EmbeddingProvider,
        config: SkillsConfig,
        *,
        reranker: Reranker | None = None,
        token_budget: int = 3000,
    ) -> None: ...

    async def retrieve_for_prompt(
        self,
        query: str,
        *,
        project_context: ProjectCard | None = None,
        limit: int = 3,
        token_budget: int | None = None,
        seed_retriever: Callable[[str, int], Awaitable[str]] | None = None,
    ) -> str: ...

    async def retrieve(
        self,
        query: str,
        *,
        project_context: ProjectCard | None = None,
        limit: int = 3,
        token_budget: int | None = None,
    ) -> RetrievalBundle: ...
```

`retrieve_for_prompt` delegates to `seed_retriever` when every feature flag is off, which produces byte-identical output to the Phase 12 v1 `SkillRetriever.retrieve_for_prompt`. This is the guarantee `test_all_flags_off_delegates_to_v1_retriever` asserts.

`retrieve` always runs the staged pipeline. Neither method raises; both return an empty `RetrievalBundle` on any failure.

The underlying modules `core.skills.ppr`, `core.skills.hydrator`, and `core.skills.reranker` also expose their primitives directly:

```python
# core.skills.ppr
personalized_pagerank(edges, seeds, *, max_hops=1, timeout_ms=50,
                      reverse_multiplier=2.0, skill_names=None)
    -> list[RankedNode]

# core.skills.hydrator
hydrate(items, body_loader, *, token_budget, priority_order=None,
        token_counter=DEFAULT_TOKEN_COUNTER) -> RetrievalBundle
DEFAULT_TOKEN_COUNTER(text: str) -> int

# core.skills.reranker
Reranker(model_name=..., timeout_ms=120)
    async def rerank(query, items) -> tuple[RetrievalItem, ...]
```

### `core.skills.types`

All frozen dataclasses with `slots=True`, except `EdgeKind` (Enum):

| Type | Kind | Public fields (summary) |
|---|---|---|
| `EdgeKind` | `str, Enum` | `PREREQUISITE`, `PROVIDES`, `CONSUMES`, `SUPERSEDES`, `SEMANTIC`, `CONFLICTS` |
| `SkillNode` | Dataclass | `node_id`, `kind`, `name`, `metadata` |
| `SkillEdge` | Dataclass | `edge_id`, `src`, `dst`, `kind`, `weight`, `provenance` |
| `RetrievalItem` | Dataclass | `name`, `score`, `reason`, `body`, `token_count`, `is_negative` |
| `RetrievalBundle` | Dataclass | `items`, `truncated`, `truncation_strategy`, `total_tokens`, `stages_run`, `fallback_used` |
| `ProvenancePin` | Dataclass | `source_runs`, `authored_by`, `commit_hash`, `license`, `schema_hash`, `scan_result`, `eval_score`, `reviewer`, `reviewer_id`, `kernel_version`, `admission_evidence` |
| `NegativeRecord` | Dataclass | `failure_id`, `skill_name`, `failure_category`, `context_signature`, `attempted`, `observed`, `run_id`, `confidence`, `first_seen`, `last_seen`, `provenance`, `adopted_count`, `rejected_count` |
| `AdmissionVerdict` | Dataclass | `accepted`, `reason`, `blocking_findings`, `warning_findings`, `scan_result`, `dedupe_status`, `near_duplicate_of`, `eval_score` |
| `ProjectCard` | Dataclass | `repo_id`, `commit_hash`, `languages`, `frameworks`, `test_frameworks`, `test_command`, `build_system`, `blast_recommendation`, `caller_search_complete`, `confidence` |

Helper functions in `types.py`: `compute_edge_id(src, dst, kind) -> str` and `empty_bundle(reason="empty") -> RetrievalBundle`.

### `core.skills.provenance`

```python
build_pin(*, source_runs=(), authored_by, commit_hash, license="",
          kernel_version="", schema_hash="", scan_result="",
          eval_score=None, reviewer="auto", reviewer_id=None,
          admission_evidence=None) -> ProvenancePin

compute_schema_hash(schemas_dir: Path) -> str
verify_complete(pin: ProvenancePin) -> tuple[bool, tuple[str, ...]]
require_complete(pin: ProvenancePin) -> None    # raises STORAGE_SCHEMA_INVALID
save_pin(store: SQLiteStore, skill_name: str, pin: ProvenancePin) -> None
load_pin(store: SQLiteStore, skill_name: str) -> ProvenancePin | None
```

### `core.skills.security`

```python
FILESYSTEM_LEVELS: dict[str, int]      # none < sandbox_only < read_only < full
scan(markdown: str) -> AdmissionVerdict
intersect_permissions(skill_permissions, plugin_permissions) -> dict
install_audit_hook() -> None            # idempotent, once per process
skill_scope(permissions) -> ContextManager[dict]
```

`scan` is a known-pattern filter: prompt injection markers, invisible Unicode, shell substitution, exfiltration verbs targeting URLs (blocks); external URLs, conditional instructions, long lines, base64 (warns). It is not a proof of safety.

`skill_scope` installs a `sys.addaudithook` that enforces the given permissions during the block and resets the context var on exit.

### `SkillsConfig` — new fields

`core/config/schema.py` gained one dataclass. All fields have defaults from `defaults.py`.

```python
@dataclass(frozen=True, slots=True)
class SkillsConfig:
    graph_enabled: bool = False
    ppr_enabled: bool = False
    ppr_hops: int = 1
    ppr_timeout_ms: int = 50
    reranker_enabled: bool = False
    reranker_timeout_ms: int = 120
    negative_store_enabled: bool = False
    admission_strict: bool = False
    dedupe_threshold: float = 0.92
    max_active_skills: int = 200
    retire_threshold: float = 0.3
    retire_after_runs: int = 20
    trial_runs_before_active: int = 3
    retrieval_token_budget: int = 3000
    recon_blast_filter_enabled: bool = False
    evolver_enabled: bool = False
```

`Config` gained one field: `skills: SkillsConfig`. `validate()` gained one branch handling `config.skills`. Every existing config file remains valid because all fields default.

### 16 new `DEFAULT_SKILLS_*` constants in `core.config.defaults`

```
DEFAULT_SKILLS_GRAPH_ENABLED               = False
DEFAULT_SKILLS_PPR_ENABLED                 = False
DEFAULT_SKILLS_PPR_HOPS                    = 1
DEFAULT_SKILLS_PPR_TIMEOUT_MS              = 50
DEFAULT_SKILLS_RERANKER_ENABLED            = False
DEFAULT_SKILLS_RERANKER_TIMEOUT_MS         = 120
DEFAULT_SKILLS_NEGATIVE_STORE_ENABLED      = False
DEFAULT_SKILLS_ADMISSION_STRICT            = False
DEFAULT_SKILLS_DEDUPE_THRESHOLD            = 0.92
DEFAULT_SKILLS_MAX_ACTIVE_SKILLS           = 200
DEFAULT_SKILLS_RETIRE_THRESHOLD            = 0.3
DEFAULT_SKILLS_RETIRE_AFTER_RUNS           = 20
DEFAULT_SKILLS_TRIAL_RUNS_BEFORE_ACTIVE    = 3
DEFAULT_SKILLS_RETRIEVAL_TOKEN_BUDGET      = 3000
DEFAULT_SKILLS_RECON_BLAST_FILTER_ENABLED  = False
DEFAULT_SKILLS_EVOLVER_ENABLED             = False
```

### `SkillStore` — two behavior changes (Rule 1 escalation)

These are behavior changes to a Phase 3 file, approved during Phase 12 v2 verification. Public method names and signatures are unchanged; the fixes are internal and affect a pre-existing bug and a pre-existing failure mode.

| Method | Before | After |
|---|---|---|
| `_upsert_skill_row` (private) | DELETE-then-INSERT. Failed the `skill_feedback.skill_name` FK when the skill had any feedback row. | UPDATE-when-exists. Never triggers the FK. FTS delete-before-update preserved. |
| `remove` (public) | Deleted `skills` row first. Failed the same FK. | Deletes `skill_feedback` rows for the skill before `skills`. Removing a skill that has feedback now succeeds. |

Any caller that previously caught `SYS_INTERNAL` from `remove` on a skill that had feedback can drop the catch. The bug was latent in Phase 3 and was exposed by Phase 12 v2's `record_load_outcome`.

### Schema: `schemas/skill.json` v1.0 → v1.1

Six new **optional** fields (`prerequisites`, `provides`, `consumes`, `supersedes`, `provenance`, `permission_manifest`). The `state` enum gained three values: `trial`, `stable`, `retired`. The six original required fields are unchanged. Reader supports N-1; every existing `SKILL.md` validates without edit.

### Migrations: `005` and `006`

| File | Purpose |
|---|---|
| `005_skill_graph.sql` | Creates `skill_graph_nodes`, `skill_graph_edges`, `negative_knowledge`, `skill_provenance`, `skill_admission_log`. `IF NOT EXISTS`, no `ALTER` on existing tables. |
| `006_skill_states.sql` | Rebuilds `skills` table with expanded `CHECK` constraint for `trial`, `stable`, `retired`. Preserves rowids and FTS. Restores `PRAGMA foreign_keys` to its prior value (`OFF`, the project default). |

Both migrations run at Phase 12 v2 boundary. Fresh databases and existing databases both end up with the same schema.

---

## Not yet implemented

Anything not listed in Part I or Part II. In particular, at the end of Phase 12 v2:

- `codeguardian.core.orchestrator` — Phase 13 (empty stubs exist)
- `codeguardian.core.mcp` — Phase 15 (empty stubs exist)
- `codeguardian.core.reporting` — Phase 15 (empty stubs exist)
- `codeguardian.cli` — Phase 14 (empty stubs exist)
- `python/codeguardian/__main__.py` real entry point — Phase 16
- `scripts/integration_phase.py` — Phase 16
- `scripts/validate_schemas.py` — Phase 16
- `scripts/migrate.py` — Phase 16 (currently 0 bytes)
- `skills/README.md` — currently 0 bytes
- `examples/skills/python-add-type-hints/SKILL.md`, `examples/skills/typescript-extract-interface/SKILL.md` — currently 0 bytes

Every agent in Phase 11 has a `run()` method that is exercised by unit tests, but no orchestrator calls them in sequence. Retrieval (`library.retrieval()`) is callable but has no caller. Governance (`library.governor()`) has no automatic caller — a Phase 13 orchestrator must invoke `record_load_outcome` and `apply_retirement`. The full pipeline has not run end-to-end since the Phase 11 deep test.
