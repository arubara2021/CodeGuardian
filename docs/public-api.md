# CodeGuardian — Public API Reference

Every public symbol exported from every module written so far. This file
is a contract, not documentation. It lists what exists and its signature.
For behavior, read the source or its docstring.

The next chat must consult this file before asking for a source file to
modify. If a symbol is not listed here, it is not public.

**Snapshot:** end of Phase 5, plus the post-Phase-5 translation fix.
**Test count:** 256 passing.

---

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
    # 46 members, listed in codeguardian/core/errors/codes.py
    # Repository: REPO_CLONE_FAILED, REPO_AUTH_FAILED, REPO_NOT_FOUND,
    #             PATTERN_INVALID, DIFF_COMPUTE_FAILED, METADATA_UNAVAILABLE,
    #             FILE_NOT_FOUND, FILE_TOO_LARGE, FILE_BINARY,
    #             FILE_PERMISSION_DENIED
    # Apply: APPLY_CONFLICT, APPLY_AUTH_FAILED, APPLY_PARTIAL,
    #        ROLLBACK_FAILED, ROLLBACK_REF_NOT_FOUND
    # Model: MODEL_UNAVAILABLE, MODEL_RATE_LIMITED, MODEL_CONTEXT_EXCEEDED,
    #        MODEL_AUTH_FAILED, MODEL_UNKNOWN, PROVIDER_UNAVAILABLE
    # Sandbox: SANDBOX_CREATE_FAILED, SANDBOX_IMAGE_UNAVAILABLE,
    #          SANDBOX_QUOTA_EXCEEDED, SANDBOX_TIMEOUT, SANDBOX_OOM,
    #          SANDBOX_EXEC_FAILED, SANDBOX_NOT_FOUND
    # MCP: MCP_SERVER_UNAVAILABLE, MCP_PROTOCOL_ERROR, MCP_TOOL_NOT_FOUND,
    #      MCP_TOOL_TIMEOUT, MCP_TOOL_ERROR
    # Plugin: PLUGIN_LOAD_FAILED, PLUGIN_MANIFEST_INVALID,
    #         PLUGIN_PERMISSION_DENIED, PLUGIN_DISABLED
    # Network: NET_TIMEOUT, NET_UNREACHABLE, NET_DNS_FAILED
    # Config: CONFIG_INVALID, CONFIG_PARSE_ERROR, SECRET_KEYCHAIN_ERROR
    # Storage: STORAGE_MIGRATION_FAILED, STORAGE_SCHEMA_INVALID,
    #          STORAGE_GRAPH_CORRUPTED
    # Internal: SYS_INTERNAL, SYS_NOT_IMPLEMENTED, SYS_INVARIANT_VIOLATED
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
    # Raises UnknownCodeError.

retry_policy_of(code: ErrorCode | str) -> RetryPolicy
    # Raises UnknownCodeError.

compute_delay(
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: random.Random | None = None,
) -> float
    # Raises ValueError if attempt < 1.

