"""Context budget for model calls.

A ContextBudget tracks how much of the model's context window a single
agent run has consumed. It is a mutable value object: callers record
tokens after each model call, query fill percentage between calls, and
reset after compaction.

Three signals govern behaviour. Below the compact threshold the budget
reports OK and the agent proceeds unchanged. Between the compact and
refuse thresholds it reports COMPACT and the agent should trim or
summarise the context before the next call. At or above the refuse
threshold it reports REFUSE and the agent should stop, returning the
snapshot from ``snapshot()`` rather than a blank failure.

The two thresholds default to 60 and 85 percent. The window size comes
from the caller — typically ``CapabilityProfile.context_window`` when a
profile is available. When the caller does not supply a usable window
(zero, negative, or None), DEFAULT_WINDOW is used so that the budget is
always constructible.

This module imports only from the standard library. It is safe to
import at the top of _runtime.py without creating a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "DEFAULT_WINDOW",
    "BudgetSignal",
    "ContextBudget",
]


DEFAULT_WINDOW = 128_000
"""Fallback context window when the caller does not supply one.

Chosen as the current common default across frontier models. Callers
that know the actual window (from a CapabilityProfile) should pass it
explicitly; this constant only applies to the uncoupled path.
"""


class BudgetSignal(StrEnum):
    """The pressure level a budget reports at a point in time."""

    OK = "ok"
    COMPACT = "compact"
    REFUSE = "refuse"


@dataclass(slots=True)
class ContextBudget:
    """Tracks fill percentage of a model context window.

    Attributes:
        window: Total context window, in tokens. Zero or negative
            values are replaced with DEFAULT_WINDOW in __post_init__.
        compact_at: Fill fraction at which compaction is requested.
            Defaults to 0.60.
        refuse_at: Fill fraction at which the budget refuses further
            calls. Defaults to 0.85.
        input_tokens: Tokens consumed on the input side so far.
        output_tokens: Tokens consumed on the output side so far.

    The two thresholds must satisfy 0 < compact_at < refuse_at <= 1.0.
    A caller that supplies out-of-range thresholds gets a ValueError at
    construction rather than surprising behaviour at query time.
    """

    window: int = DEFAULT_WINDOW
    compact_at: float = 0.60
    refuse_at: float = 0.85
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if self.window <= 0:
            self.window = DEFAULT_WINDOW
        if not (0.0 < self.compact_at < self.refuse_at <= 1.0):
            raise ValueError(
                "thresholds must satisfy 0 < compact_at < refuse_at <= 1.0; "
                f"got compact_at={self.compact_at}, refuse_at={self.refuse_at}"
            )

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed across input and output."""
        return self.input_tokens + self.output_tokens

    @property
    def fill(self) -> float:
        """Current fill fraction of the window, in the range [0, 1+]."""
        return self.total_tokens / self.window

    def record(self, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
        """Add a model call's token counts to the budget.

        Args:
            tokens_in: Input tokens reported by the provider. Must be
                non-negative.
            tokens_out: Output tokens reported by the provider. Must be
                non-negative.

        Raises:
            ValueError: If either argument is negative.
        """
        if tokens_in < 0:
            raise ValueError(f"tokens_in must be non-negative; got {tokens_in}")
        if tokens_out < 0:
            raise ValueError(f"tokens_out must be non-negative; got {tokens_out}")
        self.input_tokens += tokens_in
        self.output_tokens += tokens_out

    def reset(self, *, summary_tokens: int = 0) -> None:
        """Reset the budget after the caller has compacted the context.

        Called by an agent after it has replaced its accumulated context
        with a summary. ``summary_tokens`` records the cost of the
        summary so that the next fill computation starts from the real
        post-compaction size rather than zero.

        Args:
            summary_tokens: Tokens consumed by the summary that
                replaced the previous context. Must be non-negative.
                Defaults to 0 (a full clear).

        Raises:
            ValueError: If ``summary_tokens`` is negative.
        """
        if summary_tokens < 0:
            raise ValueError(f"summary_tokens must be non-negative; got {summary_tokens}")
        self.input_tokens = summary_tokens
        self.output_tokens = 0

    def check(self) -> BudgetSignal:
        """Return the current pressure signal.

        Returns:
            BudgetSignal.OK below the compact threshold.
            BudgetSignal.COMPACT at or above the compact threshold and
            below the refuse threshold.
            BudgetSignal.REFUSE at or above the refuse threshold.
        """
        fill = self.fill
        if fill >= self.refuse_at:
            return BudgetSignal.REFUSE
        if fill >= self.compact_at:
            return BudgetSignal.COMPACT
        return BudgetSignal.OK

    def snapshot(
        self,
        *,
        role: str,
        last_message: str | None = None,
    ) -> dict[str, Any]:
        """Return a compact summary for a refusal or a resume.

        The snapshot is what a refusing agent returns so that a caller
        sees the state the run had reached rather than an empty failure.
        A future agent may also persist it to resume the run.

        Args:
            role: The agent role that is refusing or snapshotting.
            last_message: An optional short summary of where the work
                had reached. Used by a resuming agent to pick up.

        Returns:
            A JSON-serialisable dictionary describing the budget.
        """
        return {
            "role": role,
            "fill_percent": round(self.fill * 100, 2),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "window": self.window,
            "compact_at": self.compact_at,
            "refuse_at": self.refuse_at,
            "last_message": last_message,
        }
