"""Tests for the orchestrator loop, checkpoints, and supervisor."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.orchestrator.checkpoint_supervisor import CheckpointSupervisor
from codeguardian.core.orchestrator.checkpointing import read_checkpoint
from codeguardian.core.orchestrator.langgraph_orchestrator import LangGraphOrchestrator
from codeguardian.core.orchestrator.loop import LoopContext, SlotExecution, run_loop
from codeguardian.core.orchestrator.phase_graph import PhaseGraph, PhaseSlot
from codeguardian.core.orchestrator.phase_graphs import LEGACY_GRAPH
from codeguardian.core.orchestrator.registry import (
    AgentRegistry,
    AgentSpec,
    SlotResolver,
)
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore
from codeguardian.interfaces.orchestrator import RunConfig

_STUB_OUTPUTS: dict[str, Any] = {
    "recon": SimpleNamespace(cpg_bytes=b"fake-cpg-bytes"),
    "blast": SimpleNamespace(recommendation_adjusted="proceed"),
    "analyst": SimpleNamespace(is_refusal=False, needs_input=False),
    "tester": SimpleNamespace(is_valid=True),
    "writer": SimpleNamespace(candidates=("cand1",)),
    "sandbox_verify": "verified",
    "correctness_verifier": SimpleNamespace(),
    "security_verifier": SimpleNamespace(),
    "contract_verifier": SimpleNamespace(),
    "skill_curator": SimpleNamespace(is_refusal=False),
}
"""Shape-correct outputs for the stub agents.

Each value satisfies the exit gate on the slot with the same name in
``LEGACY_GRAPH``. ``recon`` needs ``cpg_bytes``; ``blast`` needs
``recommendation_adjusted``; the analyst, tester, writer, and curator
need their respective refusal/validity attributes; ``sandbox_verify``
needs only to be non-None. The three verifiers accept any object
because their exit gate only checks a verdict when one is present.
"""


class StubAgent:
    """A deterministic stub agent that returns the shape its slot needs."""

    def __init__(self, name: str, *, fail_after: int | None = None) -> None:
        self.name = name
        self.fail_after = fail_after
        self.call_count = 0

    async def run(self, input: Any) -> Any:
        self.call_count += 1
        if self.fail_after is not None and self.call_count > self.fail_after:
            raise CodeGuardianError(ErrorCode.SYS_INTERNAL, f"{self.name} stubbed failure")
        return _STUB_OUTPUTS.get(self.name, f"{self.name}_output")


class RecordingAuditWriter:
    """Collects audit entries and returns a synthetic hash per entry."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __call__(self, entry: dict[str, Any]) -> str:
        self.entries.append(entry)
        return f"hash_{len(self.entries):04d}"


class StubInputFactory:
    """Builds a minimal input dict for any slot."""

    def build(self, slot_name: str, context: LoopContext) -> dict[str, Any]:
        return {"slot": slot_name, "results": dict(context.results)}


class RecordingObserver:
    """Collects the (slot_name, execution) pairs the loop reports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, SlotExecution]] = []

    def __call__(
        self,
        slot_name: str,
        execution: SlotExecution,
        context: LoopContext,
    ) -> None:
        self.calls.append((slot_name, execution))


def open_store(tmp_path: Path) -> SQLiteStore:
    """Open a fresh SQLiteStore with all migrations applied."""
    store = SQLiteStore(tmp_path / "test.db")
    store.open()
    MigrationRunner(store).run()
    return store


def insert_run_row(store: SQLiteStore, run_id: str) -> None:
    """Insert the repos and runs rows a run needs before it can proceed."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.execute(
        "INSERT OR IGNORE INTO repos (repo_id, url, provider, last_seen_at) VALUES (?, ?, ?, ?)",
        ("repo_test", "file:///test", "local", now),
    )
    store.execute(
        "INSERT INTO runs (run_id, repo_id, commit_hash, target_path, "
        "mode, directive, started_at, status, artifact_dir) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
        (run_id, "repo_test", "abc", "t.py", "legacy", "d", now, "/tmp/runs"),
    )


