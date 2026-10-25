"""Exception isolation, timeouts, and budget tracking for plugin calls.

Sync plugins go through ``call``, async plugins through ``call_async``.
Both paths use the same executor or event loop and enforce the same
per-method timeout. A failure in either path disables the plugin for
the rest of the process.

# Budget tracking

A ``BudgetTracker`` may be attached to the barrier to cap total work
per phase. Two caps are enforced: a maximum number of calls and a
maximum total output size in bytes. When either cap is exceeded, the
barrier raises ``PLUGIN_TIMEOUT`` with ``details["budget"]`` naming the
cap. The phase catches this and stops; it does not retry.

# Agent mode check

When the barrier is constructed with an ``agent_mode``, each call
checks the plugin's ``action_kind`` against the mode before dispatching.
A ``plan``-mode barrier refuses to call a write or execute tool. The
check happens before the thread dispatch, so the correct error code
reaches the caller.

# Limitations

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
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .permissions import (
    AgentMode,
    PluginPermissions,
    check_action_allowed,
    plugin_active,
)

__all__ = [
    "DEFAULT_METHOD_TIMEOUTS",
    "BudgetTracker",
    "FaultBarrier",
    "shutdown_executor",
]


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
    "context_provider": {"provide": 60.0},
}

_DEFAULT_FALLBACK_TIMEOUT = 30.0

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


class BudgetTracker:
    """Per-phase cap on plugin work.

    Two independent counters: number of calls and total output bytes.
    Either can be set to ``None`` to disable that cap. The tracker is
    not thread-safe on its own; the barrier serializes the increments
    through its ``_lock``.

    When a cap is exceeded, the tracker raises ``PLUGIN_TIMEOUT`` with
    ``details["budget"]`` naming which cap was hit. The phase treats
    this as a phase-level stop, not a per-call retry.
    """

    def __init__(
        self,
        *,
        max_calls: int | None = 50,
        max_output_bytes: int | None = 1_000_000,
    ) -> None:
        self.max_calls = max_calls
        self.max_output_bytes = max_output_bytes
        self.calls = 0
        self.output_bytes = 0
        self._lock = threading.Lock()

    def check_call(self) -> None:
        """Increment the call count and raise if the cap is exceeded."""
        with self._lock:
            self.calls += 1
            if self.max_calls is not None and self.calls > self.max_calls:
                raise CodeGuardianError(
                    ErrorCode.PLUGIN_TIMEOUT,
                    f"budget exhausted: {self.calls} calls (max {self.max_calls})",
                    details={
                        "budget": "calls",
                        "current": self.calls,
                        "limit": self.max_calls,
                    },
                )

    def record_output(self, size: int) -> None:
        """Record output bytes and raise if the cap is exceeded."""
        with self._lock:
            self.output_bytes += size
            if self.max_output_bytes is not None and self.output_bytes > self.max_output_bytes:
                raise CodeGuardianError(
                    ErrorCode.PLUGIN_TIMEOUT,
                    f"budget exhausted: {self.output_bytes} bytes (max {self.max_output_bytes})",
                    details={
                        "budget": "output_bytes",
                        "current": self.output_bytes,
                        "limit": self.max_output_bytes,
                    },
                )


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


def _serialized_size(value: Any) -> int:
    """Best-effort byte size for output budget accounting."""
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 0


class FaultBarrier:
    """Isolates a plugin's exceptions, hangs, and work volume from the run."""

    def __init__(
        self,
        plugin: Any,
        plugin_name: str,
        permissions: PluginPermissions,
        method_timeouts: dict[str, float] | None = None,
        default_timeout: float = _DEFAULT_FALLBACK_TIMEOUT,
        agent_mode: AgentMode | None = None,
        budget: BudgetTracker | None = None,
    ) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.permissions = permissions
        self.method_timeouts = dict(method_timeouts or {})
        self.default_timeout = default_timeout
        self.agent_mode = agent_mode
        self.budget = budget
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

    def _check_preconditions(self, method_name: str) -> None:
        if self._disabled:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_DISABLED,
                f"plugin {self.plugin_name!r} is disabled",
                details={"plugin": self.plugin_name},
            )
        if self.agent_mode is not None:
            action_kind = getattr(self.plugin, "action_kind", None)
            if action_kind is not None:
                check_action_allowed(self.plugin_name, self.agent_mode, action_kind)
        if self.budget is not None:
            self.budget.check_call()
        method = getattr(self.plugin, method_name, None)
        if not callable(method):
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin {self.plugin_name!r} has no callable {method_name!r}",
                details={"plugin": self.plugin_name, "method": method_name},
            )

    def _record_output(self, result: Any) -> None:
        if self.budget is not None:
            self.budget.record_output(_serialized_size(result))

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a synchronous plugin method under isolation and timeout."""
        self._check_preconditions(method_name)
        method = getattr(self.plugin, method_name)
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
            result = future.result(timeout=timeout)
            self._record_output(result)
            return result
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
        """Invoke an asynchronous plugin method under isolation and timeout."""
        self._check_preconditions(method_name)
        method = getattr(self.plugin, method_name)
        timeout = self.method_timeouts.get(method_name, self.default_timeout)
        try:
            result = await asyncio.wait_for(
                _run_plugin_method_async(
                    self.plugin_name,
                    self.permissions,
                    method,
                    args,
                    kwargs,
                ),
                timeout=timeout,
            )
            self._record_output(result)
            return result
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
