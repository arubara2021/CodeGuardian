"""Per-run verification brief.

The verifier stack (block 4) runs the three verifiers against a diff.
Before it does, the orchestrator assembles a ``VerificationBrief`` that
names every piece of evidence available for that round: the diff
summary, the characterization test result, the pre-scan signals each
verifier would see, and one blind prediction per verifier. The brief
is consumed by the verifier stack to decide whether to run verifiers
at all, and by the decision-log writer to record what the loop knew
when it made the verification decision.

The blind prediction per verifier is computed without reading the
diff. Correctness predicts from the test result, security from the
original signal list, contract from the assertion severities. Each
prediction is derived deterministically from the inputs, so two runs
with the same evidence produce the same prediction.

``build_verification_brief`` is the entry point. It takes the pieces
already available in the run context and produces the brief.
``verifier_evidence_slice`` returns the sub-brief a specific verifier
role should receive when the verifier stack invokes it.
``brief_to_prompt_block`` renders the brief as the human-readable
block that goes into the decision log.

This module does not call a model. It does not touch the store. It
does not import an agent. It composes evidence from primitives.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from codeguardian.core.agents._verifier_common import (
    compute_blind_prediction,
)
from codeguardian.core.agents._verifier_common import (
    diff_stats as _diff_stats_dict,
)
from codeguardian.interfaces.verifier import ContractAssertion

__all__ = [
    "DiffSummary",
    "VerificationBrief",
    "VerifierEvidence",
    "brief_to_prompt_block",
    "build_verification_brief",
    "compute_diff_summary",
    "verifier_evidence_slice",
]


_VERIFIER_NAMES: tuple[str, ...] = (
    "correctness",
    "security",
    "contract",
)


@dataclass(frozen=True, slots=True)
class DiffSummary:
    """The shape of a diff, without any code from inside it.

    Attributes:
        lines_added: Number of ``+`` lines across all hunks.
        lines_removed: Number of ``-`` lines across all hunks.
        hunks: Number of ``@@`` markers.
        files: Every path the diff touches, sorted.
        files_touched: Convenience count matching ``len(files)``.
    """

    lines_added: int
    lines_removed: int
    hunks: int
    files: tuple[str, ...]
    files_touched: int


@dataclass(frozen=True, slots=True)
class VerifierEvidence:
    """The evidence slice one verifier should receive.

    Attributes:
        verifier: One of ``"correctness"``, ``"security"``,
            ``"contract"``.
        blind_prediction: The deterministic prediction the verifier's
            pre-scan supports. One of ``"pass"``, ``"fail"``, or
            ``"uncertain"``.
        evidence_ids: Stable identifiers for the evidence items the
            verifier will examine. Populated from the test result,
            security signals, or assertion list depending on the
            verifier.
        notes: Short human-readable notes describing the evidence.
            Empty when there is nothing to say.
    """

    verifier: str
    blind_prediction: str
    evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationBrief:
    """The complete evidence package for one verification round.

    Attributes:
        run_id: The current run identifier.
        target: The target's qualified name.
        diff_hash: SHA-256 of the diff.
        diff_summary: The shape of the diff.
        test_result: The characterization test outcome as a short
            string, or None when no test ran.
        pre_scan_verifiers: Names of the verifiers whose deterministic
            pre-scan produced a signal. Used by the verifier stack to
            decide which verifiers should rely on their pre-scan
            override versus the model verdict.
        verifier_evidence: One entry per verifier role, in
            ``correctness``, ``security``, ``contract`` order.
        contract_assertion_count: Number of contract assertions the
            blast agent synthesized.
        security_signal_count: Number of pre-existing security signals
            the caller supplied.
        budget_snapshot: The context-budget snapshot at the time the
            brief was built, or empty when no budget was active.
    """

    run_id: str
    target: str
    diff_hash: str
    diff_summary: DiffSummary
    test_result: str | None = None
    pre_scan_verifiers: tuple[str, ...] = ()
    verifier_evidence: tuple[VerifierEvidence, ...] = ()
    contract_assertion_count: int = 0
    security_signal_count: int = 0
    budget_snapshot: Mapping[str, Any] = field(default_factory=dict)


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_diff_summary(diff: str) -> DiffSummary:
    """Return the shape of ``diff``.

    Delegates to the helper the verifier agents already use so a
    change to the parsing rule stays in one place.
    """
    raw = _diff_stats_dict(diff)
    files = tuple(raw.get("files", ()))
    return DiffSummary(
        lines_added=int(raw.get("lines_added", 0)),
        lines_removed=int(raw.get("lines_removed", 0)),
        hunks=int(raw.get("hunks", 0)),
        files=files,
        files_touched=len(files),
    )


def _correctness_evidence(
    test_result: str | None,
) -> VerifierEvidence:
    """Build the correctness verifier's evidence slice."""
    if test_result is None:
        return VerifierEvidence(
            verifier="correctness",
            blind_prediction="uncertain",
            notes=("no characterization test result supplied",),
        )
    evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    if test_result.startswith("FAIL"):
        evidence_ids = ("characterization_test:failed",)
        notes = (f"test result: {test_result[:120]}",)
    elif test_result.upper().startswith("PASS"):
        evidence_ids = ("characterization_test:passed",)
        notes = ("test result: PASS",)
    prediction = compute_blind_prediction(test_result=test_result)
    return VerifierEvidence(
        verifier="correctness",
        blind_prediction=prediction,
        evidence_ids=evidence_ids,
        notes=notes,
    )