def register_legacy_stubs(registry: AgentRegistry) -> None:
    """Register stubs that satisfy every slot in the legacy graph.

    Each of the three verifier slots requires a distinguishing tag
    (``correctness``, ``security``, ``contract``) in addition to the
    shared verifier tags, so each slot needs its own stub. The stub's
    name matches the agent name the pipeline registers, not the slot
    name — ``correctness_verifier`` fills the ``verifier_correctness``
    slot. The other slots get one stub each.
    """
    stubs = [
        ("recon", frozenset({"deterministic", "cpg-builder"})),
        ("blast", frozenset({"deterministic", "graph-query"})),
        ("analyst", frozenset({"llm", "reasoning", "spec-producer"})),
        ("tester", frozenset({"llm", "coding", "test-producer"})),
        ("writer", frozenset({"llm", "coding", "code-producer"})),
        ("sandbox_verify", frozenset({"deterministic", "sandbox-runner"})),
        (
            "correctness_verifier",
            frozenset({"llm", "reasoning", "verdict-producer", "correctness"}),
        ),
        (
            "security_verifier",
            frozenset({"llm", "reasoning", "verdict-producer", "security"}),
        ),
        (
            "contract_verifier",
            frozenset({"llm", "reasoning", "verdict-producer", "contract"}),
        ),
        ("skill_curator", frozenset({"llm", "reasoning", "skill-producer"})),
    ]
    for name, tags in stubs:
        agent = StubAgent(name)
        registry.register(AgentSpec(name=name, agent=agent, capability_tags=tags))


async def run_legacy(
    *,
    store: SQLiteStore,
    registry: AgentRegistry,
    run_id: str,
    observer: RecordingObserver | None = None,
    resume_after: str | None = None,
    initial_results: dict[str, Any] | None = None,
    initial_completed: tuple[str, ...] = (),
) -> Any:
    """Run the legacy graph against the supplied stubs."""
    audit_writer = RecordingAuditWriter()
    return await run_loop(
        graph=LEGACY_GRAPH,
        registry=registry,
        resolver=SlotResolver(registry),
        store=store,
        supervisor=CheckpointSupervisor(store),
        audit_writer=audit_writer,
        input_factory=StubInputFactory(),
        run_id=run_id,
        repo_url="file:///test",
        commit_hash="abc",
        clone_root=Path("/tmp"),
        side_effect_slots=frozenset(),
        observer=observer,
        resume_after=resume_after,
        initial_results=initial_results,
        initial_completed=initial_completed,
    )


