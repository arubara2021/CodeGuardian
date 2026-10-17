"""Select which ``SandboxBackend`` to use on this host.

The selector returns a backend *name*, not a backend instance. This
keeps it a pure function that can be tested without a running daemon
and lets the caller wire the ``JSONRPCBridge`` exactly once.

The choice follows the configured preference when that preference
names an implemented backend that can actually run on this host.
Otherwise, the platform default applies. If nothing is available, a
typed ``SANDBOX_CREATE_FAILED`` error is raised rather than returning
a backend that cannot work.

Stubs (Seatbelt, Firecracker) are never auto-selected. They exist so
that a caller who imports the class gets a clear "not implemented"
error; the selector does not pretend they can isolate anything.
"""

from __future__ import annotations

import logging
import shutil
import sys

from codeguardian.core.config import Config
from codeguardian.core.config.defaults import DEFAULT_SANDBOX_BACKEND
from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = ["select_backend"]

logger = logging.getLogger(__name__)

# Backends with a working v1 implementation. Anything not in this set
# is either a stub or a future backend; the selector never returns one
# as the auto-detected choice.
_IMPLEMENTED: frozenset[str] = frozenset({"bubblewrap", "docker"})

# Per-platform fallback order. Only implemented backends appear here.
# Stubs like Seatbelt are omitted on purpose: returning a stub on macOS
# would deny the user a working docker fallback with no benefit.
_PLATFORM_PREFERENCES: dict[str, tuple[str, ...]] = {
    "linux": ("bubblewrap", "docker"),
    "darwin": ("docker",),
    "win32": ("docker",),
}

_DEFAULT_PREFERENCES: tuple[str, ...] = ("bubblewrap", "docker")


def _platform_key() -> str:
    """Return a stable platform key for preference lookup."""
    if sys.platform == "win32":
        return "win32"
    return sys.platform.split("-", 1)[0]


def _is_available(name: str) -> bool:
    """Return whether ``name`` is implemented and runnable on this host."""
    if name not in _IMPLEMENTED:
        return False
    if name == "bubblewrap":
        return sys.platform.startswith("linux") and shutil.which("bwrap") is not None
    if name == "docker":
        return shutil.which("docker") is not None
    return False


def _platform_candidates() -> tuple[str, ...]:
    """Return the platform default ordering for this host."""
    return _PLATFORM_PREFERENCES.get(_platform_key(), _DEFAULT_PREFERENCES)


def select_backend(config: Config) -> str:
    """Return the name of the sandbox backend to use on this host.

    The configured value wins when it names an implemented backend that
    can run. Any other value — ``"auto"``, a stub name, or a backend
    whose binary is absent — falls back to the platform default.

    Args:
        config: The loaded CodeGuardian configuration.

    Returns:
        The backend name. One of ``"bubblewrap"`` or ``"docker"``.

    Raises:
        CodeGuardianError: With code ``SANDBOX_CREATE_FAILED`` when no
            implemented backend is available on this host.
    """
    requested = config.general.sandbox_backend
    if requested != "auto" and requested != DEFAULT_SANDBOX_BACKEND:
        if _is_available(requested):
            return requested
        logger.warning(
            "requested sandbox backend %r is not available; falling back",
            requested,
        )
    for candidate in _platform_candidates():
        if _is_available(candidate):
            return candidate
    raise CodeGuardianError(
        ErrorCode.SANDBOX_CREATE_FAILED,
        f"no sandbox backend is available on this host (platform={sys.platform})",
        details={
            "platform": sys.platform,
            "tried": list(_platform_candidates()),
            "requested": requested,
        },
    )
