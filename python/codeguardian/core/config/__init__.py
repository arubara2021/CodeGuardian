"""Typed configuration for CodeGuardian.

Every component that reads configuration goes through this package. The
``schema`` module defines the shape of ``config.toml`` as a tree of
frozen dataclasses. The ``defaults`` module holds every default value in
one place. The ``loader`` module reads the file and returns a typed
``Config``. The ``secrets`` module reads and writes API keys through the
operating system keychain.
"""

from .defaults import (
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
)
from .loader import load_config
from .schema import (
    VALID_API_KEY_SOURCES,
    VALID_LOG_LEVELS,
    VALID_MODEL_ROLES,
    VALID_MODES,
    VALID_PROVIDER_NAMES,
    VALID_SANDBOX_BACKENDS,
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
    validate,
)
from .secrets import (
    SERVICE_NAME,
    delete_api_key,
    get_api_key,
    set_api_key,
)

__all__ = [
    "CODEGUARDIAN_CONFIG_ENV",
    "CODEGUARDIAN_HOME_ENV",
    "DEFAULT_ARTIFACTS_RETENTION_DAYS",
    "DEFAULT_AUDIT_RETENTION_DAYS",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_DATABASE",
    "DEFAULT_GRAPH_RETENTION_DAYS",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_MODE",
    "DEFAULT_NETWORK_DURING_DEPS",
    "DEFAULT_SANDBOX_BACKEND",
    "DEFAULT_SANDBOX_CPU_CORES",
    "DEFAULT_SANDBOX_MEMORY_MB",
    "DEFAULT_SANDBOX_PROCESS_LIMIT",
    "SERVICE_NAME",
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
    "delete_api_key",
    "get_api_key",
    "load_config",
    "set_api_key",
    "validate",
]
