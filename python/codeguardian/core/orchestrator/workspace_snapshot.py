"""Atomic workspace snapshot and restore with optional caller-supplied CAS.

A phase that writes to the workspace must be able to restore the
workspace to its pre-phase state when the phase fails, times out, or
is cancelled. This module provides that restore.

``snapshot_paths`` captures a set of paths relative to a root. For
each path it records whether the path existed, the inode, mode, size,
and modification time in nanoseconds, a SHA-256 of the content, and
the content itself. Content is encrypted when ``storage="disk"`` and
kept in memory otherwise.

``restore`` writes the snapshot content back into place. It uses
``renameat2(RENAME_EXCHANGE)`` on Linux for an atomic swap, and falls
back to ``os.replace`` on other platforms. Two modes are supported.

Without ``expected_current``, restore overwrites each declared path
unconditionally. This is correct for the single-writer case the
orchestrator normally runs in, and it is what the test harness uses.

With ``expected_current``, restore first checks that each path's live
content matches the caller-supplied expected bytes. When it does not,
restore refuses that path and reports the reason, so a concurrent
writer's changes are never silently overwritten. A value of ``None``
in the mapping means the path is expected to be missing.

Path handling rejects symlink components before resolution so a link
cannot be laundered into a legitimate-looking absolute path. A path
that contains a symlink component at snapshot time or at restore time
is refused, not followed.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "RestoreRefusal",
    "RestoreResult",
    "Snapshot",
    "SnapshotEntry",
    "restore",
    "snapshot_paths",
]


StorageMode = Literal["memory", "disk"]

_DEFAULT_STORAGE: StorageMode = "memory"

_RENAME_EXCHANGE = 0x2
_AT_FDCWD = -100


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One captured path inside a snapshot.

    Attributes:
        rel_path: The path relative to the snapshot root, normalized
            with forward slashes.
        existed: True when the path existed at snapshot time. A path
            that did not exist is restored by removing the current
            file, when one exists.
        inode: The inode number, or None when the path did not exist.
        mode: The file mode bits, or None.
        size: The byte size, or None.
        mtime_ns: The modification time in nanoseconds, or None. Used
            for diagnostics only; it is not part of the restore safety
            predicate.
        sha256: The SHA-256 hex digest of the content, or None.
        blob: The content. In memory mode this is plaintext; in disk
            mode this is Fernet ciphertext.
    """

    rel_path: str
    existed: bool
    inode: int | None
    mode: int | None
    size: int | None
    mtime_ns: int | None
    sha256: str | None
    blob: bytes | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A snapshot of a set of paths relative to a root.

    Attributes:
        root: The absolute root the paths are relative to.
        entries: One entry per captured path, in the order supplied.
        created_at: The UTC timestamp of the snapshot.
        storage: The storage mode used to capture the blobs.
    """

    root: Path
    entries: tuple[SnapshotEntry, ...]
    created_at: datetime
    storage: StorageMode


@dataclass(frozen=True, slots=True)
class RestoreRefusal:
    """One path that restore declined to overwrite.

    Attributes:
        rel_path: The path that was refused.
        reason: A short machine-readable reason. One of
            ``"modified_by_third_party"``, ``"expected_missing_but_present"``,
            ``"expected_present_but_missing"``, ``"not_in_expected_current"``,
            ``"parent_missing"``, ``"missing_snapshot_content"``, or
            ``"out_of_root"``.
        detail: A human-readable detail string, or None.
    """

    rel_path: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """The outcome of one restore attempt.

    Attributes:
        restored: Paths that were successfully restored to their
            snapshot content.
        refused: Paths that were refused, with reasons.
        removed: Paths that were removed because they did not exist
            at snapshot time.
    """

    restored: tuple[str, ...] = ()
    refused: tuple[RestoreRefusal, ...] = ()
    removed: tuple[str, ...] = ()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_rel(rel_path: str) -> str:
    """Normalize a relative path and reject escapes.

    Absolute paths and paths whose normalized form contains ``..``
    segments are rejected. An empty path, or a path that normalizes
    to empty, is rejected because a phase declares files, not
    directories.
    """
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/"):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"workspace path must be relative: {rel_path!r}",
            details={"rel_path": rel_path},
        )
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"workspace path may not escape the root: {rel_path!r}",
                details={"rel_path": rel_path},
            )
        parts.append(part)
    if not parts:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"workspace path is empty after normalization: {rel_path!r}",
            details={"rel_path": rel_path},
        )
    return "/".join(parts)


def _resolve_inside(root: Path, rel_path: str) -> Path:
    """Resolve a relative path inside ``root`` and reject symlinks.

    Walks each component from the root downward and rejects a symlink
    component before resolution. A link therefore cannot be laundered
    into a legitimate-looking absolute path.
    """
    normalized = _normalize_rel(rel_path)
    current = root
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink():
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"workspace path contains a symlink component: {rel_path!r}",
                details={"rel_path": rel_path, "component": str(current)},
            )
    target = root / normalized
    resolved = target.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"workspace path resolves outside the root: {rel_path!r}",
            details={"rel_path": rel_path, "resolved": str(resolved)},
        ) from exc
    return resolved


def _make_fernet() -> Any:
    """Load a Fernet instance from the OS keyring, or return None.

    Returns None when the cryptography or keyring packages are
    unavailable. In that case disk storage raises a clear error and
    memory storage works as usual.
    """
    try:
        import keyring
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    key = keyring.get_password("codeguardian", "workspace_snapshot_key")
    if key is None:
        key = Fernet.generate_key().decode("ascii")
        try:
            keyring.set_password("codeguardian", "workspace_snapshot_key", key)
        except Exception:
            return None
    return Fernet(key.encode("ascii"))


def _read_entry(
    root: Path,
    rel_path: str,
    storage: StorageMode,
    fernet: Any,
) -> SnapshotEntry:
    normalized = _normalize_rel(rel_path)
    full = _resolve_inside(root, normalized)
    if not full.exists():
        return SnapshotEntry(
            rel_path=normalized,
            existed=False,
            inode=None,
            mode=None,
            size=None,
            mtime_ns=None,
            sha256=None,
            blob=None,
        )
    stat_result = full.stat()
    content = full.read_bytes()
    sha = _sha256_bytes(content)
    if storage == "disk":
        if fernet is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "disk storage requires the cryptography and keyring "
                "packages; install them or use storage='memory'",
                details={"rel_path": normalized},
            )
        blob = fernet.encrypt(content)
    else:
        blob = content
    return SnapshotEntry(
        rel_path=normalized,
        existed=True,
        inode=stat_result.st_ino,
        mode=stat.S_IMODE(stat_result.st_mode),
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        sha256=sha,
        blob=blob,
    )


def snapshot_paths(
    root: Path,
    paths: tuple[str, ...],
    *,
    storage: StorageMode = _DEFAULT_STORAGE,
    fernet: Any = None,
) -> Snapshot:
    """Capture a snapshot of ``paths`` under ``root``.

    Args:
        root: The absolute root the paths are relative to. Must be
            absolute; a relative root is rejected.
        paths: The relative paths to snapshot. Each path is normalized
            and must stay inside ``root``. A path that contains a
            symlink component is rejected, not followed.
        storage: ``"memory"`` (the default) keeps snapshot content in
            the process. ``"disk"`` writes Fernet-encrypted blobs and
            requires the cryptography and keyring packages.
        fernet: An optional Fernet instance for disk-mode snapshots.
            When omitted, one is loaded from the OS keyring. Passing
            the same Fernet to ``snapshot_paths`` and ``restore``
            guarantees the decrypt key matches the encrypt key.

    Returns:
        A ``Snapshot`` with one entry per path, in the order supplied.

    Raises:
        CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED`` when a
            path is absolute, escapes the root, normalizes to empty,
            or contains a symlink component. With
            ``SYS_NOT_IMPLEMENTED`` when ``storage="disk"`` and the
            cryptography package is not installed.
    """
    if not root.is_absolute():
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"snapshot root must be absolute: {root!r}",
            details={"root": str(root)},
        )
    if storage == "disk" and fernet is None:
        fernet = _make_fernet()
    entries = tuple(_read_entry(root, rel_path, storage, fernet) for rel_path in paths)
    return Snapshot(
        root=root,
        entries=entries,
        created_at=datetime.now(UTC),
        storage=storage,
    )


def _rename_exchange(src: Path, dst: Path) -> bool:
    """Atomically exchange two paths on Linux.

    Returns False when the platform does not support
    ``renameat2(RENAME_EXCHANGE)`` or when the call fails. The caller
    falls back to ``os.replace``. When ``dst`` does not exist, the
    exchange fails and the fallback creates it.
    """
    if not hasattr(os, "uname") or os.uname().sysname != "Linux":
        return False
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        return False
    try:
        libc = ctypes.CDLL(libc_name, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError):
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    rc = renameat2(
        _AT_FDCWD,
        str(src).encode("utf-8"),
        _AT_FDCWD,
        str(dst).encode("utf-8"),
        _RENAME_EXCHANGE,
    )
    return rc == 0


def _unlink_if_exists(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _write_temp(parent: Path, content: bytes, mode: int) -> Path:
    """Write ``content`` to a temporary file inside ``parent``."""
    fd, name = tempfile.mkstemp(prefix=".cg-snapshot-", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(name, mode)
    except BaseException:
        _unlink_if_exists(Path(name))
        raise
    return Path(name)


def _check_expected(
    entry: SnapshotEntry,
    full: Path,
    expected_current: Mapping[str, bytes | None],
) -> str | None:
    """Return a refusal reason, or None when the live state matches."""
    if entry.rel_path not in expected_current:
        return "not_in_expected_current"
    expected = expected_current[entry.rel_path]
    live_exists = full.exists()
    if expected is None:
        if live_exists:
            return "expected_missing_but_present"
        return None
    if not live_exists:
        return "expected_present_but_missing"
    if full.read_bytes() != expected:
        return "modified_by_third_party"
    return None


def _restore_entry(
    root: Path,
    entry: SnapshotEntry,
    storage: StorageMode,
    fernet: Any,
    expected_current: Mapping[str, bytes | None] | None,
) -> str | None:
    """Restore one entry. Returns a refusal reason, or None on success."""
    full = _resolve_inside(root, entry.rel_path)

    if expected_current is not None:
        refusal = _check_expected(entry, full, expected_current)
        if refusal is not None:
            return refusal

    if not entry.existed:
        if full.exists() and not full.is_dir():
            _unlink_if_exists(full)
        return None

    if not full.parent.is_dir():
        return "parent_missing"

    if entry.blob is None:
        return "missing_snapshot_content"

    if storage == "disk":
        if fernet is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "disk storage requires the cryptography package",
                details={"rel_path": entry.rel_path},
            )
        content = fernet.decrypt(entry.blob)
    else:
        content = entry.blob

    mode = entry.mode if entry.mode is not None else 0o644
    temp = _write_temp(full.parent, content, mode)
    try:
        if _rename_exchange(temp, full):
            _unlink_if_exists(temp)
        else:
            os.replace(temp, full)
    except BaseException:
        _unlink_if_exists(temp)
        raise
    return None


def restore(
    snapshot: Snapshot,
    *,
    expected_current: Mapping[str, bytes | None] | None = None,
    fernet: Any = None,
) -> RestoreResult:
    """Restore a snapshot.

    Each entry is restored to the snapshot's captured content. When
    ``expected_current`` is provided, each path is first checked
    against the caller-supplied expected bytes; a path whose live
    content does not match is refused rather than overwritten. When
    ``expected_current`` is omitted, every path is overwritten
    unconditionally.

    Args:
        snapshot: The snapshot to restore.
        expected_current: Optional mapping from relative path to the
            content the live file is expected to have. ``None`` as a
            value means the path is expected to be missing. When
            provided, restore refuses any path whose live content
            does not match. When omitted (or None), restore overwrites
            unconditionally; callers running with concurrent writers
            should provide it.
        fernet: An optional Fernet instance for disk-mode snapshots.
            When omitted, one is loaded from the OS keyring.

    Returns:
        A ``RestoreResult`` naming the restored, refused, and removed
        paths.
    """
    if snapshot.storage == "disk" and fernet is None:
        fernet = _make_fernet()
    restored: list[str] = []
    refused: list[RestoreRefusal] = []
    removed: list[str] = []
    for entry in snapshot.entries:
        try:
            reason = _restore_entry(
                snapshot.root,
                entry,
                snapshot.storage,
                fernet,
                expected_current,
            )
        except CodeGuardianError as exc:
            refused.append(
                RestoreRefusal(
                    rel_path=entry.rel_path,
                    reason="out_of_root",
                    detail=exc.message,
                )
            )
            continue
        if reason is None:
            if entry.existed:
                restored.append(entry.rel_path)
            else:
                removed.append(entry.rel_path)
        else:
            refused.append(RestoreRefusal(rel_path=entry.rel_path, reason=reason))
    return RestoreResult(
        restored=tuple(restored),
        refused=tuple(refused),
        removed=tuple(removed),
    )
