"""Checkpoint persistence.

A checkpoint is the resumable state of a run at a phase boundary. The
loop writes one after each phase completes; a supervisor reads the
latest to build a resume plan; a crash between phases leaves the
last-written checkpoint intact so the next process can continue from
there.

The underlying table comes from migration 001 and holds four columns:
``checkpoint_id``, ``run_id``, ``phase``, ``state_json``, and
``created_at``. Migration 007 adds ``heartbeat_at`` so a long-running
phase can report liveness without waiting for the phase to complete.

``state_json`` holds the schema-shaped payload from
``schemas/checkpoint.json``: schema_version, completed_phases,
pending_phases, retries, state (arbitrary per-phase data), and
artifacts (name to path). The row columns hold the identity and
timing that the schema names but the pipeline produces directly.

``write_checkpoint`` composes both and inserts one row. It picks up
the ``heartbeat_at`` column when it is present and falls back to a
column-less insert when migration 007 has not been applied, so the
module works before and after the migration.

``read_checkpoint`` returns the most recent checkpoint for a run as a
``Checkpoint`` dataclass, or ``None`` when the run has no checkpoints
yet. It degrades to ``heartbeat_at=None`` on a pre-007 database.

``touch_heartbeat`` updates the most recent checkpoint's
``heartbeat_at`` to the current time. Returns ``False`` when the run
has no checkpoints yet (nothing to touch) or when the heartbeat
column is missing (pre-007). The supervisor uses the return value to
distinguish "no liveness signal yet" from "liveness signal updated".
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from codeguardian.core.errors import CodeGuardianError

from ..storage.sqlite_store import SQLiteStore

__all__ = [
    "Checkpoint",
    "list_checkpoints",
    "read_checkpoint",
    "touch_heartbeat",
    "write_checkpoint",
]


_DEFAULT_SCHEMA_VERSION = "1.0"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _heartbeat_supported(store: SQLiteStore) -> bool:
    """Return True when the checkpoints table has the heartbeat_at column.

    The column is added by migration 007. Checking via PRAGMA is a few
    microseconds; the check is repeated on every call rather than
    cached because the cost is negligible and a cache would have to be
    invalidated when the migration runs mid-process (which does not
    happen in practice, but the invariant is easier to keep when the
    check is stateless).
    """
    try:
        rows = store.query("PRAGMA table_info(checkpoints)")
    except CodeGuardianError:
        return False
    for row in rows:
        try:
            if str(row["name"]) == "heartbeat_at":
                return True
        except (KeyError, TypeError):
            continue
    return False


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A persisted checkpoint.

    Attributes:
        schema_version: The checkpoint schema version. Always ``"1.0"``
            for checkpoints written by this module.
        checkpoint_id: The unique identifier.
        run_id: The run this checkpoint belongs to.
        phase: The name of the phase that just completed.
        created_at: ISO 8601 timestamp of when the checkpoint was
            written.
        heartbeat_at: The most recent liveness timestamp, or ``None``
            when the heartbeat column is not present or has never been
            touched.
        completed_phases: Every phase completed up to and including
            ``phase``.
        pending_phases: Every phase still waiting to run.
        retries: The retry count accumulated at this checkpoint.
        state: Phase-specific state, opaque to this module.
        artifacts: A name-to-path map of the artifacts produced so far.
    """

    schema_version: str
    checkpoint_id: str
    run_id: str
    phase: str
    created_at: str
    heartbeat_at: str | None
    completed_phases: tuple[str, ...]
    pending_phases: tuple[str, ...]
    retries: int
    state: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)


