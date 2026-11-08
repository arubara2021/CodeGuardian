"""Tests for the deterministic gate and the verifier stack."""

from __future__ import annotations

import asyncio
from typing import Any

from codeguardian.core.agents._loop import GenerationResult
from codeguardian.core.agents._verifier_common import ExecutionOutcome
from codeguardian.core.orchestrator.deterministic_gate import (
    CheckOutcome,
    DeterministicGateResult,
    deterministic_gate,
)
from codeguardian.core.orchestrator.verifier_stack import (
    DEFAULT_K_BY_BAND,
    run_verifier_stack,
)


class FakeGateSandbox:
    """A sandbox the deterministic gate can drive in tests."""

    def __init__(self, run_results: list[ExecutionOutcome] | None = None) -> None:
        self.run_results = list(run_results or [])
        self.writes: list[tuple[str, str]] = []
        self.runs: list[list[str]] = []

    def write_file(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    def run(self, argv: list[str], *, timeout_s: float = 120.0) -> ExecutionOutcome:
        self.runs.append(list(argv))
        if self.run_results:
            return self.run_results.pop(0)
        return ExecutionOutcome(exit_code=0, stdout="", stderr="")


class FakeVerdict:
    def __init__(self, value: str, verifier_type: str = "correctness") -> None:
        self.verdict = value
        self.verifier_type = verifier_type


class FakeVerifierResult:
    def __init__(
        self,
        value: str,
        verifier_type: str = "correctness",
        reason: str | None = None,
    ) -> None:
        self.verdict = FakeVerdict(value, verifier_type)
        self.verdict_reason = reason


def passing_verifier(verifier_type: str = "correctness") -> FakeVerifierResult:
    return FakeVerifierResult("pass", verifier_type)


def failing_verifier(verifier_type: str = "correctness", reason: str = "bad") -> FakeVerifierResult:
    return FakeVerifierResult("fail", verifier_type, reason)


def passing_gate() -> DeterministicGateResult:
    return DeterministicGateResult(
        passed=True,
        checks=(CheckOutcome(name="git apply", status="pass"),),
        stderr_tail="",
        diff_hash="h",
    )


def failing_gate(reason: str = "git apply failed") -> DeterministicGateResult:
    return DeterministicGateResult(
        passed=False,
        checks=(CheckOutcome(name="git apply", status="fail", detail=reason),),
        stderr_tail=reason,
        diff_hash="h",
    )


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)


class TestDeterministicGate:
    def test_no_executor_skips(self) -> None:
        result = deterministic_gate(executor=None, diff="some diff", test_path=None)
        assert result.passed is True
        assert result.skip_reason() is not None

    def test_empty_diff_fails(self) -> None:
        sandbox = FakeGateSandbox()
        result = deterministic_gate(executor=sandbox, diff="", test_path=None)
        assert result.passed is False
        assert "empty diff" in result.stderr_tail.lower()

    def test_git_check_passes(self) -> None:
        sandbox = FakeGateSandbox()
        result = deterministic_gate(
            executor=sandbox,
            diff="--- a\n+++ b\n",
            test_path=None,
        )
        assert result.passed is True
        assert len(sandbox.writes) == 1
        assert len(sandbox.runs) == 1
        assert sandbox.runs[0][:2] == ["git", "apply"]

    def test_git_check_fails(self) -> None:
        sandbox = FakeGateSandbox(
            [ExecutionOutcome(exit_code=1, stdout="", stderr="patch does not apply")]
        )
        result = deterministic_gate(
            executor=sandbox,
            diff="--- a\n+++ b\n",
            test_path=None,
        )
        assert result.passed is False
        assert "patch does not apply" in result.stderr_tail

    def test_characterization_test_passes(self) -> None:
        sandbox = FakeGateSandbox(
            [
                ExecutionOutcome(exit_code=0, stdout="", stderr=""),
                ExecutionOutcome(exit_code=0, stdout="1 passed", stderr=""),
            ]
        )
        applied: list[str] = []
        rolled_back: list[str] = []

        def apply(diff: str) -> str:
            applied.append(diff)
            return "ref1"

        def rollback(ref: str) -> None:
            rolled_back.append(ref)

        result = deterministic_gate(
            executor=sandbox,
            diff="--- a\n+++ b\n",
            test_path="test_foo.py",
            apply_fn=apply,
            rollback_fn=rollback,
        )
        assert result.passed is True
        assert applied == ["--- a\n+++ b\n"]
        assert rolled_back == ["ref1"]

    def test_characterization_test_fails(self) -> None:
        sandbox = FakeGateSandbox(
            [
                ExecutionOutcome(exit_code=0, stdout="", stderr=""),
                ExecutionOutcome(exit_code=1, stdout="", stderr="AssertionError: drift"),
            ]
        )
        applied: list[str] = []
        rolled_back: list[str] = []

        def apply(diff: str) -> str:
            applied.append(diff)
            return "ref1"

        def rollback(ref: str) -> None:
            rolled_back.append(ref)

        result = deterministic_gate(
            executor=sandbox,
            diff="--- a\n+++ b\n",
            test_path="test_foo.py",
            apply_fn=apply,
            rollback_fn=rollback,
        )
        assert result.passed is False
        assert "AssertionError" in result.stderr_tail
        assert rolled_back == ["ref1"]


