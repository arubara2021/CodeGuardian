"""Concrete ``Orchestrator`` Protocol implementation.

Thin layer over ``loop.run_loop``. The loop walks the graph and
returns a ``LoopResult``; this class translates between the Protocol's
``RunConfig``/``RunResult`` surface and the loop's typed arguments,
inserts and updates the ``runs`` row, and constructs the artifact
paths a ``RunResult`` carries.

The class accepts an ``observer=`` keyword that fires per phase. The
deep test uses the observer to recover per-stage granularity that
``RunResult`` does not carry; a CLI does not need to supply one. The
observer is not part of the Protocol; it is a CodeGuardian-specific
extension for the harness's own tests.

LangGraph compatibility is not implemented in this class in v1. The
contract in ``interfaces/orchestrator.py`` is what callers consume;
the walk is deterministic Python. A future release can delegate the
walk to LangGraph's executor without changing the ``Orchestrator``
Protocol, because the loop already speaks ``PhaseGraph`` and
``SlotResolver``. Adding LangGraph under the hood is a one-file
change in ``loop.py``, not a caller-visible change here.

Resume requires the caller to reconstruct the state of the completed
phases. The constructor accepts an optional ``resume_factory``
callable that receives ``(run_id, completed_phases)`` and returns the
prior results mapping. When the factory is not supplied, ``resume``
raises ``SYS_NOT_IMPLEMENTED`` with a clear message. Phase 14 will
wire a concrete factory from the run's artifacts.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage.sqlite_store import SQLiteStore
from codeguardian.interfaces.orchestrator import (
    RunConfig,
    RunMode,
    RunResult,
    RunState,
    RunStatus,
)

from .checkpoint_supervisor import CheckpointSupervisor
from .checkpointing import read_checkpoint
from .loop import AuditWriter, LoopObserver, LoopResult, SlotInputFactory, run_loop
from .phase_graph import PhaseGraph
from .phase_graphs import LEGACY_GRAPH
from .registry import AgentRegistry, SlotResolver

__all__ = ["LangGraphOrchestrator"]


ResumeFactory = Callable[[str, tuple[str, ...]], Mapping[str, Any]]
"""Reconstructs prior slot results for a resume."""


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run_coroutine_sync(coro: Any) -> Any:
    """Run a coroutine from synchronous code.

    ``asyncio.run`` refuses to start when a loop is already running in
    the current thread. The orchestrator's ``run`` method is
    synchronous by contract, so it cannot simply be ``async def``; the
    deep test calls it from inside an event loop that ``asyncio.run``
    started at the top. The helper detects the running-loop case and
    dispatches the coroutine to a worker thread that has its own loop.

    The worker runs under a copy of the caller's context. A
    ``ThreadPoolExecutor`` worker starts with a fresh context by
    default, so any ``ContextVar`` the caller set — the active telemetry
    store, the current trace context, the capability attenuation scope
    — would be invisible inside the coroutine. ``contextvars.copy_context``
    captures the caller's context at the moment of dispatch and runs
    ``asyncio.run`` inside it, so the coroutine sees the same state the
    caller would. The worker's modifications to the copied context are
    local to the worker; the caller's context is not affected.

    A new thread is correct rather than wasteful because the loop
    runner is called once per pipeline, not once per phase. The
    overhead of thread creation and teardown is negligible compared to
    the model calls and sandbox executions the coroutine performs.
    """
    import contextvars

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    captured = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(captured.run, asyncio.run, coro)
        return future.result()


def _now_dt() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


def _repo_id(url: str) -> str:
    """Return the sixteen-character repo identifier for ``url``."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _graph_for_mode(mode: RunMode) -> PhaseGraph:
    """Return the PhaseGraph for ``mode``.

    Only ``"legacy"`` is supported in v1. A future release adds
    ``phase_graphs.greenfield`` and returns it here.
    """
    if mode == "legacy":
        return LEGACY_GRAPH
    raise CodeGuardianError(
        ErrorCode.SYS_NOT_IMPLEMENTED,
        f"run mode {mode!r} is not supported; only 'legacy' ships in v1",
        details={"mode": mode},
    )


