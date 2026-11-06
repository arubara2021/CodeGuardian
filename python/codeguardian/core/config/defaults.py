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
    "DEFAULT_SKILLS_ADMISSION_STRICT",
    "DEFAULT_SKILLS_DEDUPE_THRESHOLD",
    "DEFAULT_SKILLS_EVOLVER_ENABLED",
    "DEFAULT_SKILLS_GRAPH_ENABLED",
    "DEFAULT_SKILLS_MAX_ACTIVE_SKILLS",
    "DEFAULT_SKILLS_NEGATIVE_STORE_ENABLED",
    "DEFAULT_SKILLS_PPR_ENABLED",
    "DEFAULT_SKILLS_PPR_HOPS",
    "DEFAULT_SKILLS_PPR_TIMEOUT_MS",
    "DEFAULT_SKILLS_RECON_BLAST_FILTER_ENABLED",
    "DEFAULT_SKILLS_RERANKER_ENABLED",
    "DEFAULT_SKILLS_RERANKER_TIMEOUT_MS",
    "DEFAULT_SKILLS_RETIRE_AFTER_RUNS",
    "DEFAULT_SKILLS_RETIRE_THRESHOLD",
    "DEFAULT_SKILLS_RETRIEVAL_TOKEN_BUDGET",
    "DEFAULT_SKILLS_TRIAL_RUNS_BEFORE_ACTIVE",
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

# Skills defaults added in Phase 12 v2. Every feature flag defaults to
# false; a user who upgrades without editing their configuration sees
# the exact behavior of the flat retrieval path.
DEFAULT_SKILLS_GRAPH_ENABLED: bool = False
DEFAULT_SKILLS_PPR_ENABLED: bool = False
DEFAULT_SKILLS_RERANKER_ENABLED: bool = False
DEFAULT_SKILLS_NEGATIVE_STORE_ENABLED: bool = False
DEFAULT_SKILLS_ADMISSION_STRICT: bool = False
DEFAULT_SKILLS_RECON_BLAST_FILTER_ENABLED: bool = False
DEFAULT_SKILLS_EVOLVER_ENABLED: bool = False

# PageRank traversal bounds. One hop is the default because the graph
# is small enough that a prerequisite's prerequisite is rarely useful,
# and because the walk has a hard time budget that a deeper traversal
# would frequently exceed.
DEFAULT_SKILLS_PPR_HOPS: int = 1
DEFAULT_SKILLS_PPR_TIMEOUT_MS: int = 50

# Cross-encoder reranking bound. Generous enough for a MiniLM-class
# model on a laptop CPU and short enough that a slower machine degrades
# to the previous stage rather than stalling the pipeline.
DEFAULT_SKILLS_RERANKER_TIMEOUT_MS: int = 120

# Governance thresholds. The dedupe threshold follows the middle of the
# measured collision band. The retire threshold and run count follow
# the fitness-driven retirement recipe from the library lifecycle
# literature.
DEFAULT_SKILLS_DEDUPE_THRESHOLD: float = 0.92
DEFAULT_SKILLS_MAX_ACTIVE_SKILLS: int = 200
DEFAULT_SKILLS_RETIRE_THRESHOLD: float = 0.3
DEFAULT_SKILLS_RETIRE_AFTER_RUNS: int = 20
DEFAULT_SKILLS_TRIAL_RUNS_BEFORE_ACTIVE: int = 3

# Retrieval budget. Three thousand tokens is enough for roughly five
# hydrated skill bodies of average size. The caller may override.
DEFAULT_SKILLS_RETRIEVAL_TOKEN_BUDGET: int = 3000
