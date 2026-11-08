"""The deterministic graph walker.

The loop walks a ``PhaseGraph`` once per call. It reads the entry
point, resolves the slot against the registry, invokes the agent the
slot resolves to, runs the entry and exit gates around the invocation,
writes a checkpoint after each successful phase, writes one audit entry
per phase regardless of outcome, and advances to the next slot by
following the graph's transition table.

The loop does not branch on mode. It does not name any agent class.
It reads the graph and dispatches. Adding an agent, a slot, or a new
graph does not touch this file.

Four concerns are handled per phase:

1. **Gates.** The slot names a gate; ``get_gate`` yields an entry/exit
   pair. The entry gate runs before the agent; the exit gate runs
   after. A failing gate is a phase failure.

2. **Side-effect guard.** A slot in ``side_effect_slots`` has the
   ``side_effect_guard`` context set around its agent invocation. A
   fallback model call inside the phase will not retry because the
   guard is active; the orchestrator has decided the phase's side
   effects are irreversible.

3. **Checkpoint.** After a successful phase, ``write_checkpoint``
   records the phase name, completed and pending phases, and any
   caller-supplied artifacts. A checkpoint is not written when a phase
   fails; the last successful checkpoint remains the resume point.

4. **Audit.** Every phase writes one audit entry, success or failure.
   The entry carries the phase, the agent name, the result status, the
   checkpoint id (when one was just written), and any error message.

The loop stops cleanly on four signals: a completed graph, a paused
run, a context-budget refusal, or a phase failure. The
``LoopResult.stop_reason`` names which.

Hot evidence is not pushed by the loop itself. The loop hands the
``EvidenceBus`` to the context it exposes to input factories and
observers; those callers decide what to push and when. The loop drains
the bus after each phase and includes the drained events on the phase's
``SlotExecution``. That way the observer sees the events per phase and
the orchestrator can persist them.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .checkpoint_supervisor import CheckpointSupervisor
from .checkpointing import write_checkpoint
from .evidence import EvidenceBus, HotEvent
from .phase_gates import PhaseContext, get_gate
from .phase_graph import PhaseGraph
from .registry import AgentRegistry, SlotResolution, SlotResolver

__all__ = [
    "AuditWriter",
    "LoopContext",
    "LoopObserver",
    "LoopResult",
    "SlotExecution",
    "SlotInputFactory",
    "StopReason",
    "run_loop",
]


StopReason = Literal["completed", "paused", "context_overflow", "failed"]


AuditWriter = Callable[[dict[str, Any]], str]
"""Write one audit entry and return its entry_hash."""


class SlotInputFactory(Protocol):
    """Builds the input object an agent expects.

    The loop does not know what input type each agent declares. The
    caller supplies a factory that inspects the slot name and the
    accumulated context and returns the correct input. A ``KeyError``
    from the factory is treated as a phase failure.
    """

    def build(self, slot_name: str, context: LoopContext) -> Any:
        """Return the input object for the given slot."""
        ...


LoopObserver = Callable[[str, "SlotExecution", "LoopContext"], None]
"""Called after each phase. Failures inside the observer are swallowed."""


@dataclass(frozen=True, slots=True)
class LoopContext:
    """The context handed to input factories and observers.

    Attributes:
        run_id: The current run identifier.
        graph_name: The name of the graph being walked.
        repo_url: The repository URL.
        commit_hash: The repository commit.
        clone_root: The clone root path.
        results: Every slot's output so far, keyed by slot name. The
            current slot is not included until after its agent returns.
        current_slot: The slot the loop is about to invoke.
        evidence_bus: The hot-tier event accumulator. Callers may push
            events; the loop drains them at the phase boundary.
        context_budget: The per-run context budget, or None when no
            budget was supplied.
    """

    run_id: str
    graph_name: str
    repo_url: str
    commit_hash: str
    clone_root: Path
    results: Mapping[str, Any] = field(default_factory=dict)
    current_slot: str = ""
    evidence_bus: EvidenceBus | None = None
    context_budget: Any | None = None

    def result(self, slot_name: str, default: Any = None) -> Any:
        """Return the output of ``slot_name`` or ``default``."""
        return self.results.get(slot_name, default)


@dataclass(frozen=True, slots=True)
class SlotExecution:
    """One phase's execution record.

    Attributes:
        slot_name: The slot that ran.
        agent_name: The name of the agent that filled the slot.
        started_at: ISO 8601 timestamp of when the phase began.
        finished_at: ISO 8601 timestamp of when the phase ended.
        ok: True when the agent returned and the exit gate passed.
        output: The agent's return value. ``None`` when the agent did
            not return.
        error: The error that ended the phase, or None on success.
        events: The hot events drained at the phase boundary.
        checkpoint_id: The checkpoint written for this phase, or None.
        audit_hash: The audit entry hash for this phase, or None.
    """

    slot_name: str
    agent_name: str
    started_at: str
    finished_at: str
    ok: bool
    output: Any = None
    error: CodeGuardianError | None = None
    events: tuple[HotEvent, ...] = ()
    checkpoint_id: str | None = None
    audit_hash: str | None = None


@dataclass(frozen=True, slots=True)
class LoopResult:
    """The complete outcome of one walk.

    Attributes:
        run_id: The run identifier.
        graph_name: The graph name.
        completed_phases: Names of every phase that completed cleanly,
            in order.
        slot_executions: One record per phase that ran, in order.
        checkpoint_ids: Every checkpoint id written this walk, in
            order.
        audit_hashes: Every audit entry hash written this walk, in
            order.
        hot_events: Every event drained across all phases, in order.
        failed: True when the walk stopped because a phase failed.
        error: The error that stopped the walk, or None.
        stop_reason: Why the walk stopped.
    """

    run_id: str
    graph_name: str
    completed_phases: tuple[str, ...]
    slot_executions: tuple[SlotExecution, ...]
    checkpoint_ids: tuple[str, ...]
    audit_hashes: tuple[str, ...]
    hot_events: tuple[HotEvent, ...]
    failed: bool
    error: CodeGuardianError | None
    stop_reason: StopReason


_AUDIT_PHASE_MAP: Mapping[str, str] = {
    "recon": "reconnaissance",
    "blast": "blast_radius",
    "analyst": "context",
    "tester": "safety_net",
    "writer": "refactor",
    "sandbox_verify": "sandbox_verify",
    "verifier_correctness": "independent_verify",
    "verifier_security": "independent_verify",
    "verifier_contract": "independent_verify",
    "skill_curator": "output",
    "project_test_suite": "output",
}


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _audit_phase_for(slot_name: str) -> str:
    """Return the schema-valid audit phase name for a slot."""
    return _AUDIT_PHASE_MAP.get(slot_name, "output")


def _choose_next(
    graph: PhaseGraph,
    current: str,
    resolved: Mapping[str, Any],
) -> str | None:
    """Return the next slot after ``current``, or None to terminate.

    Every non-None destination that has a resolved agent wins. If
    every destination is either ``None`` or unresolved, the walk
    terminates. This is what makes an optional terminal slot
    (``project_test_suite``) skipped when no agent is registered for it.
    """
    dests = graph.transitions.get(current, (None,))
    for dest in dests:
        if dest is None:
            continue
        if dest in resolved:
            return dest
    return None


def _pending_after(graph: PhaseGraph, current: str) -> tuple[str, ...]:
    """Return every slot after ``current`` in declaration order.

    This is a hint for the checkpoint, not a strict plan. The loop
    still walks transitions from ``current``; the checkpoint's
    ``pending_phases`` records what the graph declared was left, so a
    reader sees a stable view of the graph's shape.
    """
    names = graph.slot_names()
    try:
        idx = names.index(current)
    except ValueError:
        return names
    return names[idx + 1 :]


def _is_paused(store: SQLiteStore, run_id: str) -> bool:
    """Return True when ``runs.status`` for ``run_id`` is 'paused'."""
    try:
        rows = store.query(
            "SELECT status FROM runs WHERE run_id = ? LIMIT 1",
            (run_id,),
        )
    except CodeGuardianError:
        return False
    if not rows:
        return False
    try:
        return str(rows[0]["status"]) == "paused"
    except (KeyError, IndexError):
        return False


def _build_audit_entry(
    *,
    run_id: str,
    slot_name: str,
    agent_name: str,
    execution: SlotExecution,
    checkpoint_id: str | None,
) -> dict[str, Any]:
    """Compose the schema-shaped audit entry for one phase."""
    result: dict[str, Any] = {
        "status": "success" if execution.ok else "failure",
    }
    if execution.error is not None:
        result["error"] = execution.error.message[:500]
    return {
        "run_id": run_id,
        "phase": _audit_phase_for(slot_name),
        "agent": agent_name if agent_name in _AUDIT_AGENT_NAMES else "orchestrator",
        "result": result,
        "checkpoint_id": checkpoint_id,
    }


_AUDIT_AGENT_NAMES = frozenset(
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
        "orchestrator",
        "cli",
    }
)


def _seed_registered_agents(
    store: SQLiteStore,
    resolution: SlotResolution,
) -> None:
    """Seed the registered_agents table from the slot resolution.

    The graph is the source of truth for the slots a pipeline runs. The
    registry is the source of truth for which agents fill them. Both are
    already available when this runs: the resolution maps every slot to
    the spec that filled it. The function writes each resolved spec as
    one row, using INSERT OR IGNORE so a registration that already
    exists — from a prior run or an explicit registry insert — is
    preserved.

    The migration that creates the table does not seed it. The seed
    lives here because this is the only place both the graph and the
    registry are available at once, and because the capability tags on
    the row belong to the graph, not to the schema. If a future change
    adds a slot, the next run seeds it without a migration.

    A missing registered_agents table (migration 007 not applied) is a
    no-op. The FK on agent_calls.role is declarative — the project runs
    with foreign keys off — so the loop runs whether the table exists
    or not. Seeding when the table exists is what makes the FK
    meaningful to a reader.
    """
    registered_at = _now_iso()
    for spec in resolution.resolved.values():
        capabilities_json = json.dumps(sorted(spec.capability_tags))
        try:
            store.execute(
                """
                INSERT OR IGNORE INTO registered_agents
                    (name, capabilities, backend, registered_at)
                VALUES (?, ?, ?, ?)
                """,
                (spec.name, capabilities_json, spec.backend, registered_at),
            )
        except CodeGuardianError:
            # A missing table means migration 007 has not run. The FK
            # is declarative, so the loop continues without the seed.
            return


async def run_loop(
    *,
    graph: PhaseGraph,
    registry: AgentRegistry,
    resolver: SlotResolver,
    store: SQLiteStore,
    supervisor: CheckpointSupervisor,
    audit_writer: AuditWriter,
    input_factory: SlotInputFactory,
    run_id: str,
    repo_url: str,
    commit_hash: str,
    clone_root: Path,
    context_budget: Any | None = None,
    side_effect_slots: frozenset[str] = frozenset({"sandbox_verify"}),
    observer: LoopObserver | None = None,
    resume_after: str | None = None,
    initial_results: Mapping[str, Any] | None = None,
    initial_completed: tuple[str, ...] = (),
) -> LoopResult:
    """Walk ``graph`` once.

    Args:
        graph: The PhaseGraph to walk.
        registry: The agent registry to resolve slots against.
        resolver: The slot resolver.
        store: An open SQLiteStore. Used for checkpoints and the pause
            check.
        supervisor: The checkpoint supervisor, used for heartbeats.
        audit_writer: Called with a dict-shaped audit entry per phase.
            Must return the entry's hash.
        input_factory: Builds each agent's input from the slot name
            and the accumulated context.
        run_id: The run identifier. The ``runs`` row must exist.
        repo_url: The repository URL.
        commit_hash: The repository commit.
        clone_root: The clone root path.
        context_budget: Optional per-run context budget. When the
            budget reports REFUSE after a phase, the walk stops with
            ``stop_reason="context_overflow"``.
        side_effect_slots: Slot names whose agent invocation must be
            wrapped in the router's side-effect guard.
        observer: Optional per-phase callback. Observer exceptions do
            not stop the walk.
        resume_after: When set, the walk starts at the transition from
            this slot rather than the graph's entry. ``initial_results``
            must contain every prior slot's output.
        initial_results: Prior slot outputs, for resume.
        initial_completed: Prior completed phase names, for resume.

    Returns:
        A ``LoopResult``. Never raises on a phase failure; the failure
        is recorded on the result. Raises only on a graph-start error
        (missing required slot, ambiguous resolution).
    """
    from codeguardian.core.providers.fallback_router import side_effect_guard

    resolution = resolver.resolve(graph)
    resolved = resolution.resolved

    _seed_registered_agents(store, resolution)

    bus = EvidenceBus()

    results: dict[str, Any] = dict(initial_results or {})
    completed: list[str] = list(initial_completed)
    executions: list[SlotExecution] = []
    checkpoint_ids: list[str] = []
    audit_hashes: list[str] = []
    all_events: list[HotEvent] = []

    if resume_after is not None:
        current = _choose_next(graph, resume_after, resolved)
    else:
        current = graph.entry

    stop_reason: StopReason = "completed"
    final_error: CodeGuardianError | None = None

    while current is not None:
        if _is_paused(store, run_id):
            stop_reason = "paused"
            break

        if context_budget is not None:
            try:
                if context_budget.check() == "refuse":
                    snapshot = context_budget.snapshot(role="orchestrator")
                    from .evidence import ContextBudgetExceeded

                    bus.push(
                        ContextBudgetExceeded(
                            role="orchestrator",
                            fill_percent=float(snapshot.get("fill_percent", 0.0)),
                            snapshot=dict(snapshot),
                        )
                    )
                    stop_reason = "context_overflow"
                    break
            except Exception:
                pass

        slot = graph.slot(current)
        agent_spec = resolved[current]
        gate_pair = get_gate(slot.gate)

        with contextlib.suppress(Exception):
            supervisor.heartbeat(run_id)

        started_at = _now_iso()
        output: Any = None
        phase_error: CodeGuardianError | None = None
        ok = True

        entry_context = LoopContext(
            run_id=run_id,
            graph_name=graph.name,
            repo_url=repo_url,
            commit_hash=commit_hash,
            clone_root=clone_root,
            results=dict(results),
            current_slot=current,
            evidence_bus=bus,
            context_budget=context_budget,
        )

        try:
            gate_pair.entry(
                PhaseContext(
                    run_id=run_id,
                    repo_url=repo_url,
                    commit_hash=commit_hash,
                    clone_root=clone_root,
                    results=dict(results),
                    evidence=bus.peek(),
                )
            )

            agent_input = input_factory.build(current, entry_context)

            if current in side_effect_slots:
                with side_effect_guard():
                    output = await agent_spec.agent.run(agent_input)
            else:
                output = await agent_spec.agent.run(agent_input)

            results[current] = output

            gate_pair.exit(
                PhaseContext(
                    run_id=run_id,
                    repo_url=repo_url,
                    commit_hash=commit_hash,
                    clone_root=clone_root,
                    results=dict(results),
                    evidence=bus.peek(),
                )
            )
        except CodeGuardianError as exc:
            ok = False
            phase_error = exc
        except Exception as exc:
            ok = False
            phase_error = CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"agent {agent_spec.name!r} raised {type(exc).__name__}: {exc}",
                details={"slot": current},
            )

        finished_at = _now_iso()
        drained = bus.drain()
        all_events.extend(drained)

        checkpoint_id: str | None = None
        if ok:
            try:
                checkpoint_id = write_checkpoint(
                    store,
                    run_id=run_id,
                    phase=current,
                    state={},
                    artifacts={},
                    completed_phases=(*completed, current),
                    pending_phases=_pending_after(graph, current),
                    retries=0,
                )
                checkpoint_ids.append(checkpoint_id)
            except CodeGuardianError as exc:
                ok = False
                phase_error = exc

        execution = SlotExecution(
            slot_name=current,
            agent_name=agent_spec.name,
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            output=output,
            error=phase_error,
            events=drained,
            checkpoint_id=checkpoint_id,
        )

        audit_hash: str | None = None
        try:
            audit_hash = audit_writer(
                _build_audit_entry(
                    run_id=run_id,
                    slot_name=current,
                    agent_name=agent_spec.name,
                    execution=execution,
                    checkpoint_id=checkpoint_id,
                )
            )
            audit_hashes.append(audit_hash)
        except Exception:
            pass

        execution = replace(execution, audit_hash=audit_hash)
        executions.append(execution)

        if observer is not None:
            with contextlib.suppress(Exception):
                observer(current, execution, entry_context)

        if not ok:
            stop_reason = "failed"
            final_error = phase_error
            break

        completed.append(current)
        current = _choose_next(graph, current, resolved)

    return LoopResult(
        run_id=run_id,
        graph_name=graph.name,
        completed_phases=tuple(completed),
        slot_executions=tuple(executions),
        checkpoint_ids=tuple(checkpoint_ids),
        audit_hashes=tuple(audit_hashes),
        hot_events=tuple(all_events),
        failed=(stop_reason == "failed"),
        error=final_error,
        stop_reason=stop_reason,
    )
