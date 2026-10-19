"""Central plugin registry.

``PluginRegistry.discover(sources)`` is the entry point. It walks the
given sources, validates each manifest, negotiates interface version,
loads a runtime grammar when the plugin declares one, instantiates the
plugin class, verifies it satisfies the corresponding Protocol, and
wraps it in a ``FaultBarrier``. Any plugin that fails a step is logged
and skipped; discovery never aborts on a single bad plugin.

The typed getters (``get_language``, ``get_tool``, ``get_reporter``,
``get_verifier``) return the ``FaultBarrier``. Callers use
``barrier.call("method", *args)`` for every method invocation, so
timeouts and exception isolation are enforced uniformly. Non-callable
metadata such as ``barrier.plugin.name`` is reachable directly through
``barrier.plugin``.

SQLite persistence is optional. When a ``SQLiteStore`` is provided, each
successfully loaded plugin is upserted into ``plugin_registry``. Failures
to write are logged and do not abort discovery, because the store may
not have migrations applied yet.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage.sqlite_store import SQLiteStore
from codeguardian.interfaces import (
    LanguagePlugin,
    ReporterPlugin,
    ToolPlugin,
    VerifierPlugin,
)

from .fault_barrier import DEFAULT_METHOD_TIMEOUTS, FaultBarrier
from .loader import (
    BundledSource,
    EntryPointSource,
    PluginSource,
    PluginSpec,
    discover_all,
)
from .manifest import PluginManifest, set_schema_path, validate
from .versioning import check_interface_version

__all__ = ["PluginRegistry", "RegisteredPlugin"]

_LOGGER = logging.getLogger(__name__)

_PROTOCOLS: dict[str, type] = {
    "language": LanguagePlugin,
    "tool": ToolPlugin,
    "reporter": ReporterPlugin,
    "verifier": VerifierPlugin,
}


@dataclass(frozen=True, slots=True)
class RegisteredPlugin:
    """A plugin that passed validation and is available for invocation."""

    name: str
    version: str
    type: str
    manifest: PluginManifest
    plugin: Any
    barrier: FaultBarrier
    source: str


class PluginRegistry:
    """The set of plugins discovered in one process."""

    def __init__(self) -> None:
        self._by_type: dict[str, dict[str, RegisteredPlugin]] = {
            "language": {},
            "tool": {},
            "reporter": {},
            "verifier": {},
        }

    # -- construction --------------------------------------------------

    @classmethod
    def discover(
        cls,
        sources: list[PluginSource] | tuple[PluginSource, ...],
        *,
        sqlite_store: SQLiteStore | None = None,
        schemas_dir: Path | None = None,
    ) -> PluginRegistry:
        """Discover and register every plugin the sources yield.

        Args:
            sources: Discovery sources, in order. Duplicates by
                ``(type, name)`` keep the first registration; later
                sources are ignored with a warning.
            sqlite_store: Optional persistence target. When present,
                each successfully loaded plugin is upserted into
                ``plugin_registry``.
            schemas_dir: Optional directory containing
                ``plugin-manifest.json``. When present, overrides the
                manifest module's default schema lookup.

        Returns:
            A populated ``PluginRegistry``. Plugins that failed
            validation, version check, grammar load, or protocol
            conformance are absent; the failure was logged.
        """
        if schemas_dir is not None:
            set_schema_path(Path(schemas_dir) / "plugin-manifest.json")
        registry = cls()
        for spec in discover_all(sources):
            registry._load_spec(spec, sqlite_store)
        return registry

    @classmethod
    def discover_bundled(
        cls,
        *,
        sqlite_store: SQLiteStore | None = None,
        schemas_dir: Path | None = None,
    ) -> PluginRegistry:
        """Discover only the bundled plugins."""
        return cls.discover(
            [BundledSource()],
            sqlite_store=sqlite_store,
            schemas_dir=schemas_dir,
        )

    @classmethod
    def discover_all(
        cls,
        *,
        sqlite_store: SQLiteStore | None = None,
        schemas_dir: Path | None = None,
    ) -> PluginRegistry:
        """Discover bundled and entry-point plugins together."""
        return cls.discover(
            [BundledSource(), EntryPointSource()],
            sqlite_store=sqlite_store,
            schemas_dir=schemas_dir,
        )

    # -- lookup --------------------------------------------------------

    def get_language(self, name: str) -> FaultBarrier:
        """Return the barrier for a language plugin, or raise."""
        return self._get("language", name)

    def get_tool(self, name: str) -> FaultBarrier:
        """Return the barrier for a tool plugin, or raise."""
        return self._get("tool", name)

    def get_reporter(self, name: str) -> FaultBarrier:
        """Return the barrier for a reporter plugin, or raise."""
        return self._get("reporter", name)

    def get_verifier(self, name: str) -> FaultBarrier:
        """Return the barrier for a verifier plugin, or raise."""
        return self._get("verifier", name)

    def list_plugins(self) -> list[RegisteredPlugin]:
        """Return every registered plugin, grouped by type then name."""
        result: list[RegisteredPlugin] = []
        for type_name in ("language", "tool", "reporter", "verifier"):
            result.extend(self._by_type[type_name].values())
        return result

    def get_registered(self, name: str, type_name: str) -> RegisteredPlugin:
        """Return the ``RegisteredPlugin`` record for a name and type."""
        plugin = self._by_type.get(type_name, {}).get(name)
        if plugin is None:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"no {type_name} plugin named {name!r}",
                details={"plugin": name, "type": type_name},
            )
        return plugin

    # -- internals -----------------------------------------------------

    def _get(self, type_name: str, name: str) -> FaultBarrier:
        record = self._by_type[type_name].get(name)
        if record is None:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"no {type_name} plugin named {name!r}",
                details={"plugin": name, "type": type_name},
            )
        if record.barrier.disabled:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_DISABLED,
                f"plugin {name!r} is disabled",
                details={"plugin": name, "type": type_name},
            )
        return record.barrier

    def _load_spec(
        self,
        spec: PluginSpec,
        sqlite_store: SQLiteStore | None,
    ) -> None:
        try:
            manifest = validate(spec.manifest_raw)
        except CodeGuardianError as exc:
            _LOGGER.warning("plugin manifest rejected (source=%s): %s", spec.source, exc)
            return

        try:
            compatibility = check_interface_version(manifest.interface_version)
        except CodeGuardianError as exc:
            _LOGGER.warning("plugin %s interface version rejected: %s", manifest.name, exc)
            return
        if compatibility != "current":
            _LOGGER.warning(
                "plugin %s targets interface %s (%s)",
                manifest.name,
                manifest.interface_version,
                compatibility,
            )

        if manifest.kernel_extensible and not self._load_grammar(spec, manifest):
            return

        try:
            instance = spec.plugin_class()
        except Exception as exc:
            _LOGGER.warning(
                "plugin %s instantiation failed (%s: %s)",
                manifest.name,
                type(exc).__name__,
                exc,
            )
            return

        protocol = _PROTOCOLS.get(manifest.type)
        if protocol is None:
            _LOGGER.warning(
                "plugin %s has unsupported type %r",
                manifest.name,
                manifest.type,
            )
            return
        if not isinstance(instance, protocol):
            _LOGGER.warning(
                "plugin %s does not satisfy %s protocol",
                manifest.name,
                protocol.__name__,
            )
            return

        existing = self._by_type[manifest.type].get(manifest.name)
        if existing is not None:
            _LOGGER.warning(
                "plugin %s (%s) already registered from %s; ignoring %s",
                manifest.name,
                manifest.type,
                existing.source,
                spec.source,
            )
            return

        barrier = FaultBarrier(
            plugin=instance,
            plugin_name=manifest.name,
            permissions=manifest.permissions,
            method_timeouts=DEFAULT_METHOD_TIMEOUTS.get(manifest.type, {}),
        )
        record = RegisteredPlugin(
            name=manifest.name,
            version=manifest.version,
            type=manifest.type,
            manifest=manifest,
            plugin=instance,
            barrier=barrier,
            source=spec.source,
        )
        self._by_type[manifest.type][manifest.name] = record
        _LOGGER.info("registered %s plugin %s from %s", manifest.type, manifest.name, spec.source)

        if sqlite_store is not None:
            self._write_sqlite(sqlite_store, record)

    def _load_grammar(self, spec: PluginSpec, manifest: PluginManifest) -> bool:
        if not manifest.grammar_path:
            _LOGGER.warning(
                "plugin %s is kernel_extensible but has no grammar_path",
                manifest.name,
            )
            return False
        if spec.package_path is None:
            grammar_file = Path(manifest.grammar_path)
        else:
            grammar_file = spec.package_path / manifest.grammar_path
        try:
            from codeguardian import _native
        except ImportError as exc:
            _LOGGER.warning(
                "plugin %s needs runtime grammar but _native is unavailable (%s)",
                manifest.name,
                exc,
            )
            return False
        try:
            _native.load_grammar(manifest.name, str(grammar_file))
        except Exception as exc:
            _LOGGER.warning(
                "plugin %s grammar load failed (%s: %s)",
                manifest.name,
                type(exc).__name__,
                exc,
            )
            return False
        return True

    def _write_sqlite(self, store: SQLiteStore, record: RegisteredPlugin) -> None:
        manifest_json = json.dumps(record.manifest.raw, sort_keys=True)
        loaded_at = datetime.now(UTC).isoformat()
        try:
            store.execute(
                """
                INSERT INTO plugin_registry
                    (plugin_name, plugin_version, plugin_type,
                     manifest_json, loaded_at, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(plugin_name, plugin_version) DO UPDATE SET
                    manifest_json = excluded.manifest_json,
                    loaded_at = excluded.loaded_at,
                    enabled = 1
                """,
                (
                    record.name,
                    record.version,
                    record.type,
                    manifest_json,
                    loaded_at,
                ),
            )
        except sqlite3.Error as exc:
            _LOGGER.warning("plugin %s sqlite registration failed: %s", record.name, exc)
