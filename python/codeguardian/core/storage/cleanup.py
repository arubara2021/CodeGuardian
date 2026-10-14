"""Retention sweep for on-disk artifacts, graph files, and cache entries.

CodeGuardian accumulates files across runs. Without a sweep, a user who
runs the tool daily for a year would accumulate years of debug artifacts
and unused graph files. This module implements the retention policy
documented in ``docs/05-data-model.md`` section 7: debug artifacts are
deleted after 30 days, graph files and cached payloads after 90 days,
and the audit log, run summaries, plan approvals, consent records, and
approved skills are kept forever.

The sweep deletes files but keeps database rows for runs, so the run
history remains queryable. It removes ``cpg_metadata`` rows alongside
their files, because the graph is a rebuildable cache with no value
once the file is gone. It removes ``cache_entries`` rows alongside
their payloads for the same reason.

The function is pure logic: one call performs one sweep and returns a
summary. The background thread that invokes it periodically belongs to
the orchestrator (Phase 13), not to storage. This separation keeps the
sweep easy to test without sleeping or threading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError

from .filesystem_store import FilesystemStore
from .graph_store import GraphStore
from .sqlite_store import SQLiteStore

__all__ = ["CleanupSummary", "run_cleanup"]


_DEFAULT_ARTIFACTS_RETENTION_DAYS: int = 30
_DEFAULT_GRAPH_RETENTION_DAYS: int = 90
_DEFAULT_WAL_THRESHOLD_BYTES: int = 100 * 1024 * 1024


# Files inside a run directory that are pruned when the run is old
# enough. Files not listed here are retained: audit.jsonl and
# summary.md are compliance artifacts with unlimited retention. The
# database rows for the run, its plan approval, and its consent records
# are also retained regardless of the sweep.
_PRUNABLE_RUN_FILES: tuple[str, ...] = (
    "recon.json",
    "runtime_contract_map.json",
    "blast_radius.json",
    "context_pack.json",
    "contract_assertions.json",
    "test_target.py",
    "refactored_target.py",
    "diff.patch",
    "verification.json",
    "checkpoint.json",
)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """The outcome of one cleanup sweep.

    Every count is the number of items affected on this call. Byte
    counts are a lower bound: if a file could not be stat'd before
    being removed, its size is not included. The ``wal_checkpointed``
    flag is True when the sweep forced a WAL checkpoint because the
    sidecar file had grown past the threshold.
    """

    runs_swept: int
    run_files_deleted: int
    graph_files_deleted: int
    cache_entries_deleted: int
    bytes_freed: int
    wal_checkpointed: bool


def run_cleanup(
    sqlite_store: SQLiteStore,
    filesystem_store: FilesystemStore,
    graph_store: GraphStore,
    *,
    now: datetime | None = None,
    artifacts_retention_days: int = _DEFAULT_ARTIFACTS_RETENTION_DAYS,
    graph_retention_days: int = _DEFAULT_GRAPH_RETENTION_DAYS,
    wal_threshold_bytes: int = _DEFAULT_WAL_THRESHOLD_BYTES,
) -> CleanupSummary:
    """Perform one retention sweep and return a summary.

    Args:
        sqlite_store: An open store. Not closed by this function.
        filesystem_store: Used to compute artifact paths for the runs
            that are old enough to prune.
        graph_store: Used to delete graph files and their metadata rows.
        now: The reference time. Defaults to the current UTC time. Tests
            pass an explicit value so that cutoffs are deterministic.
        artifacts_retention_days: Age at which run artifacts are pruned.
        graph_retention_days: Age at which graph files and cached
            payloads are pruned.
        wal_threshold_bytes: Size above which the SQLite WAL is
            checkpointed with TRUNCATE.

    Returns:
        A ``CleanupSummary`` describing what was removed.

    Raises:
        CodeGuardianError: With code SYS_INTERNAL if a database operation
            fails.
    """
    reference = now if now is not None else datetime.now(UTC)
    artifacts_cutoff = (reference - timedelta(days=artifacts_retention_days)).isoformat()
    graph_cutoff = (reference - timedelta(days=graph_retention_days)).isoformat()

    runs_swept = 0
    run_files_deleted = 0
    graph_files_deleted = 0
    cache_entries_deleted = 0
    bytes_freed = 0

    old_runs = sqlite_store.query(
        """
        SELECT run_id, repo_id FROM runs
        WHERE started_at < ? AND status != 'running'
        """,
        (artifacts_cutoff,),
    )
    for row in old_runs:
        run_id = row["run_id"]
        repo_id = row["repo_id"]
        for filename in _PRUNABLE_RUN_FILES:
            path = filesystem_store.artifact_path(repo_id, run_id, filename)
            if not path.exists():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            try:
                path.unlink()
            except OSError:
                # A file that cannot be removed is left in place. The
                # next sweep will try again. There is no recovery
                # action worth taking here; the sweep is best-effort.
                continue
            run_files_deleted += 1
            bytes_freed += size
        runs_swept += 1

    old_graphs = sqlite_store.query(
        """
        SELECT repo_id, commit_hash, binary_path FROM cpg_metadata
        WHERE built_at < ?
        """,
        (graph_cutoff,),
    )
    for row in old_graphs:
        repo_id = row["repo_id"]
        commit_hash = row["commit_hash"]
        binary_path_str = row["binary_path"]
        if binary_path_str:
            binary_path = Path(binary_path_str)
            if binary_path.exists():
                try:
                    size = binary_path.stat().st_size
                except OSError:
                    size = 0
                try:
                    binary_path.unlink()
                    graph_files_deleted += 1
                    bytes_freed += size
                except OSError:
                    pass
        sqlite_store.execute(
            "DELETE FROM cpg_metadata WHERE repo_id = ? AND commit_hash = ?",
            (repo_id, commit_hash),
        )

    old_cache = sqlite_store.query(
        """
        SELECT repo_id, commit_hash, cache_type, payload_path
        FROM cache_entries
        WHERE last_accessed_at < ?
        """,
        (graph_cutoff,),
    )
    for row in old_cache:
        payload_path_str = row["payload_path"]
        if payload_path_str:
            payload_path = Path(payload_path_str)
            if payload_path.exists():
                try:
                    size = payload_path.stat().st_size
                except OSError:
                    size = 0
                try:
                    payload_path.unlink()
                    bytes_freed += size
                except OSError:
                    pass
        sqlite_store.execute(
            """
            DELETE FROM cache_entries
            WHERE repo_id = ? AND commit_hash = ? AND cache_type = ?
            """,
            (row["repo_id"], row["commit_hash"], row["cache_type"]),
        )
        cache_entries_deleted += 1

    wal_checkpointed = False
    wal_path = Path(str(sqlite_store.db_path) + "-wal")
    if wal_path.exists():
        try:
            wal_size = wal_path.stat().st_size
        except OSError:
            wal_size = 0
        if wal_size > wal_threshold_bytes:
            try:
                sqlite_store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                wal_checkpointed = True
            except CodeGuardianError:
                # The checkpoint failed. It is not fatal; the WAL will
                # be checkpointed eventually by the automatic threshold.
                pass

    sqlite_store.execute(
        """
        INSERT INTO cleanup_log
            (cleanup_id, ran_at, artifacts_removed,
             cache_entries_removed, bytes_freed)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            _now_iso(),
            run_files_deleted + graph_files_deleted,
            cache_entries_deleted,
            bytes_freed,
        ),
    )

    return CleanupSummary(
        runs_swept=runs_swept,
        run_files_deleted=run_files_deleted,
        graph_files_deleted=graph_files_deleted,
        cache_entries_deleted=cache_entries_deleted,
        bytes_freed=bytes_freed,
        wal_checkpointed=wal_checkpointed,
    )
