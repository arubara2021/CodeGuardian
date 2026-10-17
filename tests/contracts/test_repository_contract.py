"""Contract tests for ``RepositoryProvider`` implementations.

Every provider must satisfy this suite. ``LocalProvider`` runs the full
contract against a real git repository. ``GitHubProvider`` and
``GitLabProvider`` share every method with ``LocalProvider``; the only
difference is the URL validation in ``clone``, which is tested without
a network.

The real clone path is verified once, against the local provider. The
GitHub and GitLab providers would test the identical ``git clone``
subprocess if given a valid URL, so testing their URL validation and
their inheritance is the meaningful contract, not a network round trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.providers.github_repository import GitHubProvider
from codeguardian.core.providers.gitlab_repository import GitLabProvider
from codeguardian.core.providers.local_repository import LocalProvider
from codeguardian.interfaces.repository import FileChange, RepoMetadata


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` and raise if it fails."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """Create a real git repository with known commits and files."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test.example")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def local_provider(tmp_path: Path, source_repo: Path) -> LocalProvider:
    """Return a ``LocalProvider`` cloned from ``source_repo``."""
    provider = LocalProvider()
    provider.clone(str(source_repo), tmp_path / "clone")
    return provider


class TestLocalProviderClone:
    def test_clone_returns_clone_path(self, tmp_path, source_repo):
        dest = tmp_path / "clone"
        provider = LocalProvider()
        result = provider.clone(str(source_repo), dest)
        assert result == dest.resolve()
        assert (dest / "hello.txt").is_file()

    def test_clone_nonexistent_source_raises(self, tmp_path):
        provider = LocalProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone(str(tmp_path / "nope"), tmp_path / "clone")
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND

    def test_clone_non_git_source_raises(self, tmp_path):
        not_git = tmp_path / "not-git"
        not_git.mkdir()
        provider = LocalProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone(str(not_git), tmp_path / "clone")
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND

    def test_clone_non_empty_dest_raises(self, tmp_path, source_repo):
        dest = tmp_path / "occupied"
        dest.mkdir()
        (dest / "existing.txt").write_text("x", encoding="utf-8")
        provider = LocalProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone(str(source_repo), dest)
        assert exc.value.code is ErrorCode.REPO_CLONE_FAILED


