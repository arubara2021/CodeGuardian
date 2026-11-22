"""Path validation and symlink-safe file opening.

This module is the single place that decides whether a filesystem
path is permitted. Two questions are answered here, separately and in
a fixed order.

**Reads.** A read is permitted when the canonical path is inside a
granted root -- the sandbox clone or any trusted read path -- and is
not inside the fixed deny set. The deny set covers credential stores
and secrets. It is checked first, so a grant cannot override a deny.

**Writes.** A write is permitted only when the canonical path is
inside the sandbox clone. Trusted read paths do not grant writes.

The module provides three public functions.

``canonicalize_and_contain`` canonicalizes a root and a candidate and
returns the canonical candidate when it is inside the root. It uses
``os.path.commonpath`` rather than a string prefix, which rejects
sibling-prefix bypasses such as ``prompts_evil`` vs ``prompts``.

``is_denied`` returns True when a canonical path matches the deny set.

``safe_open`` opens a file with a per-component ``O_NOFOLLOW`` walk on
platforms that support ``dir_fd``. The walk guarantees that no path
component is a symlink at the moment it is opened. On platforms
without ``dir_fd``, the function falls back to canonicalization plus
containment: less strong, but functional and documented.

The deny set is fixed in code. It is not configurable per run, because
a runtime-configurable deny list can be widened by whoever controls
the configuration.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path
from typing import IO, Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "DENY_PATTERNS",
    "OpenMode",
    "canonicalize_and_contain",
    "is_denied",
    "is_path_permitted",
    "safe_open",
]


OpenMode = Literal["r", "rb", "w", "wb"]


# Credential stores and secrets. Every entry is matched against the
# canonical absolute path. A trailing ``*`` matches any path under the
# directory; a leading ``**/`` matches at any depth. The list is fixed
# in code so it cannot be widened by configuration.
DENY_PATTERNS: tuple[str, ...] = (
    # SSH and cloud credential stores
    "**/.ssh",
    "**/.ssh/*",
    "**/.aws",
    "**/.aws/*",
    "**/.gnupg",
    "**/.gnupg/*",
    "**/.config/gh",
    "**/.config/gh/*",
    # Environment files
    "**/.env",
    "**/.env.*",
    # Generic credential and secret names
    "**/credentials",
    "**/credentials.*",
    "**/secrets",
    "**/secrets/*",
    "**/secrets.*",
    # Git config can hold tokens when a credential helper is configured
    "**/.git/config",
    # Private key material
    "**/id_rsa",
    "**/id_ed25519",
    "**/id_ecdsa",
    "**/*.pem",
    "**/*.key",
)


def _canonicalize(path: str | Path) -> str:
    """Return the canonical absolute path for ``path``.

    Uses ``os.path.realpath`` on top of ``os.path.abspath`` so that
    ``..`` segments are collapsed and every symlink in the path is
    resolved to its target. The returned string has no trailing slash
    except when the path is the filesystem root.
    """
    return os.path.realpath(os.path.abspath(str(path)))


def _is_under(root: str, candidate: str) -> bool:
    """Return True when ``candidate`` is inside ``root`` or equal to it.

    Uses ``os.path.commonpath``, which operates on path components
    rather than characters. A sibling directory that shares a string
    prefix -- ``prompts_evil`` against ``prompts`` -- is not inside
    the root and is correctly rejected.
    """
    if root == candidate:
        return True
    try:
        common = os.path.commonpath([root, candidate])
    except ValueError:
        # Different drives on Windows, or a mix of absolute and
        # relative paths after canonicalization. Neither can be inside
        # the other.
        return False
    return common == root


def canonicalize_and_contain(root: str | Path, candidate: str | Path) -> str:
    """Canonicalize ``candidate`` and confirm it is inside ``root``.

    Args:
        root: The permitted root. Canonicalized before comparison.
        candidate: The path to validate. Canonicalized before
            comparison.

    Returns:
        The canonical absolute path of ``candidate``.

    Raises:
        CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED`` when
            the candidate is not inside the root. The error carries
            both canonical paths in its details.
    """
    canonical_root = _canonicalize(root)
    canonical_candidate = _canonicalize(candidate)
    if not _is_under(canonical_root, canonical_candidate):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"path {candidate!r} is outside the permitted root {root!r}",
            details={
                "canonical_root": canonical_root,
                "canonical_candidate": canonical_candidate,
            },
        )
    return canonical_candidate


def _match_deny(canonical_path: str) -> bool:
    """Return True when ``canonical_path`` matches any deny pattern."""
    normalized = canonical_path.replace(os.sep, "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in DENY_PATTERNS)


def is_denied(canonical_path: str | Path) -> bool:
    """Return True when the canonical path matches the deny set.

    The caller is responsible for canonicalizing first. Passing a
    non-canonical path is not an error, but the match is only reliable
    on canonical input.
    """
    return _match_deny(str(canonical_path))


def is_path_permitted(
    *,
    sandbox_root: str | Path,
    candidate: str | Path,
    trusted_read_roots: tuple[str | Path, ...],
    for_write: bool,
) -> bool:
    """Return True when the candidate is permitted for the given mode.

    A read is permitted when the canonical candidate is inside the
    sandbox root or inside any trusted read root, and is not in the
    deny set. A write is permitted only when the canonical candidate
    is inside the sandbox root; trusted read roots do not grant
    writes.

    Args:
        sandbox_root: The sandbox root (the clone).
        candidate: The path being read or written.
        trusted_read_roots: Paths that grant reads but not writes. May
            be empty.
        for_write: True for a write check, False for a read check.

    Returns:
        True when the operation is permitted. False otherwise. The
        function never raises; a resolution failure returns False.
    """
    try:
        canonical_sandbox = _canonicalize(sandbox_root)
        canonical_candidate = _canonicalize(candidate)
    except (OSError, ValueError):
        # A path that cannot be canonicalized is denied. This is the
        # fail-closed rule: a resolution failure must never widen
        # access.
        return False

    if is_denied(canonical_candidate):
        return False

    if _is_under(canonical_sandbox, canonical_candidate):
        return True

    if for_write:
        return False

    for trusted_root in trusted_read_roots:
        try:
            canonical_trusted = _canonicalize(trusted_root)
        except (OSError, ValueError):
            continue
        if _is_under(canonical_trusted, canonical_candidate):
            return True

    return False


def _supports_dir_fd() -> bool:
    """Return True when ``os.open`` accepts ``dir_fd``."""
    return os.open in os.supports_dir_fd


def _safe_open_unix(
    canonical_root: str,
    canonical_path: str,
    mode: OpenMode,
) -> IO[bytes] | IO[str]:
    """Per-component O_NOFOLLOW walk relative to directory descriptors.

    Opens the root directory, then opens each component relative to
    the previous descriptor with ``O_NOFOLLOW`` on every call. A
    symlink at any component fails with ``ELOOP``. The final file
    descriptor is verified against ``lstat`` of the final path so a
    swap between the walk and the open is detected.
    """
    relative = os.path.relpath(canonical_path, canonical_root)
    if relative == os.curdir:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"cannot open the root directory as a file: {canonical_path!r}",
            details={"path": canonical_path},
        )
    components = relative.split(os.sep)
    flags = os.O_RDONLY
    if "w" in mode:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC

    fd = os.open(canonical_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        final_name = components[-1]
        file_fd = os.open(final_name, flags | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)

    try:
        opened_stat = os.fstat(file_fd)
        path_stat = os.lstat(canonical_path)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"file was swapped between the path check and the open: {canonical_path!r}",
                details={"path": canonical_path},
            )
    except OSError as exc:
        os.close(file_fd)
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"could not verify the opened file: {exc}",
            details={"path": canonical_path},
        ) from exc

    return os.fdopen(file_fd, mode)


def _safe_open_fallback(
    canonical_root: str,
    canonical_path: str,
    mode: OpenMode,
) -> IO[bytes] | IO[str]:
    """Fallback for platforms without ``dir_fd``.

    Canonicalizes and checks containment, then opens with standard
    ``open``. This does not protect against a symlink swap between the
    check and the open. It is the documented limitation of running
    without ``dir_fd``.
    """
    if not _is_under(canonical_root, canonical_path):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"path {canonical_path!r} is outside the sandbox root",
            details={"root": canonical_root, "path": canonical_path},
        )
    if os.path.islink(canonical_path):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"refusing to open a symlink: {canonical_path!r}",
            details={"path": canonical_path},
        )
    return open(canonical_path, mode)


def safe_open(
    sandbox_root: str | Path,
    relative_path: str,
    *,
    mode: OpenMode = "rb",
) -> IO[bytes] | IO[str]:
    """Open a file under ``sandbox_root`` with symlink refusal.

    The path is resolved relative to the sandbox root, canonicalized,
    and containment-checked. On Unix, the open walks each component
    with ``O_NOFOLLOW`` relative to a directory descriptor. On
    platforms without ``dir_fd``, the open falls back to
    canonicalization plus a symlink check on the final component.

    Args:
        sandbox_root: The sandbox root. The file must live under it.
        relative_path: The path relative to the root.
        mode: One of ``"r"``, ``"rb"``, ``"w"``, ``"wb"``.

    Returns:
        An open file object.

    Raises:
        CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED`` when
            the path escapes the root, contains a symlink component,
            or was swapped between the check and the open.
    """
    canonical_root = _canonicalize(sandbox_root)
    candidate = os.path.join(canonical_root, relative_path)
    canonical_path = canonicalize_and_contain(canonical_root, candidate)

    if is_denied(canonical_path):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"refusing to open a path in the deny set: {canonical_path!r}",
            details={"path": canonical_path},
        )

    if _supports_dir_fd():
        return _safe_open_unix(canonical_root, canonical_path, mode)
    return _safe_open_fallback(canonical_root, canonical_path, mode)
