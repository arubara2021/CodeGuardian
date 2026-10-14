"""Cross-phase integration smoke tests.

At the Phase 5 boundary this file verifies that both bridges are
importable, constructible, and degrade gracefully. Later phases add
their own cross-phase scenarios to this file.
"""

from __future__ import annotations

import pytest

from codeguardian.core.bridges import jsonrpc_bridge, pyo3_bridge
from codeguardian.core.errors import CodeGuardianError, ErrorCode


def test_bridge_modules_importable() -> None:
    assert pyo3_bridge is not None
    assert jsonrpc_bridge is not None


def test_pyo3_bridge_construction_succeeds() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    assert bridge is not None


def test_jsonrpc_bridge_construction_succeeds() -> None:
    bridge = jsonrpc_bridge.JSONRPCBridge(["true"])
    assert bridge.is_running is False


def test_jsonrpc_bridge_typed_error_on_bad_command() -> None:
    bridge = jsonrpc_bridge.JSONRPCBridge(["/definitely/not/a/real/binary"])
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.start()
    assert exc_info.value.code == ErrorCode.SANDBOX_CREATE_FAILED
