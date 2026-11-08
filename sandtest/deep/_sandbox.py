"""Sandbox lifecycle for the deep test.

Two phases, one repository at a time. The first phase runs with
network ON and is used to clone the repository and install its
dependencies. The second phase runs with network OFF and is used to
run the generator, tests, and any other command that touches untrusted
code.

The two phases are mutually exclusive. Starting the network-OFF phase
destroys the network-ON sandbox before creating the new one, so at
most one bubblewrap instance exists per session. The session is a
context manager; leaving it destroys whatever is active, even on an
exception.

Every command has a wall-clock timeout. Bubblewrap is not permitted
to hang. If the inner command exceeds its budget, the backend's outer
timeout fires and the call raises a typed error. The session still
cleans up because the context manager's ``__exit__`` destroys the
handle regardless of how the block exits.

The session does not itself parse any output. It returns the
``ExecutionResult`` objects the backend produces and lets the caller
inspect ``exit_code``, ``stdout``, and ``stderr``.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.sandbox.bubblewrap_backend import BubblewrapBackend
from codeguardian.interfaces.sandbox import (
    ExecutionResult,
    SandboxHandle,
    SandboxSpec,
)

__all__ = ["SandboxSession"]

_DEFAULT_MEMORY_MB = 2048
_DEFAULT_CPU_CORES = 2
_DEFAULT_PROCESS_LIMIT = 256
_DEFAULT_SAFE_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _wrap_with_cwd(command: str | Sequence[str], cwd: str | None) -> list[str]:
    """Return the argv that runs the command, optionally prefixed by a cd.

    A string command is passed to ``/bin/sh -c`` verbatim. A sequence
    is shell-quoted with ``shlex.quote`` so a path containing spaces or
    special characters does not break the invocation. When ``cwd`` is
    given, the command is prefixed with ``cd <cwd> &&`` so that the
    rest of the command runs from that directory. The directory is
    relative to the sandbox's working_dir, which is the mount root.
    """
    if isinstance(command, str):
        command_str = command
    else:
        command_str = " ".join(shlex.quote(part) for part in command)
    if cwd:
        command_str = f"cd {shlex.quote(cwd)} && {command_str}"
    return ["/bin/sh", "-c", command_str]


class SandboxSession:
    """Context manager wrapping the two-phase sandbox lifecycle.

    Args:
        bridge: The started JSON-RPC bridge to rpcd.
        work_root: The host directory every sandbox command runs
            inside. Bind-mounted read-write so a clone created inside
            the sandbox appears on the host and persists when the
            sandbox is destroyed.
        memory_mb: The memory limit for both sandbox phases.
        cpu_cores: The CPU limit for both sandbox phases.
        process_limit: The process limit for both sandbox phases.

    The session is not thread safe. One instance manages one bwrap at
    a time. Creating the network-OFF phase destroys the network-ON
    phase; the caller does not need to call ``close`` between them.
    """

    def __init__(
        self,
        bridge: object,
        work_root: Path,
        *,
        memory_mb: int = _DEFAULT_MEMORY_MB,
        cpu_cores: int = _DEFAULT_CPU_CORES,
        process_limit: int = _DEFAULT_PROCESS_LIMIT,
    ) -> None:
        self._bridge = bridge
        self._work_root = Path(work_root)
        self._backend = BubblewrapBackend(bridge)  # type: ignore[arg-type]
        self._memory_mb = memory_mb
        self._cpu_cores = cpu_cores
        self._process_limit = process_limit
        self._handle: SandboxHandle | None = None
        self._network_on = False

    def __enter__(self) -> SandboxSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def is_active(self) -> bool:
        """True while a sandbox handle exists."""
        return self._handle is not None

    @property
    def network_on(self) -> bool:
        """True while the current phase has network access."""
        return self._network_on

    def close(self) -> None:
        """Destroy the active sandbox. Safe to call multiple times."""
        handle = self._handle
        self._handle = None
        self._network_on = False
        if handle is None:
            return
        try:
            self._backend.destroy(handle)
        except CodeGuardianError:
            # A sandbox that has already exited reports
            # SANDBOX_NOT_FOUND here. That is not an error worth
            # propagating because the outcome the caller wanted (no
            # sandbox) is already true. Any other error is also
            # suppressed: the caller is either in a finally block or
            # an exception handler, and losing the original exception
            # would be worse than losing the destroy call.
            pass
        except Exception:
            pass

    def start_network_on(self) -> SandboxHandle:
        """Create the network-enabled sandbox.

        Used for clone and install. Every call destroys any previously
        active handle first, so a caller that reaches this method from
        an already-active state gets a clean transition rather than
        two sandboxes.
        """
        self.close()
        spec = SandboxSpec(
            language="python",
            working_dir=self._work_root,
            memory_mb=self._memory_mb,
            cpu_cores=self._cpu_cores,
            process_limit=self._process_limit,
            network_during_deps=True,
        )
        handle = self._backend.create(spec)
        self._handle = handle
        self._network_on = True
        return handle

    def start_network_off(self) -> SandboxHandle:
        """Create the network-isolated sandbox.

        Used for every command that runs after install. Destroys the
        network-enabled sandbox if one is active, then creates a fresh
        one with networking disabled.
        """
        self.close()
        spec = SandboxSpec(
            language="python",
            working_dir=self._work_root,
            memory_mb=self._memory_mb,
            cpu_cores=self._cpu_cores,
            process_limit=self._process_limit,
            network_during_deps=False,
        )
        handle = self._backend.create(spec)
        self._handle = handle
        self._network_on = False
        return handle

    def run(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """Run a command in the active sandbox.

        Args:
            command: A shell string or an argv sequence. A sequence is
                quoted so paths with spaces or special characters do
                not break the invocation.
            cwd: A directory relative to the sandbox's mount root. The
                command runs from there. Passing ``None`` runs from
                the mount root itself.
            timeout_ms: Wall-clock budget for the command. The
                backend's outer timeout fires when this is exceeded.
            env: Environment variables for the command. When omitted,
                a safe default set is used that includes PATH,
                HOME=/tmp, and GIT_TERMINAL_PROMPT=0. A caller that
                needs different values passes a complete env; the
                defaults are not merged, because merging can
                accidentally re-introduce a forbidden variable the
                caller tried to remove.

        Returns:
            The backend's ``ExecutionResult``.

        Raises:
            RuntimeError: If no sandbox is active. The caller must
                call ``start_network_on`` or ``start_network_off``
                before invoking this method.
            CodeGuardianError: Any typed error the backend raises
                (sandbox timeout, OOM kill, execution failure).
        """
        if self._handle is None:
            raise RuntimeError(
                "no active sandbox; call start_network_on() or start_network_off() first"
            )
        argv = _wrap_with_cwd(command, cwd)
        return self._backend.execute(
            self._handle,
            argv,
            env=env if env is not None else dict(_DEFAULT_SAFE_ENV),
            timeout_ms=timeout_ms,
        )

    def run_many(
        self,
        commands: Sequence[str | Sequence[str]],
        *,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
        stop_on_failure: bool = False,
    ) -> list[tuple[str | Sequence[str], ExecutionResult]]:
        """Run a sequence of commands sequentially in the active sandbox.

        Args:
            commands: The commands to run in order.
            cwd: Passed to each ``run`` call unchanged.
            timeout_ms: The budget for each individual command.
            stop_on_failure: When True, the sequence stops at the
                first nonzero exit code. When False, every command
                runs and the caller inspects results.

        Returns:
            A list of ``(command, result)`` pairs, in the order the
            commands were supplied. When ``stop_on_failure`` is True
            and a command fails, the returned list contains entries
            up to and including the failing command.
        """
        results: list[tuple[str | Sequence[str], ExecutionResult]] = []
        for command in commands:
            result = self.run(command, cwd=cwd, timeout_ms=timeout_ms)
            results.append((command, result))
            if stop_on_failure and result.exit_code != 0:
                break
        return results

    def clone(
        self,
        url: str,
        destination: str,
        *,
        timeout_ms: int = 180_000,
    ) -> ExecutionResult:
        """Clone a repository into the sandbox's work root.

        Requires the network-ON phase. The destination is relative to
        the mount root; a destination of ``"clone"`` creates
        ``<work_root>/clone`` on the host because the mount is
        read-write.

        Args:
            url: The repository URL or local path.
            destination: A directory relative to the mount root.
            timeout_ms: Wall-clock budget for the clone.

        Returns:
            The ``ExecutionResult`` of the ``git clone`` invocation.

        Raises:
            RuntimeError: When called while the sandbox is not in the
                network-ON phase.
        """
        if not self._network_on:
            raise RuntimeError(
                "clone() requires the network-ON sandbox; call start_network_on() first"
            )
        return self.run(
            ["git", "clone", url, destination],
            timeout_ms=timeout_ms,
        )

    def install(
        self,
        commands: Sequence[str],
        *,
        cwd: str = "clone",
        timeout_ms: int = 360_000,
        stop_on_failure: bool = True,
    ) -> list[tuple[str, ExecutionResult]]:
        """Run install commands against a cloned repository.

        Requires the network-ON phase. Each command is a shell string;
        the default environment keeps PATH intact and sets HOME to
        ``/tmp`` so pip's cache lives in the sandbox and does not
        leak across runs.

        Args:
            commands: The install commands, run in order.
            cwd: The clone directory relative to the mount root.
            timeout_ms: Budget for each command.
            stop_on_failure: Stop at the first nonzero exit.

        Returns:
            A list of ``(command, result)`` pairs.

        Raises:
            RuntimeError: When called while the sandbox is not in the
                network-ON phase.
        """
        if not self._network_on:
            raise RuntimeError(
                "install() requires the network-ON sandbox; call start_network_on() first"
            )
        return self.run_many(
            commands,
            cwd=cwd,
            timeout_ms=timeout_ms,
            stop_on_failure=stop_on_failure,
        )
