"""Plugin discovery sources.

Two sources feed the registry: ``BundledSource`` walks the packaged
``codeguardian.plugins_bundled`` tree; ``EntryPointSource`` reads the
``codeguardian.<type>`` entry-point groups that third-party packages
register. Both produce identical ``PluginSpec`` objects, so the registry
treats them uniformly.

A source that fails to load one plugin logs a warning and continues with
the next. Silent skipping would hide a broken plugin; raising would hide
every plugin that comes after it.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "BundledSource",
    "EntryPointSource",
    "PluginSource",
    "PluginSpec",
    "discover_all",
]

_LOGGER = logging.getLogger(__name__)

_TYPE_DIRS: tuple[tuple[str, str], ...] = (
    ("languages", "language"),
    ("tools", "tool"),
    ("reporters", "reporter"),
)

_ENTRY_POINT_GROUPS: tuple[str, ...] = (
    "codeguardian.language",
    "codeguardian.tool",
    "codeguardian.reporter",
    "codeguardian.verifier",
)


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """One plugin candidate, before validation.

    Carries the manifest dictionary the plugin declared, the class that
    implements the plugin's protocol, and the package directory when the
    plugin was discovered on disk. ``package_path`` is ``None`` for
    entry-point plugins, whose files live inside a third-party
    distribution rather than the CodeGuardian package.
    """

    source: str
    manifest_raw: dict[str, Any]
    plugin_class: type
    package_path: Path | None = field(default=None)


class PluginSource(Protocol):
    """A source of plugin candidates."""

    def iter_specs(self) -> Iterator[PluginSpec]:
        """Yield one ``PluginSpec`` per discovered plugin."""
        ...


class BundledSource:
    """Discover plugins packaged under ``codeguardian.plugins_bundled``."""

    def __init__(
        self,
        package_name: str = "codeguardian.plugins_bundled",
        package_root: Path | None = None,
    ) -> None:
        self.package_name = package_name
        if package_root is None:
            try:
                package = importlib.import_module(package_name)
            except ImportError as exc:
                raise CodeGuardianError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    f"bundled plugin package {package_name!r} not importable",
                    details={"package": package_name},
                ) from exc
            paths = getattr(package, "__path__", None)
            if not paths:
                raise CodeGuardianError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    f"bundled plugin package {package_name!r} has no __path__",
                    details={"package": package_name},
                )
            package_root = Path(next(iter(paths)))
        self.package_root = package_root

    def iter_specs(self) -> Iterator[PluginSpec]:
        for type_dir_name, plugin_type in _TYPE_DIRS:
            type_dir = self.package_root / type_dir_name
            if not type_dir.is_dir():
                continue
            for plugin_dir in sorted(type_dir.iterdir()):
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
                    continue
                module_path = f"{self.package_name}.{type_dir_name}.{plugin_dir.name}.plugin"
                try:
                    module = importlib.import_module(module_path)
                except Exception as exc:
                    _LOGGER.warning(
                        "bundled plugin import failed: %s (%s: %s)",
                        module_path,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                cls = _find_plugin_class(module)
                if cls is None:
                    _LOGGER.warning(
                        "bundled plugin module has no class with __manifest__: %s",
                        module_path,
                    )
                    continue
                manifest_raw = getattr(cls, "__manifest__", None)
                if not isinstance(manifest_raw, dict):
                    _LOGGER.warning(
                        "bundled plugin __manifest__ is not a dict: %s",
                        module_path,
                    )
                    continue
                yield PluginSpec(
                    source=f"bundled:{plugin_type}",
                    manifest_raw=manifest_raw,
                    plugin_class=cls,
                    package_path=plugin_dir,
                )


class EntryPointSource:
    """Discover plugins via ``importlib.metadata`` entry points."""

    def __init__(self, groups: Sequence[str] | None = None) -> None:
        self.groups = tuple(groups) if groups is not None else _ENTRY_POINT_GROUPS

    def iter_specs(self) -> Iterator[PluginSpec]:
        for group in self.groups:
            try:
                eps = importlib.metadata.entry_points(group=group)
            except Exception as exc:
                _LOGGER.warning(
                    "entry point group %s not readable (%s: %s)",
                    group,
                    type(exc).__name__,
                    exc,
                )
                continue
            for entry_point in eps:
                try:
                    cls = entry_point.load()
                except Exception as exc:
                    _LOGGER.warning(
                        "entry point %s:%s load failed (%s: %s)",
                        group,
                        entry_point.name,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                manifest_raw = getattr(cls, "__manifest__", None)
                if not isinstance(manifest_raw, dict):
                    _LOGGER.warning(
                        "entry point %s:%s has no __manifest__ dict",
                        group,
                        entry_point.name,
                    )
                    continue
                yield PluginSpec(
                    source=f"entry_point:{group}",
                    manifest_raw=manifest_raw,
                    plugin_class=cls,
                    package_path=None,
                )


def discover_all(sources: Sequence[PluginSource]) -> list[PluginSpec]:
    """Collect every spec the given sources yield."""
    specs: list[PluginSpec] = []
    for source in sources:
        specs.extend(source.iter_specs())
    return specs


def _find_plugin_class(module: Any) -> type | None:
    """Return the class in ``module`` that carries ``__manifest__``."""
    candidates: list[type] = []
    for value in vars(module).values():
        if isinstance(value, type) and hasattr(value, "__manifest__"):
            candidates.append(value)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    preferred = [
        cls
        for cls in candidates
        if not cls.__name__.startswith("_") and cls.__module__ == module.__name__
    ]
    return preferred[0] if preferred else candidates[0]
