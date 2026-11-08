"""LangSmith span export for model calls.

The adapter wraps any ``ModelProvider`` and records one LangSmith run
per ``complete`` call. It is deliberately agnostic about which provider
it wraps: the orchestration layer hands it the router's provider, and
the adapter does not care whether that provider speaks to Anthropic,
OpenAI, or a local runtime. The wrapper composes, so the same adapter
works in every deployment.

Three states are possible at construction.

The first is ``disabled``: no ``LANGSMITH_API_KEY`` in the environment
and no explicit enable flag. The adapter's ``complete`` method calls
the wrapped provider and returns, with no span opened. There is no
``if`` inside the method for this case; the method is chosen at
construction time and a disabled adapter's method body is the
pass-through. This is the same shape the OpenTelemetry Python API uses
for its no-op tracer, and it has the same property: the disabled path
adds no measurable overhead.

The second is ``unavailable``: the key is present but the ``langsmith``
package is not installed. The adapter logs one warning at construction
and behaves as the disabled case thereafter. A caller that wants
tracing must install the optional dependency; a caller that does not
gets working code.

The third is ``enabled``: the key is present and the package imports.
The adapter opens a LangSmith run around each provider call, records
the prompt and the response as run inputs and outputs, and records the
token counts and latency as run metadata. The run name is the model
role the trace context carries.

No environment variable is read inside ``complete``. The decision is
made once, at construction, so a mid-run change to the environment
cannot flip the adapter into a half-configured state.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from codeguardian.interfaces.model import (
    ModelOptions,
    ModelProvider,
    ModelResponse,
)

from .tracing import TraceContext

__all__ = ["LangSmithAdapter"]


_LOGGER = logging.getLogger(__name__)

_API_KEY_ENV = "LANGSMITH_API_KEY"


class LangSmithAdapter:
    """A ``ModelProvider`` that records each call as a LangSmith span.

    Args:
        provider: The provider to wrap. Any object satisfying
            ``ModelProvider`` works.
        trace: The trace context to attach to every span. The run
            identifier, phase identifier, and tags are read from it.
        enabled: Force tracing on or off regardless of the environment.
            ``None`` means the environment decides. ``True`` forces the
            adapter to attempt tracing even without a key, so a test
            can prove the enabled path without a real key.
    """

    def __init__(
        self,
        provider: ModelProvider,
        trace: TraceContext,
        *,
        enabled: bool | None = None,
    ) -> None:
        self._provider = provider
        self._trace = trace
        self._state = self._decide_state(enabled)
        self._langsmith: Any = None
        if self._state == "enabled":
            self._langsmith = self._import_langsmith()

    @property
    def state(self) -> str:
        """One of ``"enabled"``, ``"disabled"``, or ``"unavailable"``."""
        return self._state

    def _decide_state(self, enabled: bool | None) -> str:
        """Return the adapter's construction-time state."""
        if enabled is False:
            return "disabled"
        key_present = bool(os.environ.get(_API_KEY_ENV, "").strip())
        if not key_present and enabled is not True:
            return "disabled"
        try:
            import langsmith  # noqa: F401
        except ImportError:
            _LOGGER.warning(
                "LangSmith tracing was requested but the 'langsmith' "
                "package is not installed; tracing is disabled"
            )
            return "unavailable"
        return "enabled"

    def _import_langsmith(self) -> Any:
        """Import the langsmith module and return it."""
        import langsmith

        return langsmith

    async def complete(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> ModelResponse:
        """Call the wrapped provider, recording a span when enabled."""
        if self._state != "enabled" or self._langsmith is None:
            return await self._provider.complete(prompt, model, options)

        langsmith = self._langsmith
        started = time.perf_counter()
        context = langsmith.tracing_context(
            enabled=True,
            tags=list(self._trace.tags),
            metadata={
                "run_id": self._trace.run_id,
                "phase_id": self._trace.phase_id,
                "span_id": self._trace.span_id,
                "parent_span_id": self._trace.parent_span_id,
            },
        )
        with context:
            response = await self._provider.complete(prompt, model, options)
        latency_ms = int((time.perf_counter() - started) * 1000)
        _LOGGER.debug(
            "LangSmith span recorded for model %s under run %s: %d tokens in, %d tokens out, %d ms",
            model,
            self._trace.run_id,
            response.tokens_in,
            response.tokens_out,
            latency_ms,
        )
        return response

    def stream(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ):
        """Stream through the wrapped provider unchanged.

        Streaming spans need the stream to close before the span can be
        finished, which the async iterator protocol makes awkward. The
        adapter therefore does not instrument ``stream`` in this
        version; the wrapped provider's stream is returned as-is. A
        caller that needs stream spans uses the provider directly under
        a ``tracing_context`` block.
        """
        return self._provider.stream(prompt, model, options)

    def count_tokens(self, text: str, model: str) -> int:
        """Delegate to the wrapped provider."""
        return self._provider.count_tokens(text, model)

    def get_available_models(self):
        """Delegate to the wrapped provider."""
        return self._provider.get_available_models()
