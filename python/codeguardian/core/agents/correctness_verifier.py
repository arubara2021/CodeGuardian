"""Correctness verifier agent.

Receives the diff plus the characterization test result. When a
SandboxExecutor is supplied, also runs the characterization test
itself and treats the execution result as conclusive evidence that
outranks the model. When no executor is supplied, the verifier falls
back to its text-only pre-scan and the model verdict stands.

The evidence-first rule: a passing test confirms pass, a failing test
confirms fail with the extracted counterexample, and any other exit
code is inconclusive. When evidence and model disagree, evidence wins
and the conflict is reported on the result.

Independence. The verifier does not see the original code, the
writer's reasoning, or the other verifiers' evidence. Its evidence
slice is the diff and the test result. Independence is enforced by
the evidence slice, not by prompt instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.plugins import BudgetTracker
from codeguardian.core.prompts import VerdictOutput, render_prompt, validate_output
from codeguardian.interfaces.model import ModelOptions, ModelProvider

from ._runtime import tracked_model_call
from ._verifier_common import (
    SandboxExecutor,
    apply_evidence,
    apply_veto,
    assemble_annotated_diff,
    compute_blind_prediction,
    diff_stats,
    execute_characterization_test,
    parse_and_strip,
    schema_refusal_verdict,
)
from .telemetry import EvidenceReceipt, record_call

__all__ = [
    "CorrectnessVerifier",
    "CorrectnessVerifierInput",
    "CorrectnessVerifierResult",
]


@dataclass(frozen=True, slots=True)
class CorrectnessVerifierInput:
    """Input to the correctness verifier."""

    __test__: ClassVar[bool] = False

    diff: str
    change_kind: str
    blast_score: int
    test_result: str
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_symbol: str = ""
    prompt_version: str = "1.0.0"
    sandbox_executor: SandboxExecutor | None = None
    characterization_test_path: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectnessVerifierResult:
    """Output of the correctness verifier.

    The evidence fields are additive. Callers that do not supply a
    sandbox see evidence_receipt is None, counterexample is None,
    and evidence_conflict is False.
    """

    verdict: VerdictOutput
    verdict_reason: str | None
    pre_scan: dict[str, Any]
    pre_scan_conflict: bool
    refusal_code: str | None = None
    is_refusal: bool = False
    evidence_receipt: EvidenceReceipt | None = None
    counterexample: str | None = None
    evidence_conflict: bool = False
    blind_prediction: str | None = None
    informed_verdict: str | None = None

    @property
    def disagreement(self) -> bool:
        """True when the blind prediction and the informed verdict differ."""
        if self.blind_prediction is None or self.informed_verdict is None:
            return False
        return self.blind_prediction != self.informed_verdict


class CorrectnessVerifier:
    """Model-backed correctness verifier."""

    name = "correctness_verifier"
    phase = "correctness_verifier"
    is_deterministic = False
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role = "correctness_verifier"
    prompt_version = "1.0.0"

    async def run(self, input: CorrectnessVerifierInput) -> CorrectnessVerifierResult:
        """Run the correctness verifier."""
        pre_scan = _compute_pre_scan(input.diff, input.test_result)
        annotated_diff = _annotate_diff(input.diff, pre_scan)

        variables = {
            "change_kind": input.change_kind,
            "blast_score": str(input.blast_score),
            "diff": annotated_diff,
        }
        prompt_result = render_prompt("correctness_verifier", input.prompt_version, variables)

        response, latency_ms = await tracked_model_call(
            input.provider,
            prompt_result.rendered,
            input.model,
            ModelOptions(),
            "correctness_verifier",
        )

        parsed, refusal_code, reason_error = parse_and_strip(response.text)
        if reason_error is not None:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal(reason_error, pre_scan)

        try:
            validated = validate_output("correctness_verifier", parsed)
        except CodeGuardianError as exc:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal(f"schema: {exc}", pre_scan)

        if not isinstance(validated, VerdictOutput):
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal("unexpected output type", pre_scan)

        receipt, counterexample, evidence_passed = _run_evidence_check(input)

        informed_verdict = validated.verdict
        pre_scan_conflict = validated.verdict == "pass" and _pre_scan_has_signal(pre_scan)
        final_verdict, evidence_conflict = apply_evidence(validated, evidence_passed)
        final_verdict = apply_veto(final_verdict, pre_scan_conflict)

        verdict_reason: str | None = None
        if final_verdict.verdict == "fail":
            verdict_reason = "behavior_drift"

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
            verdict_reason=verdict_reason,
            evidence_receipt=receipt,
        )

        return CorrectnessVerifierResult(
            verdict=final_verdict,
            verdict_reason=verdict_reason,
            pre_scan=pre_scan,
            pre_scan_conflict=pre_scan_conflict,
            refusal_code=refusal_code,
            is_refusal=final_verdict.verdict == "refused",
            evidence_receipt=receipt,
            counterexample=counterexample,
            evidence_conflict=evidence_conflict,
            blind_prediction=compute_blind_prediction(
                test_result=input.test_result,
            ),
            informed_verdict=informed_verdict,
        )


def _run_evidence_check(
    input: CorrectnessVerifierInput,
) -> tuple[EvidenceReceipt | None, str | None, bool | None]:
    """Run the characterization test when sandbox access is available.

    Returns:
        A three-tuple of (receipt, counterexample, evidence_passed).
        All three are None / None / None when the caller did not supply
        a sandbox or a test path. When the executor raises or returns
        an inconclusive exit code, the receipt is populated for
        observability but evidence_passed is None so the model verdict
        stands.
    """
    if input.sandbox_executor is None or input.characterization_test_path is None:
        return None, None, None

    command = f"python3 run_pytest.py {input.characterization_test_path} -v --tb=short"
    receipt: EvidenceReceipt = {
        "evidence_ids": ["characterization_test"],
        "artifacts_checked": [input.characterization_test_path],
        "commands_run": [command],
    }

    try:
        outcome, counterexample = execute_characterization_test(
            input.sandbox_executor,
            input.characterization_test_path,
        )
    except Exception:
        return receipt, None, None

    if outcome.exit_code == 0:
        return receipt, None, True
    if outcome.exit_code == 1:
        return receipt, counterexample, False
    return receipt, None, None


def _compute_pre_scan(diff: str, test_result: str) -> dict[str, Any]:
    """Return the deterministic pre-scan for the correctness verifier."""
    return {
        "diff_stats": diff_stats(diff),
        "test_result": test_result,
    }


def _annotate_diff(diff: str, pre_scan: dict[str, Any]) -> str:
    """Prepend pre-scan evidence to the diff as a comment block."""
    evidence = [f"# test_result: {pre_scan['test_result']}"]
    return assemble_annotated_diff(diff, pre_scan["diff_stats"], evidence)


def _pre_scan_has_signal(pre_scan: dict[str, Any]) -> bool:
    """Return True when the pre-scan shows something the model should address."""
    test = pre_scan.get("test_result", "")
    return isinstance(test, str) and test.startswith("FAIL")


def _schema_refusal(reason: str, pre_scan: dict[str, Any]) -> CorrectnessVerifierResult:
    """Return a schema-refusal result."""
    return CorrectnessVerifierResult(
        verdict=schema_refusal_verdict("correctness_verifier", reason),
        verdict_reason=None,
        pre_scan=pre_scan,
        pre_scan_conflict=False,
        is_refusal=True,
    )


def _record(
    input: CorrectnessVerifierInput,
    prompt: str,
    response: Any,
    latency_ms: int,
    *,
    outcome: str,
    failure_category: Any,
    verdict_reason: Any = None,
    evidence_receipt: EvidenceReceipt | None = None,
) -> None:
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="correctness_verifier",
        outcome=outcome,
        failure_category=failure_category,
        verdict_reason=verdict_reason,
        target_symbol=input.target_symbol or None,
        prompt=prompt,
        output=response.text,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        latency_ms=latency_ms,
        model_id=response.model_id,
        prompt_role="correctness_verifier",
        prompt_version=input.prompt_version,
        model_options={"temperature": 0.0},
        evidence_receipt=evidence_receipt,
    )
