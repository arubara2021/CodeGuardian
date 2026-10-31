"""Correctness verifier agent.

Receives the diff plus the characterization test result. Runs a
deterministic pre-scan (diff stats, Python syntax check per file,
test result) and prepends it to the diff the model sees. Requires the
model to address any pre-scan signal that contradicts a pass verdict.

Independence. The verifier does not see the original code, the writer's
reasoning, or the other verifiers' evidence. Its evidence slice is the
diff and the test result only. Independence is enforced by the evidence
slice, not by prompt instruction.
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

from ._runtime import tracked_model_call
from .telemetry import record_call

__all__ = [
    "CorrectnessVerifier",
    "CorrectnessVerifierInput",
    "CorrectnessVerifierResult",
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
class CorrectnessVerifierInput:
    """Input to the correctness verifier."""

    __test__: ClassVar[bool] = False

    diff: str
    change_kind: str
    blast_score: int
    test_result: str  # "PASS" or "FAIL: <assertion>"
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_symbol: str = ""
    prompt_version: str = "1.0.0"


@dataclass(frozen=True, slots=True)
class CorrectnessVerifierResult:
    """Output of the correctness verifier."""

    verdict: VerdictOutput
    verdict_reason: str | None
    pre_scan: dict[str, Any]
    pre_scan_conflict: bool
    refusal_code: str | None = None
    is_refusal: bool = False


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
        """Run the correctness verifier.

        Raises:
            CodeGuardianError: Propagated from the model provider on
                failures that are not schema violations.
        """
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

        verdict_reason: str | None = "behavior_drift" if validated.verdict == "fail" else None
        pre_scan_conflict = validated.verdict == "pass" and _pre_scan_has_signal(pre_scan)

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
            verdict_reason=verdict_reason,
        )

        return CorrectnessVerifierResult(
            verdict=validated,
            verdict_reason=verdict_reason,
            pre_scan=pre_scan,
            pre_scan_conflict=pre_scan_conflict,
            refusal_code=refusal_code,
            is_refusal=validated.verdict == "refused",
        )


def _compute_pre_scan(diff: str, test_result: str) -> dict[str, Any]:
    """Return the deterministic pre-scan for the correctness verifier."""
    stats = _diff_stats(diff)
    return {
        "diff_stats": stats,
        "test_result": test_result,
    }


def _annotate_diff(diff: str, pre_scan: dict[str, Any]) -> str:
    """Prepend pre-scan evidence to the diff as a comment block."""
    stats = pre_scan["diff_stats"]
    lines = [
        "# PRE-SCAN EVIDENCE",
        f"# diff_stats: +{stats['lines_added']} -{stats['lines_removed']} "
        f"across {stats['hunks']} hunks in {len(stats['files'])} files",
        f"# test_result: {pre_scan['test_result']}",
        "# ---",
        diff,
    ]
    return "\n".join(lines)


def _pre_scan_has_signal(pre_scan: dict[str, Any]) -> bool:
    """Return True when the pre-scan shows something the model should address."""
    test = pre_scan.get("test_result", "")
    return isinstance(test, str) and test.startswith("FAIL")


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
    """Parse JSON, validate reason_code, and strip it before schema check."""
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


def _schema_refusal(reason: str, pre_scan: dict[str, Any]) -> CorrectnessVerifierResult:
    """Return a schema-refusal result."""
    return CorrectnessVerifierResult(
        verdict=VerdictOutput(
            role="correctness_verifier",
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
    input: CorrectnessVerifierInput,
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
    )
