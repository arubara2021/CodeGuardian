"""Component boundary contracts for CodeGuardian.

Every cross-component boundary in the system is defined here as a
``typing.Protocol``. Concrete implementations live in their own
packages and conform to these shapes; the core never imports a
concrete implementation.

The public surface of this package is the union of the ``__all__``
lists of its submodules. Submodules are discovered at import time so
that adding a new interface never requires editing this file. Static
type checkers cannot see dynamic assignments, so the same surface is
also declared in a ``TYPE_CHECKING`` block.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import Agent as Agent
    from .apply import (
        ApplyProvider as ApplyProvider,
    )
    from .apply import (
        ApplyResult as ApplyResult,
    )
    from .apply import (
        ConsentRecord as ConsentRecord,
    )
    from .apply import (
        ConsentScope as ConsentScope,
    )
    from .apply import (
        RollbackResult as RollbackResult,
    )
    from .language import (
        LanguagePlugin as LanguagePlugin,
    )
    from .language import (
        ReliabilityTier as ReliabilityTier,
    )
    from .language import (
        TestResult as TestResult,
    )
    from .mcp_client import (
        MCPClient as MCPClient,
    )
    from .mcp_client import (
        ToolResult as ToolResult,
    )
    from .mcp_server import (
        MCPServer as MCPServer,
    )
    from .mcp_server import (
        MCPTransport as MCPTransport,
    )
    from .model import (
        ModelChunk as ModelChunk,
    )
    from .model import (
        ModelInfo as ModelInfo,
    )
    from .model import (
        ModelOptions as ModelOptions,
    )
    from .model import (
        ModelProvider as ModelProvider,
    )
    from .model import (
        ModelResponse as ModelResponse,
    )
    from .orchestrator import (
        Orchestrator as Orchestrator,
    )
    from .orchestrator import (
        RunConfig as RunConfig,
    )
    from .orchestrator import (
        RunMode as RunMode,
    )
    from .orchestrator import (
        RunResult as RunResult,
    )
    from .orchestrator import (
        RunState as RunState,
    )
    from .orchestrator import (
        RunStatus as RunStatus,
    )
    from .reporter import ReporterPlugin as ReporterPlugin
    from .repository import (
        FileChange as FileChange,
    )
    from .repository import (
        RepoMetadata as RepoMetadata,
    )
    from .repository import (
        RepositoryProvider as RepositoryProvider,
    )
    from .sandbox import (
        ExecutionResult as ExecutionResult,
    )
    from .sandbox import (
        ResourceLimits as ResourceLimits,
    )
    from .sandbox import (
        SandboxBackend as SandboxBackend,
    )
    from .sandbox import (
        SandboxHandle as SandboxHandle,
    )
    from .sandbox import (
        SandboxSpec as SandboxSpec,
    )
    from .tool import (
        ToolDescriptor as ToolDescriptor,
    )
    from .tool import (
        ToolPlugin as ToolPlugin,
    )
    from .verifier import (
        ChangeKind as ChangeKind,
    )
    from .verifier import (
        ContractAssertion as ContractAssertion,
    )
    from .verifier import (
        Finding as Finding,
    )
    from .verifier import (
        Severity as Severity,
    )
    from .verifier import (
        VerdictValue as VerdictValue,
    )
    from .verifier import (
        VerifierContext as VerifierContext,
    )
    from .verifier import (
        VerifierPlugin as VerifierPlugin,
    )
    from .verifier import (
        VerifierVerdict as VerifierVerdict,
    )

__all__: list[str] = []

for _finder, _module_name, _is_package in pkgutil.iter_modules(__path__):
    if _module_name.startswith("_") or _is_package:
        continue
    _interface_module = importlib.import_module(f"{__name__}.{_module_name}")
    for _symbol in getattr(_interface_module, "__all__", ()):
        if _symbol not in __all__:
            __all__.append(_symbol)
        globals()[_symbol] = getattr(_interface_module, _symbol)
