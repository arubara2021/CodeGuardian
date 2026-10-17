"""Isolated execution for untrusted code.

A sandbox backend creates an isolated environment, runs commands in
it, and destroys it. The interface is the same across every backend,
so the pipeline runs on Bubblewrap, Seatbelt, Firecracker, or Docker
without change. Network access is disabled except during a
dependency-resolution phase controlled by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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
        language: Primary language the sandbox will execute. It is a
            caller hint, not an allow list. Bubblewrap runs any Linux
            binary, and Docker runs whatever image the caller supplies,
            so the language name does not gate creation.
        working_dir: Host directory that is bind-mounted read-write
            into the sandbox at the same absolute path. Every sandbox
            must have one. The daemon uses it both as the bind source
            and as the working directory commands run in, so a caller
            that places files in ``working_dir`` before creation finds
            them inside the sandbox without path translation.
        memory_mb: Memory limit in mebibytes.
        cpu_cores: CPU core limit.
        process_limit: Maximum number of processes.
        network_during_deps: Whether the network is enabled during
            dependency resolution. The execution phase that follows is
            always network-isolated.
        image: Container image the backend will run. Required by the
            Docker backend, ignored by Bubblewrap and by the stubs.
    """

    language: str
    working_dir: Path
    memory_mb: int
    cpu_cores: int
    process_limit: int
    network_during_deps: bool
    image: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    """A handle to a running sandbox.

    The fields mirror the handle the Rust daemon emits from
    ``sandbox.create``. Keeping them in lockstep means that
    ``execute`` and ``destroy`` can pass the handle back to the daemon
    without a Python-side side table, and the daemon can reconstruct
    the sandbox's resource state from the handle alone.

    Attributes:
        id: Backend-specific identifier for the sandbox.
        backend: Name of the backend that created the sandbox.
        language: The language the sandbox was created for.
        working_dir: The host directory bound into the sandbox.
        network_enabled: Whether the sandbox permits network access.
        memory_mb: Memory limit enforced on the sandbox.
        cpu_cores: CPU limit enforced on the sandbox.
        process_limit: Process limit enforced on the sandbox.
        image: Container image the sandbox runs when the backend is
            Docker; ``None`` for backends that do not use one.
    """

    id: str
    backend: str
    language: str
    working_dir: Path
    network_enabled: bool
    memory_mb: int
    cpu_cores: int
    process_limit: int
    image: str | None = None


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
        oom_killed: Whether the process was killed by the kernel OOM
            killer. Distinguishing this from a timeout matters: an
            OOM kill means the memory limit was too low, a timeout
            means the process was too slow, and a caller retrying with
            a larger budget picks a different knob for each case.
        output_truncated: Whether captured output exceeded the
            backend's buffer and was truncated. Truncation is not an
            error, but a caller that depends on the full output must
            know that the value it sees is not the whole value.
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int
    oom_killed: bool
    output_truncated: bool


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
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout_ms: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecutionResult:
        """Run ``command`` inside the sandbox.

        ``command`` is an argument vector, not a shell string. A
        vector passes through to the child process unchanged, so the
        backend never has to make shell-quoting decisions, and a
        caller that builds a vector cannot accidentally smuggle a
        metacharacter into a shell word. A caller that wants shell
        semantics runs ``["/bin/sh", "-c", script]`` explicitly.

        Args:
            sandbox: Handle returned by ``create``.
            command: Argument vector to run. The first element is the
                program, the remaining elements are its arguments.
            env: Environment variables to set on the child process.
                When ``None``, the backend uses its own default
                environment, which never includes the caller's
                environment variables. The backend sanitizes this
                mapping before it reaches the child.
            cwd: Working directory for the command, expressed as an
                absolute path. When ``None``, the command runs in the
                sandbox's ``working_dir``.
            timeout_ms: Maximum wall-clock milliseconds before the
                process is killed. When ``None``, the backend uses
                its default.
            stdin: Bytes written to the child's standard input, after
                which the stream is closed. When ``None``, the child
                receives an immediate EOF.

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
