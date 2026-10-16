"""Enforcement tests for the sandbox engine.

Unlike the sandbox control-plane tests, these tests actually run
commands inside a sandbox and verify the enforcement held. They are
skipped when the rpcd binary or the `bwrap` executable is not
available.

The tests assert six properties: that a command runs and its output
comes back, that stdin is delivered, that the working directory is
writable while the rest of the filesystem is read-only, that the
timeout terminates a long-running process, that a forbidden environment
variable is refused, and that a top-level working directory is refused.

Timeout handling has two layers. The inner layer is the `timeout`
command running inside the sandbox, which terminates the process at
the requested deadline and returns an execution result with the
`timed_out` flag set. The outer layer is a Rust-side tokio timeout
that fires only if the inner one failed; it returns a
`CodeGuardianError`. The test below exercises the inner layer, which
is the normal path.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError


def _binary_path() -> str | None:
    env = os.environ.get("CODEGUARDIAN_RPCD")
    if env and Path(env).is_file():
        return env
    default = Path.home() / ".cache" / "codeguardian-target" / "release" / "codeguardian-rpcd"
    if default.is_file():
        return str(default)
    return None


_BINARY = _binary_path()
_HAS_BWRAP = shutil.which("bwrap") is not None

pytestmark = pytest.mark.skipif(
    _BINARY is None or not _HAS_BWRAP,
    reason="rpcd binary or bwrap not available",
)


@pytest.fixture
def bridge():
    with JSONRPCBridge([_BINARY]) as b:
        yield b


@pytest.fixture
def handle(bridge, tmp_path):
    return bridge.call(
        "sandbox.create",
        {
            "language": "python",
            "working_dir": str(tmp_path),
            "backend": "bubblewrap",
        },
    )


def test_execute_echo_returns_stdout(bridge, handle):
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/echo", "hello"],
            "timeout_ms": 10000,
        },
    )
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_execute_returns_stderr(bridge, handle):
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/sh", "-c", "echo err 1>&2"],
            "timeout_ms": 10000,
        },
    )
    assert result["exit_code"] == 0
    assert "err" in result["stderr"]


def test_execute_nonzero_exit_code(bridge, handle):
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/sh", "-c", "exit 42"],
            "timeout_ms": 10000,
        },
    )
    assert result["exit_code"] == 42
    assert result["timed_out"] is False


def test_execute_stdin_is_delivered(bridge, handle):
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/cat"],
            "stdin": "piped input",
            "timeout_ms": 10000,
        },
    )
    assert result["exit_code"] == 0
    assert "piped input" in result["stdout"]


def test_execute_working_dir_is_writable(bridge, handle, tmp_path):
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": [
                "/bin/sh",
                "-c",
                f"touch {tmp_path}/written && /bin/echo ok",
            ],
            "timeout_ms": 10000,
        },
    )
    assert result["exit_code"] == 0
    assert (tmp_path / "written").exists()


def test_execute_root_is_read_only(bridge, handle):
    bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/sh", "-c", "touch /forbidden 2>&1 || true"],
            "timeout_ms": 10000,
        },
    )
    assert not Path("/forbidden").exists()


def test_execute_timeout_sets_flag(bridge, handle):
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/sh", "-c", "sleep 60"],
            "timeout_ms": 2000,
        },
    )
    assert result["timed_out"] is True


def test_execute_timeout_returns_within_deadline(bridge, handle):
    # The inner `timeout` command fires at 2000 ms. The outer Rust-side
    # timeout fires six seconds later. If the outer one is firing, the
    # call takes more than eight seconds. This test fails if the inner
    # layer did not work.
    import time

    start = time.monotonic()
    bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": ["/bin/sh", "-c", "sleep 60"],
            "timeout_ms": 2000,
        },
    )
    elapsed = time.monotonic() - start
    assert elapsed < 6.0, f"call took {elapsed:.1f}s; inner timeout did not fire"


def test_execute_rejects_forbidden_ld_preload(bridge, handle):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.execute",
            {
                "handle": handle,
                "command": ["/bin/true"],
                "env": {"LD_PRELOAD": "/tmp/evil.so"},
                "timeout_ms": 10000,
            },
        )


def test_execute_rejects_forbidden_pythonpath(bridge, handle):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.execute",
            {
                "handle": handle,
                "command": ["/bin/true"],
                "env": {"PYTHONPATH": "/tmp/injected"},
                "timeout_ms": 10000,
            },
        )


def test_create_rejects_root_working_dir(bridge):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": "/",
                "backend": "bubblewrap",
            },
        )


def test_create_rejects_etc_working_dir(bridge):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": "/etc",
                "backend": "bubblewrap",
            },
        )


def test_create_rejects_nonexistent_working_dir(bridge):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": "/this/path/does/not/exist",
                "backend": "bubblewrap",
            },
        )


def test_handle_carries_quota(bridge, tmp_path):
    handle = bridge.call(
        "sandbox.create",
        {
            "language": "python",
            "working_dir": str(tmp_path),
            "backend": "bubblewrap",
            "memory_mb": 512,
            "cpu_cores": 1,
            "process_limit": 128,
        },
    )
    assert handle["memory_mb"] == 512
    assert handle["cpu_cores"] == 1
    assert handle["process_limit"] == 128


def test_execute_large_output_marks_truncated(bridge, handle):
    # Write 20 MB to stdout. The cap is 16 MB; the flag must be set.
    result = bridge.call(
        "sandbox.execute",
        {
            "handle": handle,
            "command": [
                "/bin/sh",
                "-c",
                "head -c 20971520 /dev/zero | tr '\\0' 'a'",
            ],
            "timeout_ms": 30000,
        },
    )
    assert result["output_truncated"] is True
