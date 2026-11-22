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

# Model inference scope

LLM-backed agents run with ``network=False`` by default so untrusted
repository content cannot trigger outbound network exfiltration. Model
provider adapters enter ``model_inference_scope`` strictly around the
HTTP inference call, which temporarily binds ``filesystem="read_only"``
(needed to load TLS trust stores while blocking writes and deletions),
``network=True``, and ``process_spawn=False`` while restricting all DNS
lookups and socket connections to the configured endpoint hostnames.

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

import ipaddress
import os
import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins.path_guard import is_path_permitted

__all__ = [
    "AgentMode",
    "FilesystemLevel",
    "PluginPermissions",
    "agent_mode_active",
    "check_action_allowed",
    "current_agent_mode",
    "extract_hosts_from_url",
    "install_audit_hook",
    "is_installed",
    "model_inference_scope",
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

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class PluginPermissions:
    """The permissions a plugin declared in its manifest."""

    filesystem: FilesystemLevel
    network: bool
    process_spawn: bool = False
    env_read: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _NetworkScope:
    """Host-allowlisted egress policy for a single model inference call."""

    allowed_hosts: frozenset[str]
    allow_loopback: bool


_ACTIVE: ContextVar[PluginPermissions | None] = ContextVar("_cg_active_plugin_perms", default=None)
_ACTIVE_NAME: ContextVar[str | None] = ContextVar("_cg_active_plugin_name", default=None)
_AGENT_MODE: ContextVar[AgentMode | None] = ContextVar("_cg_agent_mode", default=None)
_SANDBOX_ROOT: ContextVar[Path | None] = ContextVar("_cg_sandbox_root", default=None)
_SANDBOX_READ_EXTRAS: ContextVar[tuple[Path, ...]] = ContextVar(
    "_cg_sandbox_read_extras", default=()
)
_NET_SCOPE: ContextVar[_NetworkScope | None] = ContextVar("_cg_net_scope", default=None)

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


def _normalize_host(raw: Any) -> str | None:
    """Return a canonical lowercase hostname or IP string, or None."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("ascii", "ignore")
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    cleaned = text.strip().lower().rstrip(".")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if not cleaned:
        return None
    try:
        return ipaddress.ip_address(cleaned).compressed
    except ValueError:
        return cleaned


def extract_hosts_from_url(url: str | None) -> frozenset[str]:
    """Return the normalized endpoint hostnames permitted for ``url``."""
    if not url:
        return frozenset()
    hostname = urlsplit(url).hostname
    normalized = _normalize_host(hostname)
    if normalized is None:
        return frozenset()
    if normalized in _LOOPBACK_HOSTS:
        return _LOOPBACK_HOSTS
    return frozenset({normalized})


def _is_ip_literal(target: str) -> bool:
    """Return True if ``target`` is a valid IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(target)
    except ValueError:
        return False
    return True


def _is_blocked_connect_ip(target: str, *, allow_loopback: bool) -> bool:
    """Return True if ``target`` is link-local, private, or unapproved loopback."""
    try:
        ip = ipaddress.ip_address(target)
    except ValueError:
        return False
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    if ip.is_loopback:
        return not allow_loopback
    if ip.is_private:
        return not allow_loopback
    return False


def _check_network(event: str, args: tuple, perms: PluginPermissions) -> None:
    if not perms.network:
        _deny("network", event)
        return

    scope = _NET_SCOPE.get()
    if scope is None:
        return

    if event == "socket.bind":
        _deny("network", "socket.bind (disallowed in model inference scope)")
        return

    if event in ("socket.getaddrinfo", "socket.gethostbyname"):
        raw_host = args[0] if args else None
        host = _normalize_host(raw_host)
        if host is None:
            _deny("network", f"{event} ({raw_host!r})")
            return
        if host in scope.allowed_hosts:
            return
        if _is_ip_literal(host) and not _is_blocked_connect_ip(
            host, allow_loopback=scope.allow_loopback
        ):
            return
        _deny("network", f"{event} ({raw_host!r})")
        return

    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        if not isinstance(address, tuple) or not address:
            _deny("network", f"socket.connect ({address!r})")
            return
        target = _normalize_host(address[0])
        if target is None:
            _deny("network", f"socket.connect ({address!r})")
            return
        if target in scope.allowed_hosts:
            return
        if _is_ip_literal(target) and not _is_blocked_connect_ip(
            target, allow_loopback=scope.allow_loopback
        ):
            return
        _deny("network", f"socket.connect ({target!r})")


def _check_open(args: tuple) -> None:
    perms = _ACTIVE.get()
    if perms is None:
        return

    path = args[0] if args else None
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None

    level = perms.filesystem

    writing = False
    if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
        mode is None and isinstance(flags, int) and (flags & _WRITE_FLAGS)
    ):
        writing = True

    if isinstance(path, int):
        if level == "none" or (level == "read_only" and writing):
            _deny("filesystem", "open (fd)")
        return

    if path is None:
        if level in ("none", "read_only"):
            _deny("filesystem", "open (unknown path)")
        return

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
            return
        candidate: str | Path = os.fsdecode(path) if isinstance(path, bytes) else path
        if not is_path_permitted(
            sandbox_root=root,
            candidate=candidate,
            trusted_read_roots=_SANDBOX_READ_EXTRAS.get(),
            for_write=writing,
        ):
            action = "write outside sandbox" if writing else "open outside sandbox"
            _deny("filesystem", f"{action}: {path}")


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
        for arg in args:
            if not isinstance(arg, (str, bytes, Path)):
                continue
            candidate: str | Path = os.fsdecode(arg) if isinstance(arg, bytes) else arg
            if not is_path_permitted(
                sandbox_root=root,
                candidate=candidate,
                trusted_read_roots=(),
                for_write=True,
            ):
                _deny("filesystem", f"{event} outside sandbox: {arg}")


