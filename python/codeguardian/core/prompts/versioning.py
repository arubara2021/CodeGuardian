"""Version-aware prompt loader.

Prompts live in two trees:

- ``prompts/_base/v<version>.md`` holds the shared rules every role
  inherits.
- ``prompts/<role>/v<version>.md`` holds the role-specific prompt.

A ``(role, version)`` pair is self-contained: it loads both files. A
role prompt at ``v1.0.0`` pairs with ``_base`` at ``v1.0.0`` and no
other. Bumping a role prompt's version requires the same bump on the
base file. That is deliberate: the base rules and the role contract
change together, so they version together.

# Frontmatter

A prompt file may carry YAML frontmatter delimited by ``---`` lines.
The loader parses it and returns it separately from the body. The body
is what gets rendered. Frontmatter carries metadata the pipeline reads
without rendering: ``role``, ``version``, ``mode``, and any
role-specific fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "PromptBundle",
    "load_prompt",
    "set_prompts_root",
]

_DEFAULT_PROMPTS_ROOT: Path = Path(__file__).resolve().parents[4] / "prompts"
_PROMPTS_ROOT_OVERRIDE: Path | None = None

_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_VALID_ROLES = frozenset(
    {
        "analyst",
        "tester",
        "writer",
        "skill_curator",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
    }
)
_FRONTMATTER_OPEN = "---\n"
_FRONTMATTER_CLOSE = "\n---\n"


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """A loaded prompt pair, before rendering."""

    role: str
    version: str
    base_text: str
    role_text: str
    frontmatter: dict[str, Any] = field(default_factory=dict)


def set_prompts_root(path: Path | None) -> None:
    """Override the directory prompt files are loaded from.

    Passing ``None`` restores the default. Intended for tests and for
    installs where ``prompts/`` is not adjacent to the Python package.
    """
    global _PROMPTS_ROOT_OVERRIDE
    _PROMPTS_ROOT_OVERRIDE = Path(path) if path is not None else None


def load_prompt(
    role: str,
    version: str,
    prompts_root: Path | None = None,
) -> PromptBundle:
    """Load the base and role prompt files for ``(role, version)``.

    Args:
        role: One of the seven role names.
        version: A MAJOR.MINOR.PATCH version string.
        prompts_root: Optional override for the prompts directory.
            Falls back to the module-level override, then to the
            default location relative to this package.

    Returns:
        A ``PromptBundle`` whose ``base_text`` and ``role_text`` are the
        file bodies with frontmatter stripped. ``frontmatter`` carries
        the parsed metadata from the role file.

    Raises:
        CodeGuardianError: ``SYS_INTERNAL`` if the role is unknown, the
            version is malformed, or either file is missing or
            unreadable.
    """
    _validate_role(role)
    _validate_version(version)

    root = _resolve_root(prompts_root)
    base_path = root / "_base" / f"v{version}.md"
    role_path = root / role / f"v{version}.md"

    base_text = _read_stripped(base_path, kind="base")
    role_body = _read_stripped(role_path, kind="role")

    frontmatter, role_body_without_fm = _split_frontmatter(role_body, role_path)

    return PromptBundle(
        role=role,
        version=version,
        base_text=base_text,
        role_text=role_body_without_fm,
        frontmatter=frontmatter,
    )


def _resolve_root(prompts_root: Path | None) -> Path:
    if prompts_root is not None:
        return Path(prompts_root)
    if _PROMPTS_ROOT_OVERRIDE is not None:
        return _PROMPTS_ROOT_OVERRIDE
    return _DEFAULT_PROMPTS_ROOT


def _validate_role(role: str) -> None:
    if role not in _VALID_ROLES:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"unknown prompt role: {role!r}",
            details={"role": role, "valid": sorted(_VALID_ROLES)},
        )


def _validate_version(version: str) -> None:
    if not _VERSION_PATTERN.match(version):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"prompt version must be MAJOR.MINOR.PATCH; got {version!r}",
            details={"version": version},
        )


def _read_stripped(path: Path, *, kind: str) -> str:
    if not path.is_file():
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"{kind} prompt file not found: {path}",
            details={"path": str(path), "kind": kind},
        )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"cannot read {kind} prompt file {path}: {exc}",
            details={"path": str(path), "kind": kind},
        ) from exc


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith(_FRONTMATTER_OPEN):
        return {}, text

    end = text.find(_FRONTMATTER_CLOSE, len(_FRONTMATTER_OPEN))
    if end == -1:
        if text.rstrip().endswith("---") and text.count("---") >= 2:
            return _parse_frontmatter(text[len(_FRONTMATTER_OPEN) : -3], path), ""
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"prompt file has unclosed frontmatter: {path}",
            details={"path": str(path)},
        )

    yaml_text = text[len(_FRONTMATTER_OPEN) : end]
    body = text[end + len(_FRONTMATTER_CLOSE) :]
    return _parse_frontmatter(yaml_text, path), body


def _parse_frontmatter(yaml_text: str, path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"prompt frontmatter is not valid YAML: {path}: {exc}",
            details={"path": str(path)},
        ) from exc
    if not isinstance(data, dict):
        return {}
    return data
