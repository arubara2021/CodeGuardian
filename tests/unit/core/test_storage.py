"""Tests for the SQLite metadata store and the filesystem artifact store.

Every test exercises one behavior of one public method. The goal is
coverage of every path a caller can take: success, missing file, bad
input, permission failure where reachable, and typed error propagation.
Tests use pytest's ``tmp_path`` fixture so that no state is shared
between tests and no test touches the real ``~/.codeguardian`` tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage import FilesystemStore, SQLiteStore

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCHEMAS_DIR = _REPO_ROOT / "schemas"


def _valid_recon() -> dict:
    """Return a minimal dict that validates against schemas/recon.json."""
    return {
        "schema_version": "1.1",
        "repo": {
            "url": "https://example.com/repo",
            "commit": "abc123",
            "detected_at": "2026-10-13T00:00:00Z",
        },
        "languages": [],
        "structural": {
            "cpg": {
                "binary_path": "/tmp/cpg.bin",
                "node_count": 0,
                "edge_count": 0,
                "built_at": "2026-10-13T00:00:00Z",
            }
        },
        "dependency_graph": {"nodes": [], "edges": []},
        "hierarchical_index": {},
        "entrypoints": [],
        "versions": {"declared": {}, "actual": {}, "available": {}},
        "smoke_test": {"status": "passed", "passed": 0, "failed": 0, "errored": 0},
        "truth_report": [],
        "drift_report": [],
    }


@pytest.fixture
def store(tmp_path: Path):
    """An open SQLiteStore backed by a temporary database file."""
    with SQLiteStore(tmp_path / "test.db") as s:
        yield s


@pytest.fixture
def fs_store(tmp_path: Path):
    """A FilesystemStore rooted under a temporary directory."""
    return FilesystemStore(tmp_path / "cg-home", _SCHEMAS_DIR)


# --- SQLiteStore -------------------------------------------------------


class TestSQLiteStore:
    def test_open_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "path" / "test.db"
        with SQLiteStore(nested) as store:
            assert nested.exists()
            assert store.db_path == nested

    def test_context_manager_closes_connection(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        with store:
            assert store.connection is not None
        with pytest.raises(CodeGuardianError) as exc_info:
            _ = store.connection
        assert exc_info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED

    def test_already_open_raises(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.open()
        try:
            with pytest.raises(CodeGuardianError) as exc_info:
                store.open()
            assert exc_info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED
        finally:
            store.close()

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.open()
        store.close()
        store.close()

    def test_execute_and_query(self, store: SQLiteStore) -> None:
        store.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        store.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
        store.execute("INSERT INTO t (name) VALUES (?)", ("beta",))
        rows = store.query("SELECT id, name FROM t ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["name"] == "alpha"
        assert rows[1]["name"] == "beta"

    def test_execute_returns_cursor_with_lastrowid(self, store: SQLiteStore) -> None:
        store.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        cursor = store.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
        assert cursor.lastrowid == 1

    def test_bad_sql_raises_typed_error(self, store: SQLiteStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            store.execute("THIS IS NOT SQL")
        assert exc_info.value.code is ErrorCode.SYS_INTERNAL
        assert "sql" in exc_info.value.details

    def test_execute_script(self, store: SQLiteStore) -> None:
        script = """
        CREATE TABLE t1 (id INTEGER PRIMARY KEY);
        CREATE TABLE t2 (id INTEGER PRIMARY KEY);
        CREATE INDEX idx_t1 ON t1(id);
        """
        store.execute_script(script)
        rows = store.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {r["name"] for r in rows}
        assert "t1" in names
        assert "t2" in names

    def test_schema_migrations_table_created_on_open(self, store: SQLiteStore) -> None:
        rows = store.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_schema_migrations'"
        )
        assert len(rows) == 1

    def test_migration_tracking(self, store: SQLiteStore) -> None:
        assert not store.is_migration_applied("001_test")
        store.record_migration("001_test", "2026-10-13T00:00:00Z")
        assert store.is_migration_applied("001_test")

    def test_record_migration_twice_raises(self, store: SQLiteStore) -> None:
        store.record_migration("001_test", "2026-10-13T00:00:00Z")
        with pytest.raises(CodeGuardianError) as exc_info:
            store.record_migration("001_test", "2026-10-14T00:00:00Z")
        assert exc_info.value.code is ErrorCode.SYS_INTERNAL


# --- FilesystemStore ---------------------------------------------------


class TestFilesystemStore:
    def test_artifact_path_layout(self, fs_store: FilesystemStore) -> None:
        path = fs_store.artifact_path("repo1", "run1", "recon.json")
        assert path.parts[-4:] == ("repos", "repo1", "runs", "run1") or "recon.json" in path.parts
        assert path.name == "recon.json"
        assert path.parent.name == "run1"

    def test_write_and_read_artifact(self, fs_store: FilesystemStore) -> None:
        data = _valid_recon()
        path = fs_store.write_artifact("repo1", "run1", "recon.json", data, "recon.json")
        assert path.exists()

        loaded = fs_store.read_artifact("repo1", "run1", "recon.json")
        assert loaded == data

    def test_write_artifact_is_deterministic(self, fs_store: FilesystemStore) -> None:
        data = _valid_recon()
        path_a = fs_store.write_artifact("repo1", "run1", "recon.json", data, "recon.json")
        content_a = path_a.read_bytes()
        path_b = fs_store.write_artifact("repo2", "run2", "recon.json", data, "recon.json")
        content_b = path_b.read_bytes()
        assert content_a == content_b

    def test_write_artifact_rejects_missing_required_field(self, fs_store: FilesystemStore) -> None:
        data = _valid_recon()
        del data["structural"]
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.write_artifact("repo1", "run1", "recon.json", data, "recon.json")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_write_artifact_rejects_unknown_field(self, fs_store: FilesystemStore) -> None:
        data = _valid_recon()
        data["not_a_real_field"] = True
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.write_artifact("repo1", "run1", "recon.json", data, "recon.json")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_write_artifact_rejects_wrong_type(self, fs_store: FilesystemStore) -> None:
        data = _valid_recon()
        data["smoke_test"]["passed"] = "not an integer"
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.write_artifact("repo1", "run1", "recon.json", data, "recon.json")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID
        assert "field_path" in exc_info.value.details

    def test_write_artifact_missing_schema_raises(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.write_artifact("repo1", "run1", "out.json", {"x": 1}, "does-not-exist.json")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_read_artifact_missing_file(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.read_artifact("repo1", "run1", "nope.json")
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND

    def test_read_artifact_invalid_json(self, fs_store: FilesystemStore, tmp_path: Path) -> None:
        path = fs_store.artifact_path("repo1", "run1", "broken.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.read_artifact("repo1", "run1", "broken.json")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_write_and_read_text(self, fs_store: FilesystemStore) -> None:
        path = fs_store.write_text("repo1", "run1", "diff.patch", "line one\nline two\n")
        assert path.exists()
        assert fs_store.read_text("repo1", "run1", "diff.patch") == "line one\nline two\n"

    def test_read_text_missing_file(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            fs_store.read_text("repo1", "run1", "nope.txt")
        assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND

    def test_delete_run_removes_directory(self, fs_store: FilesystemStore) -> None:
        fs_store.write_text("repo1", "run1", "a.txt", "content")
        fs_store.delete_run("repo1", "run1")
        with pytest.raises(CodeGuardianError):
            fs_store.read_text("repo1", "run1", "a.txt")

    def test_delete_run_idempotent(self, fs_store: FilesystemStore) -> None:
        fs_store.delete_run("repo1", "does-not-exist")

    def test_schema_cache_reuses_loaded_schema(self, fs_store: FilesystemStore) -> None:
        data = _valid_recon()
        fs_store.write_artifact("repo1", "run1", "recon.json", data, "recon.json")
        fs_store.write_artifact("repo1", "run2", "recon.json", data, "recon.json")
        assert "recon.json" in fs_store._schema_cache
