"""Two-tier cache for CodeGuardian: in-memory LRU plus disk.

The cache serves two distinct workloads. The in-memory tier holds
parsed ASTs and file contents for the duration of a run, keyed by a
string the caller chooses. It is a bounded LRU: when the entry count
exceeds the configured maximum, the least-recently-used entry is
evicted. There is no byte-size accounting; each entry counts as one,
regardless of how large the value is. This is the correct trade-off for
ASTs, which vary in size but whose count is bounded by the number of
files a single run touches.

The disk tier holds recon manifests, runtime contract maps, blast
radius reports, and MCP tool responses. Keys are the tuple
``(repo_id, commit_hash, cache_type)``, matching the primary key of the
``cache_entries`` table. Payloads smaller than the configured threshold
are stored inline in a SQLite BLOB; larger payloads are written to
``<root>/repos/<repo_id>/cache/<cache_type>-<commit_hash>.json`` and a
path is stored. The threshold exists so that SQLite stays compact and
so that a large recon manifest does not bloat the database file.

The disk tier tracks access counts and last-access timestamps on every
read. This lets the cleanup sweep identify cold entries. The access
columns are updated on ``get_disk`` only; a ``put_disk`` resets the
access count to one.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .filesystem_store import FilesystemStore
from .sqlite_store import SQLiteStore

__all__ = ["Cache"]


_DEFAULT_MAX_MEMORY_ENTRIES: int = 256
_DEFAULT_INLINE_THRESHOLD_BYTES: int = 65536


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Cache:
    """An LRU memory tier plus a SQLite-backed disk tier.

    The cache is scoped to one repository. All disk entries are written
    under ``(repo_id, commit_hash, cache_type)`` where ``repo_id`` is
    fixed at construction. A caller that works with multiple
    repositories constructs one cache per repository.
    """

    def __init__(
        self,
        sqlite_store: SQLiteStore,
        filesystem_store: FilesystemStore,
        repo_id: str,
        *,
        max_memory_entries: int = _DEFAULT_MAX_MEMORY_ENTRIES,
        inline_threshold_bytes: int = _DEFAULT_INLINE_THRESHOLD_BYTES,
    ) -> None:
        """Construct a cache bound to one repository.

        Args:
            sqlite_store: An open store used for the ``cache_entries``
                table. Not closed by this object.
            filesystem_store: Used to derive the on-disk cache path. Its
                ``root`` property determines where the file goes.
            repo_id: The repository identifier. Fixed for the cache's
                lifetime.
            max_memory_entries: Maximum number of entries in the LRU.
                Once exceeded, the least-recently-used entry is evicted.
            inline_threshold_bytes: Payloads strictly smaller than this
                are stored inline in the database. Payloads of this size
                or larger are written to a file and a path is stored.
        """
        self._sqlite = sqlite_store
        self._filesystem = filesystem_store
        self._repo_id = repo_id
        self._max_memory_entries = max_memory_entries
        self._inline_threshold_bytes = inline_threshold_bytes
        self._memory: OrderedDict[str, Any] = OrderedDict()

    # --- Memory tier -----------------------------------------------------

    @property
    def memory_size(self) -> int:
        """The current number of entries in the in-memory LRU."""
        return len(self._memory)

    def get_memory(self, key: str) -> Any | None:
        """Return the value for a key, or ``None`` if absent.

        A successful read moves the entry to the most-recently-used
        position so that it is evicted last.

        Args:
            key: The cache key. The caller chooses the key format.

        Returns:
            The cached value, or ``None`` if no entry exists for the key.
            A stored ``None`` is indistinguishable from a miss; callers
            that need the distinction should store a sentinel.
        """
        if key not in self._memory:
            return None
        self._memory.move_to_end(key)
        return self._memory[key]

    def put_memory(self, key: str, value: Any) -> None:
        """Store a value in the LRU, evicting the oldest if needed.

        Args:
            key: The cache key.
            value: The value. Any Python object.
        """
        if key in self._memory:
            self._memory.move_to_end(key)
        self._memory[key] = value
        while len(self._memory) > self._max_memory_entries:
            self._memory.popitem(last=False)

    def clear_memory(self) -> None:
        """Remove every entry from the in-memory tier."""
        self._memory.clear()

    # --- Disk tier -------------------------------------------------------

    def _cache_path(self, commit_hash: str, cache_type: str) -> Path:
        """Return the on-disk path for a large cache payload."""
        return (
            self._filesystem.root
            / "repos"
            / self._repo_id
            / "cache"
            / f"{cache_type}-{commit_hash}.json"
        )

    def get_disk(self, commit_hash: str, cache_type: str) -> bytes | None:
        """Return the cached payload for a key, or ``None`` on a miss.

        On a hit, the entry's ``last_accessed_at`` timestamp and
        ``access_count`` are updated. If the database says a payload
        exists on disk but the file is missing, the row is deleted and
        the call is treated as a miss.

        Args:
            commit_hash: The commit hash the payload is keyed against.
            cache_type: One of the types allowed by the schema:
                ``recon``, ``runtime_contract_map``, ``blast``, ``ast``,
                ``mcp_response``.

        Returns:
            The payload as raw bytes, or ``None``.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL on a database
                failure, FILE_PERMISSION_DENIED if a disk-resident
                payload exists but cannot be read.
        """
        rows = self._sqlite.query(
            """
            SELECT payload_path, payload_inline
            FROM cache_entries
            WHERE repo_id = ? AND commit_hash = ? AND cache_type = ?
            """,
            (self._repo_id, commit_hash, cache_type),
        )
        if not rows:
            return None

        row = rows[0]
        inline = row["payload_inline"]
        path_str = row["payload_path"]

        if inline is not None:
            payload = bytes(inline)
        elif path_str is not None:
            path = Path(path_str)
            try:
                payload = path.read_bytes()
            except FileNotFoundError:
                # The row says the payload is on disk but the file is
                # gone. This happens if an external process deleted the
                # cache directory. Drop the row so subsequent lookups
                # are fast misses.
                self._sqlite.execute(
                    """
                    DELETE FROM cache_entries
                    WHERE repo_id = ? AND commit_hash = ? AND cache_type = ?
                    """,
                    (self._repo_id, commit_hash, cache_type),
                )
                return None
            except PermissionError as exc:
                raise CodeGuardianError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    f"Cannot read cached payload: {path}",
                    details={"path": str(path)},
                ) from exc
        else:
            # Row exists but has neither an inline payload nor a path.
            # This is a corrupt row. Treat as a miss.
            return None

        self._sqlite.execute(
            """
            UPDATE cache_entries
            SET last_accessed_at = ?, access_count = access_count + 1
            WHERE repo_id = ? AND commit_hash = ? AND cache_type = ?
            """,
            (_now_iso(), self._repo_id, commit_hash, cache_type),
        )
        return payload

    def put_disk(self, commit_hash: str, cache_type: str, payload: bytes) -> None:
        """Store a payload, choosing inline or on-disk by size.

        The write is an ``INSERT OR REPLACE``: an existing entry for the
        same key is fully replaced, and the access count resets to one.

        Args:
            commit_hash: The commit hash.
            cache_type: The cache type. Must satisfy the CHECK
                constraint of the ``cache_entries`` table.
            payload: The raw bytes to store.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL on a database
                failure, or with code FILE_PERMISSION_DENIED or
                SYS_INTERNAL if a large payload cannot be written to
                disk.
        """
        now = _now_iso()

        if len(payload) < self._inline_threshold_bytes:
            self._sqlite.execute(
                """
                INSERT OR REPLACE INTO cache_entries
                    (repo_id, commit_hash, cache_type,
                     payload_path, payload_inline,
                     created_at, last_accessed_at, access_count)
                VALUES (?, ?, ?, NULL, ?, ?, ?, 1)
                """,
                (self._repo_id, commit_hash, cache_type, payload, now, now),
            )
            return

        path = self._cache_path(commit_hash, cache_type)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot write cache payload: {path}",
                details={"path": str(path)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot write cache payload: {exc}",
                details={"path": str(path)},
            ) from exc

        self._sqlite.execute(
            """
            INSERT OR REPLACE INTO cache_entries
                (repo_id, commit_hash, cache_type,
                 payload_path, payload_inline,
                 created_at, last_accessed_at, access_count)
            VALUES (?, ?, ?, ?, NULL, ?, ?, 1)
            """,
            (self._repo_id, commit_hash, cache_type, str(path), now, now),
        )

    def invalidate(self, commit_hash: str, cache_type: str) -> None:
        """Remove a disk cache entry and, if it exists, its payload file.

        Idempotent: invalidating a key that does not exist is not an
        error. Called when a new commit supersedes the cached state.

        Args:
            commit_hash: The commit hash.
            cache_type: The cache type.
        """
        rows = self._sqlite.query(
            """
            SELECT payload_path FROM cache_entries
            WHERE repo_id = ? AND commit_hash = ? AND cache_type = ?
            """,
            (self._repo_id, commit_hash, cache_type),
        )
        if rows and rows[0]["payload_path"]:
            path = Path(rows[0]["payload_path"])
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                # The file could not be removed. Deleting the database
                # row is the more important step; the file is orphaned
                # and the cleanup sweep will find it by age.
                pass
        self._sqlite.execute(
            """
            DELETE FROM cache_entries
            WHERE repo_id = ? AND commit_hash = ? AND cache_type = ?
            """,
            (self._repo_id, commit_hash, cache_type),
        )
