"""Tests for path validation and symlink-safe file opening.

Seven probes, one per guard class. Each probe is a standalone test
that can be run in isolation. The suite is deliberately small and
focused: it proves the guards work, not that the audit hook is wired
into every call site. The wiring is proven by the deep test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.orchestrator.path_guard import (
    canonicalize_and_contain,
    is_denied,
    is_path_permitted,
    safe_open,
)


class TestPrefixSibling:
    def test_sibling_with_shared_prefix_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "prompts"
        root.mkdir()
        sibling = tmp_path / "prompts_evil"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("secret")

        with pytest.raises(CodeGuardianError):
            canonicalize_and_contain(root, sibling / "secret.txt")

    def test_containment_helper_rejects_sibling(self, tmp_path: Path) -> None:
        root = tmp_path / "prompts"
        root.mkdir()
        sibling = tmp_path / "prompts_evil"
        sibling.mkdir()
        assert not is_path_permitted(
            sandbox_root=root,
            candidate=sibling,
            trusted_read_roots=(),
            for_write=False,
        )


class TestTraversal:
    def test_dotdot_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "prompts"
        root.mkdir()
        target = root / ".." / "etc" / "passwd"
        with pytest.raises(CodeGuardianError):
            canonicalize_and_contain(root, target)

    def test_nested_dotdot_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "prompts"
        root.mkdir()
        target = root / "sub" / ".." / ".." / "etc" / "passwd"
        with pytest.raises(CodeGuardianError):
            canonicalize_and_contain(root, target)


class TestSymlink:
    def test_symlink_component_rejected_by_safe_open(
        self,
        tmp_path: Path,
    ) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = sandbox / "link"
        link.symlink_to(outside)

        with pytest.raises(CodeGuardianError):
            safe_open(sandbox, "link/secret.txt")

    def test_final_symlink_rejected_by_safe_open(
        self,
        tmp_path: Path,
    ) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = sandbox / "file.txt"
        link.symlink_to(outside)

        with pytest.raises(CodeGuardianError):
            safe_open(sandbox, "file.txt")

    def test_regular_file_opens(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("hello")
        with safe_open(sandbox, "file.txt") as f:
            assert f.read() == b"hello"


class TestTOCTOUSimulation:
    def test_no_follow_flag_on_every_component(self, tmp_path: Path) -> None:
        """Every os.open call during safe_open carries O_NOFOLLOW."""
        if os.open not in os.supports_dir_fd:
            pytest.skip("dir_fd not supported on this platform")

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        sub = sandbox / "sub"
        sub.mkdir()
        (sub / "file.txt").write_text("hello")

        flags_seen: list[int] = []
        real_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        original = os.open
        os.open = spy_open
        try:
            with safe_open(sandbox, "sub/file.txt") as f:
                f.read()
        finally:
            os.open = original

        nofollow = os.O_NOFOLLOW
        per_component_opens = [f for f in flags_seen if f & os.O_DIRECTORY]
        final_open = [f for f in flags_seen if not (f & os.O_DIRECTORY)]
        for flags in per_component_opens:
            assert flags & nofollow, f"directory open missing O_NOFOLLOW: {flags:#o}"
        for flags in final_open:
            assert flags & nofollow, f"file open missing O_NOFOLLOW: {flags:#o}"


class TestAlias:
    def test_dot_env_via_dot_slash_rejected(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / ".env").write_text("SECRET=1")

        assert is_denied(str((sandbox / ".env").resolve()))
        assert not is_path_permitted(
            sandbox_root=sandbox,
            candidate=sandbox / ".env",
            trusted_read_roots=(),
            for_write=False,
        )

    def test_dot_env_with_trailing_dot_rejected(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / ".env").write_text("SECRET=1")

        # On most filesystems the trailing dot is a distinct name;
        # the point is that both forms canonicalize to a path the
        # deny set rejects.
        assert not is_path_permitted(
            sandbox_root=sandbox,
            candidate=sandbox / ".env.",
            trusted_read_roots=(),
            for_write=False,
        )


class TestDenyPrecedence:
    def test_deny_overrides_trusted_read_root(self, tmp_path: Path) -> None:
        trusted = tmp_path / "trusted"
        trusted.mkdir()
        (trusted / "id_rsa").write_text("private key")

        assert not is_path_permitted(
            sandbox_root=tmp_path / "sandbox",
            candidate=trusted / "id_rsa",
            trusted_read_roots=(trusted,),
            for_write=False,
        )

    def test_deny_overrides_sandbox_root(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / ".env").write_text("SECRET=1")

        assert not is_path_permitted(
            sandbox_root=sandbox,
            candidate=sandbox / ".env",
            trusted_read_roots=(),
            for_write=False,
        )


class TestFailClosed:
    def test_broken_symlink_denied(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        broken = sandbox / "broken"
        broken.symlink_to(tmp_path / "does-not-exist")

        assert not is_path_permitted(
            sandbox_root=sandbox,
            candidate=broken,
            trusted_read_roots=(),
            for_write=False,
        )

    def test_nonexistent_parent_denied_by_safe_open(
        self,
        tmp_path: Path,
    ) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        with pytest.raises((CodeGuardianError, FileNotFoundError)):
            safe_open(sandbox, "missing/child.txt")


class TestTrustedReadPaths:
    def test_read_under_trusted_root_permitted(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        trusted = tmp_path / "prompts"
        trusted.mkdir()
        (trusted / "analyst.md").write_text("prompt")

        assert is_path_permitted(
            sandbox_root=sandbox,
            candidate=trusted / "analyst.md",
            trusted_read_roots=(trusted,),
            for_write=False,
        )

    def test_write_under_trusted_root_refused(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        trusted = tmp_path / "prompts"
        trusted.mkdir()

        assert not is_path_permitted(
            sandbox_root=sandbox,
            candidate=trusted / "new.md",
            trusted_read_roots=(trusted,),
            for_write=True,
        )

    def test_read_under_sandbox_root_permitted(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "src.py").write_text("x = 1")

        assert is_path_permitted(
            sandbox_root=sandbox,
            candidate=sandbox / "src.py",
            trusted_read_roots=(),
            for_write=False,
        )
