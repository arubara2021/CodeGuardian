"""Contract tests for every ``SandboxBackend`` implementation.

A contract test asserts the properties every implementation must
satisfy. It is not a substitute for the unit tests in
``tests/unit/core/test_sandbox_backends.py``; those test one backend in
depth. This file tests that all four backends answer the five-method
``SandboxBackend`` Protocol consistently, and that each one's documented
outcome on ``create`` is what its callers should expect.
"""

from __future__ import annotations

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
from codeguardian.interfaces.sandbox import (
    ResourceLimits,
    SandboxHandle,
    SandboxSpec,
)


def _binary_path() -> str | None:
    env = os.environ.get("CODEGUARDIAN_RPCD")
    if env and Path(env).is_file():
        return env
    default = Path.home() / ".cache" / "codeguardian-target" / "release" / "codeguardian-rpcd"
    return str(default) if default.is_file() else None


_BINARY = _binary_path()
_HAS_BWRAP = shutil.which("bwrap") is not None


@pytest.fixture
def bridge():
    if _BINARY is None:
        pytest.skip("rpcd binary not built")
    with JSONRPCBridge([_BINARY]) as b:
        yield b


@pytest.fixture
def spec(tmp_path: Path) -> SandboxSpec:
    return SandboxSpec(
        language="python",
        working_dir=tmp_path.resolve(),
        memory_mb=512,
        cpu_cores=1,
        process_limit=64,
        network_during_deps=False,
    )


BACKENDS = [
    pytest.param(
        "bubblewrap",
        BubblewrapBackend,
        marks=pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed"),
        id="bubblewrap",
    ),
    pytest.param("docker", DockerBackend, id="docker"),
    pytest.param("seatbelt", SeatbeltBackend, id="seatbelt"),
    pytest.param("firecracker", FirecrackerBackend, id="firecracker"),
]


@pytest.mark.parametrize("name,backend_class", BACKENDS)
class TestSandboxBackendContract:
    """Properties every backend must satisfy, regardless of implementation."""

    def test_get_resource_limits_returns_limits(self, name, backend_class):
        del name
        backend = backend_class(None)  # type: ignore[arg-type]
        limits = backend.get_resource_limits()
        assert isinstance(limits, ResourceLimits)
        assert limits.memory_mb > 0
        assert limits.cpu_cores > 0
        assert limits.process_limit > 0

    def test_supports_language_returns_bool(self, name, backend_class):
        del name
        backend = backend_class(None)  # type: ignore[arg-type]
        result = backend.supports_language("python")
        assert isinstance(result, bool)

    def test_create_returns_handle_or_raises_typed_error(self, name, backend_class, bridge, spec):
        del name
        backend = backend_class(bridge)
        try:
            handle = backend.create(spec)
        except CodeGuardianError as exc:
            assert exc.code in {
                ErrorCode.SANDBOX_CREATE_FAILED,
                ErrorCode.SANDBOX_IMAGE_UNAVAILABLE,
            }
            return
        try:
            assert isinstance(handle, SandboxHandle)
        finally:
            backend.destroy(handle)

    def test_execute_before_create_raises(self, name, backend_class, spec):
        del name
        backend = backend_class(None)  # type: ignore[arg-type]
        fake_handle = SandboxHandle(
            id="fake",
            backend="stub",
            language=spec.language,
            working_dir=spec.working_dir,
            network_enabled=False,
            memory_mb=spec.memory_mb,
            cpu_cores=spec.cpu_cores,
            process_limit=spec.process_limit,
            image=None,
        )
        if backend_class is BubblewrapBackend:
            pytest.skip("bubblewrap needs a live handle; covered by unit tests")
        if backend_class is DockerBackend:
            pytest.skip("docker execute needs a live handle; covered by unit tests")
        with pytest.raises(CodeGuardianError) as exc:
            backend.execute(fake_handle, ["/bin/echo"])
        assert exc.value.code is ErrorCode.SANDBOX_EXEC_FAILED


class TestBubblewrapContract:
    """The full lifecycle for the one backend that actually runs code."""

    @pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
    def test_full_lifecycle(self, bridge, spec):
        backend = BubblewrapBackend(bridge)
        handle = backend.create(spec)
        try:
            assert handle.backend == "bubblewrap"
            assert handle.working_dir == spec.working_dir
            result = backend.execute(handle, ["/bin/echo", "contract"])
            assert result.exit_code == 0
            assert result.stdout.strip() == "contract"
        finally:
            backend.destroy(handle)


class TestStubContract:
    """The two stubs share the same 'not implemented' behavior."""

    @pytest.mark.parametrize(
        "backend_class",
        [SeatbeltBackend, FirecrackerBackend],
        ids=["seatbelt", "firecracker"],
    )
    def test_create_raises_with_not_implemented_message(self, backend_class, spec):
        backend = backend_class(None)  # type: ignore[arg-type]
        with pytest.raises(CodeGuardianError) as exc:
            backend.create(spec)
        assert exc.value.code is ErrorCode.SANDBOX_CREATE_FAILED
        assert "not implemented" in exc.value.message.lower()

    @pytest.mark.parametrize(
        "backend_class",
        [SeatbeltBackend, FirecrackerBackend],
        ids=["seatbelt", "firecracker"],
    )
    def test_supports_language_returns_false(self, backend_class):
        backend = backend_class(None)  # type: ignore[arg-type]
        assert backend.supports_language("python") is False
        assert backend.supports_language("typescript") is False
