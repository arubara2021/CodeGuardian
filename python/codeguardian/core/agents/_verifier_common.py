"""Shared helpers for the three verifier agents.

The correctness, security, and contract verifiers all use the same four
utilities: a diff-stat summariser, a JSON parse-and-strip routine for
the model's response, an annotated-diff assembler that prepends the
pre-scan evidence to the diff the model sees, and a schema-refusal
verdict builder. Extracting them here keeps the three verifiers focused
on their own pre-scan logic and removes the duplication that made a
single change to the refusal protocol require three coordinated edits.

What is here: pure functions with no state. Every function is
deterministic given its arguments.

What is not here: per-verifier logic. Each verifier's pre-scan (which
patterns it looks for, which flags it emits) stays in its own module.
The evidence lines the model sees differ per verifier, so each
verifier builds them locally and hands them to ``assemble_annotated_diff``.

No CHECK constraints, no schema, no migration. This module is pure
code shared by three modules in the same package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from codeguardian.core.prompts import VerdictOutput

__all__ = [
    "HUNK_RE",
    "VALID_REFUSAL_CODES",
    "ExecutionOutcome",
    "SandboxExecutor",
    "apply_evidence",
    "apply_veto",
    "assemble_annotated_diff",
    "compute_blind_prediction",
    "compute_disagreement",
    "diff_stats",
    "execute_characterization_test",
    "parse_and_strip",
    "schema_refusal_verdict",
]


HUNK_RE = re.compile(r"^@@ .* @@", re.MULTILINE)

VALID_REFUSAL_CODES = frozenset(
    {
        "target_missing",
        "directive_ambiguous",
        "context_contradicts",
        "tool_unavailable",
        "budget_exhausted",
        "unsupported_language",
    }
)


def diff_stats(diff: str) -> dict[str, Any]:
    """Return added and removed line counts, hunk count, and touched files.

    Args:
        diff: A unified diff string.

    Returns:
        A dictionary with keys ``lines_added``, ``lines_removed``,
        ``hunks``, and ``files``. ``files`` is a sorted list of the
        paths that appear on ``+++`` lines, with the ``b/`` prefix
        stripped when present.
    """
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
        "hunks": len(HUNK_RE.findall(diff)),
        "files": sorted(files),
    }


def parse_and_strip(text: str) -> tuple[dict[str, Any], str | None, str | None]:
    """Parse a JSON response and strip the ``reason_code`` field.

    The verifier output schema does not include ``reason_code``; the
    caller reads it separately and passes it to telemetry. Parsing
    here, validating the code against ``VALID_REFUSAL_CODES``, and
    stripping the field is done once so the three verifiers do not
    duplicate the logic.

    Args:
        text: The model's response text, expected to be a JSON object.

    Returns:
        A three-tuple ``(parsed, reason_code, error)``. On success,
        ``error`` is None and ``parsed`` is the response with the
        ``reason_code`` key removed. On failure, ``error`` is a
        human-readable string and ``parsed`` is an empty dictionary.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, None, f"invalid JSON: {exc}"

    if not isinstance(parsed, dict):
        return {}, None, "model output is not a JSON object"

    reason_code = parsed.pop("reason_code", None)
    if reason_code is not None and reason_code not in VALID_REFUSAL_CODES:
        return (
            {},
            None,
            f"invalid reason_code {reason_code!r}; valid: {sorted(VALID_REFUSAL_CODES)}",
        )

    return parsed, reason_code, None


