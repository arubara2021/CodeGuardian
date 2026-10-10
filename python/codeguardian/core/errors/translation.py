"""Translate standard library exceptions into CodeGuardianError.

The taxonomy in ``taxonomy.py`` is only useful if raise sites go through
it. Python's standard library raises dozens of exception types that would
bypass the taxonomy entirely if allowed to propagate unchanged:
``FileNotFoundError``, ``ConnectionRefusedError``, ``UnicodeDecodeError``,
and so on. This module is the single translation point that catches those
exceptions and re-raises them as ``CodeGuardianError`` with the correct
code and category.

Two entry points are provided. ``translate`` converts an already-caught
exception into a ``CodeGuardianError``. ``typed`` is a decorator that
wraps a sync or async function so that any unhandled standard library
exception is translated automatically. Both are idempotent: a
``CodeGuardianError`` is returned unchanged, so a caller may nest
``typed`` decorators without producing double-wrapped errors.

The translation is deliberately lossy in one direction: the original
exception is preserved as the ``__cause__`` of the new error, so
tracebacks still show the full chain. Only the outer type changes.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .taxonomy import CodeGuardianError, ErrorCode

__all__ = ["translate", "typed"]

P = ParamSpec("P")
R = TypeVar("R")


# Mapping from standard library exception types to CodeGuardian error
# codes. The mapping is deliberately explicit rather than inferred from
# the exception name, because several stdlib exceptions have similar
# names but different correct responses. PermissionError and
# FileNotFoundError are the clearest example: both are OSError
# subclasses, but the user-facing message and the underlying cause are
# different.
_EXCEPTION_CODES: dict[type[BaseException], ErrorCode] = {
    FileNotFoundError: ErrorCode.FILE_NOT_FOUND,
    IsADirectoryError: ErrorCode.FILE_NOT_FOUND,
    NotADirectoryError: ErrorCode.FILE_NOT_FOUND,
    PermissionError: ErrorCode.FILE_PERMISSION_DENIED,
    FileExistsError: ErrorCode.SYS_INTERNAL,
    ConnectionRefusedError: ErrorCode.NET_UNREACHABLE,
    ConnectionResetError: ErrorCode.NET_UNREACHABLE,
    ConnectionAbortedError: ErrorCode.NET_UNREACHABLE,
    BrokenPipeError: ErrorCode.NET_UNREACHABLE,
    socket.gaierror: ErrorCode.NET_DNS_FAILED,
    TimeoutError: ErrorCode.NET_TIMEOUT,
    UnicodeDecodeError: ErrorCode.FILE_BINARY,
}


def _classify(exc: BaseException) -> ErrorCode:
    """Return the error code that best describes the given exception.

    The lookup walks the exception's method resolution order so that
    subclasses of a mapped type are classified correctly. For example, a
    subclass of ``PermissionError`` defined by a downstream library is
    still classified as ``FILE_PERMISSION_DENIED``.

    Anything not found in the mapping is classified as ``SYS_INTERNAL``,
    because an unclassified standard library exception indicates a gap
    in CodeGuardian's translation table, not a failure of the user's
    environment.
    """
    for exc_type in type(exc).__mro__:
        code = _EXCEPTION_CODES.get(exc_type)
        if code is not None:
            return code
    return ErrorCode.SYS_INTERNAL


def translate(exc: BaseException) -> CodeGuardianError:
    """Convert a standard library exception into a typed error.

    If the exception is already a ``CodeGuardianError``, it is returned
    unchanged. This makes the function safe to call at every layer of a
    call stack without producing nested wrapping.

    Args:
        exc: Any exception instance.

    Returns:
        A ``CodeGuardianError`` whose ``code`` is the closest match for
        the exception's type, whose ``message`` is the original
        exception's string form, and whose ``details`` records the
        original exception's fully qualified type name so a reader can
        tell exactly what was translated.
    """
    if isinstance(exc, CodeGuardianError):
        return exc
    code = _classify(exc)
    message = str(exc) or type(exc).__name__
    details: dict[str, Any] = {
        "original_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
    }
    return CodeGuardianError(code=code, message=message, details=details)


def typed(fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap a function so standard library exceptions become typed errors.

    The decorator detects whether the wrapped function is a coroutine
    function and produces an async or sync wrapper accordingly. Both
    wrappers preserve the original function's metadata via
    ``functools.wraps``.

    ``CodeGuardianError`` instances raised inside the wrapped function
    are re-raised unchanged, so a lower layer may raise a typed error and
    an upper layer may apply ``typed`` without double-wrapping.

    ``BaseException`` subclasses that are not ``Exception`` subclasses,
    such as ``KeyboardInterrupt``, ``SystemExit``, and
    ``asyncio.CancelledError``, are deliberately not translated. They
    represent control-flow signals rather than failures, and translating
    them would break the process-level behavior the interpreter expects.
    """
    if asyncio.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            try:
                return await fn(*args, **kwargs)
            except CodeGuardianError:
                raise
            except Exception as exc:
                raise translate(exc) from exc

        return async_wrapper  # type: ignore[return-value]

    @wraps(fn)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except CodeGuardianError:
            raise
        except Exception as exc:
            raise translate(exc) from exc

    return sync_wrapper  # type: ignore[return-value]
