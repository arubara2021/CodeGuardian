"""Behavioral contract for every registered language plugin.

Every assertion here fails for a stub plugin that returns ``{}`` or an
empty string. That is the point: shape-only assertions let a do-nothing
plugin pass. These check that the plugin actually produces work.
"""

from __future__ import annotations

import pytest

from codeguardian import _native
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import PluginRegistry

_SAMPLES: dict[str, tuple[str, str]] = {
    "python": (".py", "def f(x):\n    return x + 1\n"),
    "typescript": (".ts", "function f(x: number): number { return x + 1; }\n"),
    "javascript": (".js", "function f(x) { return x + 1; }\n"),
    "java": (".java", "class C { static int f(int x) { return x + 1; } }\n"),
    "go": (".go", "package main\nfunc f(x int) int { return x + 1 }\n"),
    "rust": (".rs", "fn f(x: i32) -> i32 { x + 1 }\n"),
    "c": (".c", "int f(int x) { return x + 1; }\n"),
    "cpp": (".cpp", "int f(int x) { return x + 1; }\n"),
}

_EXPECTED_HIGH_TIER = frozenset({"python", "typescript"})
_EXPECTED_BEST_EFFORT = frozenset({"javascript", "java", "go", "rust", "c", "cpp"})

_RUNNER_MARKERS = {
    "pytest": "def test_",
    "vitest": "it.skip",
    "junit": "@Test",
    "go test": "func Test",
    "cargo test": "#[test]",
    "make test": "main",
}


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture(scope="module")
def language_plugins(registry: PluginRegistry) -> list[tuple[str, object]]:
    return [
        (record.name, registry.get_language(record.name))
        for record in registry.list_plugins()
        if record.type == "language"
    ]


def _barrier_and_sample(language_plugins, tmp_path):
    assert language_plugins, "no language plugins registered"
    return language_plugins, tmp_path


def test_every_bundled_language_is_registered(
    language_plugins: list[tuple[str, object]],
) -> None:
    names = sorted(name for name, _ in language_plugins)
    assert names == [
        "c",
        "cpp",
        "go",
        "java",
        "javascript",
        "python",
        "rust",
        "typescript",
    ]


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_detect_claims_expected_extension(
    registry: PluginRegistry, language_name: str, tmp_path
) -> None:
    barrier = registry.get_language(language_name)
    extension, _ = _SAMPLES[language_name]
    sample = tmp_path / f"sample{extension}"
    sample.write_text("", encoding="utf-8")
    assert barrier.call("detect", sample) is True


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_detect_rejects_unknown_extension(
    registry: PluginRegistry, language_name: str, tmp_path
) -> None:
    barrier = registry.get_language(language_name)
    unknown = tmp_path / "sample.unknownext"
    unknown.write_text("", encoding="utf-8")
    assert barrier.call("detect", unknown) is False


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_parse_produces_nodes(registry: PluginRegistry, language_name: str, tmp_path) -> None:
    barrier = registry.get_language(language_name)
    extension, source = _SAMPLES[language_name]
    sample = tmp_path / f"sample{extension}"
    sample.write_text(source, encoding="utf-8")
    cpg = barrier.call("parse", sample)
    assert isinstance(cpg, (bytes, bytearray))
    assert len(cpg) > 0
    summary = _native.graph_summary(bytes(cpg))
    assert summary["node_count"] > 0, language_name
    assert summary["file_count"] == 1, language_name


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_parse_produces_edges(registry: PluginRegistry, language_name: str, tmp_path) -> None:
    barrier = registry.get_language(language_name)
    extension, source = _SAMPLES[language_name]
    sample = tmp_path / f"sample{extension}"
    sample.write_text(source, encoding="utf-8")
    cpg = barrier.call("parse", sample)
    summary = _native.graph_summary(bytes(cpg))
    assert summary["edge_count"] > 0, language_name


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_generate_test_returns_runner_appropriate_output(
    registry: PluginRegistry, language_name: str, tmp_path
) -> None:
    barrier = registry.get_language(language_name)
    plugin = barrier.plugin
    extension, source = _SAMPLES[language_name]
    sample = tmp_path / f"sample{extension}"
    sample.write_text(source, encoding="utf-8")
    out = barrier.call("generate_test", sample)
    assert isinstance(out, str)
    assert len(out) > 20
    marker = _RUNNER_MARKERS.get(plugin.test_runner, "")
    if marker:
        assert marker in out, (language_name, plugin.test_runner)


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_run_tests_raises_not_implemented(registry: PluginRegistry, language_name: str) -> None:
    barrier = registry.get_language(language_name)
    with pytest.raises(CodeGuardianError) as excinfo:
        barrier.call("run_tests", None, "irrelevant")
    assert excinfo.value.code == ErrorCode.SYS_NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_blast_scope_is_declared(registry: PluginRegistry, language_name: str) -> None:
    plugin = registry.get_language(language_name).plugin
    assert plugin.blast_scope in {"same-file", "cross-file"}


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_reliability_tier_matches_declared_contract(
    registry: PluginRegistry, language_name: str
) -> None:
    plugin = registry.get_language(language_name).plugin
    if language_name in _EXPECTED_BEST_EFFORT:
        assert plugin.reliability_tier == "best-effort"
    elif language_name in _EXPECTED_HIGH_TIER:
        assert plugin.reliability_tier == "high"
    else:
        pytest.fail(f"unclassified language in contract: {language_name}")


@pytest.mark.parametrize(
    "language_name",
    [
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
    ],
)
def test_kernel_extensible_is_false_for_bundled(
    registry: PluginRegistry, language_name: str
) -> None:
    plugin = registry.get_language(language_name).plugin
    assert plugin.kernel_extensible is False
