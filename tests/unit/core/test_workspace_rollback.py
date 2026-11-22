"""Tests for workspace snapshot and restore.

The snapshot module provides the state that a failed phase rolls back
to. The tests exercise the cases that determine whether a rollback is
safe: byte-identical restore, multi-path restore, the
compare-and-swap refusal when a third party modified the file, symlink
refusal, encrypted disk storage, and the non-Linux fallback path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.orchestrator.workspace_snapshot import (
    Snapshot,
    restore,
    snapshot_paths,
)


class TestSingleFileRestore:
    def test_byte_identical_restore(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_bytes(b"original content \x00\x01\x02")
        snap = snapshot_paths(tmp_path, ("file.txt",))
        target.write_bytes(b"modified")
        result = restore(snap)
        assert "file.txt" in result.restored
        assert target.read_bytes() == b"original content \x00\x01\x02"

    def test_restore_creates_deleted_file(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("original")
        snap = snapshot_paths(tmp_path, ("file.txt",))
        target.unlink()
        result = restore(snap)
        assert "file.txt" in result.restored
        assert target.read_text() == "original"

    def test_removes_file_created_after_snapshot(self, tmp_path: Path) -> None:
        snap = snapshot_paths(tmp_path, ("new.txt",))
        (tmp_path / "new.txt").write_text("agent-created")
        result = restore(snap)
        assert "new.txt" in result.removed
        assert not (tmp_path / "new.txt").exists()

    def test_preserves_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "exec.sh"
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        snap = snapshot_paths(tmp_path, ("exec.sh",))
        target.write_text("modified")
        restore(snap)
        assert (target.stat().st_mode & 0o777) == 0o755


class TestMultiPathRestore:
    def test_all_paths_restored(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a original")
        (tmp_path / "b.py").write_text("b original")
        (tmp_path / "c.py").write_text("c original")
        snap = snapshot_paths(tmp_path, ("a.py", "b.py", "c.py"))
        (tmp_path / "a.py").write_text("a modified")
        (tmp_path / "b.py").unlink()
        (tmp_path / "c.py").write_text("c modified")
        result = restore(snap)
        assert set(result.restored) == {"a.py", "b.py", "c.py"}
        assert (tmp_path / "a.py").read_text() == "a original"
        assert (tmp_path / "b.py").read_text() == "b original"
        assert (tmp_path / "c.py").read_text() == "c original"


class TestCompareAndSwap:
    def test_matching_expected_current_succeeds(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("snapshot content")
        snap = snapshot_paths(tmp_path, ("file.txt",))
        target.write_text("agent content")
        result = restore(snap, expected_current={"file.txt": b"agent content"})
        assert "file.txt" in result.restored
        assert target.read_text() == "snapshot content"

    def test_third_party_modification_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("snapshot")
        snap = snapshot_paths(tmp_path, ("file.txt",))
        target.write_text("agent wrote this")
        target.write_text("third party overwrote")
        result = restore(snap, expected_current={"file.txt": b"agent wrote this"})
        assert len(result.refused) == 1
        assert result.refused[0].reason == "modified_by_third_party"
        assert target.read_text() == "third party overwrote"

    def test_expected_missing_but_present_refused(self, tmp_path: Path) -> None:
        snap = snapshot_paths(tmp_path, ("file.txt",))
        (tmp_path / "file.txt").write_text("unexpected")
        result = restore(snap, expected_current={"file.txt": None})
        assert len(result.refused) == 1
        assert result.refused[0].reason == "expected_missing_but_present"

    def test_path_not_in_expected_current_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("snapshot")
        snap = snapshot_paths(tmp_path, ("file.txt",))
        target.write_text("modified")
        result = restore(snap, expected_current={})
        assert len(result.refused) == 1
        assert result.refused[0].reason == "not_in_expected_current"


class TestSymlinkRefusal:
    def test_symlink_component_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "file.txt").write_text("inside")
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(CodeGuardianError):
            snapshot_paths(tmp_path, ("link/file.txt",))

    def test_symlink_at_target_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real.txt"
        real.write_text("content")
        link = tmp_path / "file.txt"
        link.symlink_to(real)
        with pytest.raises(CodeGuardianError):
            snapshot_paths(tmp_path, ("file.txt",))


class TestPathValidation:
    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CodeGuardianError):
            snapshot_paths(tmp_path, ("/etc/passwd",))

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CodeGuardianError):
            snapshot_paths(tmp_path, ("../outside.txt",))

    def test_empty_normalized_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CodeGuardianError):
            snapshot_paths(tmp_path, ("./././",))

    def test_relative_root_rejected(self) -> None:
        with pytest.raises(CodeGuardianError):
            snapshot_paths(Path("relative"), ("file.txt",))


class TestDiskStorage:
    def test_encrypted_snapshot_does_not_contain_plaintext(
        self,
        tmp_path: Path,
    ) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            pytest.skip("cryptography package not installed")
        fernet = Fernet(Fernet.generate_key())
        target = tmp_path / "file.txt"
        target.write_text("SECRET-PLAINTEXT-MARKER")
        snap = snapshot_paths(
            tmp_path,
            ("file.txt",),
            storage="disk",
            fernet=fernet,
        )
        entry = snap.entries[0]
        assert entry.blob is not None
        assert b"SECRET-PLAINTEXT-MARKER" not in entry.blob
        target.write_text("modified")
        result = restore(snap, fernet=fernet)
        assert "file.txt" in result.restored
        assert target.read_text() == "SECRET-PLAINTEXT-MARKER"


class TestPlatformFallback:
    def test_fallback_when_rename_exchange_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from codeguardian.core.orchestrator import workspace_snapshot

        monkeypatch.setattr(
            workspace_snapshot,
            "_rename_exchange",
            lambda _s, _d: False,
        )
        target = tmp_path / "file.txt"
        target.write_text("snapshot content")
        snap = snapshot_paths(tmp_path, ("file.txt",))
        target.write_text("modified")
        result = restore(snap)
        assert "file.txt" in result.restored
        assert target.read_text() == "snapshot content"


class TestSnapshotShape:
    def test_entries_in_order(self, tmp_path: Path) -> None:
        (tmp_path / "z.txt").write_text("z")
        (tmp_path / "a.txt").write_text("a")
        snap: Snapshot = snapshot_paths(tmp_path, ("z.txt", "a.txt"))
        assert [e.rel_path for e in snap.entries] == ["z.txt", "a.txt"]

    def test_missing_path_recorded(self, tmp_path: Path) -> None:
        snap = snapshot_paths(tmp_path, ("missing.txt",))
        assert snap.entries[0].existed is False
        assert snap.entries[0].blob is None
