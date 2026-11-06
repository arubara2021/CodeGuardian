"""Admission scanning and invocation guard for skills.

A skill is untrusted supply-chain input. Even a skill written by the
Skill Curator is a model output, not human-reviewed code. This module
implements the two checks that make a skill safe to load and safe to
invoke.

The scanner examines the full text of a ``SKILL.md`` file for known
attack patterns. It is a fixed list of regular expressions and Unicode
codepoint checks; it uses no model and no network. It catches the
categories of attack that dominate the published corpora of malicious
skills, and it does not catch novel patterns. Its output is an
``AdmissionVerdict`` that names every finding by category so a caller
can log it and a reviewer can act on it.

The invocation guard intersects a skill's declared permissions with
the permissions of any plugin the skill calls. The intersection is the
effective permission set during the call. Enforcement happens through
an audit hook installed once per process; the hook reads a context
variable and refuses any event that exceeds the effective permissions.

This module depends on Phase 1's error taxonomy but adds no new codes.
A blocked admission raises ``PLUGIN_PERMISSION_DENIED`` because a
permission violation is what it is. Non-blocking findings are returned
in the verdict and logged by the caller; the scanner never raises for
a warning.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .types import AdmissionVerdict

__all__ = [
    "FILESYSTEM_LEVELS",
    "install_audit_hook",
    "intersect_permissions",
    "scan",
    "skill_scope",
]

logger = logging.getLogger(__name__)


FILESYSTEM_LEVELS: dict[str, int] = {
    "none": 0,
    "sandbox_only": 1,
    "read_only": 2,
    "full": 3,
}


# Prompt-injection patterns. A hit in the body of a skill blocks
# admission because a skill whose instructions include one of these
# phrases is attempting to override the system prompt.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|prompts?)", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions\s*:", re.IGNORECASE),
    re.compile(
        r"\bdisregard\s+(?:all\s+)?(?:the\s+)?"
        r"(?:previous|prior|above|earlier)\b",
        re.IGNORECASE,
    ),
)

# Command-substitution markers. A hit blocks admission because the
# marker only appears when the skill intends to run a command on the
# reader's machine, which is never a legitimate instruction in a
# skill body.
_SHELL_SUBSTITUTION_PATTERNS: tuple[re.Pattern[str], ...] = (re.compile(r"\$\([^)]+\)"),)

# Exfiltration verbs. A hit blocks admission because a skill that tells
# the reader to send, post, upload, or fetch has an outbound channel.
_EXFILTRATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:send|post|upload|transmit|forward)\b"
        r"(?:\s+\w+){0,5}"
        r"\s+to\s+(?:https?://|ftp://|file://|[\w.]+@)",
        re.IGNORECASE,
    ),
    re.compile(r"\bexfiltrate\b", re.IGNORECASE),
    re.compile(r"\bcurl\s", re.IGNORECASE),
    re.compile(r"\bwget\s", re.IGNORECASE),
)

# Warning patterns. A hit does not block admission. A caller running
# with strict admission enabled promotes warnings to blocks; a caller
# without strict admission logs the warnings and proceeds.
_WARNING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://\S+", re.IGNORECASE), "external_url"),
    (re.compile(r"file://\S+", re.IGNORECASE), "file_url"),
    (re.compile(r"\bif\s+the\s+caller\b", re.IGNORECASE), "conditional_caller"),
    (re.compile(r"\bwhen\s+the\s+user\b", re.IGNORECASE), "conditional_user"),
    (re.compile(r"\bwhen\s+the\s+agent\b", re.IGNORECASE), "conditional_agent"),
    (re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"), "long_base64"),
    (re.compile(r"[^\n]{4000,}"), "long_line"),
)

# Invisible Unicode codepoints. A single occurrence blocks admission
# because none of these has a legitimate use in a skill body. RTL
# overrides, zero-width joiners, and byte-order marks are all common
# obfuscation channels for injection text.
_INVISIBLE_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x200D,  # zero-width joiner
        0x202A,  # left-to-right embedding
        0x202B,  # right-to-left embedding
        0x202C,  # pop directional formatting
        0x202D,  # left-to-right override
        0x202E,  # right-to-left override
        0x2060,  # word joiner
        0xFEFF,  # zero-width no-break space (BOM)
    }
)


def scan(markdown: str) -> AdmissionVerdict:
    """Scan a ``SKILL.md`` body for known attack patterns.

    Args:
        markdown: The full text of the file, including frontmatter. The
            scanner examines the entire text because injection markers
            can appear in either section.

    Returns:
        An ``AdmissionVerdict`` whose ``accepted`` field is ``False``
        when at least one blocking finding was raised and ``True``
        otherwise. Warning findings are recorded on the verdict whether
        or not the skill was accepted. ``reason`` names the first
        blocking finding when blocked.
    """
    blocking: list[str] = []
    warning: list[str] = []

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(markdown):
            blocking.append(f"prompt_injection:{pattern.pattern}")
            break

    for pattern in _SHELL_SUBSTITUTION_PATTERNS:
        if pattern.search(markdown):
            blocking.append("shell_substitution")
            break

    for pattern in _EXFILTRATION_PATTERNS:
        if pattern.search(markdown):
            blocking.append(f"exfiltration:{pattern.pattern}")
            break

    for char in markdown:
        if ord(char) in _INVISIBLE_CODEPOINTS:
            blocking.append(f"invisible_unicode:U+{ord(char):04X}")
            break
        category = unicodedata.category(char)
        if category == "Cf":
            blocking.append(f"format_char:U+{ord(char):04X}")
            break

    for pattern, label in _WARNING_PATTERNS:
        if pattern.search(markdown):
            warning.append(label)

    if blocking:
        reason = blocking[0]
        scan_result = "block"
    elif warning:
        reason = f"{len(warning)} warning(s)"
        scan_result = "warn"
    else:
        reason = "clean"
        scan_result = "clean"

    return AdmissionVerdict(
        accepted=not blocking,
        reason=reason,
        blocking_findings=tuple(blocking),
        warning_findings=tuple(warning),
        scan_result=scan_result,
    )


def intersect_permissions(
    skill_permissions: Mapping[str, Any],
    plugin_permissions: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the effective permissions during a skill-to-plugin call.

    The effective permissions are the narrower of the two: a call is
    permitted only when both the skill and the plugin allow it. The
    filesystem level follows the ordering in ``FILESYSTEM_LEVELS``.
    Network and process-spawn booleans are combined with logical AND.
    Environment-read lists are intersected.

    Args:
        skill_permissions: The skill's declared permissions, in the
            shape used by ``skill.json``.
        plugin_permissions: The plugin's declared permissions, in the
            shape used by ``plugin-manifest.json``.

    Returns:
        A new permissions dict. Callers may add keys not present in
        either input; only the four shared keys are narrowed.
    """
    result: dict[str, Any] = {}

    skill_fs = str(skill_permissions.get("filesystem", "none"))
    plugin_fs = str(plugin_permissions.get("filesystem", "none"))
    skill_level = FILESYSTEM_LEVELS.get(skill_fs, 0)
    plugin_level = FILESYSTEM_LEVELS.get(plugin_fs, 0)
    effective_level = min(skill_level, plugin_level)
    reverse = {v: k for k, v in FILESYSTEM_LEVELS.items()}
    result["filesystem"] = reverse[effective_level]

    result["network"] = bool(skill_permissions.get("network", False)) and bool(
        plugin_permissions.get("network", False)
    )
    result["process_spawn"] = bool(skill_permissions.get("process_spawn", False)) and bool(
        plugin_permissions.get("process_spawn", False)
    )

    skill_env = set(skill_permissions.get("env_read", []) or [])
    plugin_env = set(plugin_permissions.get("env_read", []) or [])
    result["env_read"] = sorted(skill_env & plugin_env)

    return result


