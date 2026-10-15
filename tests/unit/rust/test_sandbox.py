"""Tests for the sandbox engine exposed through the JSON-RPC daemon.

The daemon is a separate binary. The tests spawn it with
`JSONRPCBridge` and drive it with real requests. They are skipped when
the binary is not built.

The tests exercise the sandbox control plane only: health, handle
creation, and handle destruction. Executing a command inside a sandbox
requires an installed backend and is exercised by end-to-end tests in
a later phase.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError


def _binary_path() -> str | None:
    """Return the path to the rpcd binary, or None if not built."""
    env = os.environ.get("CODEGUARDIAN_RPCD")
    if env and Path(env).is_file():
        return env
    default = Path.home() / ".cache" / "codeguardian-target" / "release" / "codeguardian-rpcd"
    if default.is_file():
        return str(default)
    return None


_BINARY = _binary_path()

pytestmark = pytest.mark.skipif(
    _BINARY is None,
    reason="rpcd binary not built; run: cargo build --release -p codeguardian-rpcd",
)


@pytest.fixture
def bridge():
    """Yield a started bridge to the rpcd binary."""
    with JSONRPCBridge([_BINARY]) as b:
        yield b


def test_sandbox_health_reports_status_ok(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["status"] == "ok"


def test_sandbox_health_reports_backends(bridge):
    result = bridge.call("sandbox.health", {})
    backends = result["backends"]
    assert "bubblewrap" in backends
    assert "docker" in backends


def test_sandbox_health_bubblewrap_supports_python(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["bubblewrap"]["supports_python"] is True


def test_sandbox_health_bubblewrap_supports_typescript(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["bubblewrap"]["supports_typescript"] is True


def test_sandbox_health_docker_supports_python(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["docker"]["supports_python"] is True


def test_sandbox_create_returns_handle(bridge, tmp_path):
    result = bridge.call(
        "sandbox.create",
        {
            "language": "python",
            "working_dir": str(tmp_path),
            "backend": "bubblewrap",
        },
    )
    assert result["backend"] == "bubblewrap"
    assert result["language"] == "python"
    assert result["id"].startswith("bwrap-")
    assert result["network_enabled"] is False


def test_sandbox_create_uses_bubblewrap_by_default(bridge, tmp_path):
    result = bridge.call(
        "sandbox.create",
        {"language": "python", "working_dir": str(tmp_path)},
    )
    assert result["backend"] == "bubblewrap"


def test_sandbox_create_docker_backend(bridge, tmp_path):
    result = bridge.call(
        "sandbox.create",
        {
            "language": "typescript",
            "working_dir": str(tmp_path),
            "backend": "docker",
        },
    )
    assert result["backend"] == "docker"
    assert result["id"].startswith("docker-")


def test_sandbox_create_unknown_backend_raises(bridge, tmp_path):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": str(tmp_path),
                "backend": "qemu",
            },
        )


def test_sandbox_create_unknown_language_with_docker_raises(bridge, tmp_path):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.create",
            {
                "language": "ruby",
                "working_dir": str(tmp_path),
                "backend": "docker",
            },
        )


def test_sandbox_destroy_succeeds_for_created_handle(bridge, tmp_path):
    handle = bridge.call(
        "sandbox.create",
        {"language": "python", "working_dir": str(tmp_path)},
    )
    result = bridge.call("sandbox.destroy", handle)
    assert result["destroyed"] is True


def test_sandbox_destroy_unknown_backend_raises(bridge, tmp_path):
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.destroy",
            {
                "id": "fake-1",
                "backend": "qemu",
                "language": "python",
                "working_dir": str(tmp_path),
                "network_enabled": False,
            },
        )


def test_sandbox_destroy_foreign_handle_raises(bridge, tmp_path):
    # A handle whose backend field names bubblewrap but whose id has the
    # docker prefix is not one bubblewrap produced; the backend returns
    # NotFound because the field does not match its own name.
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.destroy",
            {
                "id": "docker-99",
                "backend": "qemu-not-a-real-backend",
                "language": "python",
                "working_dir": str(tmp_path),
                "network_enabled": False,
            },
        )
