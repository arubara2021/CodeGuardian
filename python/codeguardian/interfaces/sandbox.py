"""Isolated execution for untrusted code.

A sandbox backend creates an isolated environment, runs commands in
it, and destroys it. The interface is the same across every backend,
so the pipeline runs on Bubblewrap, Seatbelt, Firecracker, or Docker
without change. Network access is disabled except during a
dependency-resolution phase controlled by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = [
    "ExecutionResult",
    "ResourceLimits",
    "SandboxBackend",
    "SandboxHandle",
    "SandboxSpec",
]


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """The configuration used to create a sandbox.

    Attributes:
        language: Primary language the sandbox will execute.
        memory_mb: Memory limit in mebibytes.
        cpu_cores: CPU core limit.
        process_limit: Maximum number of processes.
        network_during_deps: Whether the network is enabled during
            dependency resolution.
    """

    language: str
    memory_mb: int
    cpu_cores: int
    process_limit: int
    network_during_deps: bool


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    """An opaque reference to a running sandbox.

    Attributes:
        id: Backend-specific identifier for the sandbox.
        backend: Name of the backend that created the sandbox.
        created_at: Timestamp the sandbox was created.
    """

    id: str
    backend: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The outcome of one command run inside a sandbox.

    Attributes:
        stdout: Standard output captured from the process.
        stderr: Standard error captured from the process.
        exit_code: Process exit code.
        timed_out: Whether the process was killed for exceeding the
            timeout.
        duration_ms: Wall-clock duration of the run, in milliseconds.
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """The limits a backend is currently configured to enforce.

    Attributes:
        memory_mb: Memory limit in mebibytes.
        cpu_cores: CPU core limit.
        process_limit: Maximum number of processes.
        network_enabled: Whether the backend allows network access.
    """

    memory_mb: int
    cpu_cores: int
    process_limit: int
    network_enabled: bool


class SandboxBackend(Protocol):
    """Isolation backend for untrusted code execution.

    Implementations enforce the same set of guarantees: network
    isolated except during dependency resolution, CPU and memory
    quotas enforced, process limit enforced, workspace reset between
    runs, sandbox destroyed at the end of the run. Failures are
    reported as typed ``CodeGuardianError`` codes.
    """

    def create(self, config: SandboxSpec) -> SandboxHandle:
        """Create a sandbox for the given specification.

        Args:
            config: The specification to honor.

        Returns:
            A handle to the running sandbox.

        Raises:
            CodeGuardianError: ``SANDBOX_CREATE_FAILED`` if the
                sandbox cannot be created,
                ``SANDBOX_IMAGE_UNAVAILABLE`` if the required image
                is not available, ``SANDBOX_QUOTA_EXCEEDED`` if the
                requested limits exceed what the backend can provide.
        """
        ...

    def execute(
        self,
        sandbox: SandboxHandle,
        command: str,
        timeout: int,
    ) -> ExecutionResult:
        """Run ``command`` inside the sandbox.

        Args:
            sandbox: Handle returned by ``create``.
            command: Shell command to run.
            timeout: Maximum wall-clock seconds before the process is
                killed.

        Returns:
            The result of the run.

        Raises:
            CodeGuardianError: ``SANDBOX_TIMEOUT`` if the command
                exceeded the timeout, ``SANDBOX_OOM`` if the process
                was killed for exceeding the memory limit,
                ``SANDBOX_EXEC_FAILED`` if execution failed for any
                other reason, ``SANDBOX_NOT_FOUND`` if the sandbox
                handle is no longer valid.
        """
        ...

    def destroy(self, sandbox: SandboxHandle) -> None:
        """Destroy a sandbox and release its resources.

        Args:
            sandbox: Handle to the sandbox to destroy.

        Raises:
            CodeGuardianError: ``SANDBOX_NOT_FOUND`` if the sandbox
                has already been destroyed.
        """
        ...

    def supports_language(self, language: str) -> bool:
        """Return whether the backend can run this language.

        Args:
            language: Name of the language, for example ``python``.

        Returns:
            True if the backend has an environment for the language.
        """
        ...

    def get_resource_limits(self) -> ResourceLimits:
        """Return the limits the backend currently enforces.

        Returns:
            The active resource limits.
        """
        ...
