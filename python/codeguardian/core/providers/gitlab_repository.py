"""GitLab repository provider.

Clone over HTTPS, SSH, or the ``git@gitlab.com:`` form. All other
methods are inherited from ``LocalProvider``.
"""

from __future__ import annotations

from pathlib import Path

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .local_repository import (
    LocalProvider,
    _classify_clone_failure,
    _run_git,
)

__all__ = ["GitLabProvider"]

_GITLAB_PREFIXES = (
    "https://gitlab.com/",
    "http://gitlab.com/",
    "ssh://git@gitlab.com/",
    "git@gitlab.com:",
)


class GitLabProvider(LocalProvider):
    """A repository provider that clones from gitlab.com."""

    _provider_name: str = "gitlab"

    def clone(self, url: str, dest: Path) -> Path:
        """Clone a GitLab repository into ``dest``.

        Raises ``REPO_NOT_FOUND`` when ``url`` does not name a GitLab
        repository.
        """
        if not url.startswith(_GITLAB_PREFIXES):
            raise CodeGuardianError(
                ErrorCode.REPO_NOT_FOUND,
                f"not a GitLab URL: {url}",
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
