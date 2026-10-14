"""Integration tests for the in-process PyO3 bridge.

Exercises the compiled native extension from Python. The suite is
skipped if ``codeguardian._native`` is not importable, so CI runs on
platforms without the extension still pass.
"""

from __future__ import annotations

import pytest

from codeguardian.core.bridges import pyo3_bridge
from codeguardian.core.errors import CodeGuardianError, ErrorCode

pytestmark = pytest.mark.skipif(
    not pyo3_bridge.is_native_available(),
    reason="native extension not built",
)


def test_is_native_available() -> None:
    assert pyo3_bridge.is_native_available() is True


def test_echo_round_trip() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    assert bridge.echo("hello") == "hello"


def test_echo_empty_string() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    assert bridge.echo("") == ""


def test_echo_unicode() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    text = "hello \u00e9 \u4e2d\u6587 \U0001f389"
    assert bridge.echo(text) == text


def test_echo_newlines() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    text = "line1\nline2\r\nline3"
    assert bridge.echo(text) == text


def test_echo_null_byte() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    text = "before\x00after"
    assert bridge.echo(text) == text


def test_echo_large_string() -> None:
    bridge = pyo3_bridge.PyO3Bridge()
    text = "a" * 100_000
    assert bridge.echo(text) == text


def test_multiple_bridges_independent() -> None:
    first = pyo3_bridge.PyO3Bridge()
    second = pyo3_bridge.PyO3Bridge()
    assert first.echo("first") == "first"
    assert second.echo("second") == "second"


def test_missing_native_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pyo3_bridge, "_native_module", None)
    monkeypatch.setattr(pyo3_bridge, "_load_attempted", True)
    bridge = pyo3_bridge.PyO3Bridge()
    with pytest.raises(CodeGuardianError) as exc_info:
        bridge.echo("x")
    assert exc_info.value.code == ErrorCode.SYS_NOT_IMPLEMENTED
    assert "codeguardian._native" in exc_info.value.message
