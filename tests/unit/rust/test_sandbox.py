"""Tests for the sandbox engine exposed through the JSON-RPC daemon.

The daemon is a separate binary. The tests spawn it with
`JSONRPCBridge` and drive it with real requests. They are skipped when
the binary is not built.

The tests exercise the sandbox control plane only: health, handle
creation, and handle destruction. Executing a command inside a sandbox
requires an installed backend and is exercised by the enforcement
tests in `test_sandbox_enforcement.py`.
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
    assert "seatbelt" in backends
    assert "firecracker" in backends


def test_sandbox_health_bubblewrap_supports_python(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["bubblewrap"]["supports_python"] is True


def test_sandbox_health_bubblewrap_supports_typescript(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["bubblewrap"]["supports_typescript"] is True


def test_sandbox_health_docker_supports_python(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["docker"]["supports_python"] is True


def test_sandbox_health_stubs_support_nothing(bridge):
    result = bridge.call("sandbox.health", {})
    assert result["backends"]["seatbelt"]["supports_python"] is False
    assert result["backends"]["firecracker"]["supports_python"] is False


def test_sandbox_create_bubblewrap_returns_handle(bridge, tmp_path):
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
    assert result["image"] is None


def test_sandbox_create_bubblewrap_uses_default_backend(bridge, tmp_path):
    result = bridge.call(
        "sandbox.create",
        {"language": "python", "working_dir": str(tmp_path)},
    )
    assert result["backend"] == "bubblewrap"


def test_sandbox_create_bubblewrap_accepts_any_language(bridge, tmp_path):
    # Bubblewrap runs any Linux binary. Language is a caller hint, not
    # an allow list, so a language with no special support works.
    result = bridge.call(
        "sandbox.create",
        {
            "language": "rust",
            "working_dir": str(tmp_path),
            "backend": "bubblewrap",
        },
    )
    assert result["backend"] == "bubblewrap"
    assert result["language"] == "rust"


def test_sandbox_create_docker_requires_image(bridge, tmp_path):
    # Docker needs an image to know what to run. Without one, create
    # fails with an ImageUnavailable error that names the missing field.
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.call(
            "sandbox.create",
            {
                "language": "typescript",
                "working_dir": str(tmp_path),
                "backend": "docker",
            },
        )
    assert "image" in exc_info.value.message.lower()


def test_sandbox_create_docker_requires_image_regardless_of_language(bridge, tmp_path):
    # No language is rejected by docker upfront; the missing image is
    # what fails, and it fails the same way for any language.
    with pytest.raises(CodeGuardianError):
        bridge.call(
            "sandbox.create",
            {
                "language": "ruby",
                "working_dir": str(tmp_path),
                "backend": "docker",
            },
        )


def test_sandbox_create_seatbelt_stub_raises(bridge, tmp_path):
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": str(tmp_path),
                "backend": "seatbelt",
            },
        )
    assert "not implemented" in exc_info.value.message.lower()


def test_sandbox_create_firecracker_stub_raises(bridge, tmp_path):
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": str(tmp_path),
                "backend": "firecracker",
            },
        )
    assert "not implemented" in exc_info.value.message.lower()


def test_sandbox_create_unknown_backend_raises(bridge, tmp_path):
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.call(
            "sandbox.create",
            {
                "language": "python",
                "working_dir": str(tmp_path),
                "backend": "qemu",
            },
        )
    assert "unknown backend" in exc_info.value.message.lower()


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
