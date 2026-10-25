"""Runtime permission enforcement via sys.addaudithook.

A plugin declares its permissions in its manifest. This module makes the
declaration binding: after the audit hook is installed, any audited
operation the process performs while a plugin's context is active is
checked against that plugin's permissions. A violation raises
``PermissionError`` and the operation fails.

# Agent mode

A plugin's declared permissions are one side of the check. The other
side is the calling agent's mode. ``plan`` agents may call read-only
tools. ``auto-edit`` agents may call read and write tools. ``workspace``
and ``yolo`` agents may call tools of any action kind.

The ``check_action_allowed`` function performs the mode check. It is
called by the fault barrier before dispatching a tool call. A tool whose
``action_kind`` is not permitted by the current agent mode is refused
with ``PLUGIN_PERMISSION_DENIED``.

# Limitations

Three limitations are documented, not hidden.

1. Env reads are not auditable. CPython fires audit events for
   ``os.putenv`` and ``os.unsetenv``, not for ``os.environ[...]`` or
   ``os.getenv``. A plugin that declares ``env_read: []`` can still read
   environment variables. The manifest field is advisory for readers; it
   cannot be enforced from the Python layer.

2. C extensions bypass the hook. ``sys.addaudithook`` is a Python-level
   facility. A plugin that ships a compiled extension calling ``socket``
   or ``open`` directly reaches the OS without the hook observing the
   call.

3. Threads spawned by the plugin do not inherit the contextvars.
   ``_ACTIVE`` is thread-local. A plugin that starts its own thread and
   performs an audited operation there does so without an active
   context.

The hook is installed once per process. It cannot be uninstalled; CPython
does not expose a removal API. The fast path is a single ``ContextVar``
lookup per audited event, so the overhead when no plugin is active is
negligible.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "AgentMode",
    "FilesystemLevel",
    "PluginPermissions",
    "agent_mode_active",
    "check_action_allowed",
    "current_agent_mode",
    "install_audit_hook",
    "is_installed",
    "plugin_active",
    "sandbox_scope",
]

FilesystemLevel = Literal["none", "sandbox_only", "read_only", "full"]
AgentMode = Literal["plan", "auto-edit", "workspace", "yolo"]

_AGENT_MODE_ALLOWED: dict[AgentMode, frozenset[str]] = {
    "plan": frozenset({"read"}),
    "auto-edit": frozenset({"read", "write"}),
    "workspace": frozenset({"read", "write", "execute", "network"}),
    "yolo": frozenset({"read", "write", "execute", "network"}),
}


@dataclass(frozen=True, slots=True)
class PluginPermissions:
    """The permissions a plugin declared in its manifest."""

    filesystem: FilesystemLevel
    network: bool
    process_spawn: bool = False
    env_read: tuple[str, ...] = ()


_ACTIVE: ContextVar[PluginPermissions | None] = ContextVar("_cg_active_plugin_perms", default=None)
_ACTIVE_NAME: ContextVar[str | None] = ContextVar("_cg_active_plugin_name", default=None)
_AGENT_MODE: ContextVar[AgentMode | None] = ContextVar("_cg_agent_mode", default=None)
_SANDBOX_ROOT: ContextVar[Path | None] = ContextVar("_cg_sandbox_root", default=None)

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_NETWORK_EVENTS = frozenset(
    {
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.gethostbyname",
    }
)

_SPAWN_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
    }
)

_DESTRUCTIVE_FS_EVENTS = frozenset(
    {
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.rename",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
    }
)

_WRITE_FLAGS = 0
for _flag_name in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"):
    _WRITE_FLAGS |= getattr(os, _flag_name, 0)


def _deny(permission: str, action: str) -> None:
    plugin = _ACTIVE_NAME.get() or "<unknown plugin>"
    raise PermissionError(
        f"plugin {plugin!r} attempted {action!r} but did not declare {permission!r} permission"
    )


def _check_open(args: tuple) -> None:
    perms = _ACTIVE.get()
    if perms is None:
        return

    path = args[0] if args else None
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None

    level = perms.filesystem

    if isinstance(path, int):
        if level in ("none", "read_only"):
            _deny("filesystem", "open (fd)")
        return

    if path is None:
        if level in ("none", "read_only"):
            _deny("filesystem", "open (unknown path)")
        return

    writing = False
    if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
        mode is None and isinstance(flags, int) and (flags & _WRITE_FLAGS)
    ):
        writing = True

    if level == "none":
        _deny("filesystem", "open")
        return
    if level == "full":
        return
    if level == "read_only":
        if writing:
            _deny("filesystem", "open (write)")
        return
    if level == "sandbox_only":
        root = _SANDBOX_ROOT.get()
        if root is None:
            _deny("filesystem", "open (no sandbox scope)")
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError):
            _deny("filesystem", f"open (unresolvable: {path!r})")
            return
        try:
            resolved.relative_to(Path(root).resolve())
        except ValueError:
            _deny("filesystem", f"open outside sandbox: {path}")


def _check_destructive(event: str, args: tuple) -> None:
    perms = _ACTIVE.get()
    if perms is None:
        return

    level = perms.filesystem
    if level == "full":
        return
    if level == "none":
        _deny("filesystem", event)
        return
    if level == "read_only":
        _deny("filesystem", f"{event} (read_only)")
        return
    if level == "sandbox_only":
        root = _SANDBOX_ROOT.get()
        if root is None:
            _deny("filesystem", f"{event} (no sandbox scope)")
            return
        root_resolved = Path(root).resolve()
        for arg in args:
            if not isinstance(arg, (str, bytes, Path)):
                continue
            try:
                path = Path(arg).resolve()
            except (OSError, ValueError):
                continue
            try:
                path.relative_to(root_resolved)
            except ValueError:
                _deny("filesystem", f"{event} outside sandbox: {arg}")


def _audit_hook(event: str, args: tuple) -> None:
    if _ACTIVE.get() is None:
        return
    if event in _NETWORK_EVENTS:
        perms = _ACTIVE.get()
        if perms is not None and not perms.network:
            _deny("network", event)
        return
    if event in _SPAWN_EVENTS:
        perms = _ACTIVE.get()
        if perms is not None and not perms.process_spawn:
            _deny("process_spawn", event)
        return
    if event == "open":
        _check_open(args)
        return
    if event in _DESTRUCTIVE_FS_EVENTS:
        _check_destructive(event, args)
        return


def install_audit_hook() -> None:
    """Install the process-wide audit hook exactly once."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        sys.addaudithook(_audit_hook)
        _INSTALLED = True


