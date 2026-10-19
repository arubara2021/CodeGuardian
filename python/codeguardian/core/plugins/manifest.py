"""Plugin manifest parsing, validation, and default application.

A plugin's manifest is the JSON object that declares its name, version,
type, interface version, and permissions. This module validates that
object against ``schemas/plugin-manifest.json`` (v1.2), applies the
schema's defaults for optional fields, and returns a typed
``PluginManifest``.

Validation is not best-effort. A manifest with a missing required field,
an unknown key, a value outside an enum, or a ``kernel_extensible: true``
without a ``grammar_path`` raises ``PLUGIN_MANIFEST_INVALID`` with
``details['path']`` naming the failing field.

The schema path resolves relative to the repository (``parents[4]`` of
this file). For wheel installs where the schema is not next to the
Python package, call ``set_schema_path`` with the correct location. The
registry does this at construction if ``schemas_dir`` is provided.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .permissions import PluginPermissions

__all__ = ["PluginManifest", "set_schema_path", "validate"]


_SCHEMA_PATH = Path(__file__).resolve().parents[4] / "schemas" / "plugin-manifest.json"
_SCHEMA_PATH_OVERRIDE: Path | None = None


def set_schema_path(path: Path | None) -> None:
    """Override the schema file used by ``validate``.

    Passing ``None`` restores the default path. Clears the compiled
    validator cache so the next ``validate`` call reads the new file.
    Intended for tests and non-repo installs. Production code should not
    call this except at registry construction when ``schemas_dir`` is
    supplied.
    """
    global _SCHEMA_PATH_OVERRIDE
    _SCHEMA_PATH_OVERRIDE = Path(path) if path is not None else None
    _get_validator.cache_clear()


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A validated plugin manifest.

    The typed fields are the ones callers act on: ``type`` chooses the
    protocol to validate against, ``interface_version`` drives
    compatibility negotiation, ``permissions`` drives the audit hook,
    and ``kernel_extensible`` / ``grammar_path`` trigger runtime grammar
    loading. Every other manifest field is preserved verbatim in
    ``raw``.
    """

    name: str
    version: str
    type: str
    interface_version: str
    description: str | None
    capabilities: tuple[str, ...]
    permissions: PluginPermissions
    kernel_extensible: bool
    grammar_path: str | None
    raw: Mapping[str, Any]


@lru_cache(maxsize=1)
def _get_validator() -> Draft202012Validator:
    path = _SCHEMA_PATH_OVERRIDE or _SCHEMA_PATH
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"plugin manifest schema not found at {path}",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    schema = json.loads(content)
    return Draft202012Validator(schema)


def validate(raw: Mapping[str, Any]) -> PluginManifest:
    """Validate a raw plugin manifest and return the typed form.

    Args:
        raw: The parsed JSON object from the plugin's manifest.

    Returns:
        A ``PluginManifest`` with the schema's defaults applied.

    Raises:
        CodeGuardianError: ``PLUGIN_MANIFEST_INVALID`` if the manifest
            does not conform to the schema. ``details['path']`` names the
            failing field, ``details['message']`` carries the schema
            message, and ``details['errors']`` counts every violation
            found.
    """
    if not isinstance(raw, Mapping):
        raise CodeGuardianError(
            ErrorCode.PLUGIN_MANIFEST_INVALID,
            "plugin manifest must be a mapping",
            details={"path": "<root>", "type": type(raw).__name__},
        )

    errors = sorted(
        _get_validator().iter_errors(raw),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise CodeGuardianError(
            ErrorCode.PLUGIN_MANIFEST_INVALID,
            f"plugin manifest invalid at {path}: {first.message}",
            details={
                "path": path,
                "message": first.message,
                "errors": len(errors),
            },
        )

    return _build(raw)


def _build(raw: Mapping[str, Any]) -> PluginManifest:
    perms_raw = raw["permissions"]
    permissions = PluginPermissions(
        filesystem=perms_raw["filesystem"],
        network=bool(perms_raw["network"]),
        process_spawn=bool(perms_raw.get("process_spawn", False)),
        env_read=tuple(perms_raw.get("env_read", ())),
    )

    language_raw = raw.get("language", {})
    kernel_extensible = bool(language_raw.get("kernel_extensible", False))
    grammar_path = language_raw.get("grammar_path")

    return PluginManifest(
        name=raw["name"],
        version=raw["version"],
        type=raw["type"],
        interface_version=raw["interface_version"],
        description=raw.get("description"),
        capabilities=tuple(raw.get("capabilities", ())),
        permissions=permissions,
        kernel_extensible=kernel_extensible,
        grammar_path=grammar_path,
        raw=dict(raw),
    )
