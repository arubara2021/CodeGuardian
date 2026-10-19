"""Integration tests for runtime tree-sitter grammar loading.

The test needs a compiled `libtree_sitter_ruby` shared library. Three
places are checked, in order:

1. The ``CODEGUARDIAN_TEST_RUBY_GRAMMAR`` environment variable.
2. The ``tree-sitter-ruby`` pip package: its installed directory ships
   the compiled library.
3. ``~/.cache/codeguardian-target/release/deps`` for a compiled artifact
   left over from another build.

If none is available, every test in this module skips with a clear
reason. Nothing here depends on network access or a build step.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from codeguardian import _native

_SO_NAMES = {
    "linux": "libtree_sitter_ruby.so",
    "darwin": "libtree_sitter_ruby.dylib",
    "win32": "tree_sitter_ruby.dll",
}


def _so_filename() -> str:
    key = (
        "win32"
        if sys.platform.startswith("win")
        else ("darwin" if sys.platform == "darwin" else "linux")
    )
    return _SO_NAMES[key]


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []

    env = os.environ.get("CODEGUARDIAN_TEST_RUBY_GRAMMAR")
    if env:
        candidates.append(Path(env))

    try:
        import tree_sitter_ruby

        package_dir = Path(tree_sitter_ruby.__file__).parent
        for candidate in package_dir.rglob(_so_filename()):
            candidates.append(candidate)
    except Exception:
        pass

    cache_dir = Path.home() / ".cache" / "codeguardian-target"
    if cache_dir.is_dir():
        for candidate in cache_dir.rglob(_so_filename()):
            candidates.append(candidate)

    return candidates


def _find_grammar_library() -> Path | None:
    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture(scope="module")
def ruby_library() -> Path:
    library = _find_grammar_library()
    if library is None:
        pytest.skip(
            "no libtree_sitter_ruby shared library found. Install "
            "`tree-sitter-ruby`, or set CODEGUARDIAN_TEST_RUBY_GRAMMAR "
            "to a compiled library path, to run these tests."
        )
    return library


def test_load_grammar_registers_language(ruby_library: Path) -> None:
    _native.load_grammar("ruby", str(ruby_library))
    languages = _native.list_languages()
    assert "ruby" in languages
    assert "python" in languages
    assert languages == sorted(languages)


def test_parse_ruby_through_kernel(ruby_library: Path) -> None:
    if "ruby" not in _native.list_languages():
        _native.load_grammar("ruby", str(ruby_library))

    source = "def add(a, b)\n  a + b\nend\n\ndef call_add\n  add(1, 2)\nend\n"
    cpg = _native.build_graph([("main.rb", "ruby", source)])
    assert isinstance(cpg, (bytes, bytearray))
    assert len(cpg) > 0

    summary = _native.graph_summary(bytes(cpg))
    assert summary["node_count"] > 0
    assert summary["edge_count"] > 0
    assert summary["file_count"] == 1


def test_duplicate_load_raises_already_loaded(ruby_library: Path) -> None:
    if "ruby" not in _native.list_languages():
        _native.load_grammar("ruby", str(ruby_library))

    with pytest.raises(Exception) as excinfo:
        _native.load_grammar("ruby", str(ruby_library))

    code = getattr(excinfo.value, "code", "")
    assert code == "GRAMMAR_ALREADY_LOADED", code


def test_missing_library_raises_library_not_found() -> None:
    with pytest.raises(Exception) as excinfo:
        _native.load_grammar(
            "definitely-not-a-real-language-xyz",
            "/tmp/does-not-exist-lib.so",
        )
    code = getattr(excinfo.value, "code", "")
    assert code == "GRAMMAR_LIBRARY_NOT_FOUND", code


def test_built_in_language_cannot_be_overwritten(ruby_library: Path) -> None:
    with pytest.raises(Exception) as excinfo:
        _native.load_grammar("python", str(ruby_library))
    code = getattr(excinfo.value, "code", "")
    assert code == "GRAMMAR_ALREADY_LOADED", code


def test_unrecognized_language_still_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        _native.build_graph([("x.zzz", "definitely-unknown-lang", "")])
    assert "unrecognized language" in str(excinfo.value).lower()
