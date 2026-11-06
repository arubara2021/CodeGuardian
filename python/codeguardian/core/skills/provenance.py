"""Provenance pins for skills.

A provenance pin records where a skill came from and how it was
admitted. It is written once at admission and read during security
investigations and rollback. The pin is stored in two places:

1. A subset appears in the ``SKILL.md`` frontmatter so the source of
   truth carries its own history. The subset is ``source_runs``,
   ``authored_by``, ``commit_hash``, and ``license``.
2. The full record lives in the ``skill_provenance`` table so that
   fields not suitable for hand-editing the Markdown file, such as
   the scanner result and the eval score, survive.

This module depends on Phase 1's error taxonomy but adds no new codes.
A pin that fails the completeness check raises ``STORAGE_SCHEMA_INVALID``
because the failure is a shape problem, not a runtime problem, and it
is not retryable. Every other code path either returns a value or
returns ``None``; nothing in this module degrades silently.

The completeness rule is deliberately asymmetric. A skill authored by
the Skill Curator must cite at least one source run; that is what makes
the origin traceable. A skill authored by a human may cite zero runs
because the human is the origin.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .types import ProvenancePin

__all__ = [
    "build_pin",
    "compute_schema_hash",
    "load_pin",
    "require_complete",
    "save_pin",
    "verify_complete",
]

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compute_schema_hash(schemas_dir: Path) -> str:
    """Return the SHA-256 of the skill frontmatter schema file.

    The hash is recorded on every provenance pin so a later investigation
    can tell which version of the schema a skill validated against. When
    the schema changes, the hash changes, and any pin recorded under the
    old schema is distinguishable from one recorded under the new one.

    Args:
        schemas_dir: The directory holding the JSON Schema files.

    Returns:
        A hex digest prefixed with ``sha256:``.

    Raises:
        CodeGuardianError: With code ``STORAGE_SCHEMA_INVALID`` if the
            schema file cannot be read.
    """
    schema_path = Path(schemas_dir) / "skill.json"
    try:
        content = schema_path.read_bytes()
    except FileNotFoundError as exc:
        raise CodeGuardianError(
            ErrorCode.STORAGE_SCHEMA_INVALID,
            f"Skill schema file not found: {schema_path}",
            details={"path": str(schema_path)},
        ) from exc
    except OSError as exc:
        raise CodeGuardianError(
            ErrorCode.STORAGE_SCHEMA_INVALID,
            f"Cannot read skill schema file: {exc}",
            details={"path": str(schema_path)},
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    return f"sha256:{digest}"


def build_pin(
    *,
    source_runs: tuple[str, ...] | list[str] = (),
    authored_by: str,
    commit_hash: str,
    license: str = "",
    kernel_version: str = "",
    schema_hash: str = "",
    scan_result: str = "",
    eval_score: float | None = None,
    reviewer: str = "auto",
    reviewer_id: str | None = None,
    admission_evidence: dict[str, Any] | None = None,
) -> ProvenancePin:
    """Assemble a ``ProvenancePin`` from the values recorded at admission.

    This function does not verify completeness. Callers that require
    a complete pin call ``require_complete`` after building. Callers
    that are still assembling a pin (for example, before the eval gate
    has run) build it here and fill in the missing fields later.

    Args:
        source_runs: Run identifiers that produced the skill.
        authored_by: ``"skill_curator"`` or ``"human"``.
        commit_hash: The repository HEAD at proposal time.
        license: The declared or inherited license identifier.
        kernel_version: The kernel version active at admission.
        schema_hash: The hash of the schema the skill validated against.
        scan_result: The scanner's verdict, one of ``"clean"``,
            ``"warn"``, or ``"block"``.
        eval_score: The eval harness score, or ``None`` when skipped.
        reviewer: ``"auto"`` or ``"human"``.
        reviewer_id: A human reviewer identifier, or ``None``.
        admission_evidence: Additional evidence captured by the
            admission gate.

    Returns:
        A frozen ``ProvenancePin``.
    """
    return ProvenancePin(
        source_runs=tuple(source_runs),
        authored_by=authored_by,
        commit_hash=commit_hash,
        license=license,
        schema_hash=schema_hash,
        scan_result=scan_result,
        eval_score=eval_score,
        reviewer=reviewer,
        reviewer_id=reviewer_id,
        kernel_version=kernel_version,
        admission_evidence=admission_evidence or {},
    )


def verify_complete(pin: ProvenancePin) -> tuple[bool, tuple[str, ...]]:
    """Return whether a pin satisfies the completeness rule.

    The rule: every pin must name an author and a commit. A pin whose
    author is ``"skill_curator"`` must additionally cite at least one
    source run. A pin authored by a human may cite zero runs.

    Args:
        pin: The pin to check.

    Returns:
        A tuple of ``(is_complete, missing_fields)``. When complete,
        the second element is an empty tuple. When incomplete, it lists
        the names of the fields that are missing or invalid.
    """
    missing: list[str] = []
    if not pin.authored_by or pin.authored_by not in ("skill_curator", "human"):
        missing.append("authored_by")
    if not pin.commit_hash:
        missing.append("commit_hash")
    if pin.authored_by == "skill_curator" and not pin.source_runs:
        missing.append("source_runs")
    return (len(missing) == 0, tuple(missing))


def require_complete(pin: ProvenancePin) -> None:
    """Raise if the pin does not satisfy the completeness rule.

    Args:
        pin: The pin to check.

    Raises:
        CodeGuardianError: With code ``STORAGE_SCHEMA_INVALID`` if any
            required field is missing or invalid. The ``details`` field
            carries the list of missing fields under the key
            ``missing``.
    """
    complete, missing = verify_complete(pin)
    if complete:
        return
    raise CodeGuardianError(
        ErrorCode.STORAGE_SCHEMA_INVALID,
        f"Skill provenance is incomplete: missing {', '.join(missing)}",
        details={"missing": list(missing)},
    )


def save_pin(
    store: SQLiteStore,
    skill_name: str,
    pin: ProvenancePin,
) -> None:
    """Write a pin to the ``skill_provenance`` table.

    The operation is an upsert. Re-admitting a skill overwrites its
    previous pin. The history of admission attempts is preserved in
    the ``skill_admission_log`` table, which is append-only.

    Args:
        store: An open ``SQLiteStore``.
        skill_name: The skill the pin belongs to.
        pin: The pin to persist.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if the write
            fails.
    """
    source_runs_json = json.dumps(list(pin.source_runs))
    store.execute(
        """
        INSERT OR REPLACE INTO skill_provenance (
            skill_name, source_runs_json, authored_by, commit_hash,
            license, schema_hash, scan_result, eval_score, reviewer,
            reviewer_id, kernel_version, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            skill_name,
            source_runs_json,
            pin.authored_by,
            pin.commit_hash,
            pin.license or None,
            pin.schema_hash or None,
            pin.scan_result or None,
            pin.eval_score,
            pin.reviewer or "auto",
            pin.reviewer_id,
            pin.kernel_version or None,
            _now_iso(),
        ),
    )


