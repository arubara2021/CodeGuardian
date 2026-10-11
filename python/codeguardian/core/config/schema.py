"""Shape of the user's config.toml as a tree of frozen dataclasses.

Every field in the config file is declared here with its type, its
default when the user omits it, and its allowed values when the domain
is finite. The ``validate`` function takes a plain dict (typically the
result of ``tomllib.load``) and returns a typed ``Config``, raising
``CodeGuardianError`` with ``CONFIG_INVALID`` on any problem.

Validation is hand-written rather than driven by a JSON Schema or a
data-validation library. The structure is small, the rules are specific,
and hand-written checks produce error messages that name the exact
location in the config file (``config.sandbox.memory_limit_mb``) along
with the expected and actual value. A generic schema validator would
produce a machine path that is harder for a user to act on.

Nothing in this module reads the filesystem or the environment. It is a
pure function from dict to typed config. Loading a file, reading
environment variables, and merging defaults are the responsibility of
``loader.py``. The default values themselves live in ``defaults.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .defaults import (
    DEFAULT_ARTIFACTS_RETENTION_DAYS,
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_DATABASE,
    DEFAULT_GRAPH_RETENTION_DAYS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MODE,
    DEFAULT_NETWORK_DURING_DEPS,
    DEFAULT_SANDBOX_BACKEND,
    DEFAULT_SANDBOX_CPU_CORES,
    DEFAULT_SANDBOX_MEMORY_MB,
    DEFAULT_SANDBOX_PROCESS_LIMIT,
)

__all__ = [
    "VALID_API_KEY_SOURCES",
    "VALID_LOG_LEVELS",
    "VALID_MODEL_ROLES",
    "VALID_MODES",
    "VALID_PROVIDER_NAMES",
    "VALID_SANDBOX_BACKENDS",
    "Config",
    "ExclusionsConfig",
    "GeneralConfig",
    "MCPConfig",
    "MCPServerConfig",
    "ModelRoleConfig",
    "ModelsConfig",
    "ProviderConfig",
    "ProvidersConfig",
    "SandboxConfig",
    "StorageConfig",
    "validate",
]


VALID_MODES: frozenset[str] = frozenset({"legacy", "greenfield"})
VALID_SANDBOX_BACKENDS: frozenset[str] = frozenset(
    {"auto", "bubblewrap", "seatbelt", "firecracker", "docker"}
)
VALID_LOG_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})
VALID_MODEL_ROLES: frozenset[str] = frozenset(
    {
        "analyst",
        "tester",
        "writer",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
        "skill_curator",
    }
)
VALID_API_KEY_SOURCES: frozenset[str] = frozenset({"keychain", "env"})
VALID_PROVIDER_NAMES: frozenset[str] = frozenset({"anthropic", "openai", "deepseek", "local"})


def _fail(path: str, message: str) -> None:
    """Raise a CONFIG_INVALID error with the given path and message.

    The path names the exact location in the config file, such as
    ``config.sandbox.memory_limit_mb``. The path is included both in the
    message and in the error's ``details`` so that a caller can act on
    it programmatically.
    """
    raise CodeGuardianError(
        code=ErrorCode.CONFIG_INVALID,
        message=f"{path}: {message}",
        details={"path": path},
    )


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    """Return the value if it is a mapping, otherwise raise."""
    if not isinstance(value, Mapping):
        _fail(path, f"expected a table, got {type(value).__name__}")
    return value


def _require_string(value: Any, path: str) -> str:
    """Return the value if it is a string, otherwise raise."""
    if not isinstance(value, str):
        _fail(path, f"expected a string, got {type(value).__name__}")
    return value


def _require_bool(value: Any, path: str) -> bool:
    """Return the value if it is a boolean, otherwise raise."""
    if not isinstance(value, bool):
        _fail(path, f"expected a boolean, got {type(value).__name__}")
    return value


def _require_int(value: Any, path: str) -> int:
    """Return the value if it is an integer, otherwise raise.

    ``bool`` is a subclass of ``int`` in Python, so a boolean value
    would pass an ``isinstance(value, int)`` check. That is not what a
    reader of the config expects, so booleans are rejected here even
    though they are technically integers.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"expected an integer, got {type(value).__name__}")
    return value