def _security_evidence(
    original_security_signals: Sequence[str],
) -> VerifierEvidence:
    """Build the security verifier's evidence slice."""
    if not original_security_signals:
        return VerifierEvidence(
            verifier="security",
            blind_prediction="pass",
            notes=("no pre-existing security signals",),
        )
    evidence_ids = tuple(f"security_signal:{i}" for i in range(len(original_security_signals)))
    notes = tuple(signal[:120] for signal in original_security_signals)
    prediction = compute_blind_prediction(
        original_security_signals=tuple(original_security_signals),
    )
    return VerifierEvidence(
        verifier="security",
        blind_prediction=prediction,
        evidence_ids=evidence_ids,
        notes=notes,
    )


def _contract_evidence(
    contract_assertions: Sequence[ContractAssertion],
) -> VerifierEvidence:
    """Build the contract verifier's evidence slice."""
    if not contract_assertions:
        return VerifierEvidence(
            verifier="contract",
            blind_prediction="pass",
            notes=("no contract assertions provided",),
        )
    evidence_ids = tuple(f"contract_assertion:{i}" for i in range(len(contract_assertions)))
    notes = tuple(
        f"{a.caller}: {a.assumption[:100]} (severity={a.severity})" for a in contract_assertions
    )
    severities = tuple(a.severity for a in contract_assertions)
    prediction = compute_blind_prediction(
        contract_assertion_severities=severities,
    )
    return VerifierEvidence(
        verifier="contract",
        blind_prediction=prediction,
        evidence_ids=evidence_ids,
        notes=notes,
    )


def _pre_scan_verifiers(
    correctness: VerifierEvidence,
    security: VerifierEvidence,
    contract: VerifierEvidence,
) -> tuple[str, ...]:
    """Return the names of verifiers whose pre-scan flagged a signal.

    A signal is anything that would make the verifier's ``apply_veto``
    downgrade a model pass: correctness when the test failed, security
    when a pre-existing signal exists, contract when a critical
    assertion exists.
    """
    names: list[str] = []
    if correctness.blind_prediction == "fail":
        names.append("correctness")
    if security.blind_prediction == "fail":
        names.append("security")
    if contract.blind_prediction in ("fail", "uncertain"):
        names.append("contract")
    return tuple(names)