def assemble_annotated_diff(
    diff: str,
    stats: Mapping[str, Any],
    evidence_lines: Sequence[str] = (),
) -> str:
    """Prepend the pre-scan header and evidence lines to a diff.

    All three verifiers see the same header (a marker line and a
    one-line diff-stat summary) followed by their own evidence lines,
    a separator, and the diff. Building the header and footer here
    keeps the annotation format consistent across the three verifiers
    and makes a future change to the format a one-file change.

    Args:
        diff: The unified diff to annotate.
        stats: A dictionary as returned by ``diff_stats``.
        evidence_lines: Verifier-specific evidence lines, already
            prefixed with ``# ``. Inserted verbatim between the header
            and the separator.

    Returns:
        The annotated diff as a single string, with newline separators.
    """
    header = [
        "# PRE-SCAN EVIDENCE",
        f"# diff_stats: +{stats['lines_added']} -{stats['lines_removed']} "
        f"across {stats['hunks']} hunks in {len(stats['files'])} files",
    ]
    return "\n".join([*header, *evidence_lines, "# ---", diff])


def schema_refusal_verdict(role: str, reason: str) -> VerdictOutput:
    """Return a refused VerdictOutput for a schema violation.

    Used by all three verifiers when the model's response fails to
    parse or fails schema validation. The prefix ``schema:`` on the
    reason tells the caller this was a structural failure, not a
    substantive verdict.

    Args:
        role: The verifier role name (for example ``"security_verifier"``).
        reason: The specific schema failure message.

    Returns:
        A VerdictOutput with verdict ``"refused"``, no confidence, no
        findings, and the prefixed reason.
    """
    return VerdictOutput(
        role=role,
        verdict="refused",
        confidence=None,
        findings=(),
        reason=f"schema: {reason}",
    )


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """The result of running one command in a sandbox.

    Attributes:
        exit_code: The process exit code. Zero means the command
            succeeded. One from pytest means tests failed. Two through
            five indicate a collection or internal error. Any
            non-zero value other than one is treated as inconclusive
            evidence by the verifiers.
        stdout: Captured standard output, ANSI codes stripped.
        stderr: Captured standard error, ANSI codes stripped.
    """

    exit_code: int
    stdout: str
    stderr: str


