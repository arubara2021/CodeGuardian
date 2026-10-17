"""``SandboxBackend`` stub for macOS Seatbelt.

The Seatbelt backend is not implemented in v1. Every method that would
create, execute, or destroy a sandbox raises ``SANDBOX_CREATE_FAILED``
with a message naming the missing implementation. The class exists so
that a caller importing it gets a clear typed error rather than an
``ImportError`` from a missing module.

When Seatbelt is implemented, the class signature will not change: it
will continue to take a ``JSONRPCBridge`` and to answer the same five
``SandboxBackend`` methods. The bridge will route to a Rust Seatbelt
handler instead of the one this stub stands in for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.config.defaults import (
    DEFAULT_SANDBOX_CPU_CORES,
    DEFAULT_SANDBOX_MEMORY_MB,
    DEFAULT_SANDBOX_PROCESS_LIMIT,
)
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.sandbox import (
    ExecutionResult,
    ResourceLimits,
    SandboxHandle,
    SandboxSpec,
)

__all__ = ["SeatbeltBackend"]


_DEFAULT_LIMITS = ResourceLimits(
    memory_mb=DEFAULT_SANDBOX_MEMORY_MB,
    cpu_cores=DEFAULT_SANDBOX_CPU_CORES,
    process_limit=DEFAULT_SANDBOX_PROCESS_LIMIT,
    network_enabled=False,
)


class SeatbeltBackend:
    """The ``SandboxBackend`` stub for Seatbelt on macOS."""

    def __init__(
        self,
        bridge: JSONRPCBridge,
        *,
        resource_limits: ResourceLimits | None = None,
    ) -> None:
        del bridge
        self._limits = resource_limits or _DEFAULT_LIMITS

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Raise; Seatbelt is not implemented in v1."""
        raise CodeGuardianError(
            ErrorCode.SANDBOX_CREATE_FAILED,
            "seatbelt backend is not implemented in v1",
            details={"language": spec.language, "platform": "darwin"},
        )

    def execute(
        self,
        sandbox: SandboxHandle,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout_ms: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecutionResult:
        """Raise; Seatbelt is not implemented in v1."""
        del sandbox, command, env, cwd, timeout_ms, stdin
        raise CodeGuardianError(
            ErrorCode.SANDBOX_EXEC_FAILED,
            "seatbelt backend is not implemented in v1",
        )

    def destroy(self, sandbox: SandboxHandle) -> None:
        """Raise; Seatbelt is not implemented in v1."""
        del sandbox
        raise CodeGuardianError(
            ErrorCode.SANDBOX_NOT_FOUND,
            "seatbelt backend is not implemented in v1",
        )

    def supports_language(self, language: str) -> bool:
        """Return False; no language is supported by a stub."""
        del language
        return False

    def get_resource_limits(self) -> ResourceLimits:
        """Return the resource limits the stub reports by default."""
        return self._limits
