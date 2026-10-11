"""Canonical default values for the CodeGuardian configuration.

Every default the configuration system applies lives here as a named
constant. ``schema.py`` imports these values for the dataclass field
defaults, and ``loader.py`` imports the path constants to resolve the
config file location. Putting the values in one module means a change to
a default is a change to one line, not a search across three files.

Nothing in this module imports another CodeGuardian module. It is a leaf.
"""

from __future__ import annotations

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
]

# Environment variables read by the loader. Both are optional. They
# override the built-in defaults but are themselves overridden by an
# explicit path passed to ``load_config``.
CODEGUARDIAN_HOME_ENV = "CODEGUARDIAN_HOME"
CODEGUARDIAN_CONFIG_ENV = "CODEGUARDIAN_CONFIG"

# The default data directory and the config file inside it. The tilde
# is expanded at read time so the constants stay portable across
# operating systems and test environments.
DEFAULT_CONFIG_DIR = "~/.codeguardian"
DEFAULT_CONFIG_FILENAME = "config.toml"

# General settings.
DEFAULT_MODE = "legacy"
DEFAULT_SANDBOX_BACKEND = "auto"
DEFAULT_LOG_LEVEL = "info"

# Storage retention. Audit logs default to zero, which means "keep
# forever"; that is the behavior a compliance artifact requires.
DEFAULT_DATABASE = "~/.codeguardian/codeguardian.db"
DEFAULT_ARTIFACTS_RETENTION_DAYS = 30
DEFAULT_GRAPH_RETENTION_DAYS = 90
DEFAULT_AUDIT_RETENTION_DAYS = 0

# Sandbox resource limits. These are conservative defaults that fit a
# developer laptop. A cloud deployment raises them explicitly.
DEFAULT_SANDBOX_MEMORY_MB = 1024
DEFAULT_SANDBOX_CPU_CORES = 2
DEFAULT_SANDBOX_PROCESS_LIMIT = 256
DEFAULT_NETWORK_DURING_DEPS = True
