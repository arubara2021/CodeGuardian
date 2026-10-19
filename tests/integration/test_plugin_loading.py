"""Integration tests for the plugin discovery and loading pipeline."""

from __future__ import annotations

import logging

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import (
    BundledSource,
    EntryPointSource,
    PluginRegistry,
    PluginSpec,
    discover_all,
)
from codeguardian.core.plugins.registry import RegisteredPlugin


class _StubTool:
    """A minimal valid tool plugin used as a known-good spec."""

    name = "stub-tool"
    description = "stub tool for tests"
    input_schema = {"type": "object", "properties": {}}
    output_schema = {"type": "object", "properties": {}}

    __manifest__ = {
        "name": "stub-tool",
        "version": "1.0.0",
        "type": "tool",
        "interface_version": "1.0",
        "permissions": {"filesystem": "none", "network": False},
    }

    async def call(self, input: dict[str, object]) -> dict[str, object]:
        return {"ok": True}


class _StubToolV2(_StubTool):
    __manifest__ = dict(_StubTool.__manifest__)
    __manifest__["interface_version"] = "2.0"


class _StubToolV11(_StubTool):
    __manifest__ = dict(_StubTool.__manifest__)
    __manifest__["interface_version"] = "1.1"
    __manifest__["name"] = "stub-tool-v11"


class _NotATool:
    """Has a __manifest__ but does not satisfy ToolPlugin."""

    __manifest__ = {
        "name": "not-a-tool",
        "version": "1.0.0",
        "type": "tool",
        "interface_version": "1.0",
        "permissions": {"filesystem": "none", "network": False},
    }


class _FakeSource:
    """Yields a fixed list of specs, bypassing filesystem and entry points."""

    def __init__(self, specs: list[PluginSpec]) -> None:
        self._specs = specs

    def iter_specs(self):
        yield from self._specs


def _spec(cls: type, name: str | None = None) -> PluginSpec:
    return PluginSpec(
        source="test",
        manifest_raw=dict(cls.__manifest__),
        plugin_class=cls,
        package_path=None,
    )


def test_bundled_discovery_finds_every_plugin() -> None:
    registry = PluginRegistry.discover_bundled()
    languages = sorted(p.name for p in registry.list_plugins() if p.type == "language")
    tools = sorted(p.name for p in registry.list_plugins() if p.type == "tool")
    reporters = sorted(p.name for p in registry.list_plugins() if p.type == "reporter")
    assert languages == [
        "c",
        "cpp",
        "go",
        "java",
        "javascript",
        "python",
        "rust",
        "typescript",
    ]
    assert tools == ["ast-grep", "code-analysis"]
    assert reporters == ["sarif"]


def test_bundled_source_yields_matching_specs() -> None:
    specs = discover_all([BundledSource()])
    assert len(specs) == 11
    names = {spec.manifest_raw["name"] for spec in specs}
    assert names == {
        "python",
        "typescript",
        "javascript",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
        "ast-grep",
        "code-analysis",
        "sarif",
    }


def test_entry_point_source_yields_nothing_by_default() -> None:
    specs = discover_all([EntryPointSource()])
    assert isinstance(specs, list)


def test_broken_manifest_does_not_abort_discovery() -> None:
    bad = _spec(_StubTool)
    bad.manifest_raw["name"] = "Invalid Name With Spaces"
    source = _FakeSource([bad, _spec(_StubTool)])
    registry = PluginRegistry.discover([source])
    names = [p.name for p in registry.list_plugins()]
    assert "stub-tool" in names


def test_interface_version_too_new_is_rejected() -> None:
    source = _FakeSource([_spec(_StubToolV2)])
    registry = PluginRegistry.discover([source])
    assert not registry.list_plugins()


def test_interface_version_newer_minor_is_accepted_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="codeguardian.core.plugins.registry"):
        source = _FakeSource([_spec(_StubToolV11)])
        registry = PluginRegistry.discover([source])
    names = [p.name for p in registry.list_plugins()]
    assert "stub-tool-v11" in names
    assert any(
        "newer_minor" in rec.message or "targets interface" in rec.message for rec in caplog.records
    )