def _require_positive_int(value: Any, path: str) -> int:
    """Return the value if it is a strictly positive integer."""
    number = _require_int(value, path)
    if number < 1:
        _fail(path, f"expected an integer >= 1, got {number}")
    return number


def _require_non_negative_int(value: Any, path: str) -> int:
    """Return the value if it is an integer greater than or equal to zero."""
    number = _require_int(value, path)
    if number < 0:
        _fail(path, f"expected an integer >= 0, got {number}")
    return number


def _require_list(value: Any, path: str) -> list[Any]:
    """Return the value if it is a list, otherwise raise."""
    if not isinstance(value, list):
        _fail(path, f"expected an array, got {type(value).__name__}")
    return value


def _require_string_list(value: Any, path: str) -> tuple[str, ...]:
    """Return a tuple of strings, raising on the first non-string item."""
    items = _require_list(value, path)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(_require_string(item, f"{path}[{index}]"))
    return tuple(result)


def _require_one_of(value: Any, path: str, allowed: frozenset[str]) -> str:
    """Return the string if it is in the allowed set, otherwise raise."""
    text = _require_string(value, path)
    if text not in allowed:
        options = ", ".join(sorted(allowed))
        _fail(path, f"expected one of {{{options}}}, got {text!r}")
    return text


def _reject_unknown(data: Mapping[str, Any], path: str, allowed: frozenset[str]) -> None:
    """Raise if the mapping contains any key outside the allowed set."""
    for key in data:
        if not isinstance(key, str):
            _fail(path, f"keys must be strings, got {type(key).__name__}")
        if key not in allowed:
            options = ", ".join(sorted(allowed))
            _fail(
                path,
                f"unknown key {key!r}; allowed keys are {{{options}}}",
            )


@dataclass(frozen=True, slots=True)
class GeneralConfig:
    """The ``[general]`` table."""

    mode: str = DEFAULT_MODE
    sandbox_backend: str = DEFAULT_SANDBOX_BACKEND
    log_level: str = DEFAULT_LOG_LEVEL