class TestLoopWalksLegacyGraph:
    @pytest.mark.asyncio
    async def test_loop_completes_legacy_graph(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            registry = AgentRegistry()
            register_legacy_stubs(registry)
            result = await run_legacy(store=store, registry=registry, run_id="r1")
            assert result.stop_reason == "completed"
            assert result.failed is False
            assert len(result.completed_phases) == 10
            assert len(result.checkpoint_ids) == 10
            assert len(result.audit_hashes) == 10
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_slot_executions_have_expected_shape(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            registry = AgentRegistry()
            register_legacy_stubs(registry)
            result = await run_legacy(store=store, registry=registry, run_id="r1")
            for execution in result.slot_executions:
                assert execution.ok is True
                assert execution.output is not None
                assert execution.error is None
                assert execution.checkpoint_id is not None
                assert execution.audit_hash is not None
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_audit_entries_per_phase(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            registry = AgentRegistry()
            register_legacy_stubs(registry)
            audit_writer = RecordingAuditWriter()
            await run_loop(
                graph=LEGACY_GRAPH,
                registry=registry,
                resolver=SlotResolver(registry),
                store=store,
                supervisor=CheckpointSupervisor(store),
                audit_writer=audit_writer,
                input_factory=StubInputFactory(),
                run_id="r1",
                repo_url="file:///test",
                commit_hash="abc",
                clone_root=Path("/tmp"),
                side_effect_slots=frozenset(),
            )
            assert len(audit_writer.entries) == 10
            for entry in audit_writer.entries:
                assert entry["run_id"] == "r1"
                assert entry["result"]["status"] == "success"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_observer_fires_per_phase(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            registry = AgentRegistry()
            register_legacy_stubs(registry)
            observer = RecordingObserver()
            await run_legacy(
                store=store,
                registry=registry,
                run_id="r1",
                observer=observer,
            )
            assert len(observer.calls) == 10
            slot_names = [name for name, _ in observer.calls]
            assert slot_names[0] == "recon"
            assert slot_names[-1] == "skill_curator"
        finally:
            store.close()


class TestLoopFailureAndResume:
    @pytest.mark.asyncio
    async def test_failed_phase_stops_loop(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            registry = AgentRegistry()
            for name, tags in [
                ("recon", frozenset({"deterministic", "cpg-builder"})),
                ("blast", frozenset({"deterministic", "graph-query"})),
            ]:
                agent = StubAgent(name, fail_after=0 if name == "blast" else None)
                registry.register(AgentSpec(name=name, agent=agent, capability_tags=tags))
            graph = PhaseGraph(
                name="small",
                entry="recon",
                slots=(
                    PhaseSlot(
                        name="recon",
                        capability_tags=frozenset({"deterministic", "cpg-builder"}),
                    ),
                    PhaseSlot(
                        name="blast",
                        capability_tags=frozenset({"deterministic", "graph-query"}),
                    ),
                ),
                transitions={"recon": ("blast",), "blast": (None,)},
            )
            result = await run_loop(
                graph=graph,
                registry=registry,
                resolver=SlotResolver(registry),
                store=store,
                supervisor=CheckpointSupervisor(store),
                audit_writer=RecordingAuditWriter(),
                input_factory=StubInputFactory(),
                run_id="r1",
                repo_url="file:///test",
                commit_hash="abc",
                clone_root=Path("/tmp"),
                side_effect_slots=frozenset(),
            )
            assert result.stop_reason == "failed"
            assert result.failed is True
            assert "recon" in result.completed_phases
            assert "blast" not in result.completed_phases
            assert len(result.checkpoint_ids) == 1
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_resume_after_checkpoint_reaches_completion(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            graph = PhaseGraph(
                name="small",
                entry="a",
                slots=(
                    PhaseSlot(name="a", capability_tags=frozenset({"x"})),
                    PhaseSlot(name="b", capability_tags=frozenset({"x"})),
                    PhaseSlot(name="c", capability_tags=frozenset({"x"})),
                ),
                transitions={"a": ("b",), "b": ("c",), "c": (None,)},
            )
            registry = AgentRegistry()
            registry.register(
                AgentSpec(
                    name="step",
                    agent=StubAgent("step"),
                    capability_tags=frozenset({"x"}),
                )
            )
            first = await run_loop(
                graph=graph,
                registry=registry,
                resolver=SlotResolver(registry),
                store=store,
                supervisor=CheckpointSupervisor(store),
                audit_writer=RecordingAuditWriter(),
                input_factory=StubInputFactory(),
                run_id="r1",
                repo_url="file:///test",
                commit_hash="abc",
                clone_root=Path("/tmp"),
                side_effect_slots=frozenset(),
            )
            assert first.stop_reason == "completed"
            last_checkpoint = read_checkpoint(store, "r1")
            assert last_checkpoint is not None
            assert last_checkpoint.phase == "c"

            resumed = await run_loop(
                graph=graph,
                registry=registry,
                resolver=SlotResolver(registry),
                store=store,
                supervisor=CheckpointSupervisor(store),
                audit_writer=RecordingAuditWriter(),
                input_factory=StubInputFactory(),
                run_id="r1",
                repo_url="file:///test",
                commit_hash="abc",
                clone_root=Path("/tmp"),
                side_effect_slots=frozenset(),
                resume_after="a",
                initial_results={"a": "a_output"},
                initial_completed=("a",),
            )
            assert resumed.stop_reason == "completed"
            assert resumed.completed_phases == ("a", "b", "c")
        finally:
            store.close()


class TestSeedRegisteredAgents:
    @pytest.mark.asyncio
    async def test_seed_populates_registered_agents(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            registry = AgentRegistry()
            register_legacy_stubs(registry)
            await run_legacy(store=store, registry=registry, run_id="r1")
            rows = store.query("SELECT name FROM registered_agents")
            names = {row["name"] for row in rows}
            assert "writer" in names
            assert "correctness_verifier" in names
            assert "recon" in names
        finally:
            store.close()


class TestCheckpointSupervisor:
    def test_detects_stale_run(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "stale_run")
            old = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
            store.execute(
                "INSERT INTO checkpoints "
                "(checkpoint_id, run_id, phase, state_json, created_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("cp1", "stale_run", "recon", "{}", old, old),
            )
            supervisor = CheckpointSupervisor(store)
            stale = supervisor.find_stale_runs()
            assert "stale_run" in stale
        finally:
            store.close()

    def test_ignores_fresh_run(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "fresh_run")
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            store.execute(
                "INSERT INTO checkpoints "
                "(checkpoint_id, run_id, phase, state_json, created_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("cp1", "fresh_run", "recon", "{}", now, now),
            )
            supervisor = CheckpointSupervisor(store)
            assert "fresh_run" not in supervisor.find_stale_runs()
        finally:
            store.close()

    def test_resume_plan_from_checkpoint(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            state = json.dumps(
                {
                    "schema_version": "1.0",
                    "completed_phases": ["recon", "blast"],
                    "pending_phases": ["analyst"],
                    "retries": 0,
                    "state": {},
                    "artifacts": {},
                },
                sort_keys=True,
            )
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            store.execute(
                "INSERT INTO checkpoints "
                "(checkpoint_id, run_id, phase, state_json, created_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("cp1", "r1", "blast", state, now, now),
            )
            supervisor = CheckpointSupervisor(store)
            plan = supervisor.build_resume_plan("r1", graph=LEGACY_GRAPH)
            assert plan.resume_after_phase == "blast"
            assert plan.completed_phases == ("recon", "blast")
            assert plan.pending_phases == ("analyst",)
        finally:
            store.close()

    def test_resume_plan_without_checkpoint(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            insert_run_row(store, "r1")
            supervisor = CheckpointSupervisor(store)
            plan = supervisor.build_resume_plan("r1", graph=LEGACY_GRAPH)
            assert plan.resume_after_phase is None
            assert plan.completed_phases == ()
            assert plan.pending_phases == LEGACY_GRAPH.slot_names()
        finally:
            store.close()


class TestLangGraphOrchestrator:
    def _build(
        self,
        tmp_path: Path,
        *,
        observer: RecordingObserver | None = None,
    ) -> tuple[LangGraphOrchestrator, SQLiteStore]:
        store = open_store(tmp_path)
        registry = AgentRegistry()
        register_legacy_stubs(registry)
        orchestrator = LangGraphOrchestrator(
            store=store,
            registry=registry,
            supervisor=CheckpointSupervisor(store),
            audit_writer=RecordingAuditWriter(),
            input_factory=StubInputFactory(),
            artifact_root=tmp_path / "artifacts",
            observer=observer,
            side_effect_slots=frozenset(),
        )
        return orchestrator, store

    def test_orchestrator_run_completes(self, tmp_path: Path) -> None:
        orchestrator, store = self._build(tmp_path)
        try:
            result = orchestrator.run(
                RunConfig(
                    url="file:///test",
                    target="t.py:foo",
                    directive="test directive",
                )
            )
            assert result.status == "success"
            assert result.error is None
        finally:
            store.close()

    def test_orchestrator_observer_fires(self, tmp_path: Path) -> None:
        observer = RecordingObserver()
        orchestrator, store = self._build(tmp_path, observer=observer)
        try:
            orchestrator.run(
                RunConfig(
                    url="file:///test",
                    target="t.py:foo",
                    directive="test directive",
                )
            )
            assert len(observer.calls) == 10
        finally:
            store.close()

    def test_orchestrator_get_state(self, tmp_path: Path) -> None:
        orchestrator, store = self._build(tmp_path)
        try:
            result = orchestrator.run(
                RunConfig(
                    url="file:///test",
                    target="t.py:foo",
                    directive="test directive",
                )
            )
            state = orchestrator.get_state(result.run_id)
            assert state.run_id == result.run_id
            assert len(state.completed_phases) == 10
            assert state.paused is False
        finally:
            store.close()
