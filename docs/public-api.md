
# CodeGuardian — Public API Reference

Every public symbol exported from every module written so far. This file
is a contract, not documentation. It lists what exists and its signature.
For behavior, read the source or its docstring.

The next chat must consult this file before asking for a source file to
modify. If a symbol is not listed here, it is not public.

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
    REPO_CLONE_FAILED, REPO_AUTH_FAILED, REPO_NOT_FOUND,
    PATTERN_INVALID, DIFF_COMPUTE_FAILED, METADATA_UNAVAILABLE,
    FILE_NOT_FOUND, FILE_TOO_LARGE, FILE_BINARY, FILE_PERMISSION_DENIED,
    APPLY_CONFLICT, APPLY_AUTH_FAILED, APPLY_PARTIAL,
    ROLLBACK_FAILED, ROLLBACK_REF_NOT_FOUND,
    MODEL_UNAVAILABLE, MODEL_RATE_LIMITED, MODEL_CONTEXT_EXCEEDED,
    MODEL_AUTH_FAILED, MODEL_UNKNOWN, PROVIDER_UNAVAILABLE,
    SANDBOX_CREATE_FAILED, SANDBOX_IMAGE_UNAVAILABLE,
    SANDBOX_QUOTA_EXCEEDED, SANDBOX_TIMEOUT, SANDBOX_OOM,
    SANDBOX_EXEC_FAILED, SANDBOX_NOT_FOUND,
    MCP_SERVER_UNAVAILABLE, MCP_PROTOCOL_ERROR, MCP_TOOL_NOT_FOUND,
    MCP_TOOL_TIMEOUT, MCP_TOOL_ERROR,
    PLUGIN_LOAD_FAILED, PLUGIN_MANIFEST_INVALID,
    PLUGIN_PERMISSION_DENIED, PLUGIN_DISABLED,
    NET_TIMEOUT, NET_UNREACHABLE, NET_DNS_FAILED,
    CONFIG_INVALID, CONFIG_PARSE_ERROR, SECRET_KEYCHAIN_ERROR,
    SYS_INTERNAL, SYS_NOT_IMPLEMENTED, SYS_INVARIANT_VIOLATED
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

    # Attributes set in __init__
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

typed(fn: Callable[P, R]) -> Callable[P, R]
    # Supports sync and async functions.

redact(value: Any, *, marker: str = DEFAULT_MARKER) -> Any
    # Never mutates input. Returns a new structure.
```

---

## `codeguardian.core.config`

```python
from codeguardian.core.config import (
    # Valid-set constants
    VALID_API_KEY_SOURCES,
    VALID_LOG_LEVELS,
    VALID_MODEL_ROLES,
    VALID_MODES,
    VALID_PROVIDER_NAMES,
    VALID_SANDBOX_BACKENDS,

    # Default-value constants
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

    # Config dataclasses
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

    # Functions
    delete_api_key,
    get_api_key,
    load_config,
    set_api_key,
    validate,
)
```

### Constants

```python
VALID_MODES: frozenset[str]              # {"legacy", "greenfield"}
VALID_SANDBOX_BACKENDS: frozenset[str]   # {"auto", "bubblewrap", "seatbelt", "firecracker", "docker"}
VALID_LOG_LEVELS: frozenset[str]         # {"debug", "info", "warning", "error"}
VALID_MODEL_ROLES: frozenset[str]        # 7 roles
VALID_API_KEY_SOURCES: frozenset[str]    # {"keychain", "env"}
VALID_PROVIDER_NAMES: frozenset[str]     # {"anthropic", "openai", "deepseek", "local"}

CODEGUARDIAN_HOME_ENV: str = "CODEGUARDIAN_HOME"
CODEGUARDIAN_CONFIG_ENV: str = "CODEGUARDIAN_CONFIG"

DEFAULT_CONFIG_DIR: str = "~/.codeguardian"
DEFAULT_CONFIG_FILENAME: str = "config.toml"
DEFAULT_MODE: str = "legacy"
DEFAULT_SANDBOX_BACKEND: str = "auto"
DEFAULT_LOG_LEVEL: str = "info"
DEFAULT_DATABASE: str = "~/.codeguardian/codeguardian.db"
DEFAULT_ARTIFACTS_RETENTION_DAYS: int = 30
DEFAULT_GRAPH_RETENTION_DAYS: int = 90
DEFAULT_AUDIT_RETENTION_DAYS: int = 0
DEFAULT_SANDBOX_MEMORY_MB: int = 1024
DEFAULT_SANDBOX_CPU_CORES: int = 2
DEFAULT_SANDBOX_PROCESS_LIMIT: int = 256
DEFAULT_NETWORK_DURING_DEPS: bool = True
SERVICE_NAME: str = "codeguardian"
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
    # Raises CodeGuardianError(SECRET_KEYCHAIN_ERROR) on backend failure.
```

---

## Rust crates (stubs only)

Seven `Cargo.toml` files exist under `rust/crates/` with only a
`[package]` section. No Rust source compiles yet. Real contents arrive in
Phase 5 onward.

- `codeguardian-kernel`
- `codeguardian-blast`
- `codeguardian-sandbox`
- `codeguardian-audit`
- `codeguardian-policy`
- `codeguardian-rpc`
- `codeguardian-pyoo3` (lib only; `crate-type = ["cdylib", "rlib"]`)

---

## Not yet implemented

Anything not listed above. In particular: storage, interfaces, bridges,
Rust kernel, Rust engines, providers, plugins, prompts, agents, skills
module, orchestrator, CLI, MCP, reporters, and every entry point outside
`python -m codeguardian`.
```

---

## How to use these in the next chat

Attach these four files:

1. `skills.md`
2. `BUILD_ORDER.md`
3. `BUILD_STATE.md`
4. `docs/public-api.md`
