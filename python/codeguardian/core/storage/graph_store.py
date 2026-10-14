"""Python-side management of the binary Code Property Graph file.

A Code Property Graph is written by the Rust kernel (Phase 6) as a
memory-mapped binary file. Python never parses the graph structure; the
kernel does. What Python needs is a small set of operations around that
file: know its path, check whether it exists, read a fixed-size header
that describes the payload, and record and retrieve the metadata row in
SQLite that points at it.

This module provides exactly those operations. It does not open the
payload. It does not mmap the graph. It does not attempt to interpret
any byte past the 64-byte header.

The header format is fixed and versioned. Format version 1 defines a
64-byte little-endian structure:

    offset  size  field
    ------  ----  -----
    0       4     magic, b"CGCP"
    4       2     format_version, uint16, currently 1
    6       2     header_size, uint16, currently 64
    8       4     flags, uint32, bit 0 = incremental, bits 1-31 reserved
    12      8     node_count, uint64
    20      8     edge_count, uint64
    28      8     payload_offset, uint64, offset of payload from file start
    36      8     payload_length, uint64, size of payload in bytes
    44      8     payload_checksum, uint64, checksum of the payload
    52      12    reserved, all zero in v1

The checksum algorithm is defined by the writer. Python reads the field
and passes it through in the CPGHeader dataclass, but does not verify
it: verification requires reading the payload, which is the kernel's
job. Structural integrity (magic number, header size, payload bounds)
is checked here so that a truncated or misplaced file is caught before
the kernel is asked to open it.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .sqlite_store import SQLiteStore

__all__ = ["CPGHeader", "GraphStore"]


_MAGIC: bytes = b"CGCP"
_FORMAT_VERSION: int = 1
_HEADER_SIZE: int = 64

# The layout is little-endian throughout. Every platform CodeGuardian
# targets (x86_64, ARM64) is little-endian, and the Rust writer will use
# the same convention. The reserved field is 12 zero bytes.
_HEADER_FORMAT: str = "<4sHH I QQQQQ12s"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CPGHeader:
    """The decoded header of a Code Property Graph file.

    Every field is read from the file. The ``incremental`` property is
    derived from bit 0 of ``flags``; the remaining bits are reserved and
    must be zero in v1.
    """

    format_version: int
    flags: int
    node_count: int
    edge_count: int
    payload_offset: int
    payload_length: int
    payload_checksum: int

    @property
    def incremental(self) -> bool:
        """Whether the file was produced by an incremental update."""
        return bool(self.flags & 0x1)


class GraphStore:
    """Reads CPG headers and maintains the ``cpg_metadata`` table.

    One instance per storage root. The store is stateless beyond the
    root path and the SQLite connection it was given; no file handles
    are held between method calls. The SQLite store passed to the
    constructor is used only by ``write_metadata``, ``read_metadata``,
    and ``delete``; ``read_header`` needs no database access.
    """

    def __init__(self, root: Path, sqlite_store: SQLiteStore) -> None:
        """Construct the store. No I/O happens until a method is called.

        Args:
            root: The root of the on-disk storage tree, typically
                ``~/.codeguardian``. The graph directory
                ``<root>/repos/<repo_id>/graph/`` is derived from this.
            sqlite_store: An open store used for the metadata row. The
                store is not closed by this object.
        """
        self._root = Path(root)
        self._store = sqlite_store

    @property
    def root(self) -> Path:
        """The root of the on-disk storage tree."""
        return self._root

    def cpg_path(self, repo_id: str, commit_hash: str) -> Path:
        """Return the path the CPG file for this commit would have.

        Does not touch the filesystem. The layout matches the design
        document: ``<root>/repos/<repo_id>/graph/cpg-<commit>.bin``.

        Args:
            repo_id: The repository identifier.
            commit_hash: The commit hash the graph was built against.

        Returns:
            The path, whether or not the file exists.
        """
        return self._root / "repos" / repo_id / "graph" / f"cpg-{commit_hash}.bin"

    def exists(self, repo_id: str, commit_hash: str) -> bool:
        """Return whether the CPG file for this commit is on disk."""
        return self.cpg_path(repo_id, commit_hash).exists()

    def read_header(self, repo_id: str, commit_hash: str) -> CPGHeader:
        """Read and validate the 64-byte header of a CPG file.

        Args:
            repo_id: The repository identifier.
            commit_hash: The commit hash.

        Returns:
            The decoded header.

        Raises:
            CodeGuardianError: With code FILE_NOT_FOUND if the file does
                not exist, FILE_PERMISSION_DENIED if it cannot be read,
                STORAGE_GRAPH_CORRUPTED if the file is shorter than the
                header, the magic number is wrong, the format version is
                unsupported, the header size is not 64, the payload
                offset is less than 64, or the payload bounds do not
                match the file size, or SYS_INTERNAL for any other I/O
                failure.
        """
        path = self.cpg_path(repo_id, commit_hash)

        try:
            file_size = path.stat().st_size
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"CPG file not found: {path}",
                details={"path": str(path)},
            ) from exc
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot stat CPG file: {path}",
                details={"path": str(path)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot stat CPG file: {exc}",
                details={"path": str(path)},
            ) from exc

        if file_size < _HEADER_SIZE:
            raise CodeGuardianError(
                ErrorCode.STORAGE_GRAPH_CORRUPTED,
                f"CPG file is {file_size} bytes, shorter than the {_HEADER_SIZE}-byte header.",
                details={"path": str(path), "size": file_size},
            )

        try:
            with path.open("rb") as handle:
                raw = handle.read(_HEADER_SIZE)
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot read CPG file: {path}",
                details={"path": str(path)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot read CPG file: {exc}",
                details={"path": str(path)},
            ) from exc

        (
            magic,
            format_version,
            header_size,
            flags,
            node_count,
            edge_count,
            payload_offset,
            payload_length,
            payload_checksum,
            _reserved,
        ) = struct.unpack(_HEADER_FORMAT, raw)

        if magic != _MAGIC:
            raise CodeGuardianError(
                ErrorCode.STORAGE_GRAPH_CORRUPTED,
                f"CPG file has magic {magic!r}, expected {_MAGIC!r}.",
                details={"path": str(path), "magic": magic.hex()},
            )

        if format_version != _FORMAT_VERSION:
            raise CodeGuardianError(
                ErrorCode.STORAGE_GRAPH_CORRUPTED,
                f"CPG file has format version {format_version}, "
                f"this build supports version {_FORMAT_VERSION}.",
                details={"path": str(path), "format_version": format_version},
            )

        if header_size != _HEADER_SIZE:
            raise CodeGuardianError(
                ErrorCode.STORAGE_GRAPH_CORRUPTED,
                f"CPG file declares header size {header_size}, expected {_HEADER_SIZE}.",
                details={"path": str(path), "header_size": header_size},
            )

        if payload_offset < _HEADER_SIZE:
            raise CodeGuardianError(
                ErrorCode.STORAGE_GRAPH_CORRUPTED,
                f"CPG payload offset {payload_offset} is inside the header.",
                details={"path": str(path), "payload_offset": payload_offset},
            )

        expected_size = payload_offset + payload_length
        if expected_size != file_size:
            raise CodeGuardianError(
                ErrorCode.STORAGE_GRAPH_CORRUPTED,
                f"CPG file size {file_size} does not match header bounds {expected_size}.",
                details={
                    "path": str(path),
                    "file_size": file_size,
                    "expected_size": expected_size,
                },
            )

        return CPGHeader(
            format_version=format_version,
            flags=flags,
            node_count=node_count,
            edge_count=edge_count,
            payload_offset=payload_offset,
            payload_length=payload_length,
            payload_checksum=payload_checksum,
        )

    def write_metadata(
        self,
        repo_id: str,
        commit_hash: str,
        header: CPGHeader,
        binary_path: Path,
        build_duration_ms: int | None = None,
    ) -> None:
        """Insert or replace the ``cpg_metadata`` row for this commit.

        The binary file is assumed to have been written by the caller.
        This method only records the metadata that points at it. The
        primary key is a new UUID; an existing row for the same
        ``(repo_id, commit_hash)`` is replaced in full.

        Args:
            repo_id: The repository identifier. Must exist in ``repos``.
            commit_hash: The commit hash.
            header: The decoded header of the file.
            binary_path: The path to the binary file.
            build_duration_ms: Optional build duration in milliseconds.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL if the insert
                fails.
        """
        self._store.execute(
            """
            INSERT OR REPLACE INTO cpg_metadata
                (cpg_id, repo_id, commit_hash, node_count, edge_count,
                 build_duration_ms, built_at, incremental, binary_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                repo_id,
                commit_hash,
                header.node_count,
                header.edge_count,
                build_duration_ms,
                _now_iso(),
                1 if header.incremental else 0,
                str(binary_path),
            ),
        )

    def read_metadata(self, repo_id: str, commit_hash: str) -> dict[str, object] | None:
        """Return the ``cpg_metadata`` row for this commit, or ``None``.

        Args:
            repo_id: The repository identifier.
            commit_hash: The commit hash.

        Returns:
            A dictionary with the row's fields, or ``None`` if no row
            exists.
        """
        rows = self._store.query(
            """
            SELECT cpg_id, node_count, edge_count, build_duration_ms,
                   built_at, incremental, binary_path
            FROM cpg_metadata
            WHERE repo_id = ? AND commit_hash = ?
            """,
            (repo_id, commit_hash),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "cpg_id": row["cpg_id"],
            "node_count": row["node_count"],
            "edge_count": row["edge_count"],
            "build_duration_ms": row["build_duration_ms"],
            "built_at": row["built_at"],
            "incremental": bool(row["incremental"]),
            "binary_path": row["binary_path"],
        }

    def delete(self, repo_id: str, commit_hash: str, *, remove_metadata: bool = True) -> bool:
        """Delete the CPG file and, optionally, its metadata row.

        Idempotent: deleting a graph that does not exist is not an error.
        Called by the cleanup sweep when a graph ages out of the
        retention window.

        Args:
            repo_id: The repository identifier.
            commit_hash: The commit hash.
            remove_metadata: If True (the default), also delete the
                ``cpg_metadata`` row. If False, the metadata row is left
                in place.

        Returns:
            True if the binary file existed and was removed, False if it
            did not exist.

        Raises:
            CodeGuardianError: With code FILE_PERMISSION_DENIED or
                SYS_INTERNAL if the file exists but cannot be removed.
        """
        path = self.cpg_path(repo_id, commit_hash)
        deleted = False
        if path.exists():
            try:
                path.unlink()
                deleted = True
            except PermissionError as exc:
                raise CodeGuardianError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    f"Cannot delete CPG file: {path}",
                    details={"path": str(path)},
                ) from exc
            except OSError as exc:
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"Cannot delete CPG file: {exc}",
                    details={"path": str(path)},
                ) from exc

        if remove_metadata:
            self._store.execute(
                "DELETE FROM cpg_metadata WHERE repo_id = ? AND commit_hash = ?",
                (repo_id, commit_hash),
            )
        return deleted