class LangGraphOrchestrator:
    """Concrete Orchestrator Protocol implementation."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        registry: AgentRegistry,
        supervisor: CheckpointSupervisor,
        audit_writer: AuditWriter,
        input_factory: SlotInputFactory,
        artifact_root: Path,
        context_budget_factory: Callable[[RunConfig], Any] | None = None,
        observer: LoopObserver | None = None,
        resume_factory: ResumeFactory | None = None,
        side_effect_slots: frozenset[str] = frozenset({"sandbox_verify"}),
    ) -> None:
        self._store = store
        self._registry = registry
        self._supervisor = supervisor
        self._audit_writer = audit_writer
        self._input_factory = input_factory
        self._artifact_root = Path(artifact_root)
        self._context_budget_factory = context_budget_factory
        self._observer = observer
        self._resume_factory = resume_factory
        self._side_effect_slots = side_effect_slots

    def run(self, config: RunConfig) -> RunResult:
        """Start a run and block until it completes or stops."""
        graph = _graph_for_mode(config.mode)
        run_id = str(uuid.uuid4())
        started_at = _now_dt()
        repo_id = _repo_id(config.url)
        artifact_dir = self._artifact_root / repo_id / "runs" / run_id

        self._insert_run(
            run_id=run_id,
            repo_id=repo_id,
            config=config,
            artifact_dir=artifact_dir,
            started_at=started_at,
        )

        context_budget = None
        if self._context_budget_factory is not None:
            try:
                context_budget = self._context_budget_factory(config)
            except Exception:
                context_budget = None

        try:
            loop_result = self._run_loop_sync(
                graph=graph,
                run_id=run_id,
                config=config,
                context_budget=context_budget,
            )
        except CodeGuardianError:
            self._finalize_run(run_id, "failure", _now_dt())
            raise

        final_status: RunStatus = "success" if not loop_result.failed else "failure"
        if loop_result.stop_reason == "paused":
            final_status = "paused"
        elif loop_result.stop_reason == "context_overflow":
            final_status = "stopped"
        self._finalize_run(run_id, final_status, _now_dt())

        return self._build_run_result(
            run_id=run_id,
            status=final_status,
            started_at=started_at,
            artifact_dir=artifact_dir,
            error=loop_result.error,
        )

    def resume(self, run_id: str) -> RunResult:
        """Resume a run from its last successful checkpoint."""
        if self._resume_factory is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "resume requires a resume_factory on the orchestrator; "
                "Phase 14 will wire one from the run's artifacts",
                details={"run_id": run_id},
            )

        row = self._read_run(run_id)
        graph = LEGACY_GRAPH
        plan = self._supervisor.build_resume_plan(run_id, graph=graph)
        initial_results = self._resume_factory(run_id, plan.completed_phases)

        config = RunConfig(
            url=str(row["url"]),
            target=str(row["target_path"] or ""),
            directive=str(row["directive"] or ""),
            mode=self._mode_from_row(row),
            verify=bool(row["verification_enabled"]),
            apply=False,
        )
        artifact_dir = Path(str(row["artifact_dir"]))
        started_at = datetime.fromisoformat(str(row["started_at"]))

        loop_result = self._run_loop_sync(
            graph=graph,
            run_id=run_id,
            config=config,
            context_budget=None,
            resume_after=plan.resume_after_phase,
            initial_results=initial_results,
            initial_completed=plan.completed_phases,
        )

        final_status: RunStatus = "success" if not loop_result.failed else "failure"
        if loop_result.stop_reason == "paused":
            final_status = "paused"
        elif loop_result.stop_reason == "context_overflow":
            final_status = "stopped"
        self._finalize_run(run_id, final_status, _now_dt())

        return self._build_run_result(
            run_id=run_id,
            status=final_status,
            started_at=started_at,
            artifact_dir=artifact_dir,
            error=loop_result.error,
        )

    def pause(self, run_id: str) -> None:
        """Request a pause after the current phase completes."""
        self._require_run(run_id)
        self._store.execute(
            "UPDATE runs SET status = 'paused' WHERE run_id = ?",
            (run_id,),
        )

    def get_state(self, run_id: str) -> RunState:
        """Return the current state of a run."""
        row = self._read_run(run_id)
        checkpoint = read_checkpoint(self._store, run_id)
        if checkpoint is not None:
            current = checkpoint.phase
            completed = checkpoint.completed_phases
            pending = checkpoint.pending_phases
            retries = checkpoint.retries
        else:
            graph = _graph_for_mode(self._mode_from_row(row))
            current = graph.entry
            completed = ()
            pending = graph.slot_names()
            retries = 0
        return RunState(
            run_id=run_id,
            current_phase=current,
            completed_phases=completed,
            pending_phases=pending,
            retries=retries,
            paused=str(row["status"]) == "paused",
        )

    # --- Internal helpers -------------------------------------------------

    def _run_loop_sync(
        self,
        *,
        graph: PhaseGraph,
        run_id: str,
        config: RunConfig,
        context_budget: Any | None,
        resume_after: str | None = None,
        initial_results: Mapping[str, Any] | None = None,
        initial_completed: tuple[str, ...] = (),
    ) -> LoopResult:
        """Invoke the loop synchronously."""
        resolver = SlotResolver(self._registry)
        coro = run_loop(
            graph=graph,
            registry=self._registry,
            resolver=resolver,
            store=self._store,
            supervisor=self._supervisor,
            audit_writer=self._audit_writer,
            input_factory=self._input_factory,
            run_id=run_id,
            repo_url=config.url,
            commit_hash=self._commit_hash_for(run_id),
            clone_root=self._clone_root_for(run_id),
            context_budget=context_budget,
            side_effect_slots=self._side_effect_slots,
            observer=self._observer,
            resume_after=resume_after,
            initial_results=initial_results,
            initial_completed=initial_completed,
        )
        return _run_coroutine_sync(coro)

    def _commit_hash_for(self, run_id: str) -> str:
        """Read the run's commit hash."""
        rows = self._store.query(
            "SELECT commit_hash FROM runs WHERE run_id = ? LIMIT 1",
            (run_id,),
        )
        return str(rows[0]["commit_hash"]) if rows else ""

    def _clone_root_for(self, run_id: str) -> Path:
        """Return the clone root for the run.

        For v1 the clone root lives beside the artifact directory at
        ``<artifact_root>/<repo_id>/clone``. The run does not store the
        clone root path on the row, so it is derived from the artifact
        directory's grandparent.
        """
        rows = self._store.query(
            "SELECT artifact_dir FROM runs WHERE run_id = ? LIMIT 1",
            (run_id,),
        )
        if not rows:
            return Path.cwd()
        artifact_dir = Path(str(rows[0]["artifact_dir"]))
        # artifact_dir = <root>/<repo_id>/runs/<run_id>; clone root is
        # <root>/<repo_id>/clone.
        return artifact_dir.parent.parent / "clone"

    def _insert_run(
        self,
        *,
        run_id: str,
        repo_id: str,
        config: RunConfig,
        artifact_dir: Path,
        started_at: datetime,
    ) -> None:
        """Insert the runs row and ensure the repo exists."""
        now = _now_iso()
        self._store.execute(
            """
            INSERT OR IGNORE INTO repos
                (repo_id, url, provider, last_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (repo_id, config.url, "local", now),
        )
        with contextlib.suppress(OSError):
            artifact_dir.mkdir(parents=True, exist_ok=True)
        self._store.execute(
            """
            INSERT INTO runs (
                run_id, repo_id, commit_hash, target_path, mode,
                directive, started_at, status, artifact_dir,
                verification_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                run_id,
                repo_id,
                "HEAD",
                config.target,
                config.mode,
                config.directive,
                started_at.isoformat().replace("+00:00", "Z"),
                str(artifact_dir),
                1 if config.verify else 0,
            ),
        )

    def _finalize_run(
        self,
        run_id: str,
        status: RunStatus,
        finished_at: datetime,
    ) -> None:
        """Update the runs row with the final status."""
        self._store.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
            (
                status,
                finished_at.isoformat().replace("+00:00", "Z"),
                run_id,
            ),
        )

    def _read_run(self, run_id: str) -> Any:
        """Return the runs row for ``run_id``.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when the run does not exist.
        """
        rows = self._store.query(
            """
            SELECT r.run_id, r.repo_id, r.commit_hash, r.target_path,
                   r.mode, r.directive, r.started_at, r.finished_at,
                   r.status, r.artifact_dir, r.verification_enabled,
                   repos.url AS url
            FROM runs r
            JOIN repos ON repos.repo_id = r.repo_id
            WHERE r.run_id = ?
            LIMIT 1
            """,
            (run_id,),
        )
        if not rows:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"run {run_id!r} is not present in runs",
                details={"run_id": run_id},
            )
        return rows[0]

    def _require_run(self, run_id: str) -> None:
        self._read_run(run_id)

    def _mode_from_row(self, row: Any) -> RunMode:
        value = str(row["mode"] or "legacy")
        if value not in ("legacy", "greenfield"):
            return "legacy"
        return value  # type: ignore[return-value]

    def _build_run_result(
        self,
        *,
        run_id: str,
        status: RunStatus,
        started_at: datetime,
        artifact_dir: Path,
        error: CodeGuardianError | None,
    ) -> RunResult:
        """Construct the RunResult from the artifacts on disk."""
        diff_path = artifact_dir / "diff.patch"
        blast_path = artifact_dir / "blast_radius.json"
        verification_path = artifact_dir / "verification.json"
        audit_path = artifact_dir / "audit.jsonl"
        return RunResult(
            run_id=run_id,
            status=status,
            started_at=started_at,
            finished_at=_now_dt(),
            artifact_dir=artifact_dir,
            diff_path=diff_path if diff_path.is_file() else None,
            blast_radius_path=blast_path if blast_path.is_file() else None,
            verification_path=(verification_path if verification_path.is_file() else None),
            audit_path=audit_path if audit_path.is_file() else None,
            error=error.message if error is not None else None,
        )
