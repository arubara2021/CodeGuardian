"""Security verifier agent.

Receives the diff and the original security-relevant signals detected
in the pre-refactor code. Runs a deterministic pre-scan against a
documented regex list, then prepends both the pre-scan and the
original signals to the diff the model sees.

Independence. The verifier does not see the characterization test
result, the original source code, or the other verifiers' evidence.
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
    "SecurityVerifier",
    "SecurityVerifierInput",
    "SecurityVerifierResult",
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

_SECURITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eval_call", re.compile(r"\beval\s*\(")),
    ("exec_call", re.compile(r"\bexec\s*\(")),
    ("shell_true", re.compile(r"subprocess\.[\w.]+\s*\([^)]*shell\s*=\s*True")),
    ("os_system", re.compile(r"\bos\.system\s*\(")),
    ("sql_fstring", re.compile(r"\.execute\s*\(\s*f[\"']")),
    ("sql_format", re.compile(r"\.execute\s*\([^)]*%\s*[\(\w]")),
    ("pickle_loads", re.compile(r"\bpickle\.loads\s*\(")),
    ("weak_hash", re.compile(r"\b(?:md5|sha1)\s*\(")),
    ("weak_cipher", re.compile(r"\b(?:DES|RC4)\b")),
    (
        "secret_literal",
        re.compile(
            r"(?:api[_-]?key|password|secret|token)\s*=\s*"
            r"[\"'][A-Za-z0-9_+/=\-]{16,}[\"']"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class SecurityVerifierInput:
    """Input to the security verifier."""

    __test__: ClassVar[bool] = False

    diff: str
    change_kind: str
    blast_score: int
    original_security_signals: tuple[str, ...]
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_symbol: str = ""
    prompt_version: str = "1.0.0"


@dataclass(frozen=True, slots=True)
class SecurityVerifierResult:
    """Output of the security verifier."""

    verdict: VerdictOutput
    verdict_reason: str | None
    pre_scan: dict[str, Any]
    pre_scan_conflict: bool
    refusal_code: str | None = None
    is_refusal: bool = False


class SecurityVerifier:
    """Model-backed security verifier."""

    name = "security_verifier"
    phase = "security_verifier"
    is_deterministic = False
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role = "security_verifier"
    prompt_version = "1.0.0"

    async def run(self, input: SecurityVerifierInput) -> SecurityVerifierResult:
        """Run the security verifier."""
        pre_scan = _compute_pre_scan(input.diff)
        annotated_diff = _annotate_diff(input.diff, pre_scan, input.original_security_signals)

        variables = {
            "change_kind": input.change_kind,
            "blast_score": str(input.blast_score),
            "diff": annotated_diff,
        }
        prompt_result = render_prompt("security_verifier", input.prompt_version, variables)

        response, latency_ms = await tracked_model_call(
            input.provider,
            prompt_result.rendered,
            input.model,
            ModelOptions(),
            "security_verifier",
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
            validated = validate_output("security_verifier", parsed)
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

        verdict_reason: str | None = (
            "vulnerability_introduced" if validated.verdict == "fail" else None
        )
        pre_scan_conflict = validated.verdict == "pass" and len(pre_scan.get("matches", [])) > 0

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
            verdict_reason=verdict_reason,
        )

        return SecurityVerifierResult(
            verdict=validated,
            verdict_reason=verdict_reason,
            pre_scan=pre_scan,
            pre_scan_conflict=pre_scan_conflict,
            refusal_code=refusal_code,
            is_refusal=validated.verdict == "refused",
        )


def _compute_pre_scan(diff: str) -> dict[str, Any]:
    """Return the deterministic pre-scan for the security verifier."""
    stats = _diff_stats(diff)
    matches: list[dict[str, Any]] = []
    line_no = 0
    for raw in diff.splitlines():
        line_no += 1
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        content = raw[1:]
        for pattern_id, pattern in _SECURITY_PATTERNS:
            if pattern.search(content):
                matches.append(
                    {
                        "id": pattern_id,
                        "line": line_no,
                        "snippet": content.strip()[:120],
                    }
                )
    return {"diff_stats": stats, "matches": matches}


def _annotate_diff(
    diff: str,
    pre_scan: dict[str, Any],
    original_signals: tuple[str, ...],
) -> str:
    """Prepend pre-scan evidence and original signals to the diff."""
    stats = pre_scan["diff_stats"]
    lines = [
        "# PRE-SCAN EVIDENCE",
        f"# diff_stats: +{stats['lines_added']} -{stats['lines_removed']} "
        f"across {stats['hunks']} hunks in {len(stats['files'])} files",
    ]
    for match in pre_scan["matches"]:
        lines.append(f"# match: {match['id']} at diff line {match['line']}: {match['snippet']}")
    if original_signals:
        lines.append("# original_security_signals:")
        for signal in original_signals:
            lines.append(f"#   - {signal}")
    lines.append("# ---")
    lines.append(diff)
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


def _schema_refusal(reason: str, pre_scan: dict[str, Any]) -> SecurityVerifierResult:
    """Return a schema-refusal result."""
    return SecurityVerifierResult(
        verdict=VerdictOutput(
            role="security_verifier",
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
    input: SecurityVerifierInput,
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
        role="security_verifier",
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
        prompt_role="security_verifier",
        prompt_version=input.prompt_version,
        model_options={"temperature": 0.0},
    )
