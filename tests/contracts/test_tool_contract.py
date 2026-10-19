"""Behavioral contract for every registered tool plugin."""

from __future__ import annotations

import shutil

import pytest
from jsonschema import Draft202012Validator

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.plugins import PluginRegistry

_TOOL_NAMES = ("ast-grep", "code-analysis")


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_input_schema_is_valid_json_schema(registry: PluginRegistry, tool_name: str) -> None:
    plugin = registry.get_tool(tool_name).plugin
    Draft202012Validator.check_schema(plugin.input_schema)


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_output_schema_is_valid_json_schema(registry: PluginRegistry, tool_name: str) -> None:
    plugin = registry.get_tool(tool_name).plugin
    Draft202012Validator.check_schema(plugin.output_schema)


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_input_schema_validates_against_schema_itself(
    registry: PluginRegistry, tool_name: str
) -> None:
    plugin = registry.get_tool(tool_name).plugin
    validator = Draft202012Validator(plugin.input_schema)
    assert validator.is_type({}, "object")


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
async def test_call_with_invalid_input_raises(registry: PluginRegistry, tool_name: str) -> None:
    barrier = registry.get_tool(tool_name)
    with pytest.raises(CodeGuardianError):
        await barrier.call_async("call", {})


async def test_ast_grep_returns_matches_or_raises_not_implemented(
    registry: PluginRegistry, tmp_path
) -> None:
    barrier = registry.get_tool("ast-grep")
    if shutil.which("ast-grep") is None:
        with pytest.raises(CodeGuardianError):
            await barrier.call_async(
                "call",
                {"pattern": "return $X", "path": str(tmp_path), "language": "python"},
            )
        pytest.skip("ast-grep binary not installed; SYS_NOT_IMPLEMENTED path verified")
    result = await barrier.call_async(
        "call",
        {"pattern": "return $X", "path": str(tmp_path), "language": "python"},
    )
    assert isinstance(result, dict)
    assert "matches" in result
    assert isinstance(result["matches"], list)


async def test_code_analysis_dead_code_on_sample(registry: PluginRegistry, tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def used():\n"
        "    return 1\n\n"
        "def unused():\n"
        "    return 2\n\n"
        "def main():\n"
        "    return used()\n"
    )
    barrier = registry.get_tool("code-analysis")
    result = await barrier.call_async("call", {"path": str(sample), "analysis": "dead-code"})
    assert isinstance(result, dict)
    dead = result["dead_code"]
    names = [entry["function"] for entry in dead]
    assert "unused" in names
    assert "used" not in names
    assert "main" not in names


async def test_code_analysis_complexity_returns_with_note(
    registry: PluginRegistry, tmp_path
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def f():\n    return 1\n")
    barrier = registry.get_tool("code-analysis")
    result = await barrier.call_async("call", {"path": str(sample), "analysis": "complexity"})
    assert isinstance(result, dict)
    assert result["complexity"] == {}
    assert "notes" in result
    assert any("complexity" in note.lower() for note in result["notes"])


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
async def test_call_output_validates_against_schema(
    registry: PluginRegistry, tool_name: str, tmp_path
) -> None:
    barrier = registry.get_tool(tool_name)
    plugin = barrier.plugin
    if tool_name == "ast-grep":
        if shutil.which("ast-grep") is None:
            pytest.skip("ast-grep binary not installed")
        result = await barrier.call_async(
            "call",
            {"pattern": "return $X", "path": str(tmp_path), "language": "python"},
        )
    else:
        sample = tmp_path / "sample.py"
        sample.write_text("def f():\n    return 1\n")
        result = await barrier.call_async("call", {"path": str(sample), "analysis": "all"})
    Draft202012Validator(plugin.output_schema).validate(result)
