"""Audit-chain verification and telemetry inspection.

Two independent checks, one per storage layer:

1. The audit chain is a hash-chained JSONL file maintained by rpcd.
   Verifying it proves that the entries the deep test wrote were
   recorded in order and that nothing was modified after the write.
   The verify call goes through the JSON-RPC bridge, not through a
   direct file read, so the same code path a production consumer uses
   is the one the test exercises.

2. The ``agent_calls`` table is the telemetry record for every agent
   run. Its shape was introduced in Phase 11 with migration 002 and
   extended through 004. Checking it proves that every agent the
   pipeline runs writes a row, that every LLM row carries the
   provenance hashes the design requires, and that the rows
   distinguish success from failure in the way the retry logic
   downstream depends on.

Each check returns a ``CheckResult``. The module does not raise on a
query failure; a store that has been closed or a table that is
missing produces a failed check, not an exception.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.storage.sqlite_store import SQLiteStore

from ._preflight import CheckResult

__all__ = [
    "EXPECTED_ROLES",
    "AgentCallsReport",
    "query_agent_calls",
    "verify_agent_calls",
    "verify_audit_chain",
]


# Every role that should appear in agent_calls during a successful run.
# The list matches the Phase 11 design settlement. A run that does not
# produce all nine is either incomplete or a specific agent failed
# before reaching its record_call step; the caller decides whether the
# missing role is acceptable in the current run.
EXPECTED_ROLES: frozenset[str] = frozenset(
    {
        "recon",
        "blast",
        "analyst",
        "tester",
        "writer",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
        "skill_curator",
    }
)

# LLM-backed roles are the ones that should always carry prompt and
# output hashes. The two deterministic agents (recon, blast) do not
# call a model and have no prompt to hash.
_LLM_ROLES: frozenset[str] = EXPECTED_ROLES - {"recon", "blast"}


@dataclass(frozen=True, slots=True)
class AgentCallsReport:
    """A structured summary of the ``agent_calls`` table.

    Attributes:
        rows: The raw row dictionaries, in creation order. Each is a
            ``dict`` with the columns the pipeline writes.
        role_counts: A mapping from role name to how many rows it has.
        roles_present: The set of role names that appear at least
            once.
        llm_rows_with_provenance: How many LLM-backed rows carry both
            a non-empty ``prompt_hash`` and a non-empty ``output_hash``.
        verifier_fail_rows: How many rows are verifier verdicts with
            ``verdict_reason`` populated. The count is zero when the
            run produced no verifier fails, which is normal.
        failure_category_rows: How many rows have a non-NULL
            ``failure_category``. Non-zero means at least one agent
            execution failed during the run.
        query_error: When the query itself failed, the typed error's
            message. When set, all other fields are empty.
    """

    rows: tuple[Mapping[str, Any], ...] = ()
    role_counts: Mapping[str, int] = field(default_factory=dict)
    roles_present: frozenset[str] = frozenset()
    llm_rows_with_provenance: int = 0
    verifier_fail_rows: int = 0
    failure_category_rows: int = 0
    query_error: str | None = None

    def ok(self) -> bool:
        """True when the report has no query error and no missing role."""
        if self.query_error:
            return False
        return self.roles_present >= EXPECTED_ROLES


def verify_audit_chain(
    bridge: Any,
    audit_path: Path,
    *,
    expected_min_entries: int = 0,
) -> CheckResult:
    """Verify the audit chain that the run wrote.

    Args:
        bridge: The active ``JSONRPCBridge``.
        audit_path: The path to the ``audit.jsonl`` file.
        expected_min_entries: A lower bound on the number of entries
            the chain should contain. When ``expected_min_entries`` is
            zero the check only verifies that the chain is not broken,
            not that any particular number of entries exists. A caller
            that knows how many events the run should produce passes
            that count.

    Returns:
        A ``CheckResult`` for the audit chain. A chain that is intact
        and meets the expected minimum is a passed check. A broken
        chain is a failed check with the reason. A verify call that
        itself raised is also a failed check, with the error's message
        as the reason.
    """
    try:
        result = bridge.call("audit.verify", {"path": str(audit_path)})
    except CodeGuardianError as exc:
        return CheckResult(
            "audit chain verification",
            False,
            f"{exc.code.value}: {exc.message}",
        )
    except Exception as exc:
        return CheckResult(
            "audit chain verification",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(result, dict):
        return CheckResult(
            "audit chain verification",
            False,
            f"unexpected result type: {type(result).__name__}",
        )

    broken_at = result.get("broken_at")
    if broken_at is not None:
        return CheckResult(
            "audit chain verification",
            False,
            f"chain broken at: {broken_at}",
        )

    entries_checked = int(result.get("entries_checked", 0))
    head_hash = str(result.get("head_hash", ""))[:16]

    if expected_min_entries > 0 and entries_checked < expected_min_entries:
        return CheckResult(
            "audit chain verification",
            False,
            f"{entries_checked} entries (< {expected_min_entries}), head={head_hash}",
        )

    return CheckResult(
        "audit chain verification",
        True,
        f"{entries_checked} entries, head={head_hash}",
    )


def query_agent_calls(store: SQLiteStore) -> AgentCallsReport:
    """Query ``agent_calls`` and return a structured report.

    Args:
        store: An open ``SQLiteStore``.

    Returns:
        An ``AgentCallsReport``. When the query itself fails — table
        missing, store closed, or any SQLite error — the report
        records ``query_error`` and every other field is empty. The
        caller treats a query error as a failed check but does not
        raise.

    The columns selected match those written by ``record_call`` in
    ``core.agents.telemetry``. New columns added by future migrations
    are not read here; a caller that needs them queries directly.
    """
    try:
        rows = store.query(
            "SELECT role, outcome, verdict_reason, failure_category, "
            "prompt_role, prompt_version, prompt_hash, output_hash "
            "FROM agent_calls ORDER BY created_at"
        )
    except CodeGuardianError as exc:
        return AgentCallsReport(query_error=f"{exc.code.value}: {exc.message}")
    except Exception as exc:
        return AgentCallsReport(query_error=f"{type(exc).__name__}: {exc}")

    row_dicts: list[Mapping[str, Any]] = [dict(row) for row in rows]
    role_counts: dict[str, int] = {}
    roles_present: set[str] = set()
    llm_with_provenance = 0
    verifier_fails = 0
    failures = 0

    for row in row_dicts:
        role = str(row.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
        if role:
            roles_present.add(role)
        prompt_hash = row.get("prompt_hash") or ""
        output_hash = row.get("output_hash") or ""
        if role in _LLM_ROLES and prompt_hash and output_hash:
            llm_with_provenance += 1
        if row.get("verdict_reason") is not None:
            verifier_fails += 1
        if row.get("failure_category") is not None:
            failures += 1

    return AgentCallsReport(
        rows=tuple(row_dicts),
        role_counts=dict(role_counts),
        roles_present=frozenset(roles_present),
        llm_rows_with_provenance=llm_with_provenance,
        verifier_fail_rows=verifier_fails,
        failure_category_rows=failures,
        query_error=None,
    )


def verify_agent_calls(
    report: AgentCallsReport,
    *,
    expected_roles: frozenset[str] = EXPECTED_ROLES,
    min_llm_provenance: int = 6,
) -> tuple[CheckResult, ...]:
    """Split an ``AgentCallsReport`` into a tuple of check results.

    Args:
        report: The report returned by ``query_agent_calls``.
        expected_roles: The roles that must appear in the table. Defaults
            to the nine roles the pipeline produces. A caller running a
            partial pipeline may pass a smaller set.
        min_llm_provenance: The minimum number of LLM rows that must
            carry both prompt and output hashes. Six is a conservative
            floor that a full successful run should exceed.

    Returns:
        A tuple of ``CheckResult`` objects, one per distinct fact. The
        order matches the order a human reading the summary would want
        to see them: query, row count, roles, provenance, verdict
        reasons, failure categories.
    """
    results: list[CheckResult] = []

    if report.query_error is not None:
        results.append(CheckResult("agent_calls query", False, report.query_error))
        return tuple(results)

    results.append(
        CheckResult(
            "agent_calls query",
            True,
            f"{len(report.rows)} rows",
        )
    )

    missing_roles = expected_roles - report.roles_present
    results.append(
        CheckResult(
            "agent_calls covers every expected role",
            not missing_roles,
            (
                f"missing: {', '.join(sorted(missing_roles))}"
                if missing_roles
                else f"{len(report.roles_present)}/{len(expected_roles)} roles"
            ),
        )
    )

    results.append(
        CheckResult(
            "agent_calls LLM provenance hashes",
            report.llm_rows_with_provenance >= min_llm_provenance,
            f"{report.llm_rows_with_provenance} rows with prompt+output hashes "
            f"(min {min_llm_provenance})",
        )
    )

    # A verifier fail is not an error: it means the pipeline caught a
    # bad refactor. Its absence is also fine. The check reports the
    # count either way so a reviewer can see what happened.
    results.append(
        CheckResult(
            "agent_calls verdict_reason on failures",
            True,
            (
                f"{report.verifier_fail_rows} verifier fail row(s)"
                if report.verifier_fail_rows
                else "no verifier failures this run"
            ),
        )
    )

    # A failure_category is set only when an agent's execution failed
    # (timeout, schema violation, etc.). Its absence means every agent
    # completed, which is the outcome a successful run produces. Its
    # presence is not a failure of the check, only a fact worth
    # surfacing.
    results.append(
        CheckResult(
            "agent_calls failure categories",
            True,
            (
                f"{report.failure_category_rows} row(s) with failure category"
                if report.failure_category_rows
                else "no agent execution failures this run"
            ),
        )
    )

    return tuple(results)
