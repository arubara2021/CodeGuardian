"""Tests for the action_kind, sensitivity, and is_read_only fields.

Every bundled tool must declare all three fields. The fields determine
how the agent approval layer gates a call:

- A ``plan``-mode agent may only call tools with ``action_kind == "read"``.
- An ``auto-edit`` agent may call ``"read"`` and ``"write"``.
- A ``workspace`` agent may call any action kind.

The invariants checked here are the ones the approval layer depends on.
The one stricter invariant — ``is_read_only == (action_kind == "read")`` —
is NOT checked because it is false for a legitimate case: a tool with
``action_kind == "execute"`` that spawns a read-only subprocess. ast-grep
is exactly that: it spawns the binary, but does not write to the
workspace. So the invariant is softened to:
``action_kind == "write"`` implies ``is_read_only is False``.
"""

from __future__ import annotations

import pytest

from codeguardian.core.plugins import PluginRegistry

_VALID_ACTIONS = {"read", "write", "execute", "network"}
_VALID_SENSITIVITIES = {"low", "medium", "high"}


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture(scope="module")
def tool_plugins(registry: PluginRegistry):
    return [
        (record.name, registry.get_tool(record.name).plugin)
        for record in registry.list_plugins()
        if record.type == "tool"
    ]


def test_every_tool_declares_action_kind(tool_plugins):
    assert tool_plugins, "no tool plugins registered"
    for name, plugin in tool_plugins:
        assert hasattr(plugin, "action_kind"), f"{name} missing action_kind"
        assert plugin.action_kind in _VALID_ACTIONS, f"{name}: {plugin.action_kind!r}"


def test_every_tool_declares_sensitivity(tool_plugins):
    for name, plugin in tool_plugins:
        assert hasattr(plugin, "sensitivity"), f"{name} missing sensitivity"
        assert plugin.sensitivity in _VALID_SENSITIVITIES, f"{name}: {plugin.sensitivity!r}"


def test_every_tool_declares_is_read_only(tool_plugins):
    for name, plugin in tool_plugins:
        assert hasattr(plugin, "is_read_only"), f"{name} missing is_read_only"
        assert isinstance(plugin.is_read_only, bool), f"{name}: {type(plugin.is_read_only)}"


def test_write_tools_are_not_read_only(tool_plugins):
    for name, plugin in tool_plugins:
        if plugin.action_kind == "write":
            assert not plugin.is_read_only, f"{name}: write tool marked read_only"


def test_expected_tools_present(tool_plugins):
    names = sorted(name for name, _ in tool_plugins)
    assert names == ["ast-grep", "code-analysis"], names


def test_ast_grep_is_execute(tool_plugins):
    plugins = dict(tool_plugins)
    assert plugins["ast-grep"].action_kind == "execute"
    assert plugins["ast-grep"].is_read_only is True


def test_code_analysis_is_read(tool_plugins):
    plugins = dict(tool_plugins)
    assert plugins["code-analysis"].action_kind == "read"
    assert plugins["code-analysis"].is_read_only is True