def _audit_hook(event: str, args: tuple) -> None:
    perms = _ACTIVE.get()
    if perms is None:
        return
    if event in _NETWORK_EVENTS:
        _check_network(event, args, perms)
        return
    if event in _SPAWN_EVENTS:
        if not perms.process_spawn:
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
    scope_token = _NET_SCOPE.set(None)
    try:
        yield
    finally:
        _NET_SCOPE.reset(scope_token)
        _ACTIVE.reset(perms_token)
        _ACTIVE_NAME.reset(name_token)


@contextmanager
def model_inference_scope(
    allowed_hosts: Iterable[str],
    *,
    provider_name: str = "model_provider",
) -> Iterator[None]:
    """Bind host-allowlisted egress strictly for a model inference call.

    Activates ``filesystem="read_only"`` (allowing TLS trust store reads
    while blocking all file writes and deletions), ``network=True``
    restricted exclusively to ``allowed_hosts``, and
    ``process_spawn=False``. Resets immediately on exit.
    """
    normalized = frozenset(
        host for raw in allowed_hosts if (host := _normalize_host(raw)) is not None
    )
    if normalized & _LOOPBACK_HOSTS:
        normalized = normalized | _LOOPBACK_HOSTS
    allow_loopback = bool(normalized & _LOOPBACK_HOSTS)
    inference_perms = PluginPermissions(
        filesystem="read_only",
        network=True,
        process_spawn=False,
        env_read=(),
    )
    scope = _NetworkScope(
        allowed_hosts=normalized,
        allow_loopback=allow_loopback,
    )
    name_token = _ACTIVE_NAME.set(f"provider:{provider_name}")
    perms_token = _ACTIVE.set(inference_perms)
    scope_token = _NET_SCOPE.set(scope)
    try:
        yield
    finally:
        _NET_SCOPE.reset(scope_token)
        _ACTIVE.reset(perms_token)
        _ACTIVE_NAME.reset(name_token)


@contextmanager
def sandbox_scope(
    root: Path,
    *,
    read_only_extras: tuple[Path, ...] = (),
) -> Iterator[None]:
    """Set the sandbox root and optional read-only extra paths for the thread."""
    extras = tuple(Path(path) for path in read_only_extras)
    root_token = _SANDBOX_ROOT.set(Path(root))
    extras_token = _SANDBOX_READ_EXTRAS.set(extras)
    try:
        yield
    finally:
        _SANDBOX_READ_EXTRAS.reset(extras_token)
        _SANDBOX_ROOT.reset(root_token)
