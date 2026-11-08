"""Telemetry surface for the orchestration layer.

Three independent concerns live in this package: trace identity,
metric collection, and LangSmith span export. They are separate modules
because they evolve on different schedules and have different failure
modes. A missing LangSmith dependency must not break metric collection;
a SQLite write failure must not break trace propagation.

The public surface exports the protocol a caller programs against, the
two shipped implementations of that protocol, and the trace identity
type every other module reads. An OpenTelemetry adapter is a future
implementation of ``MetricsCollector``; adding it is a new module, not
an edit to this file.
"""

from __future__ import annotations

from .langsmith import LangSmithAdapter
from .metrics import (
    MetricsCollector,
    NoopMetricsCollector,
    SqliteMetricsCollector,
)
from .tracing import TraceContext

__all__ = [
    "LangSmithAdapter",
    "MetricsCollector",
    "NoopMetricsCollector",
    "SqliteMetricsCollector",
    "TraceContext",
]
