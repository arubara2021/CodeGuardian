"""Negative-knowledge store.

A negative record is not a low-effectiveness skill. It is a separate
knowledge object describing a specific failure in a specific context.
The record answers three questions: what was attempted, what actually
happened, and under what conditions. The agent that reads it adopts or
rejects it before the next attempt; the adoption counters on the
record track those decisions over time.

Keeping negative records separate from positive skills matters for one
reason: they decay on a different schedule. A positive pattern learned
under an old library version may still be right today. A negative
constraint from the same era may be wrong, because the failure it
describes is no longer possible. Combining the two under a single
time-to-live erases one or the other incorrectly.

This module never raises on a read. If the store is unavailable or the
table is empty, ``find_relevant`` returns an empty tuple and the
retrieval pipeline falls back to positive skills only. That matches the
contract in the design document: negative knowledge is additive, never
load-bearing.

Write operations raise. A failed insert, adopt, or reject would
silently lose information the caller believes it has recorded. Better
to fail loudly than to lose it quietly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .types import NegativeRecord, ProvenancePin

__all__ = [
    "add_record",
    "adopt",
    "apply_forgetting_curve",
    "delete_record",
    "find_relevant",
    "get_record",
    "list_all",
    "reject",
]

logger = logging.getLogger(__name__)


_VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "localization",
        "generation",
        "schema",
        "contract",
        "timeout",
        "context_overflow",
        "model_unavailable",
    }
)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _row_to_record(row: Mapping[str, Any]) -> NegativeRecord:
    """Convert a ``negative_knowledge`` row into a ``NegativeRecord``."""
    provenance: ProvenancePin | None = None
    provenance_json = row["provenance_json"]
    if provenance_json:
        try:
            data = json.loads(provenance_json)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            provenance = ProvenancePin(
                source_runs=tuple(data.get("source_runs", ())),
                authored_by=str(data.get("authored_by", "auto")),
                commit_hash=str(data.get("commit_hash", "")),
                license=str(data.get("license", "")),
                schema_hash=str(data.get("schema_hash", "")),
                scan_result=str(data.get("scan_result", "")),
                eval_score=data.get("eval_score"),
                reviewer=str(data.get("reviewer", "auto")),
                reviewer_id=data.get("reviewer_id"),
                kernel_version=str(data.get("kernel_version", "")),
                admission_evidence=data.get("admission_evidence", {}),
            )
    return NegativeRecord(
        failure_id=str(row["failure_id"]),
        skill_name=str(row["skill_name"]),
        failure_category=str(row["failure_category"]),
        context_signature=str(row["context_signature"]),
        attempted=str(row["attempted"]),
        observed=str(row["observed"]),
        run_id=str(row["run_id"]) if row["run_id"] else "",
        confidence=float(row["confidence"]),
        first_seen=str(row["first_seen"]),
        last_seen=str(row["last_seen"]),
        provenance=provenance,
        adopted_count=int(row["adopted_count"]),
        rejected_count=int(row["rejected_count"]),
    )


def _pin_to_json(pin: ProvenancePin | None) -> str | None:
    """Serialize a provenance pin to JSON for storage, or ``None``."""
    if pin is None:
        return None
    return json.dumps(
        {
            "source_runs": list(pin.source_runs),
            "authored_by": pin.authored_by,
            "commit_hash": pin.commit_hash,
            "license": pin.license,
            "schema_hash": pin.schema_hash,
            "scan_result": pin.scan_result,
            "eval_score": pin.eval_score,
            "reviewer": pin.reviewer,
            "reviewer_id": pin.reviewer_id,
            "kernel_version": pin.kernel_version,
            "admission_evidence": dict(pin.admission_evidence),
        }
    )


def add_record(store: SQLiteStore, record: NegativeRecord) -> None:
    """Insert or replace a failure record.

    Args:
        store: An open ``SQLiteStore``.
        record: The record to persist.

    Raises:
        CodeGuardianError: With code ``STORAGE_SCHEMA_INVALID`` if the
            failure category is not one of the recognized values. With
            code ``SYS_INTERNAL`` if the insert fails for another
            reason.
    """
    if record.failure_category not in _VALID_CATEGORIES:
        raise CodeGuardianError(
            record.failure_category,
            f"Unknown failure category: {record.failure_category!r}",
            details={"valid": sorted(_VALID_CATEGORIES)},
        )
    store.execute(
        """
        INSERT OR REPLACE INTO negative_knowledge (
            failure_id, skill_name, failure_category, context_signature,
            attempted, observed, run_id, confidence, first_seen, last_seen,
            provenance_json, adopted_count, rejected_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.failure_id,
            record.skill_name,
            record.failure_category,
            record.context_signature,
            record.attempted,
            record.observed,
            record.run_id or None,
            record.confidence,
            record.first_seen,
            record.last_seen,
            _pin_to_json(record.provenance),
            record.adopted_count,
            record.rejected_count,
        ),
    )


def get_record(store: SQLiteStore, failure_id: str) -> NegativeRecord | None:
    """Return one failure record by identifier, or ``None``."""
    try:
        rows = store.query(
            "SELECT * FROM negative_knowledge WHERE failure_id = ?",
            (failure_id,),
        )
    except CodeGuardianError:
        logger.warning("Negative store query failed for %s", failure_id, exc_info=True)
        return None
    if not rows:
        return None
    return _row_to_record(rows[0])


