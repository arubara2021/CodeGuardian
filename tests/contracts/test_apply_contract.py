"""Contract tests for ``ApplyProvider``.

The tests use a real git repository and real ``git apply``. Nothing is
mocked except the audit bridge, which is a recorded stub: the apply
logic under test is the production code, not a reimplementation.

Every failure branch is tested: bad consent hash, wrong consent scope,
remote URL, path not a directory, conflicting diff, rollback of an
unknown reference, and rollback before any apply.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.providers.apply_provider import ApplyProvider
from codeguardian.core.providers.local_repository import LocalProvider
from codeguardian.interfaces.apply import ConsentRecord
from codeguardian.interfaces.repository import FileChange


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` and raise if it fails."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


class _MockAuditBridge:
    """A minimal JSON-RPC bridge that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        return {"entry_hash": "test-hash", "prev_hash": "GENESIS"}


@pytest.fixture
def setup(tmp_path: Path):
    """Return (repo_path, diff, consent, audit_bridge) ready to apply."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@test.example")
    (source / "file.txt").write_text("old\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "initial")

    clone = tmp_path / "clone"
    repository = LocalProvider()
    repository.clone(str(source), clone)

    diff = repository.get_diff([FileChange(path="file.txt", new_content="new\n")])

    consent = ConsentRecord(
        consent_id="consent-1",
        run_id="run-1",
        granted_at=datetime.now(UTC),
        granted_by="test",
        scope="apply",
        diff_hash=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        rollback_ref="",
    )
    bridge = _MockAuditBridge()
    audit_log = tmp_path / "audit.jsonl"
    provider = ApplyProvider(repository, bridge, audit_log)
    return clone, diff, consent, bridge, provider


class TestApply:
    def test_apply_succeeds_and_modifies_file(self, setup):
        clone, diff, consent, _, provider = setup
        result = provider.apply(str(clone), diff, consent)
        assert result.success is True
        assert (clone / "file.txt").read_text(encoding="utf-8") == "new\n"
        assert result.applied_files == ("file.txt",)
        assert len(result.rollback_ref) == 40

    def test_apply_records_audit_entry(self, setup):
        clone, diff, consent, bridge, provider = setup
        provider.apply(str(clone), diff, consent)
        assert len(bridge.calls) == 1
        method, params = bridge.calls[0]
        assert method == "audit.write"
        assert params["payload"]["event"] == "apply"
        assert params["payload"]["consent_id"] == "consent-1"

    def test_apply_sha_mismatch_raises(self, setup):
        clone, diff, consent, _, provider = setup
        bad_consent = ConsentRecord(
            consent_id=consent.consent_id,
            run_id=consent.run_id,
            granted_at=consent.granted_at,
            granted_by=consent.granted_by,
            scope=consent.scope,
            diff_hash="0" * 64,
            rollback_ref=consent.rollback_ref,
        )
        with pytest.raises(CodeGuardianError) as exc:
            provider.apply(str(clone), diff, bad_consent)
        assert exc.value.code is ErrorCode.APPLY_AUTH_FAILED
        assert (clone / "file.txt").read_text(encoding="utf-8") == "old\n"

    def test_apply_wrong_scope_raises(self, setup):
        clone, diff, consent, _, provider = setup
        bad_consent = ConsentRecord(
            consent_id=consent.consent_id,
            run_id=consent.run_id,
            granted_at=consent.granted_at,
            granted_by=consent.granted_by,
            scope="verify",
            diff_hash=consent.diff_hash,
            rollback_ref=consent.rollback_ref,
        )
        with pytest.raises(CodeGuardianError) as exc:
            provider.apply(str(clone), diff, bad_consent)
        assert exc.value.code is ErrorCode.APPLY_AUTH_FAILED

    def test_apply_remote_url_raises(self, setup):
        _, diff, consent, _, provider = setup
        with pytest.raises(CodeGuardianError) as exc:
            provider.apply("https://github.com/x/y.git", diff, consent)
        assert exc.value.code is ErrorCode.SYS_NOT_IMPLEMENTED
        assert "local" in exc.value.message.lower()

    def test_apply_nonexistent_path_raises(self, setup, tmp_path):
        _, diff, consent, _, provider = setup
        with pytest.raises(CodeGuardianError) as exc:
            provider.apply(str(tmp_path / "nope"), diff, consent)
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND

    def test_apply_conflicting_diff_raises(self, setup):
        clone, diff, consent, _, provider = setup
        (clone / "file.txt").write_text("already-changed\n", encoding="utf-8")
        with pytest.raises(CodeGuardianError) as exc:
            provider.apply(str(clone), diff, consent)
        assert exc.value.code is ErrorCode.APPLY_CONFLICT
        assert (clone / "file.txt").read_text(encoding="utf-8") == "already-changed\n"


class TestRollback:
    def test_rollback_after_apply_restores_file(self, setup):
        clone, diff, consent, _, provider = setup
        result = provider.apply(str(clone), diff, consent)
        assert (clone / "file.txt").read_text(encoding="utf-8") == "new\n"
        rollback = provider.rollback(result.rollback_ref)
        assert rollback.success is True
        assert (clone / "file.txt").read_text(encoding="utf-8") == "old\n"
        assert rollback.restored_files == ("file.txt",)

    def test_rollback_before_apply_raises(self, setup):
        _, _, _, _, provider = setup
        with pytest.raises(CodeGuardianError) as exc:
            provider.rollback("deadbeef")
        assert exc.value.code is ErrorCode.ROLLBACK_REF_NOT_FOUND

    def test_rollback_unknown_ref_raises(self, setup):
        clone, diff, consent, _, provider = setup
        provider.apply(str(clone), diff, consent)
        with pytest.raises(CodeGuardianError) as exc:
            provider.rollback("0" * 40)
        assert exc.value.code is ErrorCode.ROLLBACK_REF_NOT_FOUND
