"""Plan approval and apply consent gates.

Two consent gates sit between the pipeline and mutation of the user's
code. The first, plan approval, runs before phase 0 and authorizes the
scope of the run. The second, apply consent, runs after the verifier
stack and authorizes writing the diff to the repository.

Both gates record every grant to the ``plan_approvals`` or
``consent_records`` table and to the audit chain, with an ``origin``
field that names where consent came from: ``"human"`` when a person
answered a callback, ``"config_auto"`` when a config file pre-approved
the action. A block-band target overrides ``"config_auto"`` and forces
a human callback; a run whose blast band is ``block`` cannot
auto-approve an apply.

The gates take callables rather than assume a UI. A CLI supplies a
prompt; a CI job supplies a callable that reads an environment
variable; the deep test supplies one that returns True. The gate does
not care how the answer is decided, only that the answer is recorded
with its origin.

The audit writer is a callable supplied by the caller. It takes a
dict-shaped audit entry and returns the entry's hash. This module does
not import the JSON-RPC bridge; the caller wires whichever audit
transport the deployment uses.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from codeguardian.core.storage.sqlite_store import SQLiteStore

__all__ = [
    "ApplyConsentOutcome",
    "ApplyRequest",
    "ConsentOrigin",
    "PlanApprovalOutcome",
    "PlanApprovalRequest",
    "request_apply_consent",
    "request_plan_approval",
]


ConsentOrigin = Literal["human", "config_auto"]


AuditWriter = Callable[[dict[str, Any]], str]
"""A callable that writes one audit entry and returns its entry_hash."""


PlanApprover = Callable[["PlanApprovalRequest"], bool]
"""A callable that presents the plan and returns True to approve."""


ApplyApprover = Callable[["ApplyRequest"], bool]
"""A callable that presents the diff and returns True to approve."""


@dataclass(frozen=True, slots=True)
class PlanApprovalRequest:
    """The data a plan approver sees before deciding.

    Attributes:
        run_id: The run being approved.
        plan_summary: A short human-readable summary of the plan.
        plan_hash: SHA-256 of the plan summary and target list, for
            tamper-evidence in the audit chain.
        targets: Every target the plan will touch.
        directive: The directive the plan implements.
    """

    run_id: str
    plan_summary: str
    plan_hash: str
    targets: tuple[str, ...]
    directive: str


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    """The data an apply approver sees before deciding.

    Attributes:
        run_id: The run being approved.
        diff: The unified diff.
        diff_hash: SHA-256 of the diff.
        target_path: The file the diff modifies.
        blast_recommendation: The adjusted recommendation from blast:
            ``"proceed"``, ``"review"``, or ``"block"``.
    """

    run_id: str
    diff: str
    diff_hash: str
    target_path: str
    blast_recommendation: str


@dataclass(frozen=True, slots=True)
class PlanApprovalOutcome:
    """The result of one plan approval attempt.

    Attributes:
        approved: True when the plan was approved.
        approval_id: The UUID written to ``plan_approvals``, or None
            when the plan was rejected.
        origin: Where consent came from.
        reason: A short human-readable reason, or None on approval.
    """

    approved: bool
    approval_id: str | None
    origin: ConsentOrigin
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyConsentOutcome:
    """The result of one apply consent attempt.

    Attributes:
        approved: True when the apply was authorized.
        consent_id: The UUID written to ``consent_records``, or None
            when apply was rejected.
        origin: Where consent came from. When the blast band is block
            and the requested origin was ``"config_auto"``, the
            effective origin is ``"human"``.
        reason: A short human-readable reason, or None on approval.
    """

    approved: bool
    consent_id: str | None
    origin: ConsentOrigin
    reason: str | None = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_plan_approval(
    *,
    store: SQLiteStore,
    run_id: str,
    plan_summary: str,
    targets: tuple[str, ...],
    directive: str,
    origin: ConsentOrigin,
    approve: PlanApprover,
    audit_writer: AuditWriter,
    approved_by: str = "cli",
) -> PlanApprovalOutcome:
    """Request plan approval from a human or the configuration.

    Args:
        store: An open SQLiteStore.
        run_id: The run being approved. Must exist in ``runs``; the
            caller is expected to have inserted the run row before
            calling this function.
        plan_summary: A short human-readable summary of the plan.
        targets: Every target the plan will touch. Order is significant
            for the hash.
        directive: The directive the plan implements.
        origin: ``"human"`` to invoke the approve callback;
            ``"config_auto"`` to skip the callback and record the
            approval with that origin.
        approve: Called only when ``origin == "human"``. Must return
            True to approve and False to reject.
        audit_writer: Called with a dict-shaped audit entry. Returns
            the entry's hash, which is recorded alongside the approval.
        approved_by: The identifier recorded in the ``approved_by``
            column. Defaults to ``"cli"``.

    Returns:
        A ``PlanApprovalOutcome``. Approval is recorded in both
        ``plan_approvals`` and the audit chain before returning.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` on a database
            write failure.
    """
    plan_hash = _sha256(
        json.dumps(
            {"summary": plan_summary, "targets": list(targets), "directive": directive},
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    if origin == "human":
        request = PlanApprovalRequest(
            run_id=run_id,
            plan_summary=plan_summary,
            plan_hash=plan_hash,
            targets=targets,
            directive=directive,
        )
        if not approve(request):
            return PlanApprovalOutcome(
                approved=False,
                approval_id=None,
                origin="human",
                reason="approver rejected the plan",
            )

    approval_id = str(uuid.uuid4())
    scope_json = json.dumps(
        {"targets": list(targets), "directive": directive},
        sort_keys=True,
    )
    store.execute(
        """
        INSERT INTO plan_approvals
            (approval_id, run_id, approved_at, approved_by,
             scope_kind, scope_json, plan_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            approval_id,
            run_id,
            _now_iso(),
            approved_by,
            "project" if len(targets) > 1 else "single_target",
            scope_json,
            plan_hash,
        ),
    )
    audit_writer(
        {
            "run_id": run_id,
            "phase": "consent",
            "agent": "orchestrator",
            "result": {
                "status": "success",
                "consent_scope": "plan_approval",
                "approval_id": approval_id,
                "plan_hash": plan_hash,
                "origin": origin,
            },
        }
    )
    return PlanApprovalOutcome(
        approved=True,
        approval_id=approval_id,
        origin=origin,
    )


def request_apply_consent(
    *,
    store: SQLiteStore,
    run_id: str,
    diff: str,
    target_path: str,
    blast_recommendation: str,
    origin: ConsentOrigin,
    approve: ApplyApprover,
    audit_writer: AuditWriter,
    rollback_ref: str = "",
    granted_by: str = "cli",
) -> ApplyConsentOutcome:
    """Request apply consent from a human or the configuration.

    A block-band target overrides ``origin="config_auto"`` and forces
    a human callback. The override is recorded on the outcome so a
    caller knows the effective origin.

    Args:
        store: An open SQLiteStore.
        run_id: The run being approved.
        diff: The unified diff that would be applied.
        target_path: The file the diff modifies.
        blast_recommendation: The adjusted recommendation from blast.
            When this is ``"block"``, a config_auto origin is escalated
            to human.
        origin: The requested origin.
        approve: Called when the effective origin is human.
        audit_writer: Called with a dict-shaped audit entry.
        rollback_ref: The reference to record for rollback.
        granted_by: The identifier recorded in the ``granted_by``
            column. Defaults to ``"cli"``.

    Returns:
        An ``ApplyConsentOutcome``. The ``origin`` field names the
        effective origin, which may differ from ``origin`` when the
        blast band forces an escalation.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` on a database
            write failure.
    """
    effective_origin: ConsentOrigin = origin
    escalation_note: str | None = None
    if blast_recommendation == "block" and origin == "config_auto":
        effective_origin = "human"
        escalation_note = (
            "block-band target forced a human consent callback; "
            "config_auto is not permitted on block band"
        )

    diff_hash = _sha256(diff)

    if effective_origin == "human":
        request = ApplyRequest(
            run_id=run_id,
            diff=diff,
            diff_hash=diff_hash,
            target_path=target_path,
            blast_recommendation=blast_recommendation,
        )
        if not approve(request):
            return ApplyConsentOutcome(
                approved=False,
                consent_id=None,
                origin=effective_origin,
                reason=(escalation_note or "approver rejected the apply"),
            )

    consent_id = str(uuid.uuid4())
    store.execute(
        """
        INSERT INTO consent_records
            (consent_id, run_id, granted_at, granted_by,
             scope, diff_hash, rollback_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            consent_id,
            run_id,
            _now_iso(),
            granted_by,
            "apply",
            diff_hash,
            rollback_ref or None,
        ),
    )
    audit_writer(
        {
            "run_id": run_id,
            "phase": "consent",
            "agent": "orchestrator",
            "result": {
                "status": "success",
                "consent_scope": "apply",
                "consent_id": consent_id,
                "diff_hash": diff_hash,
                "origin": effective_origin,
                "blast_recommendation": blast_recommendation,
                "escalation_note": escalation_note,
            },
        }
    )
    return ApplyConsentOutcome(
        approved=True,
        consent_id=consent_id,
        origin=effective_origin,
        reason=escalation_note,
    )