def test_protocol_mismatch_is_rejected() -> None:
    source = _FakeSource([_spec(_NotATool)])
    registry = PluginRegistry.discover([source])
    assert not registry.list_plugins()


def test_duplicate_registration_keeps_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec1 = _spec(_StubTool)
    spec2 = PluginSpec(
        source="second",
        manifest_raw=dict(_StubTool.__manifest__),
        plugin_class=_StubTool,
        package_path=None,
    )
    with caplog.at_level(logging.WARNING, logger="codeguardian.core.plugins.registry"):
        registry = PluginRegistry.discover([_FakeSource([spec1, spec2])])
    registered = [p for p in registry.list_plugins() if p.name == "stub-tool"]
    assert len(registered) == 1
    assert registered[0].source == "test"
    assert any("already registered" in rec.message for rec in caplog.records)


def test_get_returns_fault_barrier() -> None:
    registry = PluginRegistry.discover_bundled()
    barrier = registry.get_language("python")
    from codeguardian.core.plugins import FaultBarrier

    assert isinstance(barrier, FaultBarrier)
    assert barrier.plugin_name == "python"


def test_get_unknown_name_raises() -> None:
    registry = PluginRegistry.discover_bundled()
    with pytest.raises(CodeGuardianError) as excinfo:
        registry.get_language("nope")
    assert excinfo.value.code == ErrorCode.PLUGIN_LOAD_FAILED


async def test_stub_tool_through_registry_returns_output() -> None:
    source = _FakeSource([_spec(_StubTool)])
    registry = PluginRegistry.discover([source])
    barrier = registry.get_tool("stub-tool")
    result = await barrier.call_async("call", {"anything": "value"})
    assert result == {"ok": True}


async def test_failed_call_disables_plugin() -> None:
    class _BoomTool(_StubTool):
        __manifest__ = dict(_StubTool.__manifest__)
        __manifest__["name"] = "boom-tool"

        async def call(self, input: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("boom")

    source = _FakeSource([_spec(_BoomTool)])
    registry = PluginRegistry.discover([source])
    barrier = registry.get_tool("boom-tool")

    with pytest.raises(CodeGuardianError) as excinfo:
        await barrier.call_async("call", {})
    assert excinfo.value.code == ErrorCode.PLUGIN_LOAD_FAILED
    assert barrier.disabled is True

    with pytest.raises(CodeGuardianError) as excinfo2:
        registry.get_tool("boom-tool")
    assert excinfo2.value.code == ErrorCode.PLUGIN_DISABLED


def test_list_plugins_returns_registered_records() -> None:
    registry = PluginRegistry.discover_bundled()
    records = registry.list_plugins()
    assert len(records) == 11
    assert all(isinstance(record, RegisteredPlugin) for record in records)
    for record in records:
        assert record.name
        assert record.version == "1.0.0"
        assert record.type in {"language", "tool", "reporter", "verifier"}
        assert record.manifest.name == record.name


def test_get_registered_returns_by_type() -> None:
    registry = PluginRegistry.discover_bundled()
    record = registry.get_registered("python", "language")
    assert record.name == "python"
    with pytest.raises(CodeGuardianError):
        registry.get_registered("python", "tool")


def test_grammar_load_failure_for_missing_path(caplog: pytest.LogCaptureFixture) -> None:
    class _KernelExtensibleStub(_StubTool):
        __manifest__ = dict(_StubTool.__manifest__)
        __manifest__["name"] = "ext-stub"
        __manifest__["language"] = {
            "name": "ext",
            "kernel_extensible": True,
            "grammar_path": "lib/does-not-exist.so",
        }

    source = _FakeSource([_spec(_KernelExtensibleStub)])
    with caplog.at_level(logging.WARNING, logger="codeguardian.core.plugins.registry"):
        registry = PluginRegistry.discover([source])
    assert not registry.list_plugins()
    assert any("grammar load failed" in rec.message for rec in caplog.records)