def find_relevant(
    store: SQLiteStore,
    context_signature: str,
    *,
    min_confidence: float = 0.1,
    limit: int = 5,
) -> tuple[NegativeRecord, ...]:
    """Return records whose context signature matches the query.

    The lookup is an exact match on the signature. Fuzzy matching is
    deliberately not implemented here because a failure that occurred
    in a different context may not apply, and a wrong negative
    constraint is worse than no constraint.

    Args:
        store: An open ``SQLiteStore``.
        context_signature: The signature to look up.
        min_confidence: Records below this confidence are filtered out.
        limit: The maximum number of records to return.

    Returns:
        The matching records in descending confidence order, or an
        empty tuple if the store is unavailable or no record matches.
    """
    try:
        rows = store.query(
            """
            SELECT * FROM negative_knowledge
            WHERE context_signature = ? AND confidence >= ?
            ORDER BY confidence DESC, last_seen DESC
            LIMIT ?
            """,
            (context_signature, min_confidence, limit),
        )
    except CodeGuardianError:
        logger.warning(
            "Negative store query failed for signature %s",
            context_signature,
            exc_info=True,
        )
        return ()
    return tuple(_row_to_record(row) for row in rows)


def list_all(store: SQLiteStore) -> tuple[NegativeRecord, ...]:
    """Return every failure record in the store, or an empty tuple."""
    try:
        rows = store.query("SELECT * FROM negative_knowledge ORDER BY last_seen DESC")
    except CodeGuardianError:
        logger.warning("Negative store listing failed", exc_info=True)
        return ()
    return tuple(_row_to_record(row) for row in rows)


def adopt(store: SQLiteStore, failure_id: str) -> None:
    """Record that an agent adopted a failure record before a retry.

    Args:
        store: An open ``SQLiteStore``.
        failure_id: The record to update.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if the update
            fails.
    """
    store.execute(
        "UPDATE negative_knowledge SET adopted_count = adopted_count + 1 WHERE failure_id = ?",
        (failure_id,),
    )


def reject(store: SQLiteStore, failure_id: str) -> None:
    """Record that an agent rejected a failure record before a retry.

    Args:
        store: An open ``SQLiteStore``.
        failure_id: The record to update.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if the update
            fails.
    """
    store.execute(
        "UPDATE negative_knowledge SET rejected_count = rejected_count + 1 WHERE failure_id = ?",
        (failure_id,),
    )


def delete_record(store: SQLiteStore, failure_id: str) -> bool:
    """Delete a failure record.

    Args:
        store: An open ``SQLiteStore``.
        failure_id: The record to delete.

    Returns:
        True if a row was deleted, False if no row existed.
    """
    existing = store.query(
        "SELECT 1 FROM negative_knowledge WHERE failure_id = ? LIMIT 1",
        (failure_id,),
    )
    if not existing:
        return False
    store.execute(
        "DELETE FROM negative_knowledge WHERE failure_id = ?",
        (failure_id,),
    )
    return True


def apply_forgetting_curve(
    store: SQLiteStore,
    *,
    decay_factor: float = 0.9,
    reference_days: int = 30,
    min_confidence: float = 0.1,
    max_per_skill: int = 20,
) -> int:
    """Decay old records and prune those that fall below the floor.

    Three effects are applied in one pass:

    1. Every record's confidence is multiplied by a decay factor that
       depends on how long ago it was last seen. A record seen within
       the reference window is untouched; a record older than one
       reference window is decayed once, older than two windows twice,
       and so on.
    2. Records whose confidence has fallen below ``min_confidence`` are
       deleted.
    3. Records beyond the per-skill cap, ordered by confidence, are
       deleted. The cap protects against one noisy skill accumulating
       hundreds of low-quality failure records.

    Args:
        store: An open ``SQLiteStore``.
        decay_factor: The multiplier applied per ``reference_days`` of
            age. Must be between zero and one.
        reference_days: The age, in days, at which one decay factor is
            applied.
        min_confidence: The floor below which a record is deleted.
        max_per_skill: The maximum number of records retained for any
            one skill.

    Returns:
        The number of records deleted.
    """
    if not (0.0 < decay_factor <= 1.0):
        return 0
    if reference_days <= 0:
        return 0

    now = datetime.now(UTC)
    deleted = 0

    rows = store.query(
        "SELECT failure_id, skill_name, confidence, last_seen FROM negative_knowledge"
    )
    for row in rows:
        try:
            last_seen = datetime.fromisoformat(str(row["last_seen"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = max((now - last_seen).days, 0)
        periods = age_days / reference_days
        new_confidence = float(row["confidence"]) * (decay_factor**periods)
        if new_confidence < min_confidence:
            store.execute(
                "DELETE FROM negative_knowledge WHERE failure_id = ?",
                (row["failure_id"],),
            )
            deleted += 1
        elif abs(new_confidence - float(row["confidence"])) > 1e-9:
            store.execute(
                "UPDATE negative_knowledge SET confidence = ? WHERE failure_id = ?",
                (new_confidence, row["failure_id"]),
            )

    for skill_row in store.query("SELECT DISTINCT skill_name FROM negative_knowledge"):
        skill_name = skill_row["skill_name"]
        over = store.query(
            """
            SELECT failure_id FROM negative_knowledge
            WHERE skill_name = ?
            ORDER BY confidence DESC, last_seen DESC
            LIMIT -1 OFFSET ?
            """,
            (skill_name, max_per_skill),
        )
        for row in over:
            store.execute(
                "DELETE FROM negative_knowledge WHERE failure_id = ?",
                (row["failure_id"],),
            )
            deleted += 1

    return deleted
