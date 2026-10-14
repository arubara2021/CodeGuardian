"""In-process bridge to the compiled Rust extension.

The extension is imported lazily so environments without a built
native module can still import this package. When the extension is
absent, every call raises a typed ``SYS_NOT_IMPLEMENTED``.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = ["PyO3Bridge", "is_native_available"]

_MODULE_NAME = "codeguardian._native"

_native_module: ModuleType | None = None
_load_attempted = False


def _load_native() -> ModuleType | None:
    global _native_module, _load_attempted
    if _load_attempted:
        return _native_module
    _load_attempted = True
    try:
        _native_module = importlib.import_module(_MODULE_NAME)
    except ImportError:
        _native_module = None
    return _native_module


def is_native_available() -> bool:
    """Return whether the compiled extension is importable."""
    return _load_native() is not None


class PyO3Bridge:
    """Bridge to the in-process PyO3 extension."""

    def __init__(self) -> None:
        self._native = _load_native()

    def _require_native(self) -> ModuleType:
        if self._native is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                f"Native extension '{_MODULE_NAME}' is not built.",
                evidence="Run 'cargo build --release -p codeguardian-pyo3' and copy the "
                "resulting library to python/codeguardian/_native.so.",
            )
        return self._native

    def echo(self, value: str) -> str:
        """Return the input unchanged.

        Proves the bridge end to end. Batched kernel calls arrive in
        Phase 6.
        """
        return self._require_native().echo(value)
