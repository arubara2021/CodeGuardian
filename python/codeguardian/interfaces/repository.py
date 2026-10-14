"""Read-only access to source repositories.

A repository provider clones a repository into a sandbox and reads
from the clone. It has no write method: applying changes is a
separate, consent-gated operation handled by ``ApplyProvider``. The
separation is enforced by the interface itself, not by convention.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "FileChange",
    "RepoMetadata",
    "RepositoryProvider",
]


@dataclass(frozen=True, slots=True)
class FileChange:
    """A single file edit to be rendered as a unified diff.

    Attributes:
        path: Repository-relative path of the file.
        new_content: The file's new content. ``None`` means the file
            is deleted; an empty string means the file is empty.
    """

    path: str
    new_content: str | None


@dataclass(frozen=True, slots=True)
class RepoMetadata:
    """Identifying metadata for a cloned repository.

    Attributes:
        url: The URL the repository was cloned from.
        provider: The provider name, for example ``local``,
            ``github``, or ``gitlab``.
        default_branch: The default branch name, or ``None`` if the
            provider does not expose one.
        head_commit: Full commit hash at HEAD of the clone.
    """

    url: str
    provider: str
    default_branch: str | None
    head_commit: str


class RepositoryProvider(Protocol):
    """Read-only access to a source repository.

    Implementations clone into a sandbox and read from the clone. The
    original working tree is never opened for writing.

    Every method raises ``CodeGuardianError`` with a typed code on
    failure. Standard library exceptions are translated by the
    implementation, not exposed to callers.
    """

    def clone(self, url: str, dest: Path) -> Path:
        """Clone the repository at ``url`` into ``dest``.

        Args:
            url: The repository URL to clone.
            dest: Destination directory. Must be empty or nonexistent.

        Returns:
            Path to the clone, normally equal to ``dest``.

        Raises:
            CodeGuardianError: ``REPO_CLONE_FAILED`` on a failed
                clone, ``REPO_AUTH_FAILED`` on authentication
                failure, ``REPO_NOT_FOUND`` if the URL does not
                resolve.
        """
        ...

    def read_file(self, path: str) -> str:
        """Return the contents of ``path`` as text.

        Args:
            path: Repository-relative path of the file.

        Returns:
            Decoded file contents.

        Raises:
            CodeGuardianError: ``FILE_NOT_FOUND`` if the path does
                not exist, ``FILE_TOO_LARGE`` if the file exceeds the
                provider's size limit, ``FILE_BINARY`` if the file is
                not valid UTF-8, ``FILE_PERMISSION_DENIED`` if the
                file cannot be read.
        """
        ...

    def list_files(self, pattern: str = "**/*") -> list[str]:
        """Return repository-relative paths matching ``pattern``.

        Args:
            pattern: A glob pattern. The default matches every file
                in the repository.

        Returns:
            Sorted list of matching paths.

        Raises:
            CodeGuardianError: ``PATTERN_INVALID`` if the pattern is
                malformed.
        """
        ...

    def get_diff(self, changes: Sequence[FileChange]) -> str:
        """Return a unified diff for the given changes.

        The diff is computed against the state of the clone at the
        time it was made. The provider reads the original content of
        every file in ``changes`` and pairs it with the supplied new
        content.

        Args:
            changes: Edits to render as a diff.

        Returns:
            Unified diff as a single string.

        Raises:
            CodeGuardianError: ``DIFF_COMPUTE_FAILED`` if the diff
                cannot be produced.
        """
        ...

    def get_metadata(self) -> RepoMetadata:
        """Return identifying metadata for the clone.

        Returns:
            The repository's metadata.

        Raises:
            CodeGuardianError: ``METADATA_UNAVAILABLE`` if the
                provider cannot supply metadata.
        """
        ...
