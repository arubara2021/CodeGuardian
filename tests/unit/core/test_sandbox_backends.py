"""Tests for the Python sandbox backends from B-040.

The bubblewrap tests spawn ``codeguardian-rpcd`` via ``JSONRPCBridge``
and exercise the five ``SandboxBackend`` methods against the real
daemon and the real ``bwrap`` binary. No mocks. They skip cleanly if
the daemon binary is missing or if ``bwrap`` is not installed.

The docker tests check the Python-side behavior that does not require
a working container runtime. A test that actually ran ``docker run``
would skip on any machine without WSL integration enabled; because
those are not present in this file, the two docker tests always run.

The stub tests never touch the bridge. Seatbelt and Firecracker raise
``SANDBOX_CREATE_FAILED`` from ``create`` with no network or subprocess
call, so those tests always run on any host.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

import pytest

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.sandbox.bubblewrap_backend import BubblewrapBackend
from codeguardian.core.sandbox.docker_backend import DockerBackend
from codeguardian.core.sandbox.firecracker_backend import FirecrackerBackend
from codeguardian.core.sandbox.seatbelt_backend import SeatbeltBackend
from codeguardian.interfaces.sandbox import SandboxSpec


def _binary_path() -> str | None:
    """Return the rpcd binary path from env or the default build location."""
    env = os.environ.get("CODEGUARDIAN_RPCD")
    if env and Path(env).is_file():
        return env
    default = Path.home() / ".cache" / "codeguardian-target" / "release" / "codeguardian-rpcd"
    if default.is_file():
        return str(default)
    return None


_BINARY = _binary_path()
_HAS_BWRAP = shutil.which("bwrap") is not None
_HAS_DOCKER = shutil.which("docker") is not None


@pytest.fixture
def bridge():
    """Yield a started ``JSONRPCBridge`` to the daemon."""
    if _BINARY is None:
        pytest.skip("rpcd binary not built; run: " "cargo build --release -p codeguardian-rpcd")
    with JSONRPCBridge([_BINARY]) as b:
        yield b


@pytest.fixture
def spec(tmp_path: Path) -> SandboxSpec:
    """A sandbox spec pointing at a fresh temporary working directory."""
    return SandboxSpec(
        language="python",
        working_dir=tmp_path.resolve(),
        memory_mb=512,
        cpu_cores=1,
        process_limit=64,
        network_during_deps=False,
    )


@pytest.fixture
def live_bubblewrap(bridge, spec):
    """Yield a (backend, handle) pair, destroying the sandbox on exit."""
    backend = BubblewrapBackend(bridge)
    handle = backend.create(spec)
    try:
        yield backend, handle
    finally:
        with contextlib.suppress(CodeGuardianError):
            backend.destroy(handle)


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
class TestBubblewrapBackend:
    def test_create_returns_handle_with_expected_fields(self, bridge, spec):
        backend = BubblewrapBackend(bridge)
        handle = backend.create(spec)
        try:
            assert handle.backend == "bubblewrap"
            assert handle.language == "python"
            assert handle.working_dir == spec.working_dir
            assert handle.memory_mb == spec.memory_mb
            assert handle.cpu_cores == spec.cpu_cores
            assert handle.process_limit == spec.process_limit
            assert handle.network_enabled is False
            assert handle.id.startswith("bwrap-")
            assert handle.image is None
        finally:
            backend.destroy(handle)

    def test_execute_echo_returns_stdout(self, live_bubblewrap):
        backend, handle = live_bubblewrap
        result = backend.execute(handle, ["/bin/echo", "hello"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.oom_killed is False
        assert result.output_truncated is False
        assert result.duration_ms >= 0

    def test_execute_propagates_exit_code(self, live_bubblewrap):
        backend, handle = live_bubblewrap
        result = backend.execute(handle, ["/bin/sh", "-c", "exit 42"])
        assert result.exit_code == 42

    def test_execute_pwd_matches_working_dir(self, live_bubblewrap):
        backend, handle = live_bubblewrap
        result = backend.execute(handle, ["/bin/pwd"])
        assert result.exit_code == 0
        assert result.stdout.strip() == str(handle.working_dir)

    def test_execute_reports_timeout(self, live_bubblewrap):
        backend, handle = live_bubblewrap
        result = backend.execute(
            handle,
            ["/bin/sh", "-c", "sleep 5"],
            timeout_ms=500,
        )
        assert result.timed_out is True
        assert result.exit_code != 0

    def test_execute_with_env_passes_variable(self, live_bubblewrap):
        backend, handle = live_bubblewrap
        result = backend.execute(
            handle,
            ["/bin/sh", "-c", "echo $CG_TEST"],
            env={"CG_TEST": "value-from-host"},
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == "value-from-host"

    def test_execute_with_stdin_writes_to_child(self, live_bubblewrap):
        backend, handle = live_bubblewrap
        result = backend.execute(
            handle,
            ["/bin/cat"],
            stdin=b"piped-through-stdin",
        )
        assert result.exit_code == 0
        assert "piped-through-stdin" in result.stdout

    def test_destroy_succeeds(self, bridge, spec):
        backend = BubblewrapBackend(bridge)
        handle = backend.create(spec)
        backend.destroy(handle)

    def test_supports_language_returns_true_for_any(self):
        backend = BubblewrapBackend(None)  # type: ignore[arg-type]
        assert backend.supports_language("python") is True
        assert backend.supports_language("rust") is True
        assert backend.supports_language("") is True

    def test_get_resource_limits_returns_defaults(self):
        backend = BubblewrapBackend(None)  # type: ignore[arg-type]
        limits = backend.get_resource_limits()
        assert limits.memory_mb == 1024
        assert limits.cpu_cores == 2
        assert limits.process_limit == 256


class TestDockerBackend:
    def test_create_without_image_raises(self, bridge, spec):
        backend = DockerBackend(bridge)
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.create(spec)
        assert exc_info.value.code is ErrorCode.SANDBOX_IMAGE_UNAVAILABLE
        assert "image" in exc_info.value.message.lower()

    def test_supports_language_returns_true_for_any(self):
        backend = DockerBackend(None)  # type: ignore[arg-type]
        assert backend.supports_language("python") is True
        assert backend.supports_language("typescript") is True


class TestSeatbeltBackend:
    def test_create_raises_not_implemented(self, spec):
        backend = SeatbeltBackend(None)  # type: ignore[arg-type]
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.create(spec)
        assert exc_info.value.code is ErrorCode.SANDBOX_CREATE_FAILED
        assert "not implemented" in exc_info.value.message.lower()

    def test_execute_raises_not_implemented(self, spec):
        backend = SeatbeltBackend(None)  # type: ignore[arg-type]
        fake_handle = _fake_handle(spec)
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.execute(fake_handle, ["/bin/echo"])
        assert exc_info.value.code is ErrorCode.SANDBOX_EXEC_FAILED
        assert "not implemented" in exc_info.value.message.lower()

    def test_destroy_raises_not_implemented(self, spec):
        backend = SeatbeltBackend(None)  # type: ignore[arg-type]
        fake_handle = _fake_handle(spec)
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.destroy(fake_handle)
        assert exc_info.value.code is ErrorCode.SANDBOX_NOT_FOUND
        assert "not implemented" in exc_info.value.message.lower()

    def test_supports_language_returns_false(self):
        backend = SeatbeltBackend(None)  # type: ignore[arg-type]
        assert backend.supports_language("python") is False
        assert backend.supports_language("typescript") is False

    def test_get_resource_limits_returns_defaults(self):
        backend = SeatbeltBackend(None)  # type: ignore[arg-type]
        limits = backend.get_resource_limits()
        assert limits.memory_mb == 1024
        assert limits.cpu_cores == 2
        assert limits.process_limit == 256


class TestFirecrackerBackend:
    def test_create_raises_not_implemented(self, spec):
        backend = FirecrackerBackend(None)  # type: ignore[arg-type]
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.create(spec)
        assert exc_info.value.code is ErrorCode.SANDBOX_CREATE_FAILED
        assert "not implemented" in exc_info.value.message.lower()

    def test_execute_raises_not_implemented(self, spec):
        backend = FirecrackerBackend(None)  # type: ignore[arg-type]
        fake_handle = _fake_handle(spec)
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.execute(fake_handle, ["/bin/echo"])
        assert exc_info.value.code is ErrorCode.SANDBOX_EXEC_FAILED
        assert "not implemented" in exc_info.value.message.lower()

    def test_destroy_raises_not_implemented(self, spec):
        backend = FirecrackerBackend(None)  # type: ignore[arg-type]
        fake_handle = _fake_handle(spec)
        with pytest.raises(CodeGuardianError) as exc_info:
            backend.destroy(fake_handle)
        assert exc_info.value.code is ErrorCode.SANDBOX_NOT_FOUND
        assert "not implemented" in exc_info.value.message.lower()

    def test_supports_language_returns_false(self):
        backend = FirecrackerBackend(None)  # type: ignore[arg-type]
        assert backend.supports_language("python") is False
        assert backend.supports_language("typescript") is False

    def test_get_resource_limits_returns_defaults(self):
        backend = FirecrackerBackend(None)  # type: ignore[arg-type]
        limits = backend.get_resource_limits()
        assert limits.memory_mb == 1024
        assert limits.cpu_cores == 2
        assert limits.process_limit == 256


def _fake_handle(spec: SandboxSpec):
    """Build a ``SandboxHandle`` from a spec for stub tests.

    The stubs never inspect the handle, so any well-formed instance
    works. Building it from the spec keeps the field values consistent
    with what ``create`` would have returned on a real backend.
    """
    from codeguardian.interfaces.sandbox import SandboxHandle

    return SandboxHandle(
        id="stub-1",
        backend="stub",
        language=spec.language,
        working_dir=spec.working_dir,
        network_enabled=spec.network_during_deps,
        memory_mb=spec.memory_mb,
        cpu_cores=spec.cpu_cores,
        process_limit=spec.process_limit,
        image=None,
    )
