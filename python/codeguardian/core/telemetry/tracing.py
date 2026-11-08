"""Trace identity for the orchestration layer.

A ``TraceContext`` carries the four identifiers a distributed trace
needs: the run, the phase, this span, and the parent span. The
orchestration loop creates one root context per run and derives a child
context per phase. Every other telemetry module reads from that context
rather than carrying its own identifiers, so a model call, a metric
row, and a LangSmith span all agree on which run and which phase they
belong to.

The module has no third-party imports. It does not import LangSmith,
OpenTelemetry, or SQLite. That is deliberate: trace identity is the
port the other telemetry modules plug into, and a port that depends on
an adapter is not a port. A future OpenTelemetry adapter reads the same
``TraceContext`` without a change to this file.

Span identifiers are UUID4 strings. A root context's ``parent_span_id``
is ``None``. A child context's ``parent_span_id`` is the parent's
``span_id``. The nesting is what makes a trace tree reconstructable
from the flat records the collectors write.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

__all__ = ["TraceContext"]


def _new_span_id() -> str:
    """Return a fresh span identifier."""
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class TraceContext:
    """The trace identity a telemetry record carries.

    Attributes:
        run_id: The pipeline run identifier. One per ``run`` call.
        phase_id: The ``phase_executions`` row identifier for the
            current phase. Empty on the root context before a phase
            has started.
        span_id: This context's span identifier.
        parent_span_id: The parent context's ``span_id``, or ``None``
            on a root context.
        tags: Free-form string tags carried through the trace.
            LangSmith accepts them as span tags; the SQLite collector
            ignores them.
    """

    run_id: str
    phase_id: str = ""
    span_id: str = field(default_factory=_new_span_id)
    parent_span_id: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def new_root(
        cls,
        run_id: str,
        *,
        tags: tuple[str, ...] = (),
    ) -> TraceContext:
        """Return a root context for a new run.

        Args:
            run_id: The run identifier. Must be non-empty.
            tags: Optional tags to carry on the root.

        Returns:
            A root ``TraceContext`` with a fresh ``span_id`` and no
            parent.

        Raises:
            ValueError: When ``run_id`` is empty.
        """
        if not run_id:
            raise ValueError("run_id must be non-empty")
        return cls(
            run_id=run_id,
            phase_id="",
            span_id=_new_span_id(),
            parent_span_id=None,
            tags=tags,
        )

    def child(
        self,
        phase_id: str,
        *,
        tags: tuple[str, ...] = (),
    ) -> TraceContext:
        """Return a child context for one phase.

        The child inherits the run identifier and links to this
        context through ``parent_span_id``. Its ``phase_id`` is the
        ``phase_executions`` row identifier the phase will write.

        Args:
            phase_id: The phase's row identifier. Must be non-empty.
            tags: Optional tags to add to the inherited set.

        Returns:
            A child ``TraceContext``.

        Raises:
            ValueError: When ``phase_id`` is empty.
        """
        if not phase_id:
            raise ValueError("phase_id must be non-empty")
        merged = self.tags + tuple(t for t in tags if t not in self.tags)
        return TraceContext(
            run_id=self.run_id,
            phase_id=phase_id,
            span_id=_new_span_id(),
            parent_span_id=self.span_id,
            tags=merged,
        )

    def with_phase(self, phase_id: str) -> TraceContext:
        """Return a sibling context that shares this span but names a phase.

        Used when a phase identifier is assigned after the context was
        created — the loop knows the run identifier at start and the
        phase identifier only once the ``phase_executions`` row is
        written. The returned context keeps the same ``span_id`` so
        the trace tree does not grow a spurious level.
        """
        return TraceContext(
            run_id=self.run_id,
            phase_id=phase_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            tags=self.tags,
        )
