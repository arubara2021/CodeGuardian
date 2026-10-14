"""Pipeline control contract.

The orchestrator runs the phase state machine, checkpoints after
every phase, and resumes from the last successful checkpoint after a
crash. Callers start runs, inspect state, and pause or resume them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

__all__ = [
    "Orchestrator",
    "RunConfig",
    "RunMode",
    "RunResult",
    "RunState",
    "RunStatus",
]

RunMode = Literal["legacy", "greenfield"]
RunStatus = Literal["running", "success", "failure", "stopped", "dry_run", "paused"]


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Inputs required to start a run."""

    url: str
    target: str
    directive: str
    mode: RunMode = "legacy"
    verify: bool = False
    apply: bool = False


@dataclass(frozen=True, slots=True)
class RunState:
    """The resumable state of a run in progress."""

    run_id: str
    current_phase: str
    completed_phases: tuple[str, ...]
    pending_phases: tuple[str, ...]
    retries: int
    paused: bool


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of a completed run.

    Artifact paths point into the run's directory. Reporters read
    from these paths rather than from in-memory objects, so the
    orchestrator does not need to carry the artifacts.
    """

    run_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    artifact_dir: Path
    diff_path: Path | None
    blast_radius_path: Path | None
    verification_path: Path | None
    audit_path: Path | None
    error: str | None


class Orchestrator(Protocol):
    """Control surface for the phase pipeline.

    Runs are identified by ``run_id``. A crash frees the run;
    ``resume`` continues from the last successful checkpoint.
    """

    def run(self, config: RunConfig) -> RunResult:
        """Start a run and block until it completes or stops."""
        ...

    def resume(self, run_id: str) -> RunResult:
        """Resume a run from its last successful checkpoint."""
        ...

    def pause(self, run_id: str) -> None:
        """Request a pause after the current phase completes."""
        ...

    def get_state(self, run_id: str) -> RunState:
        """Return the current state of a run."""
        ...
