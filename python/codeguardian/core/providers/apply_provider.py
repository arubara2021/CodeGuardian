"""Consent-gated application of a diff to a local repository.

This is the only class in the system with write access to a
repository. It runs only after the user has approved the diff, and it
records every attempt in the audit log before touching a single file.
Non-local targets are rejected so that a v1 run cannot silently push
to a remote.

Both ``apply`` and ``rollback`` write an audit entry. The apply entry
is written before ``git apply --check``; the rollback entry is written
before ``git reset --hard``. The audit chain therefore records the
attempt for both operations, not only the forward change. A run that
applies and then rolls back produces two chained entries, and
``audit.verify`` reports ``entries_checked == 2``.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.apply import ApplyResult, ConsentRecord, RollbackResult
from codeguardian.interfaces.repository import RepositoryProvider

__all__ = ["ApplyProvider"]

_GIT_APPLY_TIMEOUT = 60
_REMOTE_URL_PREFIXES = ("http://", "https://", "ssh://", "git://")


class _AuditBridge(Protocol):
    """A JSON-RPC bridge with the single method the applier needs.

    The real ``JSONRPCBridge`` satisfies this shape structurally. Tests
    pass a minimal object exposing the same method.
    """

    def call(self, method: str, params: dict[str, object]) -> object: ...


def _is_remote_url(url: str) -> bool:
    """Return whether ``url`` names a network remote rather than a path."""
    if url.startswith(_REMOTE_URL_PREFIXES):
        return True
    if url.startswith("/"):
        return False
    return "@" in url and ":" in url.split("@", 1)[1]


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int = _GIT_APPLY_TIMEOUT,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git`` and return the completed process without raising."""
    command = ["git", *args]
    cwd_str = str(cwd)
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


def _applied_files(diff: str) -> tuple[str, ...]:
    """Return the repository-relative paths a unified diff touches.

    Both sides of every file header are inspected so that additions
    and deletions each appear once in the result.
    """
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            paths.add(line[6:])
    return tuple(sorted(paths))