class SandboxExecutor(Protocol):
    """A sandbox the verifier can run commands in.

    The verifier does not construct the sandbox; the caller does. This
    keeps the verifier testable with a fake executor and decouples it
    from any specific sandbox backend. The interface is one method:
    run an argv and get an outcome back.
    """

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 120.0,
    ) -> ExecutionOutcome:
        """Run the command and return the outcome."""
        ...


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from a string."""
    return _ANSI_ESCAPE.sub("", text)


def execute_characterization_test(
    executor: SandboxExecutor,
    test_path: str,
    *,
    runner: str = "run_pytest.py",
) -> tuple[ExecutionOutcome, str | None]:
    """Run the characterization test and classify the outcome.

    Args:
        executor: The sandbox executor to run through.
        test_path: The path to the test file, as seen from the
            sandbox's working directory.
        runner: The pytest launcher script present in the sandbox.
            Defaults to ``run_pytest.py``.

    Returns:
        A two-tuple of (outcome, counterexample). ``counterexample``
        is populated only when the test failed (exit code 1). It
        contains a short excerpt of the failing assertion extracted
        from stderr, or the last 500 characters of stderr when no
        recognizable assertion was found. Returns ``None`` for a pass
        or an inconclusive outcome.
    """
    outcome = executor.run(
        ["python3", runner, test_path, "-v", "--tb=short"],
        timeout_s=180.0,
    )
    clean_stderr = _strip_ansi(outcome.stderr)
    clean_stdout = _strip_ansi(outcome.stdout)
    cleaned = ExecutionOutcome(
        exit_code=outcome.exit_code,
        stdout=clean_stdout,
        stderr=clean_stderr,
    )

    if outcome.exit_code == 0:
        return cleaned, None
    if outcome.exit_code == 1:
        counterexample = _extract_failed_assertion(clean_stderr) or clean_stderr[-500:]
        return cleaned, counterexample
    return cleaned, None


_ASSERTION_HINT = re.compile(r"(AssertionError.*|assert .*==.*|assert .*is not None.*)")


def _extract_failed_assertion(stderr: str) -> str | None:
    """Return the first recognisable assertion line from pytest output."""
    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ASSERTION_HINT.search(stripped)
        if match:
            return match.group(1)[:500]
    return None


def apply_evidence(
    validated: VerdictOutput,
    evidence_passed: bool | None,
) -> tuple[VerdictOutput, bool]:
    """Combine a model verdict with deterministic execution evidence.

    Evidence-first rule: a conclusive deterministic check outranks the
    model. When the two agree, no conflict is reported. When they
    disagree, the evidence wins and the returned conflict flag is
    True. When evidence is inconclusive (None), the model verdict is
    returned unchanged.

    Args:
        validated: The model's verdict.
        evidence_passed: True when the evidence confirmed passing,
            False when it confirmed failing, None when inconclusive.

    Returns:
        A two-tuple of (final verdict, conflict flag).
    """
    if evidence_passed is None:
        return validated, False

    model_pass = validated.verdict == "pass"

    if evidence_passed and model_pass:
        return validated, False

    if not evidence_passed and not model_pass:
        return validated, False

    if evidence_passed:
        return (
            VerdictOutput(
                role=validated.role,
                verdict="pass",
                confidence=validated.confidence,
                findings=validated.findings,
                reason="deterministic evidence confirmed pass; model verdict overridden",
            ),
            True,
        )

    return (
        VerdictOutput(
            role=validated.role,
            verdict="fail",
            confidence=validated.confidence,
            findings=validated.findings,
            reason="deterministic evidence detected failure; model verdict overridden",
        ),
        True,
    )


def compute_blind_prediction(
    *,
    test_result: str | None = None,
    original_security_signals: tuple[str, ...] = (),
    contract_assertion_severities: Sequence[str] = (),
) -> str:
    """Return a deterministic blind prediction from spec-level evidence.

    The prediction is derived without reading the diff. It reflects
    what the caller's pre-existing evidence says the verdict should
    be, so that the informed model verdict can be compared against it.

    Args:
        test_result: The correctness test outcome from the caller,
            typically "PASS" or "FAIL: <assertion>". None when the
            verifier is not the correctness verifier.
        original_security_signals: Any security signals the caller
            recorded against the pre-refactor code.
        contract_assertion_severities: The severity of each contract
            assertion: "info", "warning", or "critical".

    Returns:
        One of "pass", "fail", "uncertain".
    """
    if test_result is not None and test_result.startswith("FAIL"):
        return "fail"
    if original_security_signals:
        return "fail"
    severities = set(contract_assertion_severities)
    if "critical" in severities:
        return "fail"
    if "warning" in severities:
        return "uncertain"
    return "pass"


def apply_veto(
    final_verdict: VerdictOutput,
    pre_scan_conflict: bool,
) -> VerdictOutput:
    """Downgrade a pass verdict to uncertain when the pre-scan conflicts.

    The veto reflects a specific epistemic state: the model looked at
    the diff and said pass, but a deterministic scan flagged something
    the model did not. That is not proof of failure — the scan can be
    wrong — but it is not a clean pass either. Downgrading to uncertain
    is the honest answer.

    A veto only applies to a pass verdict. A fail verdict from the
    model stands, because the model already found something; a
    deterministic disagreement at that level is informative but does
    not weaken the model's finding.
    """
    if not pre_scan_conflict:
        return final_verdict
    if final_verdict.verdict != "pass":
        return final_verdict
    return VerdictOutput(
        role=final_verdict.role,
        verdict="uncertain",
        confidence=final_verdict.confidence,
        findings=final_verdict.findings,
        reason=(
            "deterministic pre-scan contradicted the model's pass verdict; downgraded to uncertain"
        ),
    )


def compute_disagreement(
    blind_prediction: str | None,
    informed_verdict: str | None,
) -> bool:
    """Return True when the blind and informed verdicts differ.

    Both None means the check did not run; both non-None and equal
    means agreement. Any other combination is a disagreement worth
    logging: either the model found something the spec did not
    anticipate, or the model missed something the spec flagged.
    """
    if blind_prediction is None or informed_verdict is None:
        return False
    return blind_prediction != informed_verdict