class TestLocalProviderRead:
    def test_read_file_returns_content(self, local_provider):
        assert local_provider.read_file("hello.txt") == "hello\n"

    def test_read_file_nested_path(self, local_provider):
        assert local_provider.read_file("src/main.py") == "print('hi')\n"

    def test_read_file_missing_raises(self, local_provider):
        with pytest.raises(CodeGuardianError) as exc:
            local_provider.read_file("missing.txt")
        assert exc.value.code is ErrorCode.FILE_NOT_FOUND

    def test_read_file_directory_raises(self, local_provider):
        with pytest.raises(CodeGuardianError) as exc:
            local_provider.read_file("src")
        assert exc.value.code is ErrorCode.FILE_NOT_FOUND

    def test_read_file_path_traversal_raises(self, local_provider):
        with pytest.raises(CodeGuardianError) as exc:
            local_provider.read_file("../../../etc/passwd")
        assert exc.value.code is ErrorCode.FILE_NOT_FOUND
        assert "escapes" in exc.value.message.lower()

    def test_read_file_too_large_raises(self, tmp_path, source_repo):
        large = source_repo / "large.txt"
        large.write_text("x" * 200, encoding="utf-8")
        _git(source_repo, "add", "large.txt")
        _git(source_repo, "commit", "-q", "-m", "large")
        dest = tmp_path / "clone"
        provider = LocalProvider()
        provider.clone(str(source_repo), dest)
        provider._max_file_bytes = 100
        with pytest.raises(CodeGuardianError) as exc:
            provider.read_file("large.txt")
        assert exc.value.code is ErrorCode.FILE_TOO_LARGE

    def test_read_file_binary_raises(self, tmp_path, source_repo):
        (source_repo / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        _git(source_repo, "add", "blob.bin")
        _git(source_repo, "commit", "-q", "-m", "binary")
        dest = tmp_path / "clone"
        provider = LocalProvider()
        provider.clone(str(source_repo), dest)
        with pytest.raises(CodeGuardianError) as exc:
            provider.read_file("blob.bin")
        assert exc.value.code is ErrorCode.FILE_BINARY

    def test_read_before_clone_raises(self):
        provider = LocalProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.read_file("hello.txt")
        assert exc.value.code is ErrorCode.SYS_INVARIANT_VIOLATED


class TestLocalProviderList:
    def test_list_files_returns_all_tracked(self, local_provider):
        files = local_provider.list_files()
        assert files == sorted(["docs/guide.md", "hello.txt", "src/main.py"])

    def test_list_files_glob_filters(self, local_provider):
        files = local_provider.list_files("src/*.py")
        assert files == ["src/main.py"]

    def test_list_files_double_star_glob(self, local_provider):
        files = local_provider.list_files("**/*.md")
        assert files == ["docs/guide.md"]

    def test_list_files_no_match_returns_empty(self, local_provider):
        assert local_provider.list_files("*.zzz") == []


class TestLocalProviderDiff:
    def test_get_diff_modification(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content="world\n")])
        assert diff.startswith("--- a/hello.txt\n+++ b/hello.txt\n")
        assert "-hello" in diff
        assert "+world" in diff

    def test_get_diff_new_file(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="new.txt", new_content="fresh\n")])
        assert "--- /dev/null" in diff
        assert "+++ b/new.txt" in diff
        assert "+fresh" in diff

    def test_get_diff_deletion(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content=None)])
        assert "--- a/hello.txt" in diff
        assert "+++ /dev/null" in diff
        assert "-hello" in diff

    def test_get_diff_empty_changes_returns_empty(self, local_provider):
        assert local_provider.get_diff([]) == ""

    def test_get_diff_identical_content_returns_empty(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content="hello\n")])
        assert diff == ""


class TestLocalProviderMetadata:
    def test_get_metadata_returns_head_and_branch(self, local_provider):
        metadata = local_provider.get_metadata()
        assert isinstance(metadata, RepoMetadata)
        assert len(metadata.head_commit) == 40
        assert metadata.default_branch == "main"
        assert metadata.provider == "local"
        assert metadata.url

    def test_get_metadata_matches_git_head(self, local_provider, tmp_path):
        clone = tmp_path / "clone"
        expected = _git(clone, "rev-parse", "HEAD").stdout.strip()
        assert local_provider.get_metadata().head_commit == expected


class TestGitHubProviderURLValidation:
    def test_rejects_non_github_url(self, tmp_path):
        provider = GitHubProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone("https://gitlab.com/foo/bar.git", tmp_path / "dest")
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND
        assert "github" in exc.value.message.lower()

    def test_rejects_local_path(self, tmp_path):
        provider = GitHubProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone(str(tmp_path), tmp_path / "dest")
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND

    def test_provider_name_is_github(self):
        assert GitHubProvider._provider_name == "github"

    def test_inherits_from_local_provider(self):
        assert issubclass(GitHubProvider, LocalProvider)


class TestGitLabProviderURLValidation:
    def test_rejects_non_gitlab_url(self, tmp_path):
        provider = GitLabProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone("https://github.com/foo/bar.git", tmp_path / "dest")
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND
        assert "gitlab" in exc.value.message.lower()

    def test_rejects_local_path(self, tmp_path):
        provider = GitLabProvider()
        with pytest.raises(CodeGuardianError) as exc:
            provider.clone(str(tmp_path), tmp_path / "dest")
        assert exc.value.code is ErrorCode.REPO_NOT_FOUND

    def test_provider_name_is_gitlab(self):
        assert GitLabProvider._provider_name == "gitlab"

    def test_inherits_from_local_provider(self):
        assert issubclass(GitLabProvider, LocalProvider)
