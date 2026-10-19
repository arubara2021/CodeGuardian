"""``SandboxBackend`` implementation backed by the codeguardian-rpcd daemon.

This module is a JSON-RPC client. It does not spawn ``bwrap`` itself.
Every command goes through ``sandbox.create``, ``sandbox.execute``, and
``sandbox.destroy`` on the daemon, which owns the mount policy, the
quota enforcement, and the two-layer timeout. That split is the trust
boundary: a bug in Python cannot remove the isolation; it can only ask
the daemon to run something.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.config.defaults import (
    DEFAULT_NETWORK_DURING_DEPS,
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

__all__ = ["BubblewrapBackend"]


_DEFAULT_LIMITS = ResourceLimits(
    memory_mb=DEFAULT_SANDBOX_MEMORY_MB,
    cpu_cores=DEFAULT_SANDBOX_CPU_CORES,
    process_limit=DEFAULT_SANDBOX_PROCESS_LIMIT,
    network_enabled=DEFAULT_NETWORK_DURING_DEPS,
)

_METHOD_CREATE = "sandbox.create"
_METHOD_EXECUTE = "sandbox.execute"
_METHOD_DESTROY = "sandbox.destroy"


class BubblewrapBackend:
    """The ``SandboxBackend`` implementation for Bubblewrap on Linux."""

    def __init__(
        self,
        bridge: JSONRPCBridge,
        *,
        resource_limits: ResourceLimits | None = None,
    ) -> None:
        self._bridge = bridge
        self._limits = resource_limits or _DEFAULT_LIMITS

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Create a bubblewrap sandbox in the daemon."""
        params: dict[str, object] = {
            "language": spec.language,
            "working_dir": str(spec.working_dir),
            "backend": "bubblewrap",
            "memory_mb": spec.memory_mb,
            "cpu_cores": spec.cpu_cores,
            "process_limit": spec.process_limit,
            "network_enabled": spec.network_during_deps,
        }
        try:
            response = self._bridge.call(_METHOD_CREATE, params)
        except CodeGuardianError as exc:
            raise _translate_daemon_error(exc, _METHOD_CREATE) from exc
        return _handle_from_response(response)

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
        """Run ``command`` inside the bubblewrap sandbox."""
        params: dict[str, object] = {
            "handle": _handle_to_dict(sandbox),
            "command": list(command),
        }
        if env is not None:
            params["env"] = dict(env)
        if cwd is not None:
            params["cwd"] = str(cwd)
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        if stdin is not None:
            try:
                params["stdin"] = stdin.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CodeGuardianError(
                    ErrorCode.SANDBOX_EXEC_FAILED,
                    "stdin must be UTF-8; the v1 sandbox protocol carries stdin as a JSON string",
                    details={"length": len(stdin)},
                ) from exc
        try:
            response = self._bridge.call(_METHOD_EXECUTE, params)
        except CodeGuardianError as exc:
            raise _translate_daemon_error(exc, _METHOD_EXECUTE) from exc
        return _result_from_response(response)

    def destroy(self, sandbox: SandboxHandle) -> None:
        """Destroy the bubblewrap sandbox."""
        try:
            self._bridge.call(_METHOD_DESTROY, _handle_to_dict(sandbox))
        except CodeGuardianError as exc:
            raise _translate_daemon_error(exc, _METHOD_DESTROY) from exc

    def supports_language(self, language: str) -> bool:
        """Return True; bubblewrap runs any Linux binary."""
        del language
        return True

    def get_resource_limits(self) -> ResourceLimits:
        """Return the limits this backend enforces by default."""
        return self._limits


def _handle_to_dict(handle: SandboxHandle) -> dict[str, object]:
    """Serialize a ``SandboxHandle`` into the daemon's wire shape."""
    return {
        "id": handle.id,
        "backend": handle.backend,
        "language": handle.language,
        "working_dir": str(handle.working_dir),
        "network_enabled": handle.network_enabled,
        "memory_mb": handle.memory_mb,
        "cpu_cores": handle.cpu_cores,
        "process_limit": handle.process_limit,
        "image": handle.image,
    }


def _handle_from_response(data: object) -> SandboxHandle:
    """Build a ``SandboxHandle`` from a ``sandbox.create`` response."""
    if not isinstance(data, dict):
        raise CodeGuardianError(
            ErrorCode.SANDBOX_CREATE_FAILED,
            "daemon returned a malformed handle",
            details={"payload_type": type(data).__name__},
        )
    return SandboxHandle(
        id=str(data["id"]),
        backend=str(data["backend"]),
        language=str(data["language"]),
        working_dir=Path(str(data["working_dir"])),
        network_enabled=bool(data["network_enabled"]),
        memory_mb=int(data["memory_mb"]),
        cpu_cores=int(data["cpu_cores"]),
        process_limit=int(data["process_limit"]),
        image=(str(data["image"]) if data.get("image") else None),
    )


def _result_from_response(data: object) -> ExecutionResult:
    """Build an ``ExecutionResult`` from a ``sandbox.execute`` response."""
    if not isinstance(data, dict):
        raise CodeGuardianError(
            ErrorCode.SANDBOX_EXEC_FAILED,
            "daemon returned a malformed execution result",
            details={"payload_type": type(data).__name__},
        )
    return ExecutionResult(
        stdout=str(data["stdout"]),
        stderr=str(data["stderr"]),
        exit_code=int(data["exit_code"]),
        timed_out=bool(data["timed_out"]),
        duration_ms=int(data["duration_ms"]),
        oom_killed=bool(data.get("oom_killed", False)),
        output_truncated=bool(data.get("output_truncated", False)),
    )


def _translate_daemon_error(
    exc: CodeGuardianError,
    method: str,
) -> CodeGuardianError:
    """Translate a generic RPC error into a sandbox-specific typed error.

    The ``JSONRPCBridge`` reports every daemon error as ``SYS_INTERNAL``
    because it cannot know which RPC method the failure belongs to. The
    backend does know the method, so it narrows the code here. The
    original message and details are preserved verbatim.
    """
    message = exc.message.lower()
    if "image" in message and (
        "unavailable" in message or "required" in message or "missing" in message
    ):
        code = ErrorCode.SANDBOX_IMAGE_UNAVAILABLE
    elif "quota" in message:
        code = ErrorCode.SANDBOX_QUOTA_EXCEEDED
    elif "not found" in message or "does not exist" in message:
        code = ErrorCode.SANDBOX_NOT_FOUND
    elif "timed out" in message or "timeout" in message:
        code = ErrorCode.SANDBOX_TIMEOUT
    elif "not implemented" in message or method == _METHOD_CREATE:
        code = ErrorCode.SANDBOX_CREATE_FAILED
    elif method == _METHOD_DESTROY:
        code = ErrorCode.SANDBOX_NOT_FOUND
    else:
        code = ErrorCode.SANDBOX_EXEC_FAILED
    return CodeGuardianError(code, exc.message, details=exc.details)
