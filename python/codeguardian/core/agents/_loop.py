"""Bounded verifier repair loop.

On verifier failure the loop re-invokes generation with the failure as
feedback. Bounded at K attempts (default 2). The loop does not call
models or tools itself — it composes two caller-supplied coroutines and
records each attempt through an optional observer.

The loop is the one place in Phase 11 where a failed verification
becomes actionable feedback rather than a terminal stop. It exists so
that a caller (the deep test today, Phase 13 orchestration tomorrow)
can express "try again with the failure" without duplicating the
bounded-retry logic.

Design notes:

- The generator receives None on the first attempt and a
  VerificationResult on every subsequent attempt. A refusal halts the
  loop immediately: refusals are safety or resource signals, not
  quality failures.
- The verifier receives a GenerationResult and returns a
  VerificationResult. It is called once per generated attempt, in
  order.
- The optional on_attempt callback fires after each verification.
  Phase 13 will use it to write one decision_log row per attempt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

__all__ = [
    "GenerationResult",
    "LoopOutcome",
    "VerificationResult",
    "VerifierLoop",
]


GenT = TypeVar("GenT")


@dataclass(frozen=True, slots=True)
class GenerationResult[GenT]:
    """One generator attempt's output.

    Attributes:
        payload: The generator-specific output (candidate source,
            RefactorSpec, and so on).
        attempt: The one-based attempt number this result belongs to.
        is_refusal: True when the generator refused the task. Refusals
            halt the loop without being treated as quality failures.
        refusal_reason: The reason given for a refusal, or None.
    """

    payload: GenT
    attempt: int
    is_refusal: bool = False
    refusal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """One verifier attempt's output.

    Attributes:
        all_pass: True when the verifier accepted the generation.
        failure_summary: A one-line summary of what failed, passed to
            the next generation attempt as feedback. None when
            all_pass is True.
        detail: Structured evidence (verifier verdicts, test results,
            and so on). Not used by the loop; available to the caller
            through LoopOutcome.verifications.
    """

    all_pass: bool
    failure_summary: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoopOutcome[GenT]:
    """Final result of a verifier loop run.

    Attributes:
        final_generation: The last generation produced, or None if the
            generator never ran (for example, when max_attempts is
            zero, which cannot happen through the public constructor).
        verifications: Every verification attempt, in order. Length
            equals attempts when the loop did not halt on a refusal,
            and equals attempts minus one when it did.
        passed: True when a verification returned all_pass.
        attempts: The number of generation attempts made.
        halted_reason: A human-readable reason when passed is False, or
            None when passed is True.
    """

    final_generation: GenerationResult[GenT] | None
    verifications: tuple[VerificationResult, ...]
    passed: bool
    attempts: int
    halted_reason: str | None


AttemptObserver = Callable[[int, GenerationResult[object], VerificationResult], None]


class VerifierLoop:
    """Bounded generate-verify loop.

    Args:
        max_attempts: The maximum number of generation attempts.
            Must be at least 1. Defaults to 2.

    Raises:
        ValueError: If max_attempts is less than 1.
    """

    def __init__(self, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1; got {max_attempts}")
        self._max_attempts = max_attempts

    @property
    def max_attempts(self) -> int:
        """The configured maximum number of generation attempts."""
        return self._max_attempts

    async def run(
        self,
        *,
        generate: Callable[
            [VerificationResult | None],
            Awaitable[GenerationResult[GenT]],
        ],
        verify: Callable[
            [GenerationResult[GenT]],
            Awaitable[VerificationResult],
        ],
        on_attempt: Callable[[int, GenerationResult[GenT], VerificationResult], None] | None = None,
    ) -> LoopOutcome[GenT]:
        """Run the loop until verification passes or attempts exhaust.

        Args:
            generate: Coroutine that produces a GenerationResult. On
                the first call it receives None. On each subsequent
                call it receives the previous VerificationResult so the
                generator can address the reported failure.
            verify: Coroutine that takes a GenerationResult and returns
                a VerificationResult.
            on_attempt: Optional observer called after each successful
                verification. The loop halts immediately on a
                generation refusal, so the observer does not fire when
                a refusal occurs.

        Returns:
            A LoopOutcome. When passed is True the final generation is
            the one the verifier accepted. When passed is False the
            final generation is the last one produced and the
            halted_reason explains why the loop stopped.
        """
        verifications: list[VerificationResult] = []
        last_generation: GenerationResult[GenT] | None = None
        feedback: VerificationResult | None = None

        for attempt in range(1, self._max_attempts + 1):
            generation = await generate(feedback)
            last_generation = generation

            if generation.is_refusal:
                return LoopOutcome(
                    final_generation=generation,
                    verifications=tuple(verifications),
                    passed=False,
                    attempts=attempt,
                    halted_reason=(
                        "generator refused: " + (generation.refusal_reason or "no reason given")
                    ),
                )

            verification = await verify(generation)
            verifications.append(verification)

            if on_attempt is not None:
                on_attempt(attempt, generation, verification)

            if verification.all_pass:
                return LoopOutcome(
                    final_generation=generation,
                    verifications=tuple(verifications),
                    passed=True,
                    attempts=attempt,
                    halted_reason=None,
                )

            feedback = verification

        return LoopOutcome(
            final_generation=last_generation,
            verifications=tuple(verifications),
            passed=False,
            attempts=self._max_attempts,
            halted_reason=(f"verification failed after {self._max_attempts} attempts"),
        )
