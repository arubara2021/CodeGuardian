"""Contract verifier agent.

Receives the diff and one ContractAssertion per caller assumption.
Checks each assertion against the diff. The deterministic pre-scan
records whether each assertion has a corresponding line in the diff,
so the model can see which assertions the refactor touched.

Independence. The verifier does not see the correctness test result,
the original source, or the security signal list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.plugins import BudgetTracker
from codeguardian.core.prompts import VerdictOutput, render_prompt, validate_output
from codeguardian.interfaces.model import ModelOptions, ModelProvider
from codeguardian.interfaces.verifier import ContractAssertion

from ._runtime import tracked_model_call
from .telemetry import record_call

__all__ = [
    "ContractVerifier",
    "ContractVerifierInput",
    "ContractVerifierResult",
]


_VALID_REFUSAL_CODES = frozenset(
    {
        "target_missing",
        "directive_ambiguous",
        "context_contradicts",
        "tool_unavailable",
        "budget_exhausted",
        "unsupported_language",
    }
)

_HUNK_RE = re.compile(r"^@@ .* @@", re.MULTILINE)


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


@dataclass(frozen=True, slots=True)
class ContractVerifierResult:
    """Output of the contract verifier."""

    verdict: VerdictOutput
    verdict_reason: str | None
    pre_scan: dict[str, Any]
    pre_scan_conflict: bool
    refusal_code: str | None = None
    is_refusal: bool = False


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

        parsed, refusal_code, reason_error = _parse_and_strip(response.text)
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

        verdict_reason: str | None = "contract_broken" if validated.verdict == "fail" else None
        pre_scan_conflict = validated.verdict == "pass" and any(
            not entry["in_diff"] for entry in pre_scan["assertions"]
        )

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
            verdict_reason=verdict_reason,
        )

        return ContractVerifierResult(
            verdict=validated,
            verdict_reason=verdict_reason,
            pre_scan=pre_scan,
            pre_scan_conflict=pre_scan_conflict,
            refusal_code=refusal_code,
            is_refusal=validated.verdict == "refused",
        )


def _compute_pre_scan(diff: str, assertions: tuple[ContractAssertion, ...]) -> dict[str, Any]:
    """Return diff stats plus per-assertion in_diff flags."""
    stats = _diff_stats(diff)
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
    stats = pre_scan["diff_stats"]
    lines = [
        "# PRE-SCAN EVIDENCE",
        f"# diff_stats: +{stats['lines_added']} -{stats['lines_removed']} "
        f"across {stats['hunks']} hunks in {len(stats['files'])} files",
    ]
    for entry in pre_scan["assertions"]:
        in_diff = "yes" if entry["in_diff"] else "no"
        lines.append(f"# assertion: caller={entry['caller']}, in_diff={in_diff}")
    lines.append("# ---")
    lines.append(diff)
    return "\n".join(lines)


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


def _diff_stats(diff: str) -> dict[str, Any]:
    """Return added/removed line counts, hunk count, and file list."""
    lines_added = 0
    lines_removed = 0
    files: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            if line.startswith("+++ "):
                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                files.add(path)
            continue
        if line.startswith("+"):
            lines_added += 1
        elif line.startswith("-"):
            lines_removed += 1
    return {
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "hunks": len(_HUNK_RE.findall(diff)),
        "files": sorted(files),
    }


def _parse_and_strip(text: str) -> tuple[dict[str, Any], str | None, str | None]:
    """Parse JSON, validate reason_code, strip it."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, None, f"invalid JSON: {exc}"

    if not isinstance(parsed, dict):
        return {}, None, "model output is not a JSON object"

    reason_code = parsed.pop("reason_code", None)
    if reason_code is not None and reason_code not in _VALID_REFUSAL_CODES:
        return (
            {},
            None,
            f"invalid reason_code {reason_code!r}; valid: {sorted(_VALID_REFUSAL_CODES)}",
        )

    return parsed, reason_code, None


def _schema_refusal(reason: str, pre_scan: dict[str, Any]) -> ContractVerifierResult:
    """Return a schema-refusal result."""
    return ContractVerifierResult(
        verdict=VerdictOutput(
            role="contract_verifier",
            verdict="refused",
            confidence=None,
            findings=(),
            reason=f"schema: {reason}",
        ),
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
    )