def build_verification_brief(
    *,
    run_id: str,
    target: str,
    diff: str,
    test_result: str | None = None,
    original_security_signals: Sequence[str] = (),
    contract_assertions: Sequence[ContractAssertion] = (),
    budget_snapshot: Mapping[str, Any] | None = None,
) -> VerificationBrief:
    """Assemble a ``VerificationBrief`` from the current evidence.

    Every argument is data the run context already holds. The function
    does not read the diff to inspect code, only to compute its shape.
    It does not call a model. It does not write to the store.

    Args:
        run_id: The run identifier.
        target: The target's qualified name.
        diff: The unified diff.
        test_result: The characterization test outcome, typically
            ``"PASS"`` or ``"FAIL: <assertion>"``. ``None`` when no
            test ran.
        original_security_signals: Pre-existing security signals the
            caller supplied. Empty when there were none.
        contract_assertions: Assertions the blast agent synthesized.
            Empty when there were none.
        budget_snapshot: The context-budget snapshot at the time the
            brief was built. ``None`` when no budget was active.

    Returns:
        A ``VerificationBrief`` with one ``VerifierEvidence`` per
        verifier role, in fixed order.
    """
    diff_summary = compute_diff_summary(diff)
    diff_hash = _sha256(diff)

    correctness = _correctness_evidence(test_result)
    security = _security_evidence(tuple(original_security_signals))
    contract = _contract_evidence(tuple(contract_assertions))

    return VerificationBrief(
        run_id=run_id,
        target=target,
        diff_hash=diff_hash,
        diff_summary=diff_summary,
        test_result=test_result,
        pre_scan_verifiers=_pre_scan_verifiers(
            correctness,
            security,
            contract,
        ),
        verifier_evidence=(correctness, security, contract),
        contract_assertion_count=len(contract_assertions),
        security_signal_count=len(original_security_signals),
        budget_snapshot=dict(budget_snapshot or {}),
    )


def verifier_evidence_slice(
    brief: VerificationBrief,
    verifier: str,
) -> dict[str, Any]:
    """Return the evidence slice one verifier should receive.

    Args:
        brief: The verification brief.
        verifier: One of ``"correctness"``, ``"security"``, or
            ``"contract"``.

    Returns:
        A JSON-shaped dict with the verifier's blind prediction, its
        evidence ids and notes, and the shared context (diff hash,
        diff summary, test result, assertion and signal counts).

    Raises:
        KeyError: When ``verifier`` is not one of the three names.
    """
    for evidence in brief.verifier_evidence:
        if evidence.verifier == verifier:
            return {
                "verifier": evidence.verifier,
                "blind_prediction": evidence.blind_prediction,
                "evidence_ids": list(evidence.evidence_ids),
                "notes": list(evidence.notes),
                "diff_hash": brief.diff_hash,
                "diff_summary": {
                    "lines_added": brief.diff_summary.lines_added,
                    "lines_removed": brief.diff_summary.lines_removed,
                    "hunks": brief.diff_summary.hunks,
                    "files_touched": brief.diff_summary.files_touched,
                },
                "test_result": brief.test_result,
                "contract_assertion_count": brief.contract_assertion_count,
                "security_signal_count": brief.security_signal_count,
            }
    raise KeyError(
        f"unknown verifier {verifier!r}; known: {[e.verifier for e in brief.verifier_evidence]}"
    )


def brief_to_prompt_block(brief: VerificationBrief) -> str:
    """Render a brief as the human-readable text recorded in decision logs.

    The block names the diff shape, the test outcome, and the blind
    prediction per verifier. It is a log-facing form, not the prompt
    the verifier model sees; the verifier model's prompt is built by
    the verifier agent itself from its own annotated diff.
    """
    lines: list[str] = [
        f"## verification brief for {brief.target}",
        f"diff: +{brief.diff_summary.lines_added} "
        f"-{brief.diff_summary.lines_removed} "
        f"across {brief.diff_summary.hunks} hunks "
        f"in {brief.diff_summary.files_touched} files",
        f"diff_hash: {brief.diff_hash[:16]}...",
        f"test_result: {brief.test_result or '(not run)'}",
        f"contract_assertions: {brief.contract_assertion_count}",
        f"security_signals: {brief.security_signal_count}",
    ]
    if brief.pre_scan_verifiers:
        lines.append(f"pre_scan_flagged: {', '.join(brief.pre_scan_verifiers)}")
    for evidence in brief.verifier_evidence:
        lines.append(f"- {evidence.verifier}: blind={evidence.blind_prediction}")
        for note in evidence.notes:
            lines.append(f"    {note}")
    return "\n".join(lines)
