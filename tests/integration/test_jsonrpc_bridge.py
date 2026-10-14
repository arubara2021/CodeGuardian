"""Integration tests for the JSON-RPC bridge.

Each test spawns a real Python responder subprocess and drives it
through the bridge. The responders implement the same line-delimited
JSON-RPC 2.0 framing the Rust server will implement in Phase 6.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codeguardian.core.bridges import jsonrpc_bridge
from codeguardian.core.errors import CodeGuardianError, ErrorCode

_ECHO_SERVER = """\
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in request:
        continue
    method = request.get("method", "")
    if method == "echo":
        result = {"echo": request.get("params")}
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    elif method == "fail":
        error = {"code": -32000, "message": "requested failure"}
        response = {"jsonrpc": "2.0", "id": request["id"], "error": error}
    else:
        error = {"code": -32601, "message": "method not found"}
        response = {"jsonrpc": "2.0", "id": request["id"], "error": error}
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
"""

_SILENT_SERVER = """\
import sys
sys.stdin.readline()
sys.exit(0)
"""

_INVALID_JSON_SERVER = """\
import sys
sys.stdin.readline()
sys.stdout.write("this is not valid json\\n")
sys.stdout.flush()
"""


def _write_script(tmp_path: Path, name: str, source: str) -> list[str]:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.fixture
def echo_server_command(tmp_path: Path) -> list[str]:
    return _write_script(tmp_path, "echo_server.py", _ECHO_SERVER)


@pytest.fixture
def silent_server_command(tmp_path: Path) -> list[str]:
    return _write_script(tmp_path, "silent_server.py", _SILENT_SERVER)


@pytest.fixture
def invalid_json_server_command(tmp_path: Path) -> list[str]:
    return _write_script(tmp_path, "invalid_json_server.py", _INVALID_JSON_SERVER)


def test_call_round_trip(echo_server_command: list[str]) -> None:
    with jsonrpc_bridge.JSONRPCBridge(echo_server_command) as bridge:
        result = bridge.call("echo", {"value": 42})
    assert result == {"echo": {"value": 42}}


def test_multiple_sequential_calls(echo_server_command: list[str]) -> None:
    with jsonrpc_bridge.JSONRPCBridge(echo_server_command) as bridge:
        assert bridge.call("echo", {"n": 1}) == {"echo": {"n": 1}}
        assert bridge.call("echo", {"n": 2}) == {"echo": {"n": 2}}
        assert bridge.call("echo", {"n": 3}) == {"echo": {"n": 3}}


def test_error_response_raises_typed_error(echo_server_command: list[str]) -> None:
    with (
        jsonrpc_bridge.JSONRPCBridge(echo_server_command) as bridge,
        pytest.raises(CodeGuardianError) as exc_info,
    ):
        bridge.call("fail", {})
    assert exc_info.value.code == ErrorCode.SYS_INTERNAL
    assert "requested failure" in exc_info.value.message


def test_unknown_method_returns_typed_error(echo_server_command: list[str]) -> None:
    with (
        jsonrpc_bridge.JSONRPCBridge(echo_server_command) as bridge,
        pytest.raises(CodeGuardianError) as exc_info,
    ):
        bridge.call("nonexistent", {})
    assert exc_info.value.code == ErrorCode.SYS_INTERNAL
    assert "method not found" in exc_info.value.message


def test_notify_does_not_block(echo_server_command: list[str]) -> None:
    with jsonrpc_bridge.JSONRPCBridge(echo_server_command) as bridge:
        bridge.notify("event", {"kind": "test"})
        result = bridge.call("echo", {"after": "notify"})
    assert result == {"echo": {"after": "notify"}}


def test_invalid_json_from_server_raises_protocol_error(
    invalid_json_server_command: list[str],
) -> None:
    with (
        jsonrpc_bridge.JSONRPCBridge(invalid_json_server_command) as bridge,
        pytest.raises(CodeGuardianError) as exc_info,
    ):
        bridge.call("echo", {})
    assert exc_info.value.code == ErrorCode.MCP_PROTOCOL_ERROR


def test_server_closes_stdout_raises_typed_error(
    silent_server_command: list[str],
) -> None:
    with (
        jsonrpc_bridge.JSONRPCBridge(silent_server_command) as bridge,
        pytest.raises(CodeGuardianError) as exc_info,
    ):
        bridge.call("echo", {})
    assert exc_info.value.code == ErrorCode.SYS_INTERNAL


def test_call_before_start_raises_invariant(echo_server_command: list[str]) -> None:
    bridge = jsonrpc_bridge.JSONRPCBridge(echo_server_command)
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.call("echo", {})
    assert exc_info.value.code == ErrorCode.SYS_INVARIANT_VIOLATED


def test_double_start_raises_invariant(echo_server_command: list[str]) -> None:
    bridge = jsonrpc_bridge.JSONRPCBridge(echo_server_command)
    bridge.start()
    try:
        with pytest.raises(CodeGuardianError) as exc_info:
            bridge.start()
        assert exc_info.value.code == ErrorCode.SYS_INVARIANT_VIOLATED
    finally:
        bridge.stop()


def test_stop_terminates_subprocess(echo_server_command: list[str]) -> None:
    bridge = jsonrpc_bridge.JSONRPCBridge(echo_server_command)
    bridge.start()
    assert bridge.is_running is True
    bridge.stop()
    assert bridge.is_running is False


def test_spawn_failure_raises_typed_error() -> None:
    bridge = jsonrpc_bridge.JSONRPCBridge(["/nonexistent/binary/path"])
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.start()
    assert exc_info.value.code == ErrorCode.SANDBOX_CREATE_FAILED


def test_context_manager_cleans_up(echo_server_command: list[str]) -> None:
    with jsonrpc_bridge.JSONRPCBridge(echo_server_command) as bridge:
        assert bridge.is_running is True
        bridge.call("echo", {"ok": True})
    assert bridge.is_running is False
