"""Interface version compatibility negotiation.

The kernel publishes an interface version as ``MAJOR.MINOR``. A plugin
declares the version it targets. This module classifies the relationship
and either accepts it or raises ``PLUGIN_MANIFEST_INVALID`` with a
message the plugin author can act on.

The rules are:

- Same major, minor at or below the kernel's minor — accepted, current.
- Same major, minor above the kernel's minor — accepted with warning.
  The plugin may call methods the current kernel does not yet expose;
  those calls fail at runtime with a clear error rather than at load
  time.
- Major exactly one below the kernel's major, and greater than zero —
  accepted with warning. This is the N-1 path. It only fires when the
  kernel has shipped a major version above 1.0; at kernel 1.0 the path
  is unreachable because major 0 has no interface.
- Major above the kernel's major — rejected as too new.
- Major zero when the kernel is at 1.0, or major below the N-1
  threshold — rejected as too old.
"""

from __future__ import annotations

import re
from typing import Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "KERNEL_INTERFACE_VERSION",
    "Compatibility",
    "check_interface_version",
]

Compatibility = Literal["current", "newer_minor", "prior_major", "incompatible"]

KERNEL_INTERFACE_VERSION = "1.0"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")


def _parse(version: str) -> tuple[int, int]:
    match = _VERSION_RE.match(version)
    if match is None:
        raise CodeGuardianError(
            ErrorCode.PLUGIN_MANIFEST_INVALID,
            f"interface version {version!r} is not in MAJOR.MINOR form",
            details={"version": version},
        )
    return int(match.group(1)), int(match.group(2))


def check_interface_version(
    plugin_version: str,
    kernel_version: str = KERNEL_INTERFACE_VERSION,
) -> Compatibility:
    """Classify the compatibility between a plugin and the kernel.

    Args:
        plugin_version: The plugin's declared interface version, from its
            manifest's ``interface_version`` field.
        kernel_version: The kernel's interface version. Defaults to the
            current kernel version.

    Returns:
        ``"current"`` when the plugin targets the exact kernel version or
        an earlier minor of the same major. ``"newer_minor"`` when the
        plugin targets a later minor of the same major; the caller
        should warn. ``"prior_major"`` when the plugin targets the
        previous major version; the caller should warn. The
        ``"incompatible"`` member exists in the type for future use but
        is never returned: incompatible versions raise instead.

    Raises:
        CodeGuardianError: ``PLUGIN_MANIFEST_INVALID`` when the plugin
            major is above the kernel's, when it is below the N-1
            threshold, when the plugin major is zero under a 1.0 kernel,
            or when either version string is not in ``MAJOR.MINOR`` form.
    """
    p_major, p_minor = _parse(plugin_version)
    k_major, k_minor = _parse(kernel_version)

    if p_major == k_major:
        if p_minor <= k_minor:
            return "current"
        return "newer_minor"

    if p_major > k_major:
        raise CodeGuardianError(
            ErrorCode.PLUGIN_MANIFEST_INVALID,
            f"plugin targets interface {plugin_version}, kernel supports "
            f"{kernel_version}; major version too new",
            details={
                "plugin_version": plugin_version,
                "kernel_version": kernel_version,
            },
        )

    if p_major == k_major - 1 and p_major > 0:
        return "prior_major"

    raise CodeGuardianError(
        ErrorCode.PLUGIN_MANIFEST_INVALID,
        f"plugin targets interface {plugin_version}, kernel supports "
        f"{kernel_version}; major version too old",
        details={
            "plugin_version": plugin_version,
            "kernel_version": kernel_version,
        },
    )
