"""Backwards-compatible re-export of ``codeguardian.core.plugins.path_guard``."""

from __future__ import annotations

from codeguardian.core.plugins.path_guard import (
    DENY_PATTERNS,
    OpenMode,
    canonicalize_and_contain,
    is_denied,
    is_path_permitted,
    safe_open,
)

__all__ = [
    "DENY_PATTERNS",
    "OpenMode",
    "canonicalize_and_contain",
    "is_denied",
    "is_path_permitted",
    "safe_open",
]
