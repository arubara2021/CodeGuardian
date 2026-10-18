"""Contract tests for ``RepositoryProvider`` implementations.

Every provider must satisfy this suite. ``LocalProvider`` runs the full
contract against a real git repository. ``GitHubProvider`` and
``GitLabProvider`` share every method with ``LocalProvider``; the only
difference is the URL validation in ``clone``, which is tested without
a network.

The diff tests are shaped to catch the exact bugs that ``git apply``
will reject: no trailing newline on either side, multiple hunks, and
a realistic Python refactor. Every diff is verified with
``git apply --check`` before the test asserts anything else, so a diff
that looks right but does not apply is a hard failure.
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


def _git_apply_check(diff: str, repo: Path) -> bool:
    """Return whether ``git apply --check`` accepts ``diff`` in ``repo``."""
    result = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=str(repo),
        input=diff.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


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
        assert "--- a/hello.txt" in diff
        assert "+++ b/hello.txt" in diff
        assert "-hello" in diff
        assert "+world" in diff
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_new_file(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="new.txt", new_content="fresh\n")])
        assert "--- /dev/null" in diff
        assert "+++ b/new.txt" in diff
        assert "+fresh" in diff
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_deletion(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content=None)])
        assert "--- a/hello.txt" in diff
        assert "+++ /dev/null" in diff
        assert "-hello" in diff
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_empty_changes_returns_empty(self, local_provider):
        assert local_provider.get_diff([]) == ""

    def test_get_diff_identical_content_returns_empty(self, local_provider):
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content="hello\n")])
        assert diff == ""

    def test_get_diff_old_side_lacks_trailing_newline(self, local_provider):
        """The old file has no trailing newline; new does.

        Before the ``git diff --no-index`` fix, this produced a diff
        that ``git apply`` rejected because ``difflib`` did not emit
        the ``\\ No newline at end of file`` marker.
        """
        clone_file = local_provider.path / "hello.txt"
        clone_file.write_bytes(b"hello")
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content="hello\n")])
        assert diff != ""
        assert "\\ No newline at end of file" in diff
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_neither_side_has_trailing_newline(self, local_provider):
        clone_file = local_provider.path / "hello.txt"
        clone_file.write_bytes(b"hello")
        diff = local_provider.get_diff([FileChange(path="hello.txt", new_content="world")])
        assert diff != ""
        assert diff.count("\\ No newline at end of file") == 2
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_multi_hunk(self, local_provider):
        original = "".join(f"line{i}\n" for i in range(1, 21))
        new = original.replace("line2\n", "CHANGED2\n").replace("line18\n", "CHANGED18\n")
        (local_provider.path / "multi.txt").write_bytes(original.encode())
        diff = local_provider.get_diff([FileChange(path="multi.txt", new_content=new)])
        # Each hunk header contains "@@" at start and end, so count
        # "@@ -" (which appears only at hunk starts) to get 2.
        assert diff.count("@@ -") == 2
        assert "CHANGED2" in diff
        assert "CHANGED18" in diff
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_docstring_removal(self, local_provider):
        old = (
            '"""Calc module."""\n'
            "\n"
            "\n"
            "def add(a, b):\n"
            '    """\n'
            "    Add two numbers.\n"
            "\n"
            "    Parameters:\n"
            "    -----------\n"
            "    a: First number\n"
            "    b: Second number\n"
            '    """\n'
            "    return a + b\n"
        )
        new = (
            '"""Calc module."""\n'
            "\n"
            "\n"
            "def add(a: int, b: int) -> int:\n"
            '    """Return the sum of a and b."""\n'
            "    return a + b\n"
        )
        (local_provider.path / "calc.py").write_bytes(old.encode())
        diff = local_provider.get_diff([FileChange(path="calc.py", new_content=new)])
        assert "-    -----------" in diff
        assert "+def add(a: int, b: int) -> int:" in diff
        assert _git_apply_check(diff, local_provider.path)

    def test_get_diff_writer_shaped_refactor(self, local_provider):
        """The exact shape that failed in the Phase 8 integration run.

        A real 120B model refactoring a real Python file produces:
        new imports, new type alias, rewritten function signatures,
        rewritten docstrings, and full docstring block removals. Every
        one of those is present here.
        """
        old = (
            '"""Calc module."""\n'
            "\n"
            "\n"
            "def add(a, b):\n"
            '    """Add two numbers."""\n'
            "    return a + b\n"
            "\n"
            "\n"
            "def subtract(a, b):\n"
            '    """Subtract b from a."""\n'
            "    return a - b\n"
        )
        new = (
            '"""Calc module."""\n'
            "\n"
            "from typing import Union\n"
            "\n"
            "Number = Union[int, float]\n"
            "\n"
            "\n"
            "def add(a: Number, b: Number) -> Number:\n"
            '    """Return the sum of a and b."""\n'
            "    return a + b\n"
            "\n"
            "\n"
            "def subtract(a: Number, b: Number) -> Number:\n"
            '    """Return the difference of a and b."""\n'
            "    return a - b\n"
        )
        (local_provider.path / "calc.py").write_bytes(old.encode())
        diff = local_provider.get_diff([FileChange(path="calc.py", new_content=new)])
        assert "+from typing import Union" in diff
        assert "+Number = Union[int, float]" in diff
        assert "+def add(a: Number, b: Number) -> Number:" in diff
        assert _git_apply_check(diff, local_provider.path)


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