class ApplyProvider:
    """Applies a consent-approved diff to a local repository."""

    def __init__(
        self,
        repository: RepositoryProvider,
        audit_bridge: _AuditBridge,
        audit_log_path: Path,
    ) -> None:
        self._repository = repository
        self._audit_bridge = audit_bridge
        self._audit_log_path = audit_log_path
        self._last_repo_path: Path | None = None
        self._last_run_id: str | None = None
        self._last_consent_id: str | None = None
        self._last_diff_hash: str | None = None

    def apply(
        self,
        repo_url: str,
        diff: str,
        consent: ConsentRecord,
    ) -> ApplyResult:
        """Apply ``diff`` to the local repository at ``repo_url``.

        The order of operations is fixed:

        1. Reject a network URL. v1 only writes to local clones.
        2. Reject consent whose scope is not ``apply``.
        3. Verify ``sha256(diff) == consent.diff_hash``.
        4. Capture the current HEAD as the rollback reference.
        5. Write an audit entry that records the attempt and its target.
        6. Run ``git apply --check`` to validate the patch without
           touching the working tree.
        7. Run ``git apply`` to perform the write.

        A failure at step 6 leaves the repository unchanged. A failure
        at step 7 leaves the repository partially modified and the
        caller must invoke ``rollback`` with the returned reference to
        restore it. This is a system error, not a user error.

        Note: ``git apply`` without ``--index`` leaves a newly-created
        file untracked. ``rollback`` restores tracked files, so a
        subsequent rollback will not delete files the diff created. This
        does not affect the modification case; it becomes a correctness
        concern when Phase 11's refactor introduces new files, and will
        be addressed there.

        The run id, consent id, and diff hash of a successful apply are
        retained on the instance so that a subsequent ``rollback`` can
        write a chained audit entry that names the change being reverted.
        """
        if _is_remote_url(repo_url):
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                f"remote apply is not implemented in v1; clone locally first: {repo_url}",
                details={"repo_url": repo_url},
            )
        if consent.scope != "apply":
            raise CodeGuardianError(
                ErrorCode.APPLY_AUTH_FAILED,
                f"consent scope is {consent.scope!r}, expected 'apply'",
                details={"scope": consent.scope},
            )

        repo_path = Path(repo_url).expanduser().resolve()
        if not repo_path.is_dir():
            raise CodeGuardianError(
                ErrorCode.REPO_NOT_FOUND,
                f"repository path is not a directory: {repo_path}",
                details={"repo_url": repo_url},
            )

        diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        if diff_hash != consent.diff_hash:
            raise CodeGuardianError(
                ErrorCode.APPLY_AUTH_FAILED,
                "the diff being applied does not match the consent record",
                details={
                    "expected_hash": consent.diff_hash,
                    "actual_hash": diff_hash,
                },
            )

        pre_apply_head = self._head_commit(repo_path)
        applied = _applied_files(diff)

        self._audit_bridge.call(
            "audit.write",
            {
                "path": str(self._audit_log_path),
                "payload": {
                    "event": "apply",
                    "phase": "apply",
                    "agent": "orchestrator",
                    "run_id": consent.run_id,
                    "consent_id": consent.consent_id,
                    "diff_hash": diff_hash,
                    "rollback_ref": pre_apply_head,
                    "repo": str(repo_path),
                    "applied_files": list(applied),
                },
            },
        )

        self._git_apply(repo_path, diff, check=True)
        self._git_apply(repo_path, diff, check=False)

        self._last_repo_path = repo_path
        self._last_run_id = consent.run_id
        self._last_consent_id = consent.consent_id
        self._last_diff_hash = diff_hash
        return ApplyResult(
            success=True,
            applied_files=applied,
            rollback_ref=pre_apply_head,
        )

    def rollback(self, rollback_ref: str) -> RollbackResult:
        """Reset the working tree of the last-applied repository to ``rollback_ref``.

        The reference must resolve to a commit. ``^{commit}`` is used in
        the verification step so that a syntactically valid but
        non-resolving reference — the all-zeros hash, a typo, a blob SHA
        — fails here with ``ROLLBACK_REF_NOT_FOUND`` instead of
        surfacing as a downstream ``git reset`` error.

        ``restored_files`` is computed with ``git diff --name-only``
        against the reference, comparing the working tree to the
        commit. Because ``apply`` does not create a commit, HEAD is
        equal to the rollback reference after apply, and comparing
        ``<ref> HEAD`` would return empty. Comparing the working tree
        to ``<ref>`` is the correct comparison for "what will reset
        change".

        An audit entry is written before ``git reset --hard`` runs. The
        entry carries the run id, consent id, and diff hash of the
        original apply, so the log records which change is being
        reverted. A rollback that fails at the git step leaves a log of
        the attempt, matching the discipline of ``apply``.

        Calling ``rollback`` before any ``apply`` on this instance
        raises ``ROLLBACK_REF_NOT_FOUND``.
        """
        if self._last_repo_path is None:
            raise CodeGuardianError(
                ErrorCode.ROLLBACK_REF_NOT_FOUND,
                "no prior apply recorded on this provider instance",
                details={"rollback_ref": rollback_ref},
            )
        repo_path = self._last_repo_path

        verify = _run_git(
            ["rev-parse", "--verify", f"{rollback_ref}^{{commit}}"],
            cwd=repo_path,
        )
        if verify.returncode != 0:
            raise CodeGuardianError(
                ErrorCode.ROLLBACK_REF_NOT_FOUND,
                f"cannot resolve rollback reference: {rollback_ref}",
                details={"rollback_ref": rollback_ref},
            )

        diff_result = _run_git(
            ["diff", "--name-only", rollback_ref],
            cwd=repo_path,
        )
        restored_files: tuple[str, ...] = ()
        if diff_result.returncode == 0:
            raw = diff_result.stdout.decode("utf-8", "replace")
            restored_files = tuple(sorted(name for name in raw.splitlines() if name))

        self._audit_bridge.call(
            "audit.write",
            {
                "path": str(self._audit_log_path),
                "payload": {
                    "event": "rollback",
                    "phase": "apply",
                    "agent": "orchestrator",
                    "run_id": self._last_run_id,
                    "consent_id": self._last_consent_id,
                    "diff_hash": self._last_diff_hash,
                    "rollback_ref": rollback_ref,
                    "repo": str(repo_path),
                    "restored_files": list(restored_files),
                },
            },
        )

        reset = _run_git(["reset", "--hard", rollback_ref], cwd=repo_path)
        if reset.returncode != 0:
            stderr = reset.stderr.decode("utf-8", "replace").strip()
            raise CodeGuardianError(
                ErrorCode.ROLLBACK_FAILED,
                f"git reset --hard failed: {stderr}",
                details={"rollback_ref": rollback_ref},
            )

        return RollbackResult(success=True, restored_files=restored_files)

    def _head_commit(self, repo_path: Path) -> str:
        """Return the full hash of the current HEAD of ``repo_path``."""
        result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise CodeGuardianError(
                ErrorCode.APPLY_CONFLICT,
                f"cannot read HEAD: {stderr}",
                details={"repo": str(repo_path)},
            )
        return result.stdout.decode("utf-8", "replace").strip()

    def _git_apply(self, repo_path: Path, diff: str, *, check: bool) -> None:
        """Invoke ``git apply`` with or without ``--check``."""
        args = ["apply"]
        if check:
            args.append("--check")
        args.append("-")
        result = _run_git(
            args,
            cwd=repo_path,
            stdin=diff.encode("utf-8"),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            if check:
                raise CodeGuardianError(
                    ErrorCode.APPLY_CONFLICT,
                    f"git apply --check failed: {stderr}",
                    details={"repo": str(repo_path), "stderr": stderr},
                )
            raise CodeGuardianError(
                ErrorCode.APPLY_PARTIAL,
                f"git apply failed after --check passed: {stderr}",
                details={"repo": str(repo_path), "stderr": stderr},
            )
