"""JSON-RPC 2.0 client over a subprocess pipe.

Each message is one JSON object followed by a newline. Requests carry
an integer id; responses carry the same id. Notifications carry no id
and expect no response. Framing is line-delimited: JSON never emits a
literal newline inside a string, so one line is always one message.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from codeguardian.core.bridges.protocol import (
    RpcError,
    RpcNotification,
    RpcRequest,
    RpcResponse,
)
from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = ["JSONRPCBridge"]

_JSONRPC_VERSION = "2.0"
_SHUTDOWN_TIMEOUT_SECONDS = 5


class JSONRPCBridge:
    """Client for a Rust subprocess speaking JSON-RPC 2.0."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1

    @property
    def is_running(self) -> bool:
        """Return whether the subprocess is alive."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the subprocess and open the pipes."""
        if self._proc is not None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "JSONRPCBridge is already started.",
            )
        try:
            self._proc = subprocess.Popen(
                self._command,
                cwd=str(self._cwd) if self._cwd is not None else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SANDBOX_CREATE_FAILED,
                f"Failed to spawn bridge subprocess: {exc}",
            ) from exc

    def stop(self) -> None:
        """Terminate the subprocess and close the pipes."""
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        finally:
            self._proc = None

    def call(self, method: str, params: dict[str, object]) -> object:
        """Send a request and return its result.

        Raises ``CodeGuardianError`` with ``SYS_INTERNAL`` if the
        subprocess reports an error, or with ``MCP_PROTOCOL_ERROR`` if
        the response is not valid JSON.
        """
        request_id = self._next_id
        self._next_id += 1
        request = RpcRequest(id=request_id, method=method, params=params)
        self._write(request)
        response = self._read_response(request_id)
        if response.error is not None:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"RPC error from {method}: {response.error.message}",
                details={"rpc_code": response.error.code, "method": method},
            )
        return response.result

    def notify(self, method: str, params: dict[str, object]) -> None:
        """Send a notification. No response is expected."""
        self._write(RpcNotification(method=method, params=params))

    def __enter__(self) -> JSONRPCBridge:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def _require_proc(self) -> subprocess.Popen[str]:
        if self._proc is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "JSONRPCBridge is not started.",
            )
        return self._proc

    def _write(self, payload: RpcRequest | RpcNotification) -> None:
        proc = self._require_proc()
        if proc.stdin is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "Bridge subprocess has no stdin.",
            )
        if isinstance(payload, RpcRequest):
            wire: dict[str, object] = {
                "jsonrpc": _JSONRPC_VERSION,
                "id": payload.id,
                "method": payload.method,
                "params": payload.params,
            }
        else:
            wire = {
                "jsonrpc": _JSONRPC_VERSION,
                "method": payload.method,
                "params": payload.params,
            }
        try:
            proc.stdin.write(json.dumps(wire) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Failed to write to bridge subprocess: {exc}",
            ) from exc

    def _read_response(self, expected_id: int) -> RpcResponse:
        proc = self._require_proc()
        if proc.stdout is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "Bridge subprocess has no stdout.",
            )
        while True:
            line = proc.stdout.readline()
            if not line:
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    "Bridge subprocess closed stdout before responding.",
                )
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodeGuardianError(
                    ErrorCode.MCP_PROTOCOL_ERROR,
                    f"Bridge sent invalid JSON: {exc}",
                ) from exc
            if payload.get("id") != expected_id:
                continue
            error_payload = payload.get("error")
            rpc_error: RpcError | None = None
            if error_payload is not None:
                rpc_error = RpcError(
                    code=int(error_payload.get("code", 0)),
                    message=str(error_payload.get("message", "")),
                    data=error_payload.get("data"),
                )
            return RpcResponse(
                id=payload.get("id"),
                result=payload.get("result"),
                error=rpc_error,
            )
