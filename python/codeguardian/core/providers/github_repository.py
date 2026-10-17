"""GitHub repository provider.

Clone over HTTPS, SSH, or the ``git@github.com:`` form. All other
methods are inherited from ``LocalProvider``: after a clone, the
directory is a git repository and behaves identically to a local one.
Authentication is delegated to the host's git configuration.
"""

from __future__ import annotations

from pathlib import Path

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .local_repository import (
    LocalProvider,
    _classify_clone_failure,
    _run_git,
)

__all__ = ["GitHubProvider"]

_GITHUB_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "ssh://git@github.com/",
    "git@github.com:",
)


class GitHubProvider(LocalProvider):
    """A repository provider that clones from github.com."""

    _provider_name: str = "github"

    def clone(self, url: str, dest: Path) -> Path:
        """Clone a GitHub repository into ``dest``.

        Raises ``REPO_NOT_FOUND`` when ``url`` does not name a GitHub
        repository so that a caller cannot accidentally point a
        GitHub-configured run at a GitLab URL.
        """
        if not url.startswith(_GITHUB_PREFIXES):
            raise CodeGuardianError(
                ErrorCode.REPO_NOT_FOUND,
                f"not a GitHub URL: {url}",
                details={"url": url},
            )

        dest_resolved = dest.expanduser().resolve()
        if dest_resolved.exists() and any(dest_resolved.iterdir()):
            raise CodeGuardianError(
                ErrorCode.REPO_CLONE_FAILED,
                f"destination {dest_resolved} is not empty",
                details={"dest": str(dest_resolved)},
            )
        dest_resolved.parent.mkdir(parents=True, exist_ok=True)

        result = _run_git(
            ["clone", url, str(dest_resolved)],
            timeout=600,
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
