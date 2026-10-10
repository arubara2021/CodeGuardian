"""Tests for the standard library to CodeGuardianError translation layer.

The translation layer is what closes the gap between the taxonomy and the
code that actually touches the disk and the network. Without it, every
standard library failure leaks through as a raw exception and the
orchestrator cannot classify it. This test verifies the mapping is
correct for the exception types the codebase expects to encounter, that
the decorator works on both sync and async functions, and that the
translation is idempotent.
"""

import asyncio
import socket

import pytest

from codeguardian.core.errors import (
    CodeGuardianError,
    ErrorCategory,
    ErrorCode,
    translate,
    typed,
)


def test_translate_file_not_found():
    error = translate(FileNotFoundError("missing.txt"))
    assert error.code is ErrorCode.FILE_NOT_FOUND
    assert error.category is ErrorCategory.USER


def test_translate_permission_error():
    error = translate(PermissionError("access denied"))
    assert error.code is ErrorCode.FILE_PERMISSION_DENIED
    assert error.category is ErrorCategory.USER


def test_translate_connection_refused():
    error = translate(ConnectionRefusedError("no listener"))
    assert error.code is ErrorCode.NET_UNREACHABLE
    assert error.category is ErrorCategory.NETWORK


def test_translate_dns_failure():
    error = translate(socket.gaierror("name not known"))
    assert error.code is ErrorCode.NET_DNS_FAILED
    assert error.category is ErrorCategory.NETWORK


def test_translate_timeout():
    error = translate(TimeoutError("took too long"))
    assert error.code is ErrorCode.NET_TIMEOUT
    assert error.category is ErrorCategory.NETWORK


def test_translate_unicode_decode_error():
    try:
        b"\xff\xfe".decode("utf-8")
    except UnicodeDecodeError as exc:
        error = translate(exc)
    assert error.code is ErrorCode.FILE_BINARY
    assert error.category is ErrorCategory.USER


def test_translate_unmapped_exception_becomes_system_error():
    error = translate(ZeroDivisionError("nope"))
    assert error.code is ErrorCode.SYS_INTERNAL
    assert error.category is ErrorCategory.SYSTEM


def test_translate_subclass_is_classified_by_mro():
    class CustomPermissionError(PermissionError):
        pass

    error = translate(CustomPermissionError("denied"))
    assert error.code is ErrorCode.FILE_PERMISSION_DENIED


def test_translate_is_idempotent():
    original = CodeGuardianError(code=ErrorCode.NET_TIMEOUT, message="boom")
    assert translate(original) is original


def test_translate_records_original_type_in_details():
    error = translate(FileNotFoundError("missing.txt"))
    assert error.details["original_type"] == "builtins.FileNotFoundError"


def test_translate_preserves_message():
    error = translate(FileNotFoundError("missing.txt"))
    assert "missing.txt" in error.message


def test_typed_wraps_sync_function():
    @typed
    def failing() -> None:
        raise FileNotFoundError("missing.txt")

    with pytest.raises(CodeGuardianError) as excinfo:
        failing()
    assert excinfo.value.code is ErrorCode.FILE_NOT_FOUND


def test_typed_wraps_async_function():
    @typed
    async def failing() -> None:
        raise FileNotFoundError("missing.txt")

    with pytest.raises(CodeGuardianError) as excinfo:
        asyncio.run(failing())
    assert excinfo.value.code is ErrorCode.FILE_NOT_FOUND


def test_typed_passes_codeguardian_error_through_unchanged():
    @typed
    def failing() -> None:
        raise CodeGuardianError(code=ErrorCode.NET_TIMEOUT, message="boom")

    with pytest.raises(CodeGuardianError) as excinfo:
        failing()
    assert excinfo.value.code is ErrorCode.NET_TIMEOUT
    assert excinfo.value.message == "boom"


def test_typed_preserves_return_value():
    @typed
    def succeed() -> int:
        return 42

    assert succeed() == 42


def test_typed_preserves_function_name():
    @typed
    def named_function() -> None:
        pass

    assert named_function.__name__ == "named_function"


def test_typed_does_not_translate_cancelled_error():
    @typed
    async def cancelled() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())
