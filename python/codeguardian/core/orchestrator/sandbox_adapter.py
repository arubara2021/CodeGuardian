"""Sandbox executor adapter for the orchestration layer.

The verifier agents and the deterministic gate speak a narrow protocol:
run an argument vector and return an exit code, stdout, and stderr. The
sandbox backends speak a different one: create a handle, execute against
the handle, destroy the handle. This module is the translation layer.

The adapter owns one sandbox for the duration of a ``with`` block. The
orchestrator opens the adapter around the ``sandbox_verify`` slot, runs
every check that needs a sandbox through it, and closes it when the slot
finishes. Nothing outside the block holds a handle, so a slot that fails
mid-way cannot leak a live sandbox.

``write_file`` is a host-side write, not a remote call. The backend
bind-mounts the sandbox's ``working_dir`` read-write at the same absolute
path, which the Phase 8 ``pwd`` proof established. A file written to that
directory on the host is visible inside the sandbox with no path
translation. This is why the adapter needs no write RPC and why the
containment check below is the only thing standing between a caller and
the host filesystem.

Two exit-code mappings are not the backend's raw value. A command that
the backend killed for exceeding its timeout is reported as 124, and a
command killed by the out-of-memory killer is reported as 137. Both are
conventional shell exit codes, so a caller that only inspects
``exit_code`` still sees a failure rather than a zero that happens to
mean "killed".

The adapter is deliberately backend-agnostic. It accepts any
``SandboxBackend``, so the same code drives Bubblewrap in development and
Docker in CI. ``BubblewrapExecutorAdapter`` is an alias for the class
under the name the Phase 13 build order uses.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from codeguardian.core.agents._verifier_common import ExecutionOutcome
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.sandbox import (
    ExecutionResult,
    SandboxBackend,
    SandboxHandle,
    SandboxSpec,
)

__all__ = ["BubblewrapExecutorAdapter", "SandboxExecutorAdapter"]


_TIMEOUT_EXIT_CODE = 124
"""Conventional shell exit code for a command killed by a timeout."""

_OOM_EXIT_CODE = 137
"""Conventional shell exit code for a command killed by SIGKILL."""


def _to_outcome(result: ExecutionResult) -> ExecutionOutcome:
    """Translate a backend ``ExecutionResult`` into an ``ExecutionOutcome``.

    The translation is not a field copy. A timed-out or OOM-killed
    command is reported with the conventional exit code even when the
    backend reports zero, and a synthesized message fills an empty
    stderr so a caller reading the tail has something to report. The
    raw ``exit_code`` from the backend is discarded in those two cases
    because the caller's only signal is the outcome it receives.
    """
    stderr = result.stderr
    exit_code = result.exit_code

    if result.timed_out:
        exit_code = _TIMEOUT_EXIT_CODE
        if not stderr:
            stderr = f"command exceeded its timeout after {result.duration_ms} ms"
    elif result.oom_killed:
        exit_code = _OOM_EXIT_CODE
        if not stderr:
            stderr = "command was killed by the out-of-memory killer"

    return ExecutionOutcome(
        exit_code=exit_code,
        stdout=result.stdout,
        stderr=stderr,
    )


class SandboxExecutorAdapter:
    """Adapts a ``SandboxBackend`` to the verifier sandbox protocol.

    The adapter is a context manager. Entering it creates the sandbox;
    leaving it destroys the sandbox even when the body raised. A body
    exception always wins over a destroy failure, so a destroy error
    cannot mask the real reason a phase failed.

    Args:
        backend: The sandbox backend to drive. Any object satisfying
            ``SandboxBackend`` works; the adapter never inspects the
            concrete class.
        spec: The specification passed to ``backend.create`` on entry.

    Raises:
        CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED`` when a
            method is called before the adapter is entered, when
            ``argv`` is empty, or when ``timeout_s`` is not positive.
            With ``SANDBOX_EXEC_FAILED`` when a write escapes the
            working directory or the filesystem refuses the write.
    """

    def __init__(self, backend: SandboxBackend, spec: SandboxSpec) -> None:
        self._backend = backend
        self._spec = spec
        self._handle: SandboxHandle | None = None
        self._destroy_error: CodeGuardianError | None = None

    def __enter__(self) -> SandboxExecutorAdapter:
        """Create the sandbox and return the adapter.

        Raises:
            CodeGuardianError: Propagated from ``backend.create`` when
                the sandbox cannot be created.
        """
        self._handle = self._backend.create(self._spec)
        self._destroy_error = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Destroy the sandbox.

        A destroy failure is suppressed when the body raised, because
        the body's exception is the more useful signal. When the body
        succeeded, the destroy failure is raised so a leak is not
        silent.
        """
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._backend.destroy(handle)
        except CodeGuardianError as exc:
            if exc_type is None:
                self._destroy_error = exc
                raise
            self._destroy_error = exc

    @property
    def handle(self) -> SandboxHandle:
        """The live sandbox handle.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when the adapter has not been entered or has exited.
        """
        if self._handle is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "sandbox adapter is not active; enter it with a with-block",
                details={"working_dir": str(self._spec.working_dir)},
            )
        return self._handle

    @property
    def working_dir(self) -> Path:
        """The host directory bind-mounted into the sandbox."""
        return Path(self._spec.working_dir)

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 120.0,
    ) -> ExecutionOutcome:
        """Run a command inside the sandbox.

        Args:
            argv: The argument vector. Must be non-empty. The first
                element is the program; the rest are its arguments.
            timeout_s: Wall-clock budget in seconds. Converted to
                milliseconds for the backend.

        Returns:
            The command's outcome, with timeout and OOM exits mapped to
            the conventional codes.

        Raises:
            CodeGuardianError: ``SYS_INVARIANT_VIOLATED`` when the
                adapter is not active, ``argv`` is empty, or
                ``timeout_s`` is not positive. ``SANDBOX_EXEC_FAILED``
                when the backend raises a non-typed exception.
        """
        if self._handle is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "sandbox adapter is not active; enter it with a with-block",
                details={"argv": list(argv)},
            )
        if not argv:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "argv must contain at least the program name",
                details={"argv": []},
            )
        if timeout_s <= 0:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"timeout_s must be positive; got {timeout_s!r}",
                details={"timeout_s": timeout_s},
            )

        timeout_ms = int(timeout_s * 1000)
        try:
            result = self._backend.execute(
                self._handle,
                list(argv),
                timeout_ms=timeout_ms,
            )
        except CodeGuardianError:
            raise
        except Exception as exc:
            raise CodeGuardianError(
                ErrorCode.SANDBOX_EXEC_FAILED,
                f"sandbox backend raised {type(exc).__name__}: {exc}",
                details={"argv": list(argv)},
            ) from exc

        return _to_outcome(result)

    def write_file(self, path: str, content: str) -> None:
        """Write a file into the sandbox's working directory.

        The write happens on the host. The working directory is
        bind-mounted read-write at the same absolute path, so the file
        is visible inside the sandbox with no translation.

        A path that escapes the working directory after resolution is
        refused. This is the adapter's path-traversal guard: a relative
        path is joined to the root before resolution, and an absolute
        path is resolved as-is, so ``..`` segments and symlinks are
        both caught by the same containment check.

        Args:
            path: Absolute or root-relative path. A relative path is
                interpreted relative to the sandbox's working
                directory.
            content: UTF-8 text to write. Parent directories are
                created as needed.

        Raises:
            CodeGuardianError: ``SYS_INVARIANT_VIOLATED`` when the
                adapter is not active. ``SANDBOX_EXEC_FAILED`` when the
                resolved path is outside the working directory or the
                filesystem refuses the write.
        """
        if self._handle is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "sandbox adapter is not active; enter it with a with-block",
                details={"path": path},
            )

        root = Path(self._spec.working_dir).resolve()
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()

        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CodeGuardianError(
                ErrorCode.SANDBOX_EXEC_FAILED,
                f"refusing to write outside the sandbox working directory: {path!r}",
                details={"path": path, "working_dir": str(root)},
            ) from exc

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SANDBOX_EXEC_FAILED,
                f"could not write {path!r} into the sandbox: {exc}",
                details={"path": str(resolved)},
            ) from exc


BubblewrapExecutorAdapter = SandboxExecutorAdapter
"""Alias used by the Phase 13 build order.

The adapter is backend-agnostic, so the Bubblewrap-specific name is an
alias rather than a subclass. A caller that imports the alias gets the
same class.
"""
