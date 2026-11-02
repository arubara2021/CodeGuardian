"""Tests for VerifierLoop.

Each test uses small async fakes that return canned results. The loop
never calls a real model or a real verifier; the tests prove the
control flow, the feedback propagation, and the bounded halt.
"""

from __future__ import annotations

import asyncio

import pytest

from codeguardian.core.agents._loop import (
    GenerationResult,
    LoopOutcome,
    VerificationResult,
    VerifierLoop,
)


def _run(coro):
    return asyncio.run(coro)


class TestConstruction:
    def test_default_max_attempts(self) -> None:
        loop = VerifierLoop()
        assert loop.max_attempts == 2

    def test_explicit_max_attempts(self) -> None:
        loop = VerifierLoop(max_attempts=5)
        assert loop.max_attempts == 5

    def test_zero_max_attempts_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be at least 1"):
            VerifierLoop(max_attempts=0)

    def test_negative_max_attempts_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be at least 1"):
            VerifierLoop(max_attempts=-1)


class TestPassOnFirstAttempt:
    def test_passes_without_feedback(self) -> None:
        feedback_received: list[VerificationResult | None] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            feedback_received.append(feedback)
            return GenerationResult(payload="good", attempt=1)

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            return VerificationResult(all_pass=True)

        outcome = _run(VerifierLoop(max_attempts=2).run(generate=generate, verify=verify))
        assert isinstance(outcome, LoopOutcome)
        assert outcome.passed is True
        assert outcome.attempts == 1
        assert outcome.halted_reason is None
        assert outcome.final_generation is not None
        assert outcome.final_generation.payload == "good"
        assert len(outcome.verifications) == 1
        assert outcome.verifications[0].all_pass is True
        assert feedback_received == [None]


class TestRetryOnFailure:
    def test_retries_with_feedback_then_passes(self) -> None:
        feedback_received: list[VerificationResult | None] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            feedback_received.append(feedback)
            payload = "bad" if feedback is None else "fixed"
            return GenerationResult(payload=payload, attempt=len(feedback_received))

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            if gen.payload == "bad":
                return VerificationResult(
                    all_pass=False,
                    failure_summary="bad payload",
                    detail={"reason": "test"},
                )
            return VerificationResult(all_pass=True)

        outcome = _run(VerifierLoop(max_attempts=2).run(generate=generate, verify=verify))
        assert outcome.passed is True
        assert outcome.attempts == 2
        assert outcome.final_generation is not None
        assert outcome.final_generation.payload == "fixed"
        assert feedback_received[0] is None
        assert feedback_received[1] is not None
        assert feedback_received[1].failure_summary == "bad payload"


class TestHaltOnRefusal:
    def test_refusal_halts_immediately(self) -> None:
        verify_calls: list[GenerationResult[str]] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            return GenerationResult(
                payload="",
                attempt=1,
                is_refusal=True,
                refusal_reason="blast score is 100",
            )

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            verify_calls.append(gen)
            return VerificationResult(all_pass=True)

        outcome = _run(VerifierLoop(max_attempts=3).run(generate=generate, verify=verify))
        assert outcome.passed is False
        assert outcome.attempts == 1
        assert verify_calls == []
        assert outcome.halted_reason is not None
        assert "refused" in outcome.halted_reason
        assert "blast score is 100" in outcome.halted_reason


class TestHaltOnExhaustion:
    def test_halts_after_max_attempts(self) -> None:
        generate_calls: list[int] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            generate_calls.append(len(generate_calls) + 1)
            return GenerationResult(
                payload=f"attempt-{len(generate_calls)}",
                attempt=len(generate_calls),
            )

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            return VerificationResult(all_pass=False, failure_summary="always fails")

        outcome = _run(VerifierLoop(max_attempts=3).run(generate=generate, verify=verify))
        assert outcome.passed is False
        assert outcome.attempts == 3
        assert len(outcome.verifications) == 3
        assert outcome.final_generation is not None
        assert outcome.final_generation.payload == "attempt-3"
        assert outcome.halted_reason is not None
        assert "3 attempts" in outcome.halted_reason


class TestAttemptObserver:
    def test_observer_fires_per_verification(self) -> None:
        observed: list[tuple[int, str, bool]] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            return GenerationResult(payload="x", attempt=1)

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            return VerificationResult(all_pass=False)

        def on_attempt(
            attempt: int,
            gen: GenerationResult[str],
            ver: VerificationResult,
        ) -> None:
            observed.append((attempt, gen.payload, ver.all_pass))

        _run(
            VerifierLoop(max_attempts=2).run(
                generate=generate, verify=verify, on_attempt=on_attempt
            )
        )
        assert observed == [(1, "x", False), (2, "x", False)]

    def test_observer_does_not_fire_on_refusal(self) -> None:
        observed: list[int] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            return GenerationResult(payload="", attempt=1, is_refusal=True)

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            return VerificationResult(all_pass=True)

        def on_attempt(
            attempt: int,
            gen: GenerationResult[str],
            ver: VerificationResult,
        ) -> None:
            observed.append(attempt)

        _run(
            VerifierLoop(max_attempts=2).run(
                generate=generate, verify=verify, on_attempt=on_attempt
            )
        )
        assert observed == []


class TestFeedbackPropagation:
    def test_generator_sees_latest_failure(self) -> None:
        received: list[VerificationResult | None] = []

        async def generate(feedback: VerificationResult | None) -> GenerationResult[str]:
            received.append(feedback)
            return GenerationResult(payload="x", attempt=len(received))

        attempt_number = [0]

        async def verify(gen: GenerationResult[str]) -> VerificationResult:
            attempt_number[0] += 1
            return VerificationResult(
                all_pass=False,
                failure_summary=f"failure-{attempt_number[0]}",
            )

        _run(VerifierLoop(max_attempts=3).run(generate=generate, verify=verify))
        assert received[0] is None
        assert received[1] is not None
        assert received[1].failure_summary == "failure-1"
        assert received[2] is not None
        assert received[2].failure_summary == "failure-2"