retry_sync(
    fn: Callable[P, R],
    *args: P.args,
    retry_categories: set[ErrorCategory] | None = None,
    on_retry: Callable[[int, float, CodeGuardianError], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: P.kwargs,
) -> R

retry_async(
    fn: Callable[P, Awaitable[R]],
    *args: P.args,
    retry_categories: set[ErrorCategory] | None = None,
    on_retry: Callable[[int, float, CodeGuardianError], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: P.kwargs,
) -> R

translate(exc: BaseException) -> CodeGuardianError
    # Idempotent: returns CodeGuardianError unchanged.
    # The mapping table covers:
    #   FileNotFoundError, IsADirectoryError, NotADirectoryError
    #     -> FILE_NOT_FOUND
    #   PermissionError -> FILE_PERMISSION_DENIED
    #   FileExistsError -> SYS_INTERNAL
    #   ConnectionRefusedError, ConnectionResetError,
    #     ConnectionAbortedError, BrokenPipeError -> NET_UNREACHABLE
    #   socket.gaierror -> NET_DNS_FAILED
    #   TimeoutError -> NET_TIMEOUT
    #   UnicodeDecodeError -> FILE_BINARY
    #   json.JSONDecodeError -> CONFIG_PARSE_ERROR
    #   tomllib.TOMLDecodeError -> CONFIG_PARSE_ERROR
    # Anything else -> SYS_INTERNAL.

typed(fn: Callable[P, R]) -> Callable[P, R]
    # Supports sync and async functions.
    # Does not translate BaseException subclasses that are not
    # Exception subclasses (KeyboardInterrupt, SystemExit,
    # asyncio.CancelledError).

redact(value: Any, *, marker: str = DEFAULT_MARKER) -> Any
    # Never mutates input. Returns a new structure.
```

---

## `codeguardian.core.config`

```python
from codeguardian.core.config import (
    VALID_API_KEY_SOURCES,
    VALID_LOG_LEVELS,
    VALID_MODEL_ROLES,
    VALID_MODES,
    VALID_PROVIDER_NAMES,
    VALID_SANDBOX_BACKENDS,
    CODEGUARDIAN_CONFIG_ENV,
    CODEGUARDIAN_HOME_ENV,
    DEFAULT_ARTIFACTS_RETENTION_DAYS,
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_DATABASE,
    DEFAULT_GRAPH_RETENTION_DAYS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MODE,
    DEFAULT_NETWORK_DURING_DEPS,
    DEFAULT_SANDBOX_BACKEND,
    DEFAULT_SANDBOX_CPU_CORES,
    DEFAULT_SANDBOX_MEMORY_MB,
    DEFAULT_SANDBOX_PROCESS_LIMIT,
    SERVICE_NAME,
    Config,
    ExclusionsConfig,
    GeneralConfig,
    MCPConfig,
    MCPServerConfig,
    ModelRoleConfig,
    ModelsConfig,
    ProviderConfig,
    ProvidersConfig,
    SandboxConfig,
    StorageConfig,
    delete_api_key,
    get_api_key,
    load_config,
    set_api_key,
    validate,
)
```

### Constants

```python
VALID_MODES: frozenset[str]
VALID_SANDBOX_BACKENDS: frozenset[str]
VALID_LOG_LEVELS: frozenset[str]
VALID_MODEL_ROLES: frozenset[str]
VALID_API_KEY_SOURCES: frozenset[str]
VALID_PROVIDER_NAMES: frozenset[str]

CODEGUARDIAN_HOME_ENV: str = "CODEGUARDIAN_HOME"
CODEGUARDIAN_CONFIG_ENV: str = "CODEGUARDIAN_CONFIG"
DEFAULT_CONFIG_DIR: str
DEFAULT_CONFIG_FILENAME: str
DEFAULT_MODE: str
DEFAULT_SANDBOX_BACKEND: str
DEFAULT_LOG_LEVEL: str
DEFAULT_DATABASE: str
DEFAULT_ARTIFACTS_RETENTION_DAYS: int
DEFAULT_GRAPH_RETENTION_DAYS: int
DEFAULT_AUDIT_RETENTION_DAYS: int
DEFAULT_SANDBOX_MEMORY_MB: int
DEFAULT_SANDBOX_CPU_CORES: int
DEFAULT_SANDBOX_PROCESS_LIMIT: int
DEFAULT_NETWORK_DURING_DEPS: bool
SERVICE_NAME: str
```

### Data classes (all frozen, slots)

```python
@dataclass(frozen=True, slots=True)
class GeneralConfig:
    mode: str = DEFAULT_MODE
    sandbox_backend: str = DEFAULT_SANDBOX_BACKEND
    log_level: str = DEFAULT_LOG_LEVEL

@dataclass(frozen=True, slots=True)
class ModelRoleConfig:
    provider: str
    model: str

@dataclass(frozen=True, slots=True)
class ModelsConfig:
    analyst: ModelRoleConfig | None = None
    tester: ModelRoleConfig | None = None
    writer: ModelRoleConfig | None = None
    correctness_verifier: ModelRoleConfig | None = None
    security_verifier: ModelRoleConfig | None = None
    contract_verifier: ModelRoleConfig | None = None
    skill_curator: ModelRoleConfig | None = None

@dataclass(frozen=True, slots=True)
class ProviderConfig:
    api_key_source: str | None = None
    base_url: str | None = None

@dataclass(frozen=True, slots=True)
class ProvidersConfig:
    anthropic: ProviderConfig | None = None
    openai: ProviderConfig | None = None
    deepseek: ProviderConfig | None = None
    local: ProviderConfig | None = None

@dataclass(frozen=True, slots=True)
class StorageConfig:
    database: str = DEFAULT_DATABASE
    artifacts_retention_days: int = DEFAULT_ARTIFACTS_RETENTION_DAYS
    graph_retention_days: int = DEFAULT_GRAPH_RETENTION_DAYS
    audit_retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS

@dataclass(frozen=True, slots=True)
class SandboxConfig:
    memory_limit_mb: int = DEFAULT_SANDBOX_MEMORY_MB
    cpu_limit_cores: int = DEFAULT_SANDBOX_CPU_CORES
    process_limit: int = DEFAULT_SANDBOX_PROCESS_LIMIT
    network_during_deps: bool = DEFAULT_NETWORK_DURING_DEPS

@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None

@dataclass(frozen=True, slots=True)
class MCPConfig:
    servers: tuple[MCPServerConfig, ...] = ()

@dataclass(frozen=True, slots=True)
class ExclusionsConfig:
    patterns: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class Config:
    general: GeneralConfig = GeneralConfig()
    models: ModelsConfig = ModelsConfig()
    providers: ProvidersConfig = ProvidersConfig()
    storage: StorageConfig = StorageConfig()
    sandbox: SandboxConfig = SandboxConfig()
    mcp: MCPConfig = MCPConfig()
    exclusions: ExclusionsConfig = ExclusionsConfig()
```

### Functions

```python
validate(raw: Mapping[str, Any] | None) -> Config
    # Raises CodeGuardianError(CONFIG_INVALID) with details["path"].

load_config(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config
    # Raises CodeGuardianError with CONFIG_PARSE_ERROR,
    # CONFIG_INVALID, or FILE_PERMISSION_DENIED.

get_api_key(provider: str) -> str | None
    # Returns None when keyring is unavailable or no key is stored.

set_api_key(provider: str, value: str) -> None
    # Raises CodeGuardianError(SECRET_KEYCHAIN_ERROR) or
    # CodeGuardianError(CONFIG_INVALID) for empty input.

delete_api_key(provider: str) -> None
    # Succeeds silently if the key was not present.
```

---

## `codeguardian.core.storage`

```python
from codeguardian.core.storage import (
    CPGHeader,
    Cache,
    CleanupSummary,
    FilesystemStore,
    GraphStore,
    MigrationResult,
    MigrationRunner,
    SkillStore,
    SQLiteStore,
    run_cleanup,
)
```

### `CPGHeader`

```python
@dataclass(frozen=True, slots=True)
class CPGHeader:
    format_version: int
    flags: int
    node_count: int
    edge_count: int
    payload_offset: int
    payload_length: int
    payload_checksum: int

    @property
    def incremental(self) -> bool: ...
```

### `SQLiteStore`

```python
class SQLiteStore:
    def __init__(self, db_path: Path) -> None: ...
    @property
    def db_path(self) -> Path: ...
    @property
    def connection(self) -> sqlite3.Connection: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor: ...
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]: ...
    def execute_script(self, script: str) -> None: ...
    def is_migration_applied(self, version: str) -> bool: ...
    def record_migration(self, version: str, applied_at: str) -> None: ...
    def __enter__(self) -> SQLiteStore: ...
    def __exit__(self, *args: Any) -> None: ...
```

### `FilesystemStore`

```python
class FilesystemStore:
    def __init__(self, root: Path, schemas_dir: Path) -> None: ...
    @property
    def root(self) -> Path: ...
    @property
    def schemas_dir(self) -> Path: ...
    def artifact_path(self, repo_id: str, run_id: str, filename: str) -> Path: ...
    def write_artifact(
        self,
        repo_id: str,
        run_id: str,
        filename: str,
        data: Mapping[str, Any],
        schema_filename: str,
    ) -> Path: ...
    def read_artifact(
        self, repo_id: str, run_id: str, filename: str
    ) -> dict[str, Any]: ...
    def write_text(
        self, repo_id: str, run_id: str, filename: str, content: str
    ) -> Path: ...
    def read_text(self, repo_id: str, run_id: str, filename: str) -> str: ...
    def delete_run(self, repo_id: str, run_id: str) -> None: ...
```

### `GraphStore`

```python
class GraphStore:
    def __init__(self, root: Path, sqlite_store: SQLiteStore) -> None: ...
    def cpg_path(self, repo_id: str, commit_hash: str) -> Path: ...
    def exists(self, repo_id: str, commit_hash: str) -> bool: ...
    def read_header(self, repo_id: str, commit_hash: str) -> CPGHeader: ...
    def write_metadata(
        self,
        repo_id: str,
        commit_hash: str,
        header: CPGHeader,
        binary_path: Path,
        build_duration_ms: int | None = None,
    ) -> None: ...
    def read_metadata(
        self, repo_id: str, commit_hash: str
    ) -> dict[str, object] | None: ...
    def delete(
        self,
        repo_id: str,
        commit_hash: str,
        *,
        remove_metadata: bool = True,
    ) -> bool: ...
```

### `Cache`

```python
class Cache:
    def __init__(
        self,
        sqlite_store: SQLiteStore,
        filesystem_store: FilesystemStore,
        repo_id: str,
        *,
        max_memory_entries: int = 256,
        inline_threshold_bytes: int = 65536,
    ) -> None: ...
    @property
    def memory_size(self) -> int: ...
    def get_memory(self, key: str) -> Any | None: ...
    def put_memory(self, key: str, value: Any) -> None: ...
    def clear_memory(self) -> None: ...
    def get_disk(self, commit_hash: str, cache_type: str) -> bytes | None: ...
    def put_disk(self, commit_hash: str, cache_type: str, payload: bytes) -> None: ...
    def invalidate(self, commit_hash: str, cache_type: str) -> None: ...
```

### `MigrationRunner`

```python
@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]
    skipped: tuple[str, ...]

class MigrationRunner:
    def __init__(
        self,
        sqlite_store: SQLiteStore,
        migrations_dir: Path | None = None,
    ) -> None: ...
    @property
    def migrations_dir(self) -> Path: ...
    def run(self) -> MigrationResult: ...
```

### `Cleanup`

```python
@dataclass(frozen=True, slots=True)
class CleanupSummary:
    runs_swept: int
    run_files_deleted: int
    graph_files_deleted: int
    cache_entries_deleted: int
    bytes_freed: int
    wal_checkpointed: bool

def run_cleanup(
    sqlite_store: SQLiteStore,
    filesystem_store: FilesystemStore,
    graph_store: GraphStore,
    *,
    now: datetime | None = None,
    artifacts_retention_days: int = 30,
    graph_retention_days: int = 90,
    wal_threshold_bytes: int = 104857600,
) -> CleanupSummary: ...
```

### `SkillStore`

```python
class SkillStore:
    def __init__(
        self,
        sqlite_store: SQLiteStore,
        root: Path,
        schemas_dir: Path,
    ) -> None: ...
    @property
    def root(self) -> Path: ...
    def add(
        self,
        name: str,
        frontmatter: Mapping[str, Any],
        body: str,
        embedding: list[float] | None = None,
        *,
        overwrite: bool = False,
    ) -> Path: ...
    def get(self, name: str) -> dict[str, Any] | None: ...
    def remove(self, name: str) -> bool: ...
    def list_all(self) -> list[dict[str, Any]]: ...
    def count(self) -> int: ...
    def search_keyword(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...
    def search_semantic(
        self, embedding: list[float], limit: int = 10
    ) -> list[dict[str, Any]]: ...
    def record_feedback(
        self,
        name: str,
        run_id: str,
        outcome: str,
        task_description: str | None = None,
    ) -> None: ...
    def rebuild_index(self) -> int: ...
```

---

## `codeguardian.interfaces`

Forty-three symbols. Re-exported dynamically at import time, declared statically in a `TYPE_CHECKING` block so mypy can see them.

```python
from codeguardian.interfaces import (
    # repository
    FileChange,
    RepoMetadata,
    RepositoryProvider,
    # apply
    ApplyProvider,
    ApplyResult,
    ConsentRecord,
    ConsentScope,
    RollbackResult,
    # model
    ModelChunk,
    ModelInfo,
    ModelOptions,
    ModelProvider,
    ModelResponse,
    # sandbox
    ExecutionResult,
    ResourceLimits,
    SandboxBackend,
    SandboxHandle,
    SandboxSpec,
    # orchestrator
    Orchestrator,
    RunConfig,
    RunMode,
    RunResult,
    RunState,
    RunStatus,
    # agent
    Agent,
    # tool
    ToolDescriptor,
    ToolPlugin,
    # verifier
    ChangeKind,
    ContractAssertion,
    Finding,
    Severity,
    VerdictValue,
    VerifierContext,
    VerifierPlugin,
    VerifierVerdict,
    # reporter
    ReporterPlugin,
    # language
    LanguagePlugin,
    ReliabilityTier,
    TestResult,
    # mcp
    MCPClient,
    MCPServer,
    MCPTransport,
    ToolResult,
)
```

### Protocols

| Protocol | Methods | `runtime_checkable` |
|---|---|---|
| `RepositoryProvider` | clone, read_file, list_files, get_diff, get_metadata | No |
| `ApplyProvider` | apply, rollback | No |
| `ModelProvider` | complete, stream, count_tokens, get_available_models | No |
| `SandboxBackend` | create, execute, destroy, supports_language, get_resource_limits | No |
| `Orchestrator` | run, resume, pause, get_state | No |
| `Agent[InputT, OutputT]` | run (async) | No |
| `ToolPlugin` | call (async) | Yes |
| `VerifierPlugin` | verify (async) | Yes |
| `ReporterPlugin` | report | Yes |
| `LanguagePlugin` | detect, parse, generate_test, run_tests | Yes |
| `MCPClient` | discover_tools, call_tool, list_servers | No |
| `MCPServer` | start, stop, list_tools | No |

### Type aliases

```python
ConsentScope = Literal["apply", "verify"]
RunMode = Literal["legacy", "greenfield"]
RunStatus = Literal["running", "success", "failure", "stopped", "dry_run", "paused"]
ChangeKind = Literal["syntactic", "semantic", "contract", "signature"]
Severity = Literal["info", "warning", "critical"]
VerdictValue = Literal["pass", "fail", "uncertain"]
ReliabilityTier = Literal["high", "best-effort"]
MCPTransport = Literal["stdio", "http"]
```

### DTOs

All DTOs are `@dataclass(frozen=True, slots=True)`. Field lists are in the source; the interfaces are grouped by their owning module:

| Module | DTOs |
|---|---|
| `interfaces/repository.py` | `FileChange`, `RepoMetadata` |
| `interfaces/apply.py` | `ConsentRecord`, `ApplyResult`, `RollbackResult` |
| `interfaces/model.py` | `ModelOptions`, `ModelResponse`, `ModelChunk`, `ModelInfo` |
| `interfaces/sandbox.py` | `SandboxSpec`, `SandboxHandle`, `ExecutionResult`, `ResourceLimits` |
| `interfaces/orchestrator.py` | `RunConfig`, `RunState`, `RunResult` |
| `interfaces/tool.py` | `ToolDescriptor` |
| `interfaces/verifier.py` | `ContractAssertion`, `Finding`, `VerifierContext`, `VerifierVerdict` |
| `interfaces/language.py` | `TestResult` |
| `interfaces/mcp_client.py` | `ToolResult` |

### Async rule

Async methods: `ModelProvider.complete`, `ModelProvider.stream`, `Agent.run`, `ToolPlugin.call`, `VerifierPlugin.verify`, `MCPClient.discover_tools`, `MCPClient.call_tool`, `MCPServer.start`, `MCPServer.stop`.

Sync methods: everything else.

---

## `codeguardian.core.bridges`

```python
from codeguardian.core.bridges import (
    AUDIT_EXPORT,
    AUDIT_VERIFY,
    AUDIT_WRITE,
    RpcError,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    SANDBOX_CREATE,
    SANDBOX_DESTROY,
    SANDBOX_EXECUTE,
    SANDBOX_HEALTH,
)
```

### Constants

```python
AUDIT_WRITE: Final[str] = "audit.write"
AUDIT_VERIFY: Final[str] = "audit.verify"
AUDIT_EXPORT: Final[str] = "audit.export"
SANDBOX_CREATE: Final[str] = "sandbox.create"
SANDBOX_EXECUTE: Final[str] = "sandbox.execute"
SANDBOX_DESTROY: Final[str] = "sandbox.destroy"
SANDBOX_HEALTH: Final[str] = "sandbox.health"
```

### Data classes

```python
@dataclass(frozen=True, slots=True)
class RpcError:
    code: int
    message: str
    data: object | None = None

@dataclass(frozen=True, slots=True)
class RpcRequest:
    id: str | int
    method: str
    params: dict[str, object]

@dataclass(frozen=True, slots=True)
class RpcResponse:
    id: str | int
    result: object | None = None
    error: RpcError | None = None

@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: dict[str, object]
```

---

## `codeguardian.core.bridges.pyo3_bridge`

```python
from codeguardian.core.bridges.pyo3_bridge import (
    PyO3Bridge,
    is_native_available,
)

def is_native_available() -> bool: ...

class PyO3Bridge:
    def __init__(self) -> None: ...
    def echo(self, value: str) -> str: ...
```

Raises `CodeGuardianError(SYS_NOT_IMPLEMENTED)` on any call if the native extension `codeguardian._native` is not built.

The only function exposed in Phase 5 is `echo`. Phase 6 adds the kernel bindings.

---

## `codeguardian.core.bridges.jsonrpc_bridge`

```python
from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge

class JSONRPCBridge:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> None: ...

    @property
    def is_running(self) -> bool: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def call(self, method: str, params: dict[str, object]) -> object: ...
    def notify(self, method: str, params: dict[str, object]) -> None: ...

    def __enter__(self) -> JSONRPCBridge: ...
    def __exit__(self, *args: Any) -> None: ...
```

Wire format: line-delimited JSON-RPC 2.0. Each message is one JSON object followed by a newline.

Error mapping:
- `error` field in response → `CodeGuardianError(SYS_INTERNAL)` with the RPC code and method in details.
- Malformed JSON from subprocess → `CodeGuardianError(MCP_PROTOCOL_ERROR)`.
- Subprocess closes stdout before responding → `CodeGuardianError(SYS_INTERNAL)`.
- Call before `start` → `CodeGuardianError(SYS_INVARIANT_VIOLATED)`.
- Double start → `CodeGuardianError(SYS_INVARIANT_VIOLATED)`.
- Spawn failure → `CodeGuardianError(SANDBOX_CREATE_FAILED)`.

---

## Rust crates

The Rust workspace lives at `rust/crates/`. Seven members. Three have real content after Phase 5.

### `codeguardian-rpc`

Public types (from `rust/crates/codeguardian-rpc/src/lib.rs`):

```rust
pub const JSONRPC_VERSION: &str = "2.0";

pub struct RpcRequest {
    pub jsonrpc: String,
    pub id: u64,
    pub method: String,
    pub params: serde_json::Value,
}

pub struct RpcResponse {
    pub jsonrpc: String,
    pub id: u64,
    pub result: Option<serde_json::Value>,
    pub error: Option<RpcError>,
}

pub struct RpcError {
    pub code: i64,
    pub message: String,
    pub data: Option<serde_json::Value>,
}

pub struct RpcNotification {
    pub jsonrpc: String,
    pub method: String,
    pub params: serde_json::Value,
}

pub const PARSE_ERROR: i64 = -32700;
pub const INVALID_REQUEST: i64 = -32600;
pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INTERNAL_ERROR: i64 = -32603;

pub type Handler =
    Arc<dyn Fn(serde_json::Value) -> Result<serde_json::Value, RpcError>
        + Send + Sync + 'static>;

pub struct RpcServer { ... }

impl RpcServer {
    pub fn new() -> Self;
    pub fn register<F>(&mut self, method: impl Into<String>, handler: F)
        where F: Fn(serde_json::Value) -> Result<serde_json::Value, RpcError>
                + Send + Sync + 'static;
    pub fn handler_count(&self) -> usize;
    pub fn handle_line(&self, line: &str) -> Option<String>;
    pub fn serve<R: BufRead, W: Write>(
        &self, reader: R, writer: W,
    ) -> std::io::Result<()>;
}

pub struct RpcClient<R: BufRead, W: Write> { ... }

impl<R: BufRead, W: Write> RpcClient<R, W> {
    pub fn new(reader: R, writer: W) -> Self;
    pub fn call(
        &mut self, method: &str, params: serde_json::Value,
    ) -> std::io::Result<Result<serde_json::Value, RpcError>>;
    pub fn notify(
        &mut self, method: &str, params: serde_json::Value,
    ) -> std::io::Result<()>;
}
```

### `codeguardian-pyo3`

```rust
// rust/crates/codeguardian-pyo3/src/lib.rs

#[pyfunction]
fn echo(value: &str) -> String;

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()>;
```

Imported from Python as `codeguardian._native`. Phase 6 extends this file to register kernel bindings. This is the open Rule 1 escalation.

### `codeguardian-kernel`

```rust
// rust/crates/codeguardian-kernel/src/lib.rs
// Docstring only in Phase 5.
```

Filled in Phase 6.

### Stubs

`codeguardian-blast`, `codeguardian-sandbox`, `codeguardian-audit`, `codeguardian-policy` are still stubs. They compile with zero content.

---

## Not yet implemented

Anything not listed above. In particular:

- Every CLI command (Phase 14)
- Every agent (Phase 11)
- Every provider implementation (Phase 8)
- Every Python sandbox backend (Phase 8)
- Every plugin loader and bundled plugin (Phase 9)
- Every reporter (Phase 15)
- The MCP client and server (Phase 15)
- The orchestrator (Phase 13)
- The skill library operations (Phase 12)
- The prompts (Phase 10)
- Every Rust module inside `codeguardian-kernel` (Phase 6)
- Every Rust module inside `codeguardian-blast`, `codeguardian-sandbox`, `codeguardian-audit`, `codeguardian-policy` (Phase 7)
- `scripts/integration_phase.py` (Phase 16)
- `scripts/validate_schemas.py` (Phase 16)
- `python/codeguardian/__main__.py`'s real entry point (Phase 16)
```

---

Save this at `docs/public-api.md`, replacing the current version.

Then push it the same way as the phases summary:

```bash
git add docs/public-api.md
git commit -m "docs: update public API reference through Phase 5

Adds the interfaces package, the bridges package, the storage layer,
and the post-Phase-5 translation table entries. Reflects the state
of the codebase at the end of Phase 5."
git push
