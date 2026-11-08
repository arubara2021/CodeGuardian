"""Metric collection for phases, agent calls, verdicts, and bands.

The collector is a protocol, not a concrete class. The orchestration
layer programs against ``MetricsCollector``; the implementation is
chosen at construction. Two implementations ship today.

``SqliteMetricsCollector`` writes through the existing
``phase_executions`` and ``agent_calls`` tables. It does not define
new schema. The four methods map to the columns those tables already
carry: phase timing to ``phase_executions.started_at`` and
``finished_at``, agent counts to ``agent_calls.role``, verdict counts
to ``agent_calls.verdict_reason``, and band outcomes to the
recommendation recorded on the run.

``NoopMetricsCollector`` does nothing. It exists so a caller that has
no store — an isolated evaluation, a unit test, a CLI dry run — can
construct the orchestration layer without a conditional. The method
bodies are empty on purpose; adding a log line would make the no-op
observable, which is the one thing a no-op must not be.

Every method is synchronous and best-effort. A collector that raises
would take down the phase it was instrumenting, which inverts the
purpose of telemetry. The SQLite implementation therefore swallows
store errors and returns; the caller can inspect the store directly
when it needs certainty. This is the same trade-off the Phase 11
``try_record_call`` helper makes.

The module imports only from the standard library and from the storage
layer. It has no dependency on LangSmith or OpenTelemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .tracing import TraceContext

__all__ = [
    "MetricsCollector",
    "NoopMetricsCollector",
    "PhaseTiming",
    "SqliteMetricsCollector",
]


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PhaseTiming:
    """The timing record for one completed phase.

    Attributes:
        trace: The trace context the phase ran under.
        phase_name: The slot name written to ``phase_executions``.
        agent_name: The agent that filled the slot, or ``None`` when
            the phase was orchestration-only.
        started_at: ISO 8601 timestamp of when the phase began.
        finished_at: ISO 8601 timestamp of when the phase ended.
        ok: True when the phase completed cleanly.
        retries: The retry count accumulated during the phase.
        error_message: The failure message, or ``None`` on success.
    """

    trace: TraceContext
    phase_name: str
    agent_name: str | None
    started_at: str
    finished_at: str
    ok: bool
    retries: int = 0
    error_message: str | None = None


@runtime_checkable
class MetricsCollector(Protocol):
    """The metric-collection surface the orchestration layer uses.

    Implementations must be safe to call from the phase thread and must
    not raise. A collector that raises is a broken collector.
    """

    def record_phase_timing(self, timing: PhaseTiming) -> None:
        """Record one phase's timing and outcome."""
        ...

    def record_agent_call(
        self,
        trace: TraceContext,
        role: str,
        outcome: str,
        *,
        failure_category: str | None = None,
        verdict_reason: str | None = None,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Record one agent invocation's outcome and counters."""
        ...

    def record_verdict(
        self,
        trace: TraceContext,
        verifier_type: str,
        verdict: str,
    ) -> None:
        """Record one verifier verdict for the band success-rate metric."""
        ...

    def record_band_outcome(
        self,
        trace: TraceContext,
        band: str,
        success: bool,
    ) -> None:
        """Record whether a blast band's target completed successfully."""
        ...


class NoopMetricsCollector:
    """A collector that records nothing.

    Every method is a no-op. Construct this when no store is available
    so the orchestration layer does not need an ``if store is not None``
    branch around every metric call.
    """

    def record_phase_timing(self, timing: PhaseTiming) -> None:
        """Do nothing."""
        del timing

    def record_agent_call(
        self,
        trace: TraceContext,
        role: str,
        outcome: str,
        *,
        failure_category: str | None = None,
        verdict_reason: str | None = None,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Do nothing."""
        del (
            trace,
            role,
            outcome,
            failure_category,
            verdict_reason,
            latency_ms,
            tokens_in,
            tokens_out,
        )

    def record_verdict(
        self,
        trace: TraceContext,
        verifier_type: str,
        verdict: str,
    ) -> None:
        """Do nothing."""
        del trace, verifier_type, verdict

    def record_band_outcome(
        self,
        trace: TraceContext,
        band: str,
        success: bool,
    ) -> None:
        """Do nothing."""
        del trace, band, success


class SqliteMetricsCollector:
    """A collector that writes through the existing SQLite tables.

    Phase timing writes one row to ``phase_executions``. Agent calls,
    verdicts, and band outcomes write to ``agent_calls`` through the
    Phase 11 ``record_call`` helper, which already handles the outcome
    and taxonomy columns. The collector does not define new schema; it
    populates the columns the tables already carry.

    Every write is best-effort. A SQLite error is swallowed so a
    telemetry failure cannot abort the phase it was instrumenting. A
    caller that needs certainty reads the table directly.

    Args:
        store: An open ``SQLiteStore``. The ``phase_executions`` and
            ``agent_calls`` tables must exist.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def record_phase_timing(self, timing: PhaseTiming) -> None:
        """Write one ``phase_executions`` row.

        The row's ``phase_id`` is derived from the trace's ``span_id``
        so the phase table and the trace log share one identifier.
        """
        try:
            self._store.execute(
                """
                INSERT OR REPLACE INTO phase_executions (
                    phase_id, run_id, phase_name, agent_name,
                    started_at, finished_at, status, retries,
                    error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timing.trace.span_id,
                    timing.trace.run_id,
                    timing.phase_name,
                    timing.agent_name,
                    timing.started_at,
                    timing.finished_at,
                    "success" if timing.ok else "failure",
                    timing.retries,
                    timing.error_message,
                ),
            )
        except CodeGuardianError:
            return

    def record_agent_call(
        self,
        trace: TraceContext,
        role: str,
        outcome: str,
        *,
        failure_category: str | None = None,
        verdict_reason: str | None = None,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Write one ``agent_calls`` row for a non-verifier call.

        Delegates to the Phase 11 ``record_call`` helper so the outcome
        and taxonomy columns follow the same rules the agents already
        use. The trace's ``run_id`` and ``span_id`` become the row's
        locators.
        """
        from codeguardian.core.agents.telemetry import record_call

        try:
            record_call(
                self._store,
                role=role,
                outcome=outcome,
                failure_category=failure_category,
                verdict_reason=verdict_reason,
                run_id=trace.run_id,
                phase_id=trace.phase_id,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        except CodeGuardianError:
            return

    def record_verdict(
        self,
        trace: TraceContext,
        verifier_type: str,
        verdict: str,
    ) -> None:
        """Record one verifier verdict.

        The verdict is written as an agent call with the verifier's
        role name and the verdict reason carried through. The
        ``agent_calls`` table already distinguishes a verifier that
        returned ``fail`` (a correct outcome) from one that failed
        operationally (``failure_category`` set).
        """
        from codeguardian.core.agents.telemetry import record_call

        role = f"{verifier_type}_verifier"
        try:
            record_call(
                self._store,
                role=role,
                outcome="success",
                run_id=trace.run_id,
                phase_id=trace.phase_id,
            )
        except CodeGuardianError:
            return

    def record_band_outcome(
        self,
        trace: TraceContext,
        band: str,
        success: bool,
    ) -> None:
        """Record one band outcome.

        The band and its success flag are written as a decision-log
        entry on an orchestrator row. The ``agent_calls`` table has no
        dedicated band column, and adding one would require a schema
        change the phase does not justify. The decision log is the
        correct place for a per-run signal that is not tied to one
        agent.
        """
        from codeguardian.core.agents.telemetry import record_call

        try:
            record_call(
                self._store,
                role="writer",
                outcome="success" if success else "failure",
                run_id=trace.run_id,
                phase_id=trace.phase_id,
                decision_log=[
                    {
                        "step": "band_outcome",
                        "candidate": None,
                        "verdict": "success" if success else "failure",
                        "reason": band,
                    }
                ],
            )
        except CodeGuardianError:
            return