_ACTIVE_PERMISSIONS: ContextVar[dict[str, Any] | None] = ContextVar(
    "skill_permissions", default=None
)
_HOOK_INSTALLED: bool = False
_HOOK_LOCK = threading.Lock()

_NETWORK_EVENTS: frozenset[str] = frozenset(
    {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname"}
)
_PROCESS_EVENTS: frozenset[str] = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.fork",
        "os.exec",
        "os.execv",
        "os.execve",
        "os.spawn",
        "os.spawnl",
        "os.spawnv",
    }
)
_WRITE_EVENTS: frozenset[str] = frozenset(
    {"os.remove", "os.unlink", "os.rename", "os.rmdir", "os.mkdir", "shutil.rmtree"}
)
_OPEN_EVENTS: frozenset[str] = frozenset({"open", "os.open"})


def _extract_mode(args: tuple[Any, ...]) -> str:
    """Return the file mode from an audit event's argument tuple.

    ``open`` passes the mode as a string in position 1. ``os.open``
    passes an integer flag mask in position 1; the low two bits encode
    read versus write access. Anything else is treated as read-only to
    keep the default safe.
    """
    if len(args) < 2:
        return "r"
    mode = args[1]
    if isinstance(mode, str):
        return mode
    if isinstance(mode, int):
        if mode & 0b11:
            return "w"
        return "r"
    return "r"


def _is_read_mode(mode: str) -> bool:
    """Return True when the mode allows only reads."""
    return "r" in mode and "w" not in mode and "a" not in mode and "+" not in mode


def _event_allowed(event: str, args: tuple[Any, ...], perms: Mapping[str, Any]) -> bool:
    """Return whether the event is permitted under the given permissions."""
    if event in _NETWORK_EVENTS:
        return bool(perms.get("network", False))
    if event in _PROCESS_EVENTS:
        return bool(perms.get("process_spawn", False))
    if event in _WRITE_EVENTS:
        return str(perms.get("filesystem", "none")) == "full"
    if event in _OPEN_EVENTS:
        filesystem = str(perms.get("filesystem", "none"))
        if filesystem == "none":
            return False
        if filesystem == "full":
            return True
        if filesystem in ("read_only", "sandbox_only"):
            return _is_read_mode(_extract_mode(args))
        return False
    return True


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    """Process-wide audit hook enforcing the active skill permissions."""
    perms = _ACTIVE_PERMISSIONS.get()
    if perms is None:
        return
    if not _event_allowed(event, args, perms):
        raise PermissionError(f"skill permission denied for event {event!r}")


def install_audit_hook() -> None:
    """Install the skill-permission audit hook once per process.

    The hook is idempotent: subsequent calls are no-ops. Installation
    happens the first time a skill scope is entered, not at module
    import, so a process that never uses skills pays no cost.
    """
    global _HOOK_INSTALLED
    with _HOOK_LOCK:
        if _HOOK_INSTALLED:
            return
        sys.addaudithook(_audit_hook)
        _HOOK_INSTALLED = True


@contextmanager
def skill_scope(permissions: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Scope a block of code to the given effective permissions.

    Entering the context installs the audit hook if it is not already
    installed and sets the context variable to a copy of the given
    permissions. Leaving the context resets the variable, so calls made
    outside the scope are unaffected.

    Args:
        permissions: The effective permissions for the call, normally
            the result of ``intersect_permissions``.

    Yields:
        The permissions dict that is active during the block.
    """
    install_audit_hook()
    effective = dict(permissions)
    token = _ACTIVE_PERMISSIONS.set(effective)
    try:
        yield effective
    finally:
        _ACTIVE_PERMISSIONS.reset(token)
