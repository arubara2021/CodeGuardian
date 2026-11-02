"""Contract verifier agent.

Receives the diff and one ContractAssertion per caller assumption.
When a SandboxExecutor and a contract test path are supplied, runs
the test file and treats its exit code as conclusive evidence. When
no test path is supplied, the verifier stays model-only and the
receipt is None.

Synthesis of the contract test file is the caller's responsibility in
this block. A later block adds an in-verifier synthesizer that builds
the file from the assertions. This block establishes the execution
path and the evidence contract; the synthesis that produces the file
comes later.

Independence. The verifier does not see the correctness test result,
the original source, or the security signal list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.plugins import BudgetTracker
from codeguardian.core.prompts import VerdictOutput, render_prompt, validate_output
from codeguardian.interfaces.model import ModelOptions, ModelProvider
from codeguardian.interfaces.verifier import ContractAssertion

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
    "ContractVerifier",
    "ContractVerifierInput",
    "ContractVerifierResult",
]


@dataclass(frozen=True, slots=True)
class ContractVerifierInput:
    """Input to the contract verifier."""

    __test__: ClassVar[bool] = False

    diff: str
    change_kind: str
    blast_score: int
    contract_assertions: tuple[ContractAssertion, ...]
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_symbol: str = ""
    prompt_version: str = "1.0.0"
    sandbox_executor: SandboxExecutor | None = None
    contract_test_path: str | None = None


@dataclass(frozen=True, slots=True)
class ContractVerifierResult:
    """Output of the contract verifier.

    The evidence fields are additive. Callers that do not supply a
    sandbox or test path see evidence_receipt is None,
    counterexample is None, and evidence_conflict is False.
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


class ContractVerifier:
    """Model-backed contract verifier."""

    name = "contract_verifier"
    phase = "contract_verifier"
    is_deterministic = False
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role = "contract_verifier"
    prompt_version = "1.0.0"

    async def run(self, input: ContractVerifierInput) -> ContractVerifierResult:
        """Run the contract verifier."""
        pre_scan = _compute_pre_scan(input.diff, input.contract_assertions)
        annotated_diff = _annotate_diff(input.diff, pre_scan)

        assertions_text = _format_assertions(input.contract_assertions)
        variables = {
            "change_kind": input.change_kind,
            "blast_score": str(input.blast_score),
            "contract_assertions": assertions_text,
            "diff": annotated_diff,
        }
        prompt_result = render_prompt("contract_verifier", input.prompt_version, variables)

        response, latency_ms = await tracked_model_call(
            input.provider,
            prompt_result.rendered,
            input.model,
            ModelOptions(),
            "contract_verifier",
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
            validated = validate_output("contract_verifier", parsed)
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
        pre_scan_conflict = validated.verdict == "pass" and any(
            not entry["in_diff"] for entry in pre_scan["assertions"]
        )
        final_verdict, evidence_conflict = apply_evidence(validated, evidence_passed)
        final_verdict = apply_veto(final_verdict, pre_scan_conflict)

        verdict_reason: str | None = None
        if final_verdict.verdict == "fail":
            verdict_reason = "contract_broken"

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

        return ContractVerifierResult(
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
                contract_assertion_severities=tuple(a.severity for a in input.contract_assertions),
            ),
            informed_verdict=informed_verdict,
        )


def _run_evidence_check(
    input: ContractVerifierInput,
) -> tuple[EvidenceReceipt | None, str | None, bool | None]:
    """Run the contract test file when sandbox access is available.

    Returns:
        A three-tuple of (receipt, counterexample, evidence_passed).
        All three are None / None / None when the caller did not
        supply a sandbox or a test path.
    """
    if input.sandbox_executor is None or input.contract_test_path is None:
        return None, None, None

    command = f"python3 run_pytest.py {input.contract_test_path} -v --tb=short"
    receipt: EvidenceReceipt = {
        "evidence_ids": ["contract_test"],
        "artifacts_checked": [input.contract_test_path],
        "commands_run": [command],
    }

    try:
        outcome, counterexample = execute_characterization_test(
            input.sandbox_executor,
            input.contract_test_path,
        )
    except Exception:
        return receipt, None, None

    if outcome.exit_code == 0:
        return receipt, None, True
    if outcome.exit_code == 1:
        return receipt, counterexample, False
    return receipt, None, None


def _compute_pre_scan(
    diff: str,
    assertions: tuple[ContractAssertion, ...],
) -> dict[str, Any]:
    """Return diff stats plus per-assertion in_diff flags."""
    stats = diff_stats(diff)
    annotated: list[dict[str, Any]] = []
    for assertion in assertions:
        in_diff = _assertion_in_diff(assertion, diff)
        annotated.append(
            {
                "caller": assertion.caller,
                "assumption": assertion.assumption,
                "in_diff": in_diff,
            }
        )
    return {"diff_stats": stats, "assertions": annotated}


def _assertion_in_diff(assertion: ContractAssertion, diff: str) -> bool:
    """Return True if the assertion or its caller name appears in the diff."""
    if assertion.test_expression and assertion.test_expression in diff:
        return True
    caller_tail = assertion.caller.split(".")[-1] if assertion.caller else ""
    return bool(caller_tail and caller_tail in diff)


def _annotate_diff(diff: str, pre_scan: dict[str, Any]) -> str:
    """Prepend pre-scan evidence to the diff."""
    evidence: list[str] = []
    for entry in pre_scan["assertions"]:
        in_diff = "yes" if entry["in_diff"] else "no"
        evidence.append(f"# assertion: caller={entry['caller']}, in_diff={in_diff}")
    return assemble_annotated_diff(diff, pre_scan["diff_stats"], evidence)


def _format_assertions(assertions: tuple[ContractAssertion, ...]) -> str:
    """Format the assertions block for the contract_verifier prompt."""
    if not assertions:
        return "(no contract assertions provided)"
    lines: list[str] = []
    for i, a in enumerate(assertions, 1):
        lines.append(f"{i}. caller: {a.caller}")
        lines.append(f"   assumption: {a.assumption}")
        if a.test_expression:
            lines.append(f"   test: {a.test_expression}")
    return "\n".join(lines)


def _schema_refusal(reason: str, pre_scan: dict[str, Any]) -> ContractVerifierResult:
    """Return a schema-refusal result."""
    return ContractVerifierResult(
        verdict=schema_refusal_verdict("contract_verifier", reason),
        verdict_reason=None,
        pre_scan=pre_scan,
        pre_scan_conflict=False,
        is_refusal=True,
    )


def _record(
    input: ContractVerifierInput,
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
        role="contract_verifier",
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
        prompt_role="contract_verifier",
        prompt_version=input.prompt_version,
        model_options={"temperature": 0.0},
        evidence_receipt=evidence_receipt,
    )
