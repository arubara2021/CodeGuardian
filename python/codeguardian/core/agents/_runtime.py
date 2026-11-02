"""Agent runtime helpers.

Three helpers every agent uses around its own logic:

- fresh_barrier: builds a per-call FaultBarrier around a plugin
  instance, configured with the caller's agent_mode and budget, and
  enforcing an allowed_tools set at the dispatch boundary.
- tracked_model_call: calls ModelProvider.complete and measures the
  latency in milliseconds.
- classify_failure: maps a CodeGuardianError to a FailureCategory, or
  None for codes that do not correspond to one of the seven.

Concurrency. The PluginRegistry returns the same FaultBarrier instance
for a plugin on every get_* call. Mutating that barrier's agent_mode
and budget at call time would race between concurrent agents.
fresh_barrier constructs a new FaultBarrier per call instead; the
registry's barrier is never touched.

Least privilege. Each agent declares requires_tools. fresh_barrier
enforces that declaration: a method call to a name not in allowed_tools
raises PLUGIN_PERMISSION_DENIED before the underlying barrier
dispatches. The declared tool set becomes a runtime guarantee, not a
comment.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import (
    DEFAULT_METHOD_TIMEOUTS,
    AgentMode,
    BudgetTracker,
    FaultBarrier,
    PluginPermissions,
    PluginRegistry,
)
from codeguardian.interfaces.model import (
    ModelOptions,
    ModelProvider,
    ModelResponse,
)

from .context_budget import ContextBudget

__all__ = [
    "ContextBudget",
    "FailureCategory",
    "classify_failure",
    "fresh_barrier",
    "tracked_model_call",
]


FailureCategory = Literal[
    "localization",
    "generation",
    "schema",
    "contract",
    "timeout",
    "context_overflow",
    "model_unavailable",
]


_FAILURE_CATEGORY_BY_CODE: MappingProxyType[ErrorCode, FailureCategory] = MappingProxyType(
    {
        ErrorCode.PLUGIN_TIMEOUT: "timeout",
        ErrorCode.SANDBOX_TIMEOUT: "timeout",
        ErrorCode.NET_TIMEOUT: "timeout",
        ErrorCode.MCP_TOOL_TIMEOUT: "timeout",
        ErrorCode.MODEL_UNAVAILABLE: "model_unavailable",
        ErrorCode.MODEL_RATE_LIMITED: "model_unavailable",
        ErrorCode.MODEL_AUTH_FAILED: "model_unavailable",
        ErrorCode.PROVIDER_UNAVAILABLE: "model_unavailable",
        ErrorCode.MODEL_CONTEXT_EXCEEDED: "context_overflow",
        ErrorCode.FILE_NOT_FOUND: "localization",
        ErrorCode.REPO_NOT_FOUND: "localization",
        ErrorCode.MCP_TOOL_NOT_FOUND: "localization",
    }
)


def classify_failure(exc: BaseException) -> FailureCategory | None:
    """Return the failure category for a CodeGuardianError, or None.

    A CodeGuardianError whose code is not in the mapping does not
    correspond to one of the seven agent-level categories. Agents may
    classify those themselves when they know the context, for example
    a SYS_INTERNAL raised by validate_output is a schema failure from
    the agent's perspective.

    Non-CodeGuardianError exceptions and BaseException subclasses that
    are not Exception (KeyboardInterrupt, SystemExit) return None.

    Args:
        exc: The exception to classify.

    Returns:
        The matching FailureCategory, or None if the exception does not
        correspond to one of the seven categories.
    """
    if not isinstance(exc, CodeGuardianError):
        return None
    return _FAILURE_CATEGORY_BY_CODE.get(exc.code)


class _ScopedFaultBarrier(FaultBarrier):
    """A FaultBarrier that rejects method names outside allowed_tools.

    Used only by fresh_barrier. Private to this module. The optional
    context_budget is stored for the caller's later use; the barrier
    does not enforce it in this block.
    """

    def __init__(
        self,
        plugin: Any,
        plugin_name: str,
        permissions: PluginPermissions,
        *,
        method_timeouts: dict[str, float] | None = None,
        agent_mode: AgentMode | None = None,
        budget: BudgetTracker | None = None,
        allowed_tools: frozenset[str],
        context_budget: ContextBudget | None = None,
    ) -> None:
        super().__init__(
            plugin=plugin,
            plugin_name=plugin_name,
            permissions=permissions,
            method_timeouts=method_timeouts,
            agent_mode=agent_mode,
            budget=budget,
        )
        self._allowed_tools = allowed_tools
        self.context_budget = context_budget

    def _check_allowed(self, method_name: str) -> None:
        if method_name not in self._allowed_tools:
            raise CodeGuardianError(
                ErrorCode.PLUGIN_PERMISSION_DENIED,
                f"agent may not call {method_name!r} on plugin "
                f"{self.plugin_name!r}; allowed methods: "
                f"{sorted(self._allowed_tools)}",
                details={
                    "plugin": self.plugin_name,
                    "requested_method": method_name,
                    "allowed_tools": sorted(self._allowed_tools),
                },
            )

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a permitted method on the underlying plugin."""
        self._check_allowed(method_name)
        return super().call(method_name, *args, **kwargs)

    async def call_async(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a permitted async method on the underlying plugin."""
        self._check_allowed(method_name)
        return await super().call_async(method_name, *args, **kwargs)


@contextmanager
def fresh_barrier(
    registry: PluginRegistry,
    name: str,
    *,
    type_name: str = "context_provider",
    mode: AgentMode | None = None,
    budget: BudgetTracker | None = None,
    allowed_tools: tuple[str, ...] = (),
    context_budget: ContextBudget | None = None,
) -> Iterator[_ScopedFaultBarrier]:
    """Yield a FaultBarrier scoped to ``allowed_tools`` for one call.

    The registry's own barrier for this plugin is never mutated. A new
    FaultBarrier is constructed per invocation, so concurrent agents
    calling into the same plugin cannot interfere through shared state.

    Args:
        registry: The plugin registry to look up ``name`` in.
        name: The plugin name as registered.
        type_name: The plugin type. Defaults to ``"context_provider"``
            because every Phase 11 agent that uses a tool uses a
            context provider. Other types can be passed explicitly.
        mode: The agent's mode, enforced by the barrier's action_kind
            check.
        budget: The per-run budget tracker, shared across all tool
            calls of one agent run.
        allowed_tools: Method names the agent may call. Any other
            method raises ``PLUGIN_PERMISSION_DENIED``.

    Yields:
        A ``FaultBarrier`` subclass that enforces ``allowed_tools``.

    Raises:
        CodeGuardianError: ``PLUGIN_LOAD_FAILED`` if the plugin is not
            registered, or if a declared tool is not a callable method
            on the plugin instance.
    """
    record = registry.get_registered(name, type_name)
    plugin = record.plugin
    permissions = record.manifest.permissions

    allowed = frozenset(allowed_tools)
    for method_name in allowed:
        if not callable(getattr(plugin, method_name, None)):
            raise CodeGuardianError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin {name!r} has no callable {method_name!r}; "
                "declared tools must exist on the plugin instance",
                details={
                    "plugin": name,
                    "type": type_name,
                    "missing_method": method_name,
                },
            )

    method_timeouts = DEFAULT_METHOD_TIMEOUTS.get(type_name, {})
    barrier = _ScopedFaultBarrier(
        plugin=plugin,
        plugin_name=name,
        permissions=permissions,
        method_timeouts=method_timeouts,
        agent_mode=mode,
        budget=budget,
        allowed_tools=allowed,
        context_budget=context_budget,
    )
    try:
        yield barrier
    finally:
        # The barrier holds no resources of its own. Dropping the
        # reference is sufficient; the registry's barrier is untouched.
        pass


async def tracked_model_call(
    provider: ModelProvider,
    prompt: str,
    model: str,
    options: ModelOptions,
    role: str,
    context_budget: ContextBudget | None = None,
) -> tuple[ModelResponse, int]:
    """Call ``provider.complete`` and return the response and latency.

    Latency is measured as wall-clock milliseconds from just before the
    call to just after it returns. On exception the timing is not
    returned; the exception propagates and the caller records its own
    failure row.

    Args:
        provider: The model provider to call.
        prompt: The prompt text.
        model: The provider-specific model identifier.
        options: Generation parameters.
        role: The role this call is for. Passed for consistency with
            the ModelProvider.complete signature; available to
            instrumented callers that route by role.
        context_budget: When supplied, the response's input and output
            token counts are recorded into the budget after the call
            returns. The budget is not checked here; the calling agent
            is responsible for querying it before the next call.


    Returns:
        A tuple of (response, latency_ms).

    Raises:
        CodeGuardianError: Propagated from ``provider.complete``.
    """
    start = time.perf_counter()
    response = await provider.complete(prompt, model, options)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if context_budget is not None:
        context_budget.record(
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    return response, latency_ms