@dataclass(frozen=True, slots=True)
class ModelRoleConfig:
    """One role's model choice, as written under ``[models.<role>]``."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """The ``[models]`` table. Each role is optional.

    A role that is not configured is ``None``. The runtime reports a
    clear error at the moment the missing role is needed, which is
    better than failing config validation for a role the current run
    may never use.
    """

    analyst: ModelRoleConfig | None = None
    tester: ModelRoleConfig | None = None
    writer: ModelRoleConfig | None = None
    correctness_verifier: ModelRoleConfig | None = None
    security_verifier: ModelRoleConfig | None = None
    contract_verifier: ModelRoleConfig | None = None
    skill_curator: ModelRoleConfig | None = None


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """One provider's configuration under ``[providers.<name>]``.

    Cloud providers use ``api_key_source`` to declare where the key
    comes from. Local providers use ``base_url`` to name the endpoint.
    Both fields are optional on the dataclass because the schema does
    not enforce which shape a given provider must have; that check is
    the runtime's responsibility when the provider is instantiated.
    """

    api_key_source: str | None = None
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProvidersConfig:
    """The ``[providers]`` table.

    Each known provider name is a field. Unknown provider names are
    rejected during validation so that a typo in a config file surfaces
    immediately rather than being silently ignored.
    """

    anthropic: ProviderConfig | None = None
    openai: ProviderConfig | None = None
    deepseek: ProviderConfig | None = None
    local: ProviderConfig | None = None


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """The ``[storage]`` table."""

    database: str = DEFAULT_DATABASE
    artifacts_retention_days: int = DEFAULT_ARTIFACTS_RETENTION_DAYS
    graph_retention_days: int = DEFAULT_GRAPH_RETENTION_DAYS
    audit_retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """The ``[sandbox]`` table."""

    memory_limit_mb: int = DEFAULT_SANDBOX_MEMORY_MB
    cpu_limit_cores: int = DEFAULT_SANDBOX_CPU_CORES
    process_limit: int = DEFAULT_SANDBOX_PROCESS_LIMIT
    network_during_deps: bool = DEFAULT_NETWORK_DURING_DEPS


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """One MCP server entry in the ``[mcp].servers`` array.

    A server is defined either by a command and its arguments (a local
    stdio server) or by a URL (a remote streamable HTTP server). The
    validator accepts either shape and rejects an entry that provides
    neither.
    """

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None


@dataclass(frozen=True, slots=True)
class MCPConfig:
    """The ``[mcp]`` table."""

    servers: tuple[MCPServerConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class ExclusionsConfig:
    """The ``[exclusions]`` table."""

    patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    """The fully validated configuration tree.

    Every section is a frozen dataclass. Every section is constructed
    with its defaults when the corresponding table is absent from the
    user's config file. An empty dict is therefore valid input to
    ``validate`` and produces a ``Config`` with every default applied.

    The defaults are declared with ``field(default_factory=...)`` rather
    than as pre-built instances. Ruff's RUF009 rule flags a call such as
    ``GeneralConfig()`` in a dataclass default because the resulting
    instance would be shared across all ``Config`` objects. That sharing
    is technically safe here because every section is frozen, but the
    pattern is a known footgun in mutable dataclasses and the linter
    cannot verify the immutability. The factory form is the correct
    expression of intent: each ``Config`` gets its own section instances.
    """

    general: GeneralConfig = field(default_factory=GeneralConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    exclusions: ExclusionsConfig = field(default_factory=ExclusionsConfig)


_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "general",
        "models",
        "providers",
        "storage",
        "sandbox",
        "mcp",
        "exclusions",
    }
)
_GENERAL_KEYS: frozenset[str] = frozenset({"mode", "sandbox_backend", "log_level"})
_MODEL_ROLE_KEYS: frozenset[str] = frozenset({"provider", "model"})
_PROVIDER_KEYS: frozenset[str] = frozenset({"api_key_source", "base_url"})
_STORAGE_KEYS: frozenset[str] = frozenset(
    {
        "database",
        "artifacts_retention_days",
        "graph_retention_days",
        "audit_retention_days",
    }
)
_SANDBOX_KEYS: frozenset[str] = frozenset(
    {"memory_limit_mb", "cpu_limit_cores", "process_limit", "network_during_deps"}
)
_MCP_KEYS: frozenset[str] = frozenset({"servers"})
_MCP_SERVER_KEYS: frozenset[str] = frozenset({"name", "command", "args", "url"})
_EXCLUSIONS_KEYS: frozenset[str] = frozenset({"patterns"})


def _general_from_dict(data: Mapping[str, Any], path: str) -> GeneralConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _GENERAL_KEYS)

    mode = _require_one_of(mapping.get("mode", DEFAULT_MODE), f"{path}.mode", VALID_MODES)
    sandbox_backend = _require_one_of(
        mapping.get("sandbox_backend", DEFAULT_SANDBOX_BACKEND),
        f"{path}.sandbox_backend",
        VALID_SANDBOX_BACKENDS,
    )
    log_level = _require_one_of(
        mapping.get("log_level", DEFAULT_LOG_LEVEL),
        f"{path}.log_level",
        VALID_LOG_LEVELS,
    )
    return GeneralConfig(mode=mode, sandbox_backend=sandbox_backend, log_level=log_level)


def _model_role_from_dict(data: Mapping[str, Any], path: str) -> ModelRoleConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _MODEL_ROLE_KEYS)
    if "provider" not in mapping:
        _fail(path, "missing required key 'provider'")
    if "model" not in mapping:
        _fail(path, "missing required key 'model'")
    provider = _require_string(mapping["provider"], f"{path}.provider")
    model = _require_string(mapping["model"], f"{path}.model")
    if not provider:
        _fail(f"{path}.provider", "must not be empty")
    if not model:
        _fail(f"{path}.model", "must not be empty")
    return ModelRoleConfig(provider=provider, model=model)


def _models_from_dict(data: Mapping[str, Any], path: str) -> ModelsConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, VALID_MODEL_ROLES)

    roles: dict[str, ModelRoleConfig | None] = {}
    for role in VALID_MODEL_ROLES:
        if role in mapping:
            roles[role] = _model_role_from_dict(mapping[role], f"{path}.{role}")
        else:
            roles[role] = None

    return ModelsConfig(
        analyst=roles["analyst"],
        tester=roles["tester"],
        writer=roles["writer"],
        correctness_verifier=roles["correctness_verifier"],
        security_verifier=roles["security_verifier"],
        contract_verifier=roles["contract_verifier"],
        skill_curator=roles["skill_curator"],
    )


def _provider_from_dict(data: Mapping[str, Any], path: str) -> ProviderConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _PROVIDER_KEYS)

    api_key_source: str | None = None
    if "api_key_source" in mapping:
        api_key_source = _require_one_of(
            mapping["api_key_source"],
            f"{path}.api_key_source",
            VALID_API_KEY_SOURCES,
        )

    base_url: str | None = None
    if "base_url" in mapping:
        base_url = _require_string(mapping["base_url"], f"{path}.base_url")
        if not base_url:
            _fail(f"{path}.base_url", "must not be empty")

    if api_key_source is None and base_url is None:
        _fail(
            path,
            "must define at least one of 'api_key_source' or 'base_url'",
        )
    return ProviderConfig(api_key_source=api_key_source, base_url=base_url)


def _providers_from_dict(data: Mapping[str, Any], path: str) -> ProvidersConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, VALID_PROVIDER_NAMES)

    providers: dict[str, ProviderConfig | None] = {}
    for name in VALID_PROVIDER_NAMES:
        if name in mapping:
            providers[name] = _provider_from_dict(mapping[name], f"{path}.{name}")
        else:
            providers[name] = None

    return ProvidersConfig(
        anthropic=providers["anthropic"],
        openai=providers["openai"],
        deepseek=providers["deepseek"],
        local=providers["local"],
    )


def _storage_from_dict(data: Mapping[str, Any], path: str) -> StorageConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _STORAGE_KEYS)

    database = _require_string(
        mapping.get("database", DEFAULT_DATABASE),
        f"{path}.database",
    )
    if not database:
        _fail(f"{path}.database", "must not be empty")

    artifacts = _require_non_negative_int(
        mapping.get("artifacts_retention_days", DEFAULT_ARTIFACTS_RETENTION_DAYS),
        f"{path}.artifacts_retention_days",
    )
    graph = _require_non_negative_int(
        mapping.get("graph_retention_days", DEFAULT_GRAPH_RETENTION_DAYS),
        f"{path}.graph_retention_days",
    )
    audit = _require_non_negative_int(
        mapping.get("audit_retention_days", DEFAULT_AUDIT_RETENTION_DAYS),
        f"{path}.audit_retention_days",
    )
    return StorageConfig(
        database=database,
        artifacts_retention_days=artifacts,
        graph_retention_days=graph,
        audit_retention_days=audit,
    )


def _sandbox_from_dict(data: Mapping[str, Any], path: str) -> SandboxConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _SANDBOX_KEYS)

    memory = _require_positive_int(
        mapping.get("memory_limit_mb", DEFAULT_SANDBOX_MEMORY_MB),
        f"{path}.memory_limit_mb",
    )
    cpu = _require_positive_int(
        mapping.get("cpu_limit_cores", DEFAULT_SANDBOX_CPU_CORES),
        f"{path}.cpu_limit_cores",
    )
    processes = _require_positive_int(
        mapping.get("process_limit", DEFAULT_SANDBOX_PROCESS_LIMIT),
        f"{path}.process_limit",
    )
    network = _require_bool(
        mapping.get("network_during_deps", DEFAULT_NETWORK_DURING_DEPS),
        f"{path}.network_during_deps",
    )
    return SandboxConfig(
        memory_limit_mb=memory,
        cpu_limit_cores=cpu,
        process_limit=processes,
        network_during_deps=network,
    )


def _mcp_server_from_dict(data: Mapping[str, Any], path: str) -> MCPServerConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _MCP_SERVER_KEYS)

    if "name" not in mapping:
        _fail(path, "missing required key 'name'")
    name = _require_string(mapping["name"], f"{path}.name")
    if not name:
        _fail(f"{path}.name", "must not be empty")

    has_command = "command" in mapping
    has_url = "url" in mapping
    if has_command and has_url:
        _fail(path, "must define either 'command' or 'url', not both")
    if not has_command and not has_url:
        _fail(
            path,
            "must define either 'command' (with optional 'args') or 'url'",
        )

    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None

    if has_command:
        command = _require_string(mapping["command"], f"{path}.command")
        if not command:
            _fail(f"{path}.command", "must not be empty")
        if "args" in mapping:
            args = _require_string_list(mapping["args"], f"{path}.args")
    else:
        url = _require_string(mapping["url"], f"{path}.url")
        if not url:
            _fail(f"{path}.url", "must not be empty")

    return MCPServerConfig(name=name, command=command, args=args, url=url)


def _mcp_from_dict(data: Mapping[str, Any], path: str) -> MCPConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _MCP_KEYS)

    servers: list[MCPServerConfig] = []
    if "servers" in mapping:
        raw = _require_list(mapping["servers"], f"{path}.servers")
        for index, entry in enumerate(raw):
            servers.append(_mcp_server_from_dict(entry, f"{path}.servers[{index}]"))
    return MCPConfig(servers=tuple(servers))


def _exclusions_from_dict(data: Mapping[str, Any], path: str) -> ExclusionsConfig:
    mapping = _require_mapping(data, path)
    _reject_unknown(mapping, path, _EXCLUSIONS_KEYS)

    patterns: tuple[str, ...] = ()
    if "patterns" in mapping:
        patterns = _require_string_list(mapping["patterns"], f"{path}.patterns")
    return ExclusionsConfig(patterns=patterns)


def validate(raw: Mapping[str, Any] | None) -> Config:
    """Validate a plain dict into a typed ``Config``.

    An empty dict or ``None`` produces a ``Config`` with every default
    applied. Every problem in the input raises ``CodeGuardianError``
    with code ``CONFIG_INVALID`` and a message that names the exact
    location in the config file.

    Args:
        raw: The parsed contents of ``config.toml``, or ``None``.

    Returns:
        A fully typed ``Config``.

    Raises:
        CodeGuardianError: If any section fails validation. The error's
            ``details`` carries the offending path under the key
            ``path``.
    """
    if raw is None:
        return Config()
    mapping = _require_mapping(raw, "config")
    _reject_unknown(mapping, "config", _TOP_LEVEL_KEYS)

    general = (
        _general_from_dict(mapping["general"], "config.general")
        if "general" in mapping
        else GeneralConfig()
    )
    models = (
        _models_from_dict(mapping["models"], "config.models")
        if "models" in mapping
        else ModelsConfig()
    )
    providers = (
        _providers_from_dict(mapping["providers"], "config.providers")
        if "providers" in mapping
        else ProvidersConfig()
    )
    storage = (
        _storage_from_dict(mapping["storage"], "config.storage")
        if "storage" in mapping
        else StorageConfig()
    )
    sandbox = (
        _sandbox_from_dict(mapping["sandbox"], "config.sandbox")
        if "sandbox" in mapping
        else SandboxConfig()
    )
    mcp = _mcp_from_dict(mapping["mcp"], "config.mcp") if "mcp" in mapping else MCPConfig()
    exclusions = (
        _exclusions_from_dict(mapping["exclusions"], "config.exclusions")
        if "exclusions" in mapping
        else ExclusionsConfig()
    )

    return Config(
        general=general,
        models=models,
        providers=providers,
        storage=storage,
        sandbox=sandbox,
        mcp=mcp,
        exclusions=exclusions,
    )
