"""Tests for the audit log exposed through the JSON-RPC daemon.

The daemon is a separate binary. The tests spawn it with
`JSONRPCBridge` and drive it with real requests. They are skipped when
the binary is not built.

The audit log is a file on disk. Every test uses a temporary directory
so nothing is left behind and no test can see another's state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError


def _binary_path() -> str | None:
    """Return the path to the rpcd binary, or None if not built.

    Checks `CODEGUARDIAN_RPCD` first, then the default location the
    workspace's `CARGO_TARGET_DIR` uses.
    """
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


@pytest.fixture
def audit_file(tmp_path):
    """Return a path inside a temporary directory for the audit log."""
    return str(tmp_path / "audit.jsonl")


def test_audit_write_returns_entry_with_hashes(bridge, audit_file):
    result = bridge.call("audit.write", {"path": audit_file, "payload": {"event": "started"}})
    assert "payload_hash" in result
    assert "entry_hash" in result
    assert result["prev_hash"] == "GENESIS"


def test_audit_write_empty_payload_succeeds(bridge, audit_file):
    result = bridge.call("audit.write", {"path": audit_file, "payload": {}})
    assert result["prev_hash"] == "GENESIS"


def test_audit_write_null_payload_succeeds(bridge, audit_file):
    result = bridge.call("audit.write", {"path": audit_file, "payload": None})
    assert result["prev_hash"] == "GENESIS"


def test_audit_write_creates_file(bridge, audit_file):
    bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    assert Path(audit_file).is_file()


def test_audit_write_chains_consecutive_entries(bridge, audit_file):
    first = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    second = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 2}})
    assert second["prev_hash"] == first["entry_hash"]


def test_audit_write_different_payloads_different_hashes(bridge, audit_file):
    first = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    second = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 2}})
    assert first["payload_hash"] != second["payload_hash"]


def test_audit_write_same_payload_same_payload_hash(bridge, audit_file):
    first = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    second = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    assert first["payload_hash"] == second["payload_hash"]


def test_audit_verify_empty_file_returns_zero(bridge, audit_file):
    result = bridge.call("audit.verify", {"path": audit_file})
    assert result["entries_checked"] == 0
    assert result["head_hash"] == "GENESIS"


def test_audit_verify_nonexistent_file_returns_zero(bridge, tmp_path):
    result = bridge.call("audit.verify", {"path": str(tmp_path / "missing.jsonl")})
    assert result["entries_checked"] == 0


def test_audit_verify_after_writes_counts_entries(bridge, audit_file):
    for n in range(5):
        bridge.call("audit.write", {"path": audit_file, "payload": {"n": n}})
    result = bridge.call("audit.verify", {"path": audit_file})
    assert result["entries_checked"] == 5


def test_audit_verify_head_matches_last_entry(bridge, audit_file):
    last = bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    result = bridge.call("audit.verify", {"path": audit_file})
    assert result["head_hash"] == last["entry_hash"]


def test_audit_verify_detects_tampered_payload(bridge, audit_file):
    bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    bridge.call("audit.write", {"path": audit_file, "payload": {"n": 2}})
    lines = Path(audit_file).read_text().strip().split("\n")
    entry = json.loads(lines[0])
    entry["payload"]["n"] = 999
    lines[0] = json.dumps(entry)
    Path(audit_file).write_text("\n".join(lines) + "\n")
    with pytest.raises(CodeGuardianError):
        bridge.call("audit.verify", {"path": audit_file})


def test_audit_export_jsonl(bridge, audit_file):
    bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    bridge.call("audit.write", {"path": audit_file, "payload": {"n": 2}})
    result = bridge.call("audit.export", {"path": audit_file, "format": "jsonl"})
    assert result["count"] == 2
    lines = result["content"].strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert "entry_hash" in parsed


def test_audit_export_json_array(bridge, audit_file):
    bridge.call("audit.write", {"path": audit_file, "payload": {"n": 1}})
    result = bridge.call("audit.export", {"path": audit_file, "format": "json"})
    assert result["count"] == 1
    parsed = json.loads(result["content"])
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["payload"]["n"] == 1


def test_audit_export_empty_jsonl(bridge, audit_file):
    result = bridge.call("audit.export", {"path": audit_file, "format": "jsonl"})
    assert result["count"] == 0
    assert result["content"] == ""


def test_audit_export_unknown_format_raises(bridge, audit_file):
    with pytest.raises(CodeGuardianError):
        bridge.call("audit.export", {"path": audit_file, "format": "xml"})


def test_audit_write_missing_path_raises(bridge):
    with pytest.raises(CodeGuardianError):
        bridge.call("audit.write", {"payload": {"n": 1}})


def test_unknown_method_raises(bridge):
    with pytest.raises(CodeGuardianError):
        bridge.call("does.not.exist", {})