def is_installed() -> bool:
    """Return whether the audit hook is installed."""
    return _INSTALLED


def current_agent_mode() -> AgentMode | None:
    """Return the current agent mode, or ``None`` when no agent is active."""
    return _AGENT_MODE.get()


def check_action_allowed(
    plugin_name: str,
    mode: AgentMode,
    action_kind: str,
) -> None:
    """Verify that a plugin action is permitted by the agent mode.

    Raises:
        CodeGuardianError: ``PLUGIN_PERMISSION_DENIED`` when the action
            kind is not in the allowed set for the mode.
    """
    allowed = _AGENT_MODE_ALLOWED.get(mode)
    if allowed is None:
        raise CodeGuardianError(
            ErrorCode.PLUGIN_PERMISSION_DENIED,
            f"unknown agent mode: {mode!r}",
            details={"plugin": plugin_name, "mode": mode},
        )
    if action_kind not in allowed:
        raise CodeGuardianError(
            ErrorCode.PLUGIN_PERMISSION_DENIED,
            f"action {action_kind!r} is not allowed in agent mode {mode!r}",
            details={
                "plugin": plugin_name,
                "mode": mode,
                "action_kind": action_kind,
                "allowed": sorted(allowed),
            },
        )


@contextmanager
def agent_mode_active(mode: AgentMode) -> Iterator[None]:
    """Set the current agent mode for the duration of the block."""
    token = _AGENT_MODE.set(mode)
    try:
        yield
    finally:
        _AGENT_MODE.reset(token)


@contextmanager
def plugin_active(
    plugin_name: str,
    permissions: PluginPermissions,
) -> Iterator[None]:
    """Activate a plugin's permission context for the current thread."""
    name_token = _ACTIVE_NAME.set(plugin_name)
    perms_token = _ACTIVE.set(permissions)
    try:
        yield
    finally:
        _ACTIVE.reset(perms_token)
        _ACTIVE_NAME.reset(name_token)


@contextmanager
def sandbox_scope(root: Path) -> Iterator[None]:
    """Set the sandbox root for the current thread."""
    token = _SANDBOX_ROOT.set(Path(root))
    try:
        yield
    finally:
        _SANDBOX_ROOT.reset(token)
