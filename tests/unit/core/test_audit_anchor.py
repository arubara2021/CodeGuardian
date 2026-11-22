"""Tests for the external audit anchor.

The anchor is the mechanism that makes audit-chain truncation
detectable. The tests exercise the three shipped anchors against the
truncation, tampering, and forgery cases the anchor exists to catch.
The RFC 3161 anchor is tested against a mocked openssl invocation so
the test suite does not depend on a live Time-Stamp Authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.orchestrator.audit_anchor import (
    FileAnchor,
    NoopAnchor,
    RFC3161Anchor,
    verify_chain_against_anchor,
)

_TEST_KEY = b"unit-test-key-0123456789abcdef"


def _entries(*hashes: str) -> list[dict[str, str]]:
    return [{"entry_hash": h} for h in hashes]


class TestFileAnchor:
    def test_round_trip(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        anchor.write_head("abc123", None)
        assert anchor.read_head() == "abc123"

    def test_missing_anchor_returns_none(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        assert anchor.read_head() is None

    def test_write_overwrites(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        anchor.write_head("first", None)
        anchor.write_head("second", "first")
        assert anchor.read_head() == "second"

    def test_hmac_tamper_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "anchor.json"
        anchor = FileAnchor(path, key=_TEST_KEY)
        anchor.write_head("abc123", None)

        record = json.loads(path.read_text())
        record["head_hash"] = "forged"
        path.write_text(json.dumps(record))

        with pytest.raises(CodeGuardianError):
            anchor.read_head()

    def test_wrong_key_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "anchor.json"
        writer = FileAnchor(path, key=_TEST_KEY)
        writer.write_head("abc123", None)

        reader = FileAnchor(path, key=b"different-key-0123456789abcdefgh")
        with pytest.raises(CodeGuardianError):
            reader.read_head()


class TestVerifyAgainstAnchor:
    def test_matching_head_passes(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        anchor.write_head("h3", None)
        verify_chain_against_anchor(_entries("h1", "h2", "h3"), anchor)

    def test_truncation_detected(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        anchor.write_head("h3", None)

        with pytest.raises(CodeGuardianError):
            verify_chain_against_anchor(_entries("h1"), anchor)

    def test_empty_chain_with_anchor_rejected(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        anchor.write_head("h3", None)

        with pytest.raises(CodeGuardianError):
            verify_chain_against_anchor([], anchor)

    def test_unanchored_chain_rejected(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        with pytest.raises(CodeGuardianError):
            verify_chain_against_anchor(_entries("h1", "h2"), anchor)

    def test_both_empty_passes(self, tmp_path: Path) -> None:
        anchor = FileAnchor(tmp_path / "anchor.json", key=_TEST_KEY)
        verify_chain_against_anchor([], anchor)


class TestNoopAnchor:
    def test_write_and_read_return_none(self) -> None:
        anchor = NoopAnchor()
        anchor.write_head("h1", None)
        assert anchor.read_head() is None

    def test_verify_always_passes(self) -> None:
        verify_chain_against_anchor(_entries("h1"), NoopAnchor())
        verify_chain_against_anchor([], NoopAnchor())


class TestRFC3161Anchor:
    @pytest.fixture
    def mock_openssl(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        monkeypatch.setattr("shutil.which", lambda _name: "/fake/openssl")

        def fake_run(argv, **kwargs):
            for i, arg in enumerate(argv):
                if arg == "-out" and i + 1 < len(argv):
                    Path(argv[i + 1]).write_bytes(b"fake-token")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock = MagicMock(side_effect=fake_run)
        monkeypatch.setattr(
            "codeguardian.core.orchestrator.audit_anchor.subprocess.run",
            mock,
        )
        return mock

    def test_write_and_read_round_trip(
        self,
        tmp_path: Path,
        mock_openssl: MagicMock,
    ) -> None:
        chain = tmp_path / "audit.jsonl"
        chain.write_text("")
        anchor = RFC3161Anchor(chain)
        anchor.write_head("h1", None)
        assert (tmp_path / "audit.jsonl.head").is_file()
        assert (tmp_path / "audit.jsonl.tsr").is_file()
        assert anchor.read_head() == "h1"

    def test_missing_token_rejected(
        self,
        tmp_path: Path,
        mock_openssl: MagicMock,
    ) -> None:
        chain = tmp_path / "audit.jsonl"
        chain.write_text("")
        anchor = RFC3161Anchor(chain)
        anchor.write_head("h1", None)
        (tmp_path / "audit.jsonl.tsr").unlink()

        with pytest.raises(CodeGuardianError):
            anchor.read_head()

    def test_verify_failure_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/fake/openssl")
        mock = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(
            "codeguardian.core.orchestrator.audit_anchor.subprocess.run",
            mock,
        )
        chain = tmp_path / "audit.jsonl"
        chain.write_text("")
        anchor = RFC3161Anchor(chain)
        anchor.write_head("h1", None)

        def fail_verify(argv, **kwargs):
            if "verify" in argv:
                return MagicMock(returncode=1, stdout="", stderr="invalid signature")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock.side_effect = fail_verify
        with pytest.raises(CodeGuardianError):
            anchor.read_head()

    def test_missing_openssl_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        chain = tmp_path / "audit.jsonl"
        chain.write_text("")
        with pytest.raises(CodeGuardianError):
            RFC3161Anchor(chain)