class TestKByBand:
    def test_proceed_is_1(self) -> None:
        assert DEFAULT_K_BY_BAND["proceed"] == 1

    def test_review_is_2(self) -> None:
        assert DEFAULT_K_BY_BAND["review"] == 2

    def test_block_is_2(self) -> None:
        assert DEFAULT_K_BY_BAND["block"] == 2


class TestVerifierStack:
    def test_passes_first_attempt(self) -> None:
        gen_calls = 0
        gate_calls = 0
        apply_calls: list[str] = []
        verify_calls = 0
        rollback_calls: list[str] = []

        async def generate(feedback: Any) -> GenerationResult[Any]:
            nonlocal gen_calls
            gen_calls += 1
            return GenerationResult(payload="c1", attempt=gen_calls)

        def gate(payload: Any) -> DeterministicGateResult:
            nonlocal gate_calls
            gate_calls += 1
            return passing_gate()

        async def apply(payload: Any) -> str:
            apply_calls.append(payload)
            return f"ref_{payload}"

        async def verify(payload: Any) -> tuple[Any, ...]:
            nonlocal verify_calls
            verify_calls += 1
            return (passing_verifier(),)

        async def rollback(ref: str) -> None:
            rollback_calls.append(ref)

        result = run_async(
            run_verifier_stack(
                band="proceed",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
        )
        assert result.passed is True
        assert result.k == 1
        assert result.attempts == 1
        assert gen_calls == 1
        assert gate_calls == 1
        assert apply_calls == ["c1"]
        assert verify_calls == 1
        assert rollback_calls == []

    def test_retries_with_feedback(self) -> None:
        feedbacks: list[Any] = []

        async def generate(feedback: Any) -> GenerationResult[Any]:
            feedbacks.append(feedback)
            if feedback is None:
                return GenerationResult(payload="bad", attempt=1)
            return GenerationResult(payload="good", attempt=2)

        def gate(payload: Any) -> DeterministicGateResult:
            return passing_gate()

        async def apply(payload: Any) -> str:
            return f"ref_{payload}"

        async def verify(payload: Any) -> tuple[Any, ...]:
            if payload == "good":
                return (passing_verifier(),)
            return (failing_verifier(reason="first attempt failed"),)

        async def rollback(ref: str) -> None:
            pass

        result = run_async(
            run_verifier_stack(
                band="review",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
        )
        assert result.passed is True
        assert result.attempts == 2
        assert feedbacks[0] is None
        assert feedbacks[1] is not None
        assert "first attempt failed" in (feedbacks[1].failure_summary or "")

    def test_gate_failure_retries_without_apply(self) -> None:
        attempts = 0
        apply_calls = 0

        async def generate(feedback: Any) -> GenerationResult[Any]:
            nonlocal attempts
            attempts += 1
            return GenerationResult(payload=f"c{attempts}", attempt=attempts)

        def gate(payload: Any) -> DeterministicGateResult:
            return failing_gate("diff does not apply")

        async def apply(payload: Any) -> str:
            nonlocal apply_calls
            apply_calls += 1
            return f"ref_{payload}"

        async def verify(payload: Any) -> tuple[Any, ...]:
            return (passing_verifier(),)

        async def rollback(ref: str) -> None:
            pass

        result = run_async(
            run_verifier_stack(
                band="review",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
        )
        assert result.passed is False
        assert result.attempts == 2
        assert apply_calls == 0
        assert result.halted_reason is not None

    def test_rollback_between_rounds(self) -> None:
        rollback_calls: list[str] = []

        async def generate(feedback: Any) -> GenerationResult[Any]:
            attempt = 1 if feedback is None else 2
            return GenerationResult(payload=f"c{attempt}", attempt=attempt)

        def gate(payload: Any) -> DeterministicGateResult:
            return passing_gate()

        async def apply(payload: Any) -> str:
            return f"ref_{payload}"

        async def verify(payload: Any) -> tuple[Any, ...]:
            return (failing_verifier(reason="always fails"),)

        async def rollback(ref: str) -> None:
            rollback_calls.append(ref)

        result = run_async(
            run_verifier_stack(
                band="review",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
        )
        assert result.passed is False
        assert rollback_calls == ["ref_c1", "ref_c2"]

    def test_writer_refusal_halts_immediately(self) -> None:
        verify_calls = 0

        async def generate(feedback: Any) -> GenerationResult[Any]:
            return GenerationResult(
                payload=None,
                attempt=1,
                is_refusal=True,
                refusal_reason="cannot refactor",
            )

        def gate(payload: Any) -> DeterministicGateResult:
            return passing_gate()

        async def apply(payload: Any) -> str:
            return "ref"

        async def verify(payload: Any) -> tuple[Any, ...]:
            nonlocal verify_calls
            verify_calls += 1
            return (passing_verifier(),)

        async def rollback(ref: str) -> None:
            pass

        result = run_async(
            run_verifier_stack(
                band="review",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
        )
        assert result.passed is False
        assert result.attempts == 1
        assert verify_calls == 0
        assert "cannot refactor" in (result.halted_reason or "")

    def test_escalation_required_on_block_failure(self) -> None:
        async def generate(feedback: Any) -> GenerationResult[Any]:
            attempt = 1 if feedback is None else 2
            return GenerationResult(payload=f"c{attempt}", attempt=attempt)

        def gate(payload: Any) -> DeterministicGateResult:
            return passing_gate()

        async def apply(payload: Any) -> str:
            return f"ref_{payload}"

        async def verify(payload: Any) -> tuple[Any, ...]:
            return (failing_verifier(reason="still fails"),)

        async def rollback(ref: str) -> None:
            pass

        result = run_async(
            run_verifier_stack(
                band="block",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
        )
        assert result.passed is False
        assert result.k == 2
        assert result.escalation_required is True

    def test_k_override(self) -> None:
        attempts = 0

        async def generate(feedback: Any) -> GenerationResult[Any]:
            nonlocal attempts
            attempts += 1
            return GenerationResult(payload=f"c{attempts}", attempt=attempts)

        def gate(payload: Any) -> DeterministicGateResult:
            return passing_gate()

        async def apply(payload: Any) -> str:
            return f"ref_{payload}"

        async def verify(payload: Any) -> tuple[Any, ...]:
            return (failing_verifier(),)

        async def rollback(ref: str) -> None:
            pass

        result = run_async(
            run_verifier_stack(
                band="proceed",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
                k_override=3,
            )
        )
        assert result.k == 3
        assert result.attempts == 3
