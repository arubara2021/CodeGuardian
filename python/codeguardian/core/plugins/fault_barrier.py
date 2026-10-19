"""Exception isolation and per-method timeouts for plugin calls.

Sync plugins go through ``call``, async plugins through ``call_async``.
Both paths use the same executor or event loop and enforce the same
per-method timeout. A failure in either path disables the plugin for
the rest of the process.

Limitations, documented not hidden:

- ``Exception`` is caught, ``BaseException`` is not. ``KeyboardInterrupt``
  and ``SystemExit`` propagate unchanged.
- The active-plugin context is set inside the plugin's call frame, not
  the caller's. A plugin that spawns its own thread does not inherit it.
- A plugin that hangs inside a pure-Python busy loop holding the GIL
  cannot be timed out. ``future.result`` never fires. Alternative is a
  process pool (Phase 13).
- A plugin that hangs inside a C syscall leaks a worker thread of the
  shared pool forever. The plugin is disabled after the timeout, so
  subsequent calls do not re-enter the pool.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .permissions import PluginPermissions, plugin_active

__all__ = ["DEFAULT_METHOD_TIMEOUTS", "FaultBarrier", "shutdown_executor"]


DEFAULT_METHOD_TIMEOUTS: dict[str, dict[str, float]] = {
    "language": {
        "detect": 10.0,
        "parse": 300.0,
        "generate_test": 300.0,
        "run_tests": 600.0,
    },
    "tool": {"call": 60.0},
    "verifier": {"verify": 120.0},
    "reporter": {"report": 30.0},
}

_DEFAULT_FALLBACK_TIMEOUT = 30.0

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cg-plugin")
    return _EXECUTOR


def shutdown_executor() -> None:
    """Shut down the shared executor. Intended for test teardown."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _EXECUTOR = None


def _run_plugin_method(
    plugin_name: str,
    permissions: PluginPermissions,
    method: Any,
    args: tuple,
    kwargs: dict,
) -> Any:
    with plugin_active(plugin_name, permissions):
        return method(*args, **kwargs)


async def _run_plugin_method_async(
    plugin_name: str,
    permissions: PluginPermissions,
    method: Any,
    args: tuple,
    kwargs: dict,
) -> Any:
    with plugin_active(plugin_name, permissions):
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


class FaultBarrier:
    """Isolates a plugin's exceptions and hangs from the rest of the run."""

    def __init__(
        self,
        plugin: Any,
        plugin_name: str,
        permissions: PluginPermissions,
        method_timeouts: dict[str, float] | None = None,
        default_timeout: float = _DEFAULT_FALLBACK_TIMEOUT,
    ) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.permissions = permissions
        self.method_timeouts = dict(method_timeouts or {})
        self.default_timeout = default_timeout
        self._disabled = False
        self._lock = threading.Lock()

    @property
    def disabled(self) -> bool:
        """Whether the plugin has been disabled by a prior failure."""
        return self._disabled

    def disable(self) -> None:
        """Mark the plugin disabled. Idempotent."""
        with self._lock:
            self._disabled = True

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a synchronous plugin method under isolation and timeout.

        Raises:
            CodeGuardianError: ``PLUGIN_DISABLED`` if disabled,
                ``PLUGIN_LOAD_FAILED`` on missing method or raised
                exception, ``PLUGIN_TIMEOUT`` on hang.
        """
        if self._disabled:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_DISABLED,
                f"plugin {self.plugin_name!r} is disabled",
                details={"plugin": self.plugin_name},
            )
        method = getattr(self.plugin, method_name, None)
        if not callable(method):
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin {self.plugin_name!r} has no callable {method_name!r}",
                details={"plugin": self.plugin_name, "method": method_name},
            )
        timeout = self.method_timeouts.get(method_name, self.default_timeout)
        future = _get_executor().submit(
            _run_plugin_method,
            self.plugin_name,
            self.permissions,
            method,
            args,
            kwargs,
        )
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            self.disable()
            raise CodeGuardianError(
                ErrorCode.PLUGIN_TIMEOUT,
                f"plugin {self.plugin_name!r} method {method_name!r} exceeded {timeout}s",
                details={
                    "plugin": self.plugin_name,
                    "method": method_name,
                    "timeout_seconds": timeout,
                },
            ) from exc
        except CodeGuardianError:
            raise
        except Exception as exc:
            self.disable()
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin {self.plugin_name!r} method {method_name!r} "
                f"raised {type(exc).__name__}: {exc}",
                details={
                    "plugin": self.plugin_name,
                    "method": method_name,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            ) from exc

    async def call_async(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke an asynchronous plugin method under isolation and timeout.

        The method may be a coroutine function or a sync function that
        returns an awaitable. A sync function that returns a plain value
        is also accepted for uniformity.

        Raises:
            CodeGuardianError: same codes as ``call``.
        """
        if self._disabled:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_DISABLED,
                f"plugin {self.plugin_name!r} is disabled",
                details={"plugin": self.plugin_name},
            )
        method = getattr(self.plugin, method_name, None)
        if not callable(method):
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin {self.plugin_name!r} has no callable {method_name!r}",
                details={"plugin": self.plugin_name, "method": method_name},
            )
        timeout = self.method_timeouts.get(method_name, self.default_timeout)
        try:
            return await asyncio.wait_for(
                _run_plugin_method_async(
                    self.plugin_name,
                    self.permissions,
                    method,
                    args,
                    kwargs,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            self.disable()
            raise CodeGuardianError(
                ErrorCode.PLUGIN_TIMEOUT,
                f"plugin {self.plugin_name!r} method {method_name!r} exceeded {timeout}s",
                details={
                    "plugin": self.plugin_name,
                    "method": method_name,
                    "timeout_seconds": timeout,
                },
            ) from exc
        except CodeGuardianError:
            raise
        except Exception as exc:
            self.disable()
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin {self.plugin_name!r} method {method_name!r} "
                f"raised {type(exc).__name__}: {exc}",
                details={
                    "plugin": self.plugin_name,
                    "method": method_name,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            ) from exc
