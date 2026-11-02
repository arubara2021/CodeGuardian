"""Agent call telemetry.

One row per agent invocation in the agent_calls table. Two
classifications coexist with the coarse outcome field:

- failure_category: why the agent's execution failed (7 values).
- verdict_reason: why a verifier rejected the writer's output (4
  values). Populated only on verifier fail-verdict runs; the verifier
  itself succeeded.

The coarse outcome column from migration 002 has a five-value CHECK
constraint that predates this finer taxonomy. record_call derives the
outcome from the failure_category when the caller does not supply one.
The mapping is best-fit, not exact; downstream consumers that need
precision should read failure_category and prefer it when present.

Migration 004 adds four evidence columns (tool_chain, decision_log,
model_reasoning, evidence_receipt). They are optional, nullable, and
additive: agents that have nothing to record pass None and the row is
written unchanged. The TypedDicts ToolChainEntry, DecisionLogEntry,
and EvidenceReceipt describe the shapes those columns expect. The
helper try_record_call writes through the active store when one is
set and skips silently when one is not, so agents do not need to
know whether telemetry is wired up in the current context.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, TypedDict

from codeguardian.core.storage.sqlite_store import SQLiteStore

from ._runtime import FailureCategory

__all__ = [
    "RETRY_POLICY_BY_CATEGORY",
    "DecisionLogEntry",
    "EvidenceReceipt",
    "FailureCategory",
    "ToolChainEntry",
    "VerdictReason",
    "record_call",
    "try_record_call",
]


VerdictReason = Literal[
    "behavior_drift",
    "vulnerability_introduced",
    "contract_broken",
    "inconclusive",
]


class ToolChainEntry(TypedDict):
    """One tool invocation recorded during an agent call.

    Attributes:
        tool: The tool or plugin name.
        input: The tool input as a JSON-serialisable mapping.
        output_bytes: The size of the tool's output, in bytes.
    """

    tool: str
    input: dict[str, object]
    output_bytes: int


class DecisionLogEntry(TypedDict):
    """One decision event recorded during an agent call.

    Attributes:
        step: The stage that made the decision (pre_check, verifier,
            escalation, refusal, and so on).
        candidate: The zero-based candidate index, or None when the
            decision applies to the call as a whole.
        verdict: The decision outcome (accepted, rejected, escalated,
            and so on).
        reason: A short machine-readable reason, or None when the
            verdict is self-explanatory.
    """

    step: str
    candidate: int | None
    verdict: str
    reason: str | None


class EvidenceReceipt(TypedDict):
    """The evidence that supports a verifier's verdict.

    Attributes:
        evidence_ids: Stable identifiers for the evidence items the
            verifier examined.
        artifacts_checked: File paths or artifact names the verifier
            read.
        commands_run: Commands the verifier executed, in order.
    """

    evidence_ids: list[str]
    artifacts_checked: list[str]
    commands_run: list[str]


RETRY_POLICY_BY_CATEGORY: MappingProxyType[FailureCategory, str] = MappingProxyType(
    {
        "localization": "expand_context",
        "generation": "verifier_feedback",
        "schema": "retry_once_with_violation",
        "contract": "retry_with_violation",
        "timeout": "retry_once_then_fallback",
        "context_overflow": "retry_with_truncated_context",
        "model_unavailable": "fallback_next_model",
    }
)


# Best-fit mapping from the seven categories to the five outcome strings
# enforced by migration 002's CHECK constraint. The category column
# carries the precise reason; the outcome column carries the closest
# coarse bucket. Two categories share schema_violation and three share
# timeout because the earlier CHECK does not distinguish them; the
# finer distinction lives in failure_category.
_OUTCOME_BY_CATEGORY: MappingProxyType[FailureCategory, str] = MappingProxyType(
    {
        "localization": "schema_violation",
        "generation": "schema_violation",
        "schema": "schema_violation",
        "contract": "signature_drift",
        "timeout": "timeout",
        "context_overflow": "timeout",
        "model_unavailable": "timeout",
    }
)


def _sha256_hex(text: str) -> str:
    """Return the SHA-256 hex digest of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def record_call(
    store: SQLiteStore,
    *,
    role: str,
    outcome: str | None = None,
    failure_category: FailureCategory | None = None,
    verdict_reason: VerdictReason | None = None,
    refusal_code: str | None = None,
    call_id: str | None = None,
    run_id: str | None = None,
    phase_id: str | None = None,
    target_symbol: str | None = None,
    prompt_role: str | None = None,
    prompt_version: str | None = None,
    model_id: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    retry_count: int = 0,
    prompt: str | None = None,
    context: str | None = None,
    output: str | None = None,
    model_options: Mapping[str, object] | None = None,
    tool_chain: Sequence[ToolChainEntry] | None = None,
    decision_log: Sequence[DecisionLogEntry] | None = None,
    model_reasoning: str | None = None,
    evidence_receipt: EvidenceReceipt | None = None,
) -> str:
    """Write one agent_calls row and return its call_id.

    Args:
        store: The open SQLiteStore to write into. Migrations 001, 002,
            003, and 004 must have been applied.
        role: One of the nine agent roles. Enforced by a CHECK
            constraint on the table.
        outcome: One of the five values permitted by migration 002's
            CHECK constraint. When omitted, derived from
            ``failure_category`` via the best-fit mapping; ``success``
            when ``failure_category`` is also None.
        failure_category: The agent-level failure category, or None on
            success and verifier fail-verdict runs.
        verdict_reason: The verifier's reason for returning ``fail``,
            or None for non-verifier calls and non-fail verdicts.
        refusal_code: The typed refusal code when the agent refused.
        call_id: Pre-generated ID. When None, a UUID4 is generated.
        run_id: Optional locator. None for isolated eval runs.
        phase_id: Optional locator. None for isolated eval runs.
        target_symbol: The target being worked on, for example
            ``"Session.send"``.
        prompt_role: The prompt role. Usually equal to ``role``.
        prompt_version: The prompt version used.
        model_id: The provider-specific model identifier.
        tokens_in: Input token count reported by the provider.
        tokens_out: Output token count reported by the provider.
        latency_ms: Wall-clock milliseconds around the model call.
        retry_count: Which attempt this was. Zero on first attempt.
        prompt: The rendered prompt. Hashed, not stored.
        context: The context pack. Hashed, not stored.
        output: The agent's output. Hashed, not stored.
        model_options: The ModelOptions fields (temperature,
            max_tokens, seed) serialised as JSON.
        tool_chain: The ordered tool invocations made during this
            call. Serialised as JSON.
        decision_log: The ordered decision events recorded during this
            call. Serialised as JSON.
        model_reasoning: The model's reasoning trace, when the
            provider returns one. Stored as plain text.
        evidence_receipt: The evidence that supported a verifier's
            verdict. Serialised as JSON.

    Returns:
        The call_id that was written.

    Raises:
        CodeGuardianError: ``SYS_INTERNAL`` from the store on any
            SQLite failure, including a CHECK constraint violation
            (invalid role, invalid outcome). The verdict_reason and
            failure_category values are not CHECK-constrained, so
            unrecognised values pass through unchanged.
    """
    if call_id is None:
        call_id = str(uuid.uuid4())
    if outcome is None:
        outcome = "success" if failure_category is None else _OUTCOME_BY_CATEGORY[failure_category]

    store.execute(
        """
        INSERT INTO agent_calls (
            call_id, run_id, phase_id, target_symbol, role,
            prompt_role, prompt_version, model_id,
            tokens_in, tokens_out, latency_ms, outcome,
            refusal_code, retry_count, output_hash, created_at,
            prompt_hash, context_hash, failure_category,
            verdict_reason, model_options, tool_chain,
            decision_log, model_reasoning, evidence_receipt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            run_id,
            phase_id,
            target_symbol,
            role,
            prompt_role,
            prompt_version,
            model_id,
            tokens_in,
            tokens_out,
            latency_ms,
            outcome,
            refusal_code,
            retry_count,
            _sha256_hex(output) if output is not None else None,
            _now_iso(),
            _sha256_hex(prompt) if prompt is not None else None,
            _sha256_hex(context) if context is not None else None,
            failure_category,
            verdict_reason,
            json.dumps(dict(model_options), sort_keys=True) if model_options is not None else None,
            json.dumps([dict(entry) for entry in tool_chain], sort_keys=True)
            if tool_chain is not None
            else None,
            json.dumps([dict(entry) for entry in decision_log], sort_keys=True)
            if decision_log is not None
            else None,
            model_reasoning,
            json.dumps(dict(evidence_receipt), sort_keys=True)
            if evidence_receipt is not None
            else None,
        ),
    )
    return call_id


def try_record_call(**kwargs: Any) -> str | None:
    """Record a call when an active store is present, else skip.

    Convenience wrapper around ``record_call`` for agents that should
    write telemetry when a store is set (a full pipeline run) and skip
    silently when one is not (isolated eval and unit test contexts).

    The active store is read from the ``_store_context`` ContextVar.
    The import is local to avoid a module-level cycle between
    ``telemetry`` and ``_store_context`` inside the agents package.

    Args:
        **kwargs: Forwarded verbatim to ``record_call``. The ``store``
            argument is supplied by this function and must not be
            passed by the caller.

    Returns:
        The call_id when a store was present, or None when the write
        was skipped.
    """
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return None
    return record_call(store, **kwargs)
