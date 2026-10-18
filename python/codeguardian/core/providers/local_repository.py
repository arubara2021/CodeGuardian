"""Read-only access to a local git repository.

This is the reference implementation of ``RepositoryProvider``. Every
git operation shells out to the ``git`` binary; the module does not
depend on ``gitpython`` because shelling out is both faster and
guaranteed to match the real git semantics the rest of the toolchain
expects. Standard library exceptions are converted to typed errors at
the boundary so callers see only ``CodeGuardianError``.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.repository import FileChange, RepoMetadata

__all__ = ["LocalProvider"]

_MAX_FILE_BYTES = 10 * 1024 * 1024
_DEFAULT_GIT_TIMEOUT = 300
_CLONE_TIMEOUT = 600
_REMOTE_URL_PREFIXES = ("http://", "https://", "ssh://", "git://")


def _is_remote_url(url: str) -> bool:
    """Return whether ``url`` names a network remote rather than a path."""
    if url.startswith(_REMOTE_URL_PREFIXES):
        return True
    if url.startswith("/"):
        return False
    return "@" in url and ":" in url.split("@", 1)[1]


def _normalize_source(url: str) -> tuple[str, bool]:
    """Return ``(source, is_local)`` for a clone URL or path."""
    if url.startswith("file://"):
        return str(Path(url[len("file://") :]).expanduser().resolve()), True
    if _is_remote_url(url):
        return url, False
    return str(Path(url).expanduser().resolve()), True


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = _DEFAULT_GIT_TIMEOUT,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git`` and return the completed process without raising."""
    command = ["git", *args]
    cwd_str = str(cwd) if cwd is not None else None
    try:
        if stdin is None:
            return subprocess.run(
                command,
                cwd=cwd_str,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        return subprocess.run(
            command,
            cwd=cwd_str,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodeGuardianError(
            ErrorCode.NET_TIMEOUT,
            f"git {args[0] if args else ''} timed out after {timeout}s",
        ) from exc
    except OSError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"failed to execute git: {exc}",
        ) from exc


def _classify_clone_failure(stderr: str) -> ErrorCode:
    """Map git clone stderr text onto a typed error code."""
    text = stderr.lower()
    if "authentication failed" in text or "permission denied (publickey)" in text:
        return ErrorCode.REPO_AUTH_FAILED
    if "could not read username" in text or "terminal prompts disabled" in text:
        return ErrorCode.REPO_AUTH_FAILED
    if "repository not found" in text:
        return ErrorCode.REPO_NOT_FOUND
    if "does not appear to be a git repository" in text:
        return ErrorCode.REPO_NOT_FOUND
    if "could not resolve host" in text:
        return ErrorCode.NET_DNS_FAILED
    if "connection refused" in text or "connection reset" in text:
        return ErrorCode.NET_UNREACHABLE
    if "connection timed out" in text:
        return ErrorCode.NET_TIMEOUT
    return ErrorCode.REPO_CLONE_FAILED


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a git-style glob to a compiled regular expression."""
    parts: list[str] = ["^"]
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*" and index + 1 < length and pattern[index + 1] == "*":
            index += 2
            if index < length and pattern[index] == "/":
                parts.append("(?:.*/)?")
                index += 1
            else:
                parts.append(".*")
        elif char == "*":
            parts.append("[^/]*")
            index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(char))
            index += 1
    parts.append("$")
    return re.compile("".join(parts))


def _format_diff(path: str, old: str | None, new: str | None) -> str:
    """Render one file's change as a ``git apply``-compatible diff.

    Uses ``git diff --no-index`` rather than ``difflib`` because
    ``git apply`` and ``difflib`` disagree on two things that matter:

    - **End-of-file marker.** When a side has no trailing newline,
      ``git apply`` requires a literal ``\\ No newline at end of file``
      line after that side's last content line. ``difflib`` does not
      emit this marker, so a patch that involves such a file is
      rejected with a conflict.
    - **Filename quoting.** Paths that contain tabs, quotes, or
      non-ASCII characters need C-style escaping in the header.

    A missing side is passed to ``git diff`` as ``/dev/null`` — an
    existing character device — rather than omitting the path.
    ``git diff --no-index`` silently returns no output when one of its
    two arguments does not exist, which would make every creation and
    deletion produce an empty diff. ``/dev/null`` exists, so git
    compares against it and emits the standard ``--- /dev/null`` or
    ``+++ /dev/null`` header that ``git apply`` uses to decide the
    file is created or deleted.

    ``git diff --no-index`` exits 1 when the two sides differ, which
    is the expected result for a real change. Exit 0 (identical)
    returns an empty string; any other exit code is a hard failure.
    """
    if old == new:
        return ""
    if old is None and new is None:
        return ""

    with tempfile.TemporaryDirectory(prefix="cg-diff-") as tmpdir:
        tmp_root = Path(tmpdir)
        left_path = f"a/{path}" if old is not None else "/dev/null"
        right_path = f"b/{path}" if new is not None else "/dev/null"

        if old is not None:
            old_file = tmp_root / "a" / path
            old_file.parent.mkdir(parents=True, exist_ok=True)
            old_file.write_bytes(old.encode("utf-8"))
        if new is not None:
            new_file = tmp_root / "b" / path
            new_file.parent.mkdir(parents=True, exist_ok=True)
            new_file.write_bytes(new.encode("utf-8"))

        result = _run_git(
            ["diff", "--no-index", "--no-prefix", left_path, right_path],
            cwd=tmp_root,
        )
        if result.returncode not in (0, 1):
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise CodeGuardianError(
                ErrorCode.DIFF_COMPUTE_FAILED,
                f"git diff --no-index failed for {path}: {stderr}",
                details={"path": path, "exit_code": result.returncode},
            )
        if result.returncode == 0:
            return ""
        return result.stdout.decode("utf-8")


class LocalProvider:
    """A read-only repository provider backed by the local git binary."""

    _provider_name: str = "local"
    _max_file_bytes: int = _MAX_FILE_BYTES

    def __init__(self) -> None:
        self._path: Path | None = None

    @property
    def path(self) -> Path | None:
        """Return the path of the current clone, or ``None`` before clone."""
        return self._path

    def clone(self, url: str, dest: Path) -> Path:
        """Clone ``url`` into ``dest`` and remember it as the active clone."""
        dest_resolved = dest.expanduser().resolve()
        if dest_resolved.exists() and any(dest_resolved.iterdir()):
            raise CodeGuardianError(
                ErrorCode.REPO_CLONE_FAILED,
                f"destination {dest_resolved} is not empty",
                details={"dest": str(dest_resolved)},
            )
        dest_resolved.parent.mkdir(parents=True, exist_ok=True)

        source, is_local = _normalize_source(url)
        if is_local:
            source_path = Path(source)
            if not source_path.exists():
                raise CodeGuardianError(
                    ErrorCode.REPO_NOT_FOUND,
                    f"repository path does not exist: {source}",
                    details={"url": url},
                )
            check = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=source_path)
            if check.returncode != 0 or check.stdout.decode("utf-8", "replace").strip() != "true":
                raise CodeGuardianError(
                    ErrorCode.REPO_NOT_FOUND,
                    f"not a git working tree: {source}",
                    details={"url": url},
                )

        result = _run_git(
            ["clone", source, str(dest_resolved)],
            timeout=_CLONE_TIMEOUT,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            code = _classify_clone_failure(stderr)
            raise CodeGuardianError(
                code,
                f"git clone failed: {stderr}",
                details={"url": url, "dest": str(dest_resolved)},
            )

        self._path = dest_resolved
        return dest_resolved

    def read_file(self, path: str) -> str:
        """Return the decoded text of ``path`` relative to the clone root."""
        resolved = self._resolve_relative(path)
        if not resolved.exists():
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"file not found: {path}",
                details={"path": path},
            )
        if not resolved.is_file():
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"not a regular file: {path}",
                details={"path": path},
            )
        size = resolved.stat().st_size
        if size > self._max_file_bytes:
            raise CodeGuardianError(
                ErrorCode.FILE_TOO_LARGE,
                f"{path} is {size} bytes, exceeds limit of {self._max_file_bytes}",
                details={"path": path, "size": size},
            )
        try:
            raw = resolved.read_bytes()
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"cannot read {path}: permission denied",
                details={"path": path},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"cannot read {path}: {exc}",
                details={"path": path},
            ) from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_BINARY,
                f"{path} is not valid UTF-8",
                details={"path": path},
            ) from exc
        if "\x00" in text:
            raise CodeGuardianError(
                ErrorCode.FILE_BINARY,
                f"{path} contains null bytes",
                details={"path": path},
            )
        return text

    def list_files(self, pattern: str = "**/*") -> list[str]:
        """Return tracked files whose path matches ``pattern``, sorted."""
        root = self._require_cloned()
        try:
            matcher = _glob_to_regex(pattern)
        except re.error as exc:
            raise CodeGuardianError(
                ErrorCode.PATTERN_INVALID,
                f"invalid glob pattern {pattern!r}: {exc}",
                details={"pattern": pattern},
            ) from exc
        result = _run_git(["ls-files", "-z"], cwd=root)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"git ls-files failed: {stderr}",
            )
        raw = result.stdout.decode("utf-8", "replace")
        files = [entry for entry in raw.split("\x00") if entry]
        return sorted(name for name in files if matcher.match(name))

    def get_diff(self, changes: Sequence[FileChange]) -> str:
        """Return a unified diff that renders every change in ``changes``."""
        self._require_cloned()
        parts: list[str] = []
        for change in changes:
            try:
                old = self.read_file(change.path)
            except CodeGuardianError as exc:
                if exc.code is ErrorCode.FILE_NOT_FOUND:
                    old = None
                else:
                    raise
            parts.append(_format_diff(change.path, old, change.new_content))
        return "".join(parts)

    def get_metadata(self) -> RepoMetadata:
        """Return HEAD commit, default branch, and origin URL of the clone."""
        root = self._require_cloned()
        head = _run_git(["rev-parse", "HEAD"], cwd=root)
        if head.returncode != 0:
            stderr = head.stderr.decode("utf-8", "replace").strip()
            raise CodeGuardianError(
                ErrorCode.METADATA_UNAVAILABLE,
                f"cannot read HEAD: {stderr}",
            )
        head_commit = head.stdout.decode("utf-8", "replace").strip()

        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
        branch_name = (
            branch.stdout.decode("utf-8", "replace").strip() if branch.returncode == 0 else ""
        )
        default_branch = None if branch_name in ("", "HEAD") else branch_name

        remote = _run_git(["remote", "get-url", "origin"], cwd=root)
        url = (
            remote.stdout.decode("utf-8", "replace").strip()
            if remote.returncode == 0
            else str(root)
        )

        return RepoMetadata(
            url=url,
            provider=self._provider_name,
            default_branch=default_branch,
            head_commit=head_commit,
        )

    def _require_cloned(self) -> Path:
        """Return the clone root or raise if ``clone`` has not been called."""
        if self._path is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "clone() must be called before this operation",
            )
        return self._path

    def _resolve_relative(self, path: str) -> Path:
        """Resolve ``path`` against the clone root and reject any escape."""
        root = self._require_cloned()
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"path escapes repository root: {path}",
                details={"path": path},
            ) from exc
        return candidate
