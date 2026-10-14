"""Tests for the WAL-mode configuration of SQLiteStore.

The design specifies six PRAGMAs on every connection. Each has a
specific reason: WAL for concurrent reads, NORMAL for cheap fsync,
mmap for fewer syscalls, busy_timeout for lock contention, and so on.
These tests verify that each PRAGMA was actually applied, because a
silent fallback (network filesystem refusing WAL, for example) would
otherwise go undetected until a user hit a concurrency bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.storage import SQLiteStore


@pytest.fixture
def store(tmp_path: Path):
    with SQLiteStore(tmp_path / "test.db") as s:
        yield s


class TestWALConfiguration:
    def test_journal_mode_is_wal(self, store: SQLiteStore) -> None:
        rows = store.query("PRAGMA journal_mode")
        assert rows[0][0].lower() == "wal"

    def test_synchronous_is_normal(self, store: SQLiteStore) -> None:
        rows = store.query("PRAGMA synchronous")
        # NORMAL is 1.
        assert rows[0][0] == 1

    def test_wal_autocheckpoint(self, store: SQLiteStore) -> None:
        rows = store.query("PRAGMA wal_autocheckpoint")
        assert rows[0][0] == 10000

    def test_busy_timeout(self, store: SQLiteStore) -> None:
        rows = store.query("PRAGMA busy_timeout")
        assert rows[0][0] == 5000

    def test_cache_size(self, store: SQLiteStore) -> None:
        rows = store.query("PRAGMA cache_size")
        # Negative means KiB, so -64000 = 64 MiB.
        assert rows[0][0] == -64000

    def test_mmap_size(self, store: SQLiteStore) -> None:
        rows = store.query("PRAGMA mmap_size")
        # SQLite may clamp this to a platform maximum. The value must
        # be positive, and on systems that support 256 MiB mappings it
        # will be exactly 268435456.
        assert rows[0][0] > 0

    def test_wal_sidecar_files_created_after_write(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        store.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        store.execute("INSERT INTO t DEFAULT VALUES")
        wal_path = Path(str(store.db_path) + "-wal")
        assert wal_path.exists()

    def test_reopen_preserves_wal_mode(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        with SQLiteStore(db_path) as first:
            rows = first.query("PRAGMA journal_mode")
            assert rows[0][0].lower() == "wal"

        with SQLiteStore(db_path) as second:
            rows = second.query("PRAGMA journal_mode")
            assert rows[0][0].lower() == "wal"