def load_pin(store: SQLiteStore, skill_name: str) -> ProvenancePin | None:
    """Read a pin from the ``skill_provenance`` table.

    The ``admission_evidence`` field of the returned pin is
    reconstructed from the persisted columns rather than stored
    separately. That keeps the persisted record authoritative for the
    individual fields while still giving the caller a single object.

    Args:
        store: An open ``SQLiteStore``.
        skill_name: The skill to look up.

    Returns:
        The pin, or ``None`` if no row exists for the skill.
    """
    rows = store.query(
        """
        SELECT source_runs_json, authored_by, commit_hash, license,
               schema_hash, scan_result, eval_score, reviewer,
               reviewer_id, kernel_version
        FROM skill_provenance
        WHERE skill_name = ?
        """,
        (skill_name,),
    )
    if not rows:
        return None
    row = rows[0]
    try:
        source_runs = tuple(json.loads(row["source_runs_json"] or "[]"))
    except json.JSONDecodeError:
        source_runs = ()
    admission_evidence = {
        "schema_hash": row["schema_hash"],
        "scan_result": row["scan_result"],
        "eval_score": row["eval_score"],
        "reviewer": row["reviewer"],
    }
    return ProvenancePin(
        source_runs=source_runs,
        authored_by=row["authored_by"],
        commit_hash=row["commit_hash"],
        license=row["license"] or "",
        schema_hash=row["schema_hash"] or "",
        scan_result=row["scan_result"] or "",
        eval_score=row["eval_score"],
        reviewer=row["reviewer"] or "auto",
        reviewer_id=row["reviewer_id"],
        kernel_version=row["kernel_version"] or "",
        admission_evidence=admission_evidence,
    )
