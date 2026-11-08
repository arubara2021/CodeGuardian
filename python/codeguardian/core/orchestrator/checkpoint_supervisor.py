"""Checkpoint supervisor.

Three responsibilities, each one small.

First, liveness. The supervisor exposes ``heartbeat(run_id)``, which
touches the most recent checkpoint's ``heartbeat_at``. A long-running
phase can call it every few seconds to signal that the process is
still alive. A phase that hangs without calling it will eventually be
classified as stale by ``find_stale_runs``.

Second, detection. ``find_stale_runs`` returns every run whose
``runs.status`` is ``'running'`` and whose most recent checkpoint is
older than the supervisor's threshold. A run with no checkpoints at
all falls back to its ``runs.started_at`` so a phase-1 hang is also
detected. The threshold defaults to five minutes and is configurable
at construction.

Third, resume. ``build_resume_plan`` reads the most recent checkpoint
and produces a ``ResumePlan`` that the orchestrator loop uses to
re-enter the pipeline. A run with no checkpoint yields a plan whose
``resume_after_phase`` is ``None``, meaning the caller should start
from the graph's entry point. A run with a checkpoint yields a plan
whose ``resume_after_phase`` is the checkpoint's ``phase`` and whose
``pending_phases`` are the phases still to run.

The supervisor does not call the loop. It produces the plan; the
orchestrator walks it. That separation keeps the supervisor testable
without a running pipeline and keeps the loop free of resume logic.

Two hundred lines, no code paths that surprise. Every query is a
prepared statement against existing tables. The only column this
module depends on beyond migration 001 is ``checkpoints.heartbeat_at``,
which migration 007 adds; the code degrades cleanly when the column
is missing so a fresh build can run this module before block 7 lands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from codeguardian.core.errors import CodeGuardianError

from ..storage.sqlite_store import SQLiteStore
from .checkpointing import (
    Checkpoint,
    _heartbeat_supported,
    read_checkpoint,
    touch_heartbeat,
)
from .phase_graph import PhaseGraph

__all__ = [
    "DEFAULT_STALE_THRESHOLD_SECONDS",
    "CheckpointSupervisor",
    "ResumePlan",
]


DEFAULT_STALE_THRESHOLD_SECONDS = 300


def _now() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """Return an ISO 8601 string with a Z suffix."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """The information the orchestrator loop needs to resume a run.

    Attributes:
        run_id: The run to resume.
        resume_after_phase: The last completed phase. ``None`` means
            the run has no checkpoints yet and the caller should start
            from the graph's entry point.
        completed_phases: Every phase completed up to and including
            ``resume_after_phase``. Empty when no checkpoint exists.
        pending_phases: Every phase still to run. Empty when no
            checkpoint exists and the caller is expected to derive the
            list from the graph.
        retries: The retry count accumulated so far. Zero when no
            checkpoint exists.
        state: Phase-specific state restored from the checkpoint.
            Empty when no checkpoint exists.
        artifacts: A name-to-path mapping of artifacts produced so
            far. Empty when no checkpoint exists.
        last_checkpoint_id: The checkpoint the plan is built from, or
            ``None`` when the run has no checkpoints yet.
    """

    run_id: str
    resume_after_phase: str | None
    completed_phases: tuple[str, ...] = ()
    pending_phases: tuple[str, ...] = ()
    retries: int = 0
    state: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)
    last_checkpoint_id: str | None = None


class CheckpointSupervisor:
    """Detects stale runs, tracks heartbeats, and builds resume plans.

    Args:
        store: An open SQLiteStore.
        stale_threshold_seconds: The age at which a run's most recent
            heartbeat is considered stale. Defaults to 300 seconds
            (five minutes). A longer threshold means hung runs take
            longer to detect; a shorter threshold risks classifying a
            temporarily-slow phase as stale.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
    ) -> None:
        if stale_threshold_seconds < 1:
            raise ValueError(
                f"stale_threshold_seconds must be at least 1; got {stale_threshold_seconds}"
            )
        self._store = store
        self._threshold = timedelta(seconds=stale_threshold_seconds)

    @property
    def store(self) -> SQLiteStore:
        """The store this supervisor reads and writes."""
        return self._store

    @property
    def stale_threshold_seconds(self) -> int:
        """The configured threshold, in seconds."""
        return int(self._threshold.total_seconds())

    def heartbeat(self, run_id: str) -> bool:
        """Update the most recent checkpoint's heartbeat for ``run_id``.

        Returns:
            True when a checkpoint was updated; False when the run has
            no checkpoints yet or the heartbeat column is absent.
        """
        return touch_heartbeat(self._store, run_id)

    def find_stale_runs(self) -> tuple[str, ...]:
        """Return every stale run id.

        A run is stale when its ``runs.status`` is ``'running'`` and
        its most recent liveness signal is older than the configured
        threshold. The liveness signal is ``checkpoints.heartbeat_at``
        when available, otherwise ``checkpoints.created_at``. A run
        with no checkpoints at all uses ``runs.started_at``.

        Returns:
            The stale run ids, in ascending ``started_at`` order so a
            caller resuming them resumes the oldest first.
        """
        cutoff = _iso(_now() - self._threshold)
        rows = self._stale_rows(cutoff)
        return tuple(str(row["run_id"]) for row in rows)

    def build_resume_plan(
        self,
        run_id: str,
        *,
        graph: PhaseGraph,
    ) -> ResumePlan:
        """Return a plan for resuming ``run_id``.

        Args:
            run_id: The run to plan a resume for.
            graph: The PhaseGraph the run was walking. Used only when
                the run has no checkpoint yet, so the plan can name
                every graph slot as pending.

        Returns:
            A ``ResumePlan``. When the run has at least one
            checkpoint, ``resume_after_phase`` names the last completed
            phase and ``pending_phases`` comes from the checkpoint.
            When the run has no checkpoints, ``resume_after_phase`` is
            ``None`` and ``pending_phases`` is every slot in ``graph``.
        """
        latest: Checkpoint | None = read_checkpoint(self._store, run_id)
        if latest is None:
            return ResumePlan(
                run_id=run_id,
                resume_after_phase=None,
                completed_phases=(),
                pending_phases=graph.slot_names(),
                retries=0,
                state={},
                artifacts={},
                last_checkpoint_id=None,
            )
        return ResumePlan(
            run_id=run_id,
            resume_after_phase=latest.phase,
            completed_phases=latest.completed_phases,
            pending_phases=latest.pending_phases,
            retries=latest.retries,
            state=dict(latest.state),
            artifacts=dict(latest.artifacts),
            last_checkpoint_id=latest.checkpoint_id,
        )

    def resume(
        self,
        run_id: str,
        *,
        graph: PhaseGraph,
    ) -> ResumePlan:
        """Build a resume plan and mark the run as running.

        The caller is expected to have verified that the run is stale
        (via ``find_stale_runs``) before calling this. The method does
        not re-verify staleness; it re-verifies only that the run
        exists. Marking a fresh run as running is a no-op at the
        status level, which is why the check is safe.

        Args:
            run_id: The run to resume.
            graph: The PhaseGraph the run was walking.

        Returns:
            The ``ResumePlan`` produced by ``build_resume_plan``.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when the run does not exist in ``runs``.
        """
        self._require_run(run_id)
        plan = self.build_resume_plan(run_id, graph=graph)
        self.mark_status(run_id, "running")
        return plan

    def mark_status(self, run_id: str, status: str) -> None:
        """Update ``runs.status`` for ``run_id``.

        Args:
            run_id: The run to update.
            status: One of the values allowed by the ``runs.status``
                CHECK constraint: ``'running'``, ``'success'``,
                ``'failure'``, ``'stopped'``, ``'dry_run'``,
                ``'paused'``.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when the status is not one of the allowed values, or
                when the run does not exist.
        """
        allowed = {
            "running",
            "success",
            "failure",
            "stopped",
            "dry_run",
            "paused",
        }
        if status not in allowed:
            raise CodeGuardianError(
                "SYS_INVARIANT_VIOLATED",
                f"unknown run status {status!r}; allowed: {sorted(allowed)}",
                details={"run_id": run_id, "status": status},
            )
        self._require_run(run_id)
        self._store.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (status, run_id),
        )

    def _stale_rows(self, cutoff: str) -> list[Any]:
        """Return rows of runs that are stale as of ``cutoff``."""
        if _heartbeat_supported(self._store):
            return self._store.query(
                """
                SELECT r.run_id
                FROM runs r
                LEFT JOIN (
                    SELECT run_id,
                           MAX(COALESCE(heartbeat_at, created_at)) AS latest_beat
                    FROM checkpoints
                    GROUP BY run_id
                ) c ON c.run_id = r.run_id
                WHERE r.status = 'running'
                  AND COALESCE(c.latest_beat, r.started_at) < ?
                ORDER BY r.started_at ASC
                """,
                (cutoff,),
            )
        return self._store.query(
            """
            SELECT r.run_id
            FROM runs r
            LEFT JOIN (
                SELECT run_id, MAX(created_at) AS latest_beat
                FROM checkpoints
                GROUP BY run_id
            ) c ON c.run_id = r.run_id
            WHERE r.status = 'running'
              AND COALESCE(c.latest_beat, r.started_at) < ?
            ORDER BY r.started_at ASC
            """,
            (cutoff,),
        )

    def _require_run(self, run_id: str) -> None:
        """Raise when ``run_id`` is not present in ``runs``."""
        rows = self._store.query(
            "SELECT 1 FROM runs WHERE run_id = ? LIMIT 1",
            (run_id,),
        )
        if not rows:
            raise CodeGuardianError(
                "SYS_INVARIANT_VIOLATED",
                f"run {run_id!r} is not present in runs",
                details={"run_id": run_id},
            )
