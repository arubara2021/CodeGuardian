"""Per-run budget envelope for model calls.

A BudgetEnvelope caps the total cost, token count, and wall-clock time
that a run may spend on model calls. It is mutable: every call records
its cost and tokens, and the envelope answers a pressure query before
the next call is dispatched.

Four pressure levels exist. "ok" allows the call unchanged.
"downgrade" allows the call but signals that the router should send it
to a cheaper tier. "pause" signals that the caller should wait for the
rate-limit window to reset before retrying. "halt" refuses the call.

The degradation policy decides which levels are emitted at 80 percent
of any cap:

- "fail" emits "ok" until the cap is reached, then "halt". Suitable
  for CI, where a run that cannot complete within budget should stop
  rather than silently downgrade.
- "downgrade" emits "downgrade" at 80 percent, then "halt" at 100
  percent. Suitable for interactive runs, where the Tester's
  deterministic pre-check catches most downgrade-induced failures.
- "pause" emits "pause" at 80 percent, then "halt" at 100 percent.
  Suitable for free-tier providers with low tokens-per-minute caps.

A cap of zero means the envelope is exhausted before the first call.
A cap of None means the dimension is uncapped.

BudgetExceeded is a plain Exception, not a CodeGuardianError, because
it is a control-flow signal the router catches rather than an error
that propagates to the user. Adding a CodeGuardianErrorCode for it
would touch a Phase 1 file, which Phase 11 does not modify.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "BudgetEnvelope",
    "BudgetExceeded",
    "BudgetExceededError",
    "PressureLevel",
]


PressureLevel = Literal["ok", "downgrade", "pause", "halt"]

_DOWNGRADE_THRESHOLD = 0.80


class BudgetExceededError(Exception):
    """Raised when a budget envelope cannot afford the next call.

    Caught by the router as a control-flow signal, not propagated to
    the end user. The name carries the Error suffix to satisfy the
    project's exception-naming convention; the semantics are still
    control flow rather than a user-facing failure.

    Attributes:
        role: The agent role that exceeded the budget.
        envelope: The envelope that was exhausted. Carries the spend
            and the caps so the caller can report what happened.
    """

    def __init__(self, *, role: str, envelope: BudgetEnvelope) -> None:
        super().__init__(f"budget exhausted for role {role!r}")
        self.role = role
        self.envelope = envelope


# Backward-compatible alias. Nothing in the codebase imports the old
# name today, but keeping the alias makes the rename non-breaking for
# any external caller that learned the v1.1 name before this fix.
BudgetExceeded = BudgetExceededError


@dataclass(slots=True)
class BudgetEnvelope:
    """Mutable per-run budget for model calls.

    Attributes:
        max_cost_usd: Total cost cap in US dollars, or None for no cap.
        max_tokens: Total token cap, or None for no cap.
        max_wall_clock_s: Wall-clock cap in seconds, or None for no cap.
        degradation_policy: Which pressure level to emit at 80 percent
            of any cap. One of "fail", "downgrade", "pause".
        spent_usd: Cost accumulated so far, in US dollars.
        spent_tokens: Tokens accumulated so far.
        started_at: Monotonic timestamp set at construction.
    """

    max_cost_usd: float | None = None
    max_tokens: int | None = None
    max_wall_clock_s: float | None = None
    degradation_policy: Literal["fail", "downgrade", "pause"] = "downgrade"
    spent_usd: float = 0.0
    spent_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _halted: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.degradation_policy not in ("fail", "downgrade", "pause"):
            raise ValueError(
                f"degradation_policy must be 'fail', 'downgrade', or 'pause'; "
                f"got {self.degradation_policy!r}"
            )
        for name, value in (
            ("max_cost_usd", self.max_cost_usd),
            ("max_tokens", self.max_tokens),
            ("max_wall_clock_s", self.max_wall_clock_s),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative; got {value!r}")

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds since construction."""
        return time.monotonic() - self.started_at

    @property
    def halted(self) -> bool:
        """True once the envelope has emitted "halt" at least once."""
        return self._halted

    def check_pressure(self) -> PressureLevel:
        """Return the current pressure level.

        The first "halt" result marks the envelope halted. Every
        subsequent call returns "halt" without recomputing, so a
        caller cannot accidentally recover by refunding spend.
        """
        if self._halted:
            return "halt"
        level = self._raw_pressure()
        if level == "halt":
            self._halted = True
        return level

    def record(self, *, usd: float = 0.0, tokens: int = 0) -> None:
        """Add spend to the envelope.

        Args:
            usd: Cost in US dollars. Must be non-negative.
            tokens: Tokens consumed. Must be non-negative.

        Raises:
            ValueError: If either argument is negative.
        """
        if usd < 0:
            raise ValueError(f"usd must be non-negative; got {usd!r}")
        if tokens < 0:
            raise ValueError(f"tokens must be non-negative; got {tokens!r}")
        self.spent_usd += usd
        self.spent_tokens += tokens

    def _raw_pressure(self) -> PressureLevel:
        ratio = self._worst_ratio()
        if ratio is None:
            return "ok"
        if ratio >= 1.0:
            return "halt"
        if ratio < _DOWNGRADE_THRESHOLD:
            return "ok"
        if self.degradation_policy == "downgrade":
            return "downgrade"
        if self.degradation_policy == "pause":
            return "pause"
        return "ok"

    def _worst_ratio(self) -> float | None:
        ratios = [
            _ratio(self.spent_usd, self.max_cost_usd),
            _ratio(self.spent_tokens, self.max_tokens),
            _ratio(self.elapsed_s, self.max_wall_clock_s),
        ]
        present = [r for r in ratios if r is not None]
        if not present:
            return None
        return max(present)


def _ratio(spent: float, cap: float | None) -> float | None:
    """Return spent divided by cap, or None when the dimension is uncapped.

    A cap of zero returns infinity so that a zero budget halts the
    envelope before the first call.
    """
    if cap is None:
        return None
    if cap <= 0:
        return math.inf
    return spent / cap
