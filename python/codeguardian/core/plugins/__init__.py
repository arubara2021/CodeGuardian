"""Plugin loading, validation, permission enforcement, and dispatch.

Importing this package installs the process-wide audit hook exactly
once. Every subsequent plugin import, discovery, and invocation runs
under that hook. The public surface is the registry, the two discovery
sources, the fault barrier, the manifest and version helpers, and the
context runtime used by context provider plugins.
"""

from __future__ import annotations

from . import permissions as _permissions

_permissions.install_audit_hook()

from .context_runtime import (  # noqa: E402
    cpg_available,
    current_clone_root,
    current_cpg,
)
from .fault_barrier import (  # noqa: E402
    DEFAULT_METHOD_TIMEOUTS,
    BudgetTracker,
    FaultBarrier,
    shutdown_executor,
)
from .loader import (  # noqa: E402
    BundledSource,
    EntryPointSource,
    PluginSource,
    PluginSpec,
    discover_all,
)
from .manifest import PluginManifest, set_schema_path, validate  # noqa: E402
from .permissions import (  # noqa: E402
    AgentMode,
    FilesystemLevel,
    PluginPermissions,
    agent_mode_active,
    check_action_allowed,
    current_agent_mode,
    install_audit_hook,
    is_installed,
    plugin_active,
    sandbox_scope,
)
from .registry import PluginRegistry, RegisteredPlugin  # noqa: E402
from .versioning import (  # noqa: E402
    KERNEL_INTERFACE_VERSION,
    Compatibility,
    check_interface_version,
)

__all__ = [
    "DEFAULT_METHOD_TIMEOUTS",
    "KERNEL_INTERFACE_VERSION",
    "AgentMode",
    "BudgetTracker",
    "BundledSource",
    "Compatibility",
    "EntryPointSource",
    "FaultBarrier",
    "FilesystemLevel",
    "PluginManifest",
    "PluginPermissions",
    "PluginRegistry",
    "PluginSource",
    "PluginSpec",
    "RegisteredPlugin",
    "agent_mode_active",
    "check_action_allowed",
    "check_interface_version",
    "cpg_available",
    "current_agent_mode",
    "current_clone_root",
    "current_cpg",
    "discover_all",
    "install_audit_hook",
    "is_installed",
    "plugin_active",
    "sandbox_scope",
    "set_schema_path",
    "shutdown_executor",
    "validate",
]