def write_checkpoint(
    store: SQLiteStore,
    *,
    run_id: str,
    phase: str,
    state: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, str] | None = None,
    completed_phases: tuple[str, ...] = (),
    pending_phases: tuple[str, ...] = (),
    retries: int = 0,
    schema_version: str = _DEFAULT_SCHEMA_VERSION,
) -> str:
    """Write one checkpoint row and return its checkpoint_id.

    Args:
        store: An open SQLiteStore. The ``checkpoints`` table must
            exist; migration 001 creates it.
        run_id: The run this checkpoint belongs to. Must reference an
            existing row in ``runs`` unless the caller has disabled
            foreign keys (which the migration runner does not).
        phase: The phase that just completed.
        state: Arbitrary per-phase state. Serialized to
            ``state_json``. Defaults to an empty mapping.
        artifacts: A name-to-path mapping of artifacts produced so far.
            Defaults to an empty mapping.
        completed_phases: Every phase completed up to and including
            ``phase``. Defaults to an empty tuple.
        pending_phases: Every phase still waiting to run. Defaults to
            an empty tuple.
        retries: The retry count accumulated so far. Defaults to 0.
        schema_version: The checkpoint schema version. Defaults to
            ``"1.0"``.

    Returns:
        The checkpoint_id that was written.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` when the insert
            fails for any reason other than a missing heartbeat_at
            column.
    """
    checkpoint_id = str(uuid.uuid4())
    created_at = _now_iso()

    state_json = json.dumps(
        {
            "schema_version": schema_version,
            "completed_phases": list(completed_phases),
            "pending_phases": list(pending_phases),
            "retries": int(retries),
            "state": dict(state or {}),
            "artifacts": dict(artifacts or {}),
        },
        sort_keys=True,
    )

    if _heartbeat_supported(store):
        store.execute(
            """
            INSERT INTO checkpoints
                (checkpoint_id, run_id, phase, state_json,
                 created_at, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                run_id,
                phase,
                state_json,
                created_at,
                created_at,
            ),
        )
    else:
        store.execute(
            """
            INSERT INTO checkpoints
                (checkpoint_id, run_id, phase, state_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (checkpoint_id, run_id, phase, state_json, created_at),
        )

    return checkpoint_id


def read_checkpoint(
    store: SQLiteStore,
    run_id: str,
) -> Checkpoint | None:
    """Return the most recent checkpoint for ``run_id``, or ``None``.

    Args:
        store: An open SQLiteStore.
        run_id: The run to look up.

    Returns:
        The most recent ``Checkpoint`` or ``None`` when the run has no
        checkpoints yet.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` on a read
            failure, or ``SYS_INVARIANT_VIOLATED`` when the stored
            ``state_json`` is not valid JSON or names an unsupported
            schema version.
    """
    rows = _read_rows(store, run_id, limit=1)
    if not rows:
        return None
    return _row_to_checkpoint(rows[0])


def list_checkpoints(
    store: SQLiteStore,
    run_id: str,
    *,
    limit: int = 100,
) -> tuple[Checkpoint, ...]:
    """Return every checkpoint for ``run_id``, newest first.

    Args:
        store: An open SQLiteStore.
        run_id: The run to look up.
        limit: The maximum number of checkpoints to return. Defaults to
            100.

    Returns:
        The checkpoints in descending ``created_at`` order.
    """
    rows = _read_rows(store, run_id, limit=limit)
    return tuple(_row_to_checkpoint(row) for row in rows)


def touch_heartbeat(store: SQLiteStore, run_id: str) -> bool:
    """Update the most recent checkpoint's heartbeat_at to now.

    Args:
        store: An open SQLiteStore.
        run_id: The run whose checkpoint to touch.

    Returns:
        True when a checkpoint was updated, False when the run has no
        checkpoints yet or when the heartbeat_at column is not present
        (migration 007 not applied).
    """
    if not _heartbeat_supported(store):
        return False

    now = _now_iso()
    cursor = store.execute(
        """
        UPDATE checkpoints
        SET heartbeat_at = ?
        WHERE checkpoint_id = (
            SELECT checkpoint_id
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY created_at DESC, checkpoint_id DESC
            LIMIT 1
        )
        """,
        (now, run_id),
    )
    return cursor.rowcount > 0


def _read_rows(
    store: SQLiteStore,
    run_id: str,
    *,
    limit: int,
) -> list[Any]:
    """Return checkpoint rows for ``run_id`` newest first.

    Selects heartbeat_at when the column is present, falls back to a
    query that returns NULL in its place. Uses COALESCE in the
    fallback so downstream parsing does not branch.
    """
    if _heartbeat_supported(store):
        return store.query(
            """
            SELECT checkpoint_id, run_id, phase, state_json,
                   created_at, heartbeat_at
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY created_at DESC, checkpoint_id DESC
            LIMIT ?
            """,
            (run_id, limit),
        )
    return store.query(
        """
        SELECT checkpoint_id, run_id, phase, state_json,
               created_at, NULL AS heartbeat_at
        FROM checkpoints
        WHERE run_id = ?
        ORDER BY created_at DESC, checkpoint_id DESC
        LIMIT ?
        """,
        (run_id, limit),
    )


def _row_to_checkpoint(row: Any) -> Checkpoint:
    """Convert one checkpoint row into a ``Checkpoint`` dataclass."""
    try:
        state_dict = json.loads(row["state_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise CodeGuardianError(
            "SYS_INVARIANT_VIOLATED",
            f"checkpoint {row['checkpoint_id']!r} has an unparseable state_json: {exc}",
            details={"checkpoint_id": row["checkpoint_id"]},
        ) from exc

    if not isinstance(state_dict, dict):
        raise CodeGuardianError(
            "SYS_INVARIANT_VIOLATED",
            f"checkpoint {row['checkpoint_id']!r} state_json is not a JSON object",
            details={"checkpoint_id": row["checkpoint_id"]},
        )

    schema_version = str(state_dict.get("schema_version", "1.0"))
    if schema_version != _DEFAULT_SCHEMA_VERSION:
        raise CodeGuardianError(
            "SYS_INVARIANT_VIOLATED",
            f"checkpoint {row['checkpoint_id']!r} declares "
            f"unsupported schema_version {schema_version!r}; "
            f"this module reads {_DEFAULT_SCHEMA_VERSION!r}",
            details={
                "checkpoint_id": row["checkpoint_id"],
                "schema_version": schema_version,
            },
        )

    completed = tuple(str(p) for p in (state_dict.get("completed_phases") or ()))
    pending = tuple(str(p) for p in (state_dict.get("pending_phases") or ()))
    retries = int(state_dict.get("retries", 0) or 0)
    state_payload = state_dict.get("state") or {}
    artifacts_payload = state_dict.get("artifacts") or {}

    return Checkpoint(
        schema_version=schema_version,
        checkpoint_id=str(row["checkpoint_id"]),
        run_id=str(row["run_id"]),
        phase=str(row["phase"]),
        created_at=str(row["created_at"]),
        heartbeat_at=(str(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None),
        completed_phases=completed,
        pending_phases=pending,
        retries=retries,
        state=dict(state_payload) if isinstance(state_payload, dict) else {},
        artifacts=(
            {str(k): str(v) for k, v in artifacts_payload.items()}
            if isinstance(artifacts_payload, dict)
            else {}
        ),
    )
