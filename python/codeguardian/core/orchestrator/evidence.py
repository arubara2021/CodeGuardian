"""Hot-tier evidence events.

The orchestrator loop consumes typed evidence events synchronously to
make its next decision: retry, escalate, abort, rollback. The events
are small frozen dataclasses, not free-form dictionaries, so the loop
pattern-matches on type and cannot accidentally read a field that was
never set.

Five events cover the current decision surface:

- ``VerifierFailed``: a verifier returned ``fail`` or ``uncertain``.
  The verifier stack retries on fail with the verifier's reason as
  ``previous_attempt`` feedback.
- ``TestRunFailed``: a test run in the sandbox returned nonzero. The
  loop passes the stderr tail to the writer as ``test_failure``.
- ``ContextBudgetExceeded``: the per-run ContextBudget has crossed its
  refuse threshold. The loop stops the run and returns the snapshot.
- ``ToolCallFailed``: a context provider or tool could not complete.
  The loop logs the failure and continues when the caller marked the
  tool optional.
- ``BlastRecommendationAdjusted``: the blast agent shifted its
  recommendation one band toward caution because caller search was
  incomplete. The loop tightens the verifier K.

The cold tier is not in this module. ``agent_calls`` already carries
``tool_chain``, ``decision_log``, ``model_reasoning``, and
``evidence_receipt`` columns; the loop writes those through
``record_call`` exactly as it does today. The two tiers stay separate
so the hot path cannot accidentally depend on the audit schema.

``EvidenceBus`` is a bounded accumulator. It holds up to ``capacity``
events; older events are dropped when the bus overflows. The loop
drains the bus once per phase and writes the drained events as a
single ``decision_log`` row. ``events_to_decision_log`` converts the
typed events back to the ``DecisionLogEntry`` shape the telemetry
column expects.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "BlastRecommendationAdjusted",
    "ContextBudgetExceeded",
    "EvidenceBus",
    "HotEvent",
    "TestRunFailed",
    "ToolCallFailed",
    "VerifierFailed",
    "events_to_decision_log",
]


Band = Literal["proceed", "review", "block"]


@dataclass(frozen=True, slots=True)
class VerifierFailed:
    """One verifier returned fail or uncertain.

    Attributes:
        verifier: One of ``"correctness"``, ``"security"``, or
            ``"contract"``.
        verdict: The verifier's verdict. ``"fail"`` triggers a retry;
            ``"uncertain"`` escalates to human approval on block band.
        reason: The ``verdict_reason`` from the verifier, or None.
        evidence_ids: Stable identifiers for the evidence the verifier
            examined. Empty when the verifier did not produce a
            receipt.
    """

    verifier: str
    verdict: str
    reason: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TestRunFailed:
    """A characterization test exited nonzero in the sandbox.

    Attributes:
        test_path: The test file path relative to the clone root.
        exit_code: The process exit code.
        stderr_tail: The last few hundred characters of stderr,
            stripped of ANSI escapes. Passed to the writer as
            ``test_failure`` on retry.
    """

    test_path: str
    exit_code: int
    stderr_tail: str


@dataclass(frozen=True, slots=True)
class ContextBudgetExceeded:
    """The per-run ContextBudget has crossed its refuse threshold.

    Attributes:
        role: The agent role whose budget was exceeded.
        fill_percent: The fill percentage at the moment of refusal.
        snapshot: The snapshot returned by ``ContextBudget.snapshot``.
            Carried as a plain dict so the event is serializable.
    """

    role: str
    fill_percent: float
    snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolCallFailed:
    """A tool or context provider could not complete.

    Attributes:
        tool: The tool name, for example ``"function-body"``.
        error_code: The ``CodeGuardianError.code`` string, or None when
            the failure was not typed.
        reason: A short human-readable reason.
    """

    tool: str
    error_code: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class BlastRecommendationAdjusted:
    """The blast agent shifted its recommendation toward caution.

    Attributes:
        original: The raw recommendation from the kernel.
        adjusted: The recommendation after calibration.
        reason: The calibration reason from the blast agent.
        target: The target's qualified name, for the decision log.
    """

    original: str
    adjusted: str
    reason: str
    target: str


HotEvent = (
    VerifierFailed
    | TestRunFailed
    | ContextBudgetExceeded
    | ToolCallFailed
    | BlastRecommendationAdjusted
)


_DEFAULT_CAPACITY = 512


class EvidenceBus:
    """A bounded accumulator of hot-tier events.

    The loop pushes events during a phase and drains them at the phase
    boundary. Draining returns the accumulated events in insertion
    order and clears the bus. The capacity bound prevents a runaway
    phase from growing the bus without limit; when the bus overflows,
    the oldest event is dropped and the ``overflow_count`` increases
    so the caller can record that it happened.
    """

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1; got {capacity}")
        self._capacity = capacity
        self._events: list[HotEvent] = []
        self._overflow_count = 0

    @property
    def capacity(self) -> int:
        """The maximum number of events the bus holds."""
        return self._capacity

    @property
    def overflow_count(self) -> int:
        """The number of events dropped because the bus was full."""
        return self._overflow_count

    def __len__(self) -> int:
        return len(self._events)

    def push(self, event: HotEvent) -> None:
        """Append an event. Drops the oldest on overflow."""
        if len(self._events) >= self._capacity:
            self._events.pop(0)
            self._overflow_count += 1
        self._events.append(event)

    def drain(self) -> tuple[HotEvent, ...]:
        """Return every event and clear the bus."""
        result = tuple(self._events)
        self._events.clear()
        return result

    def peek(self) -> Sequence[HotEvent]:
        """Return a read-only view of the current events."""
        return tuple(self._events)


def events_to_decision_log(
    events: Sequence[HotEvent],
) -> list[dict[str, object]]:
    """Convert typed events to the decision_log entries telemetry expects.

    The ``decision_log`` column on ``agent_calls`` is a JSON array of
    ``DecisionLogEntry`` dicts. This function maps each typed event to
    a stable dictionary shape keyed by the event's ``step`` value:

    - ``VerifierFailed`` -> step ``"verifier_failed"``
    - ``TestRunFailed`` -> step ``"test_run_failed"``
    - ``ContextBudgetExceeded`` -> step ``"context_budget_exceeded"``
    - ``ToolCallFailed`` -> step ``"tool_call_failed"``
    - ``BlastRecommendationAdjusted`` -> step
      ``"blast_recommendation_adjusted"``

    The ``candidate`` field is None for every event because the loop
    writes one decision_log entry per event, not per candidate. A
    future refinement can add candidate-index context when the loop
    knows it.
    """
    result: list[dict[str, object]] = []
    for event in events:
        if isinstance(event, VerifierFailed):
            result.append(
                {
                    "step": "verifier_failed",
                    "candidate": None,
                    "verdict": event.verdict,
                    "reason": event.reason,
                    "verifier": event.verifier,
                    "evidence_ids": list(event.evidence_ids),
                }
            )
        elif isinstance(event, TestRunFailed):
            result.append(
                {
                    "step": "test_run_failed",
                    "candidate": None,
                    "verdict": "fail",
                    "reason": event.stderr_tail[:200],
                    "test_path": event.test_path,
                    "exit_code": event.exit_code,
                }
            )
        elif isinstance(event, ContextBudgetExceeded):
            result.append(
                {
                    "step": "context_budget_exceeded",
                    "candidate": None,
                    "verdict": "refuse",
                    "reason": f"fill_percent={event.fill_percent}",
                    "role": event.role,
                    "snapshot": event.snapshot,
                }
            )
        elif isinstance(event, ToolCallFailed):
            result.append(
                {
                    "step": "tool_call_failed",
                    "candidate": None,
                    "verdict": "error",
                    "reason": event.reason[:200],
                    "tool": event.tool,
                    "error_code": event.error_code,
                }
            )
        elif isinstance(event, BlastRecommendationAdjusted):
            result.append(
                {
                    "step": "blast_recommendation_adjusted",
                    "candidate": None,
                    "verdict": event.adjusted,
                    "reason": event.reason[:200],
                    "original": event.original,
                    "target": event.target,
                }
            )
    return result
