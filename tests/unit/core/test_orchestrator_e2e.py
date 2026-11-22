"""End-to-end tests for the orchestrator pipeline.

These tests exercise the whole set of pieces together: the loop, the
registry, the checkpoint supervisor, the audit writer, the metrics
collector, and the verifier stack. Stubs stand in for agents and
models because the goal is wiring coverage, not model quality. The
tests prove five behaviours the settlement names: a full pipeline
walks to completion, a killed phase resumes from its last checkpoint,
the verifier stack retries a bad candidate, the audit chain is
internally consistent, and a role not in the original nine passes the
new foreign key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codeguardian.core.agents._loop import GenerationResult
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.orchestrator.cancellation import CancellationToken
from codeguardian.core.orchestrator.checkpoint_supervisor import (
    CheckpointSupervisor,
)
from codeguardian.core.orchestrator.checkpointing import read_checkpoint
from codeguardian.core.orchestrator.deterministic_gate import (
    CheckOutcome,
    DeterministicGateResult,
)
from codeguardian.core.orchestrator.evidence import ToolCallFailed
from codeguardian.core.orchestrator.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from codeguardian.core.orchestrator.loop import (
    LoopContext,
    SlotExecution,
    run_loop,
)
from codeguardian.core.orchestrator.phase_graph import PhaseGraph, PhaseSlot
from codeguardian.core.orchestrator.phase_graphs import LEGACY_GRAPH
from codeguardian.core.orchestrator.registry import (
    AgentRegistry,
    AgentSpec,
    SlotResolver,
)
from codeguardian.core.orchestrator.runtime_wiring import RuntimeWiring
from codeguardian.core.orchestrator.verifier_stack import run_verifier_stack
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
    "project_test_suite": SimpleNamespace(),
}


_LEGACY_STUBS: list[tuple[str, frozenset[str]]] = [
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


class StubAgent:
    def __init__(self, name: str, *, failures: int = 0) -> None:
        self.name = name
        self._remaining_failures = failures

    async def run(self, input: Any) -> Any:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise CodeGuardianError(ErrorCode.SYS_INTERNAL, f"{self.name} scripted failure")
        return _STUB_OUTPUTS.get(self.name, f"{self.name}_output")


class ChainAuditWriter:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._prev_hash = "GENESIS"

    def __call__(self, entry: dict[str, Any]) -> str:
        full = {
            "entry_id": str(uuid.uuid4()),
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            **entry,
        }
        payload_json = json.dumps(full, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        entry_hash = hashlib.sha256((self._prev_hash + payload_hash).encode()).hexdigest()
        stored = {
            **full,
            "payload_hash": payload_hash,
            "prev_hash": self._prev_hash,
            "entry_hash": entry_hash,
        }
        self.entries.append(stored)
        self._prev_hash = entry_hash
        return entry_hash

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for entry in self.entries:
            payload = {
                key: value
                for key, value in entry.items()
                if key not in ("payload_hash", "prev_hash", "entry_hash")
            }
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            expected_payload = hashlib.sha256(payload_json.encode()).hexdigest()
            if entry["payload_hash"] != expected_payload:
                return False
            if entry["prev_hash"] != prev:
                return False
            expected_entry = hashlib.sha256((prev + expected_payload).encode()).hexdigest()
            if entry["entry_hash"] != expected_entry:
                return False
            prev = expected_entry
        return True


class StubInputFactory:
    def build(self, slot_name: str, context: LoopContext) -> dict[str, Any]:
        return {"slot": slot_name}


class RecordingObserver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SlotExecution]] = []

    def __call__(
        self,
        slot_name: str,
        execution: SlotExecution,
        context: LoopContext,
    ) -> None:
        self.calls.append((slot_name, execution))


class FakeVerdict:
    def __init__(self, value: str, verifier_type: str = "correctness") -> None:
        self.verdict = value
        self.verifier_type = verifier_type


class FakeVerifierResult:
    def __init__(
        self,
        value: str,
        verifier_type: str = "correctness",
        reason: str | None = None,
    ) -> None:
        self.verdict = FakeVerdict(value, verifier_type)
        self.verdict_reason = reason


def open_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "e2e.db")
    store.open()
    MigrationRunner(store).run()
    store.execute(
        "INSERT OR IGNORE INTO repos (repo_id, url, provider, last_seen_at) "
        "VALUES ('r', 'file:///x', 'local', '2026-11-07T00:00:00Z')"
    )
    store.execute(
        "INSERT OR IGNORE INTO runs (run_id, repo_id, commit_hash, "
        "target_path, mode, directive, started_at, status, artifact_dir) "
        "VALUES ('run1', 'r', 'abc', 't.py', 'legacy', 'd', "
        "'2026-11-07T00:00:00Z', 'running', '/tmp')"
    )
    return store


def register_legacy(
    registry: AgentRegistry,
    *,
    failures: dict[str, int] | None = None,
) -> dict[str, StubAgent]:
    failures = failures or {}
    stubs: dict[str, StubAgent] = {}
    for name, tags in _LEGACY_STUBS:
        stub = StubAgent(name, failures=failures.get(name, 0))
        registry.register(AgentSpec(name=name, agent=stub, capability_tags=tags))
        stubs[name] = stub
    return stubs


async def run_pipeline(
    *,
    store: SQLiteStore,
    registry: AgentRegistry,
    run_id: str,
    audit_writer: Any,
    observer: RecordingObserver | None = None,
    resume_after: str | None = None,
    initial_results: dict[str, Any] | None = None,
    initial_completed: tuple[str, ...] = (),
) -> Any:
    return await run_loop(
        graph=LEGACY_GRAPH,
        registry=registry,
        resolver=SlotResolver(registry),
        store=store,
        supervisor=CheckpointSupervisor(store),
        audit_writer=audit_writer,
        input_factory=StubInputFactory(),
        run_id=run_id,
        repo_url="file:///x",
        commit_hash="abc",
        clone_root=Path("/tmp"),
        side_effect_slots=frozenset(),
        observer=observer,
        resume_after=resume_after,
        initial_results=initial_results,
        initial_completed=initial_completed,
    )


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_full_pipeline_completes(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            audit_writer = ChainAuditWriter()
            result = await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=audit_writer,
            )
            assert result.stop_reason == "completed"
            assert result.failed is False
            assert len(result.completed_phases) == 10
            assert len(result.checkpoint_ids) == 10
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_store_has_phase_executions(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=ChainAuditWriter(),
            )
            rows = store.query("SELECT COUNT(*) AS c FROM registered_agents")
            assert rows[0]["c"] >= 8
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_checkpoints_written_per_phase(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=ChainAuditWriter(),
            )
            rows = store.query(
                "SELECT phase FROM checkpoints WHERE run_id = ? ORDER BY created_at",
                ("run1",),
            )
            phases = [row["phase"] for row in rows]
            assert phases == [
                "recon",
                "blast",
                "analyst",
                "tester",
                "writer",
                "sandbox_verify",
                "verifier_correctness",
                "verifier_security",
                "verifier_contract",
                "skill_curator",
            ]
        finally:
            store.close()


class TestKillAndResume:
    @pytest.mark.asyncio
    async def test_resume_after_failed_phase(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry, failures={"blast": 1})
            audit_writer = ChainAuditWriter()

            first = await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=audit_writer,
            )
            assert first.stop_reason == "failed"
            assert "recon" in first.completed_phases
            assert "blast" not in first.completed_phases

            checkpoint = read_checkpoint(store, "run1")
            assert checkpoint is not None
            assert checkpoint.phase == "recon"

            supervisor = CheckpointSupervisor(store)
            plan = supervisor.resume("run1", graph=LEGACY_GRAPH)
            assert plan.resume_after_phase == "recon"

            initial_results = {phase: _STUB_OUTPUTS[phase] for phase in plan.completed_phases}
            resumed = await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=audit_writer,
                resume_after="recon",
                initial_results=initial_results,
                initial_completed=plan.completed_phases,
            )
            assert resumed.stop_reason == "completed"
            assert len(resumed.completed_phases) == 10
        finally:
            store.close()


class TestVerifierRetry:
    @pytest.mark.asyncio
    async def test_retry_fires_on_bad_candidate(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            feedbacks: list[Any] = []

            async def generate(feedback: Any) -> GenerationResult[Any]:
                feedbacks.append(feedback)
                if feedback is None:
                    return GenerationResult(payload="bad", attempt=1)
                return GenerationResult(payload="good", attempt=2)

            def gate(payload: Any) -> DeterministicGateResult:
                return DeterministicGateResult(
                    passed=True,
                    checks=(CheckOutcome(name="git apply", status="pass"),),
                    stderr_tail="",
                    diff_hash="h",
                )

            async def apply(payload: Any) -> str:
                return f"ref_{payload}"

            async def verify(payload: Any) -> tuple[Any, ...]:
                if payload == "good":
                    return (FakeVerifierResult("pass"),)
                return (FakeVerifierResult("fail", reason="bad candidate"),)

            rollbacks: list[str] = []

            async def rollback(ref: str) -> None:
                rollbacks.append(ref)

            result = await run_verifier_stack(
                band="review",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
            assert result.passed is True
            assert result.attempts == 2
            assert feedbacks[0] is None
            assert feedbacks[1] is not None
            assert "bad candidate" in (feedbacks[1].failure_summary or "")
            assert rollbacks == ["ref_bad"]
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_block_band_escalates_on_second_failure(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            async def generate(feedback: Any) -> GenerationResult[Any]:
                attempt = 1 if feedback is None else 2
                return GenerationResult(payload=f"c{attempt}", attempt=attempt)

            def gate(payload: Any) -> DeterministicGateResult:
                return DeterministicGateResult(
                    passed=True,
                    checks=(CheckOutcome(name="git", status="pass"),),
                    stderr_tail="",
                    diff_hash="h",
                )

            async def apply(payload: Any) -> str:
                return f"ref_{payload}"

            async def verify(payload: Any) -> tuple[Any, ...]:
                return (FakeVerifierResult("fail", reason="still bad"),)

            async def rollback(ref: str) -> None:
                pass

            result = await run_verifier_stack(
                band="block",
                generate=generate,
                gate=gate,
                apply=apply,
                verify=verify,
                rollback=rollback,
            )
            assert result.passed is False
            assert result.k == 2
            assert result.escalation_required is True
        finally:
            store.close()


class TestAuditChain:
    @pytest.mark.asyncio
    async def test_chain_verifies(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            audit_writer = ChainAuditWriter()
            await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=audit_writer,
            )
            phase_entries = [e for e in audit_writer.entries if "attenuation" not in e["result"]]
            attenuation_entries = [e for e in audit_writer.entries if "attenuation" in e["result"]]
            assert len(phase_entries) == 10
            assert len(attenuation_entries) == 7
            assert len(audit_writer.entries) == 17
            assert audit_writer.verify_chain() is True
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_entries_carry_required_fields(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            audit_writer = ChainAuditWriter()
            await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=audit_writer,
            )
            for entry in audit_writer.entries:
                assert "entry_id" in entry
                assert "timestamp" in entry
                assert entry["run_id"] == "run1"
                assert "phase" in entry
                assert "agent" in entry
                assert "result" in entry
                assert "payload_hash" in entry
                assert "prev_hash" in entry
                assert "entry_hash" in entry
                assert entry["result"]["status"] == "success"
        finally:
            store.close()


class TestNewAgentRegistration:
    @pytest.mark.asyncio
    async def test_new_agent_seeded_and_fk_accepts(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            new_stub = StubAgent("project_test_writer")
            registry.register(
                AgentSpec(
                    name="project_test_writer",
                    agent=new_stub,
                    capability_tags=frozenset({"llm", "coding", "test-suite-producer"}),
                )
            )

            result = await run_pipeline(
                store=store,
                registry=registry,
                run_id="run1",
                audit_writer=ChainAuditWriter(),
            )
            assert result.stop_reason == "completed"
            assert "project_test_suite" in result.completed_phases
            assert len(result.completed_phases) == 11

            rows = store.query(
                "SELECT name FROM registered_agents WHERE name = ?",
                ("project_test_writer",),
            )
            assert rows

            store.execute(
                "INSERT INTO agent_calls "
                "(call_id, run_id, role, outcome, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "new_agent_call",
                    "run1",
                    "project_test_writer",
                    "success",
                    "2026-11-07T00:00:00Z",
                ),
            )
            call_rows = store.query(
                "SELECT role FROM agent_calls WHERE call_id = ?",
                ("new_agent_call",),
            )
            assert call_rows
            assert call_rows[0]["role"] == "project_test_writer"
        finally:
            store.close()


class TestOrchestratorEndToEnd:
    def test_run_completes_and_state_matches(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            registry = AgentRegistry()
            register_legacy(registry)
            observer = RecordingObserver()
            orchestrator = LangGraphOrchestrator(
                store=store,
                registry=registry,
                supervisor=CheckpointSupervisor(store),
                audit_writer=ChainAuditWriter(),
                input_factory=StubInputFactory(),
                artifact_root=tmp_path / "artifacts",
                observer=observer,
                side_effect_slots=frozenset(),
            )
            result = orchestrator.run(
                RunConfig(
                    url="file:///x",
                    target="t.py:foo",
                    directive="test directive",
                )
            )
            assert result.status == "success"
            assert len(observer.calls) == 10

            state = orchestrator.get_state(result.run_id)
            assert len(state.completed_phases) == 10
            assert state.paused is False
        finally:
            store.close()


class TestRuntimeWiringIntegration:
    def _make_single_phase(
        self,
        agent: Any,
        *,
        slot_name: str = "writer",
        workspace_paths: tuple[str, ...] = (),
        agent_timeout_s: float | None = None,
    ) -> PhaseGraph:
        slot = PhaseSlot(
            name=slot_name,
            capability_tags=frozenset({"x"}),
            workspace_paths=workspace_paths,
            agent_timeout_s=agent_timeout_s,
        )
        return PhaseGraph(
            name="t",
            entry=slot_name,
            slots=(slot,),
            transitions={slot_name: (None,)},
        )

    async def _run_single(
        self,
        store: SQLiteStore,
        graph: PhaseGraph,
        agent: Any,
        *,
        audit_writer: Any,
        wiring: RuntimeWiring,
        observer: Any = None,
        clone_root: Path,
    ) -> Any:
        registry = AgentRegistry()
        registry.register(
            AgentSpec(
                name=graph.slots[0].name,
                agent=agent,
                capability_tags=frozenset({"x"}),
            )
        )

        class _IF:
            def build(self, _slot, _ctx):
                return None

        return await run_loop(
            graph=graph,
            registry=registry,
            resolver=SlotResolver(registry),
            store=store,
            supervisor=CheckpointSupervisor(store),
            audit_writer=audit_writer,
            input_factory=_IF(),
            run_id="run1",
            repo_url="file:///x",
            commit_hash="abc",
            clone_root=clone_root,
            side_effect_slots=frozenset(),
            wiring=wiring,
            observer=observer,
        )

    @pytest.mark.asyncio
    async def test_observer_events_land_on_same_phase(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            class _Agent:
                name = "writer"

                async def run(self, _i):
                    return "ok"

            pushed: list[Any] = []

            def observer(slot_name: str, _execution: Any, ctx: Any) -> None:
                ev = ToolCallFailed(tool=f"obs_{slot_name}", error_code=None, reason="probe")
                ctx.evidence_bus.push(ev)
                pushed.append(ev)

            graph = self._make_single_phase(_Agent())
            audit = ChainAuditWriter()
            wiring = RuntimeWiring(store=store, run_id="run1", audit_writer=audit)
            result = await self._run_single(
                store,
                graph,
                _Agent(),
                audit_writer=audit,
                wiring=wiring,
                observer=observer,
                clone_root=tmp_path,
            )
            assert len(pushed) == 1
            events = result.slot_executions[0].events
            assert len(events) == 1
            assert getattr(events[0], "tool", "") == "obs_writer"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_timeout_rolls_back_workspace(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            target = tmp_path / "target.txt"
            target.write_text("original")

            class _SlowAgent:
                name = "writer"

                async def run(self, _i):
                    target.write_text("agent-wrote-this")
                    await asyncio.sleep(10)
                    return "never"

            graph = self._make_single_phase(
                _SlowAgent(),
                workspace_paths=("target.txt",),
                agent_timeout_s=0.2,
            )
            audit = ChainAuditWriter()
            wiring = RuntimeWiring(store=store, run_id="run1", audit_writer=audit)
            start = time.monotonic()
            result = await self._run_single(
                store,
                graph,
                _SlowAgent(),
                audit_writer=audit,
                wiring=wiring,
                clone_root=tmp_path,
            )
            elapsed = time.monotonic() - start

            assert result.stop_reason == "failed"
            assert elapsed < 3.0
            assert target.read_text() == "original"
            failure_entries = [
                e for e in audit.entries if e.get("result", {}).get("status") == "failure"
            ]
            assert len(failure_entries) == 1
            assert "target.txt" in failure_entries[0]["result"]["rolled_back"]
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_failed_agent_rolls_back_workspace(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            target = tmp_path / "target.txt"
            target.write_text("original")

            class _WritingFailingAgent:
                name = "writer"

                async def run(self, _i):
                    target.write_text("agent-wrote")
                    raise CodeGuardianError(ErrorCode.SYS_INTERNAL, "simulated failure")

            graph = self._make_single_phase(
                _WritingFailingAgent(),
                workspace_paths=("target.txt",),
            )
            audit = ChainAuditWriter()
            wiring = RuntimeWiring(store=store, run_id="run1", audit_writer=audit)
            result = await self._run_single(
                store,
                graph,
                _WritingFailingAgent(),
                audit_writer=audit,
                wiring=wiring,
                clone_root=tmp_path,
            )
            assert result.stop_reason == "failed"
            assert target.read_text() == "original"
            failure_entries = [
                e for e in audit.entries if e.get("result", {}).get("status") == "failure"
            ]
            assert len(failure_entries) == 1
            assert "rolled_back" in failure_entries[0]["result"]
            assert "target.txt" in failure_entries[0]["result"]["rolled_back"]
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_new_agent_recorded_in_audit(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            class _CustomAgent:
                name = "custom_agent"

                async def run(self, _i):
                    return "ok"

            slot = PhaseSlot(
                name="writer",
                capability_tags=frozenset({"x"}),
            )
            graph = PhaseGraph(
                name="t",
                entry="writer",
                slots=(slot,),
                transitions={"writer": (None,)},
            )
            audit = ChainAuditWriter()
            wiring = RuntimeWiring(store=store, run_id="run1", audit_writer=audit)

            registry = AgentRegistry()
            registry.register(
                AgentSpec(
                    name="custom_agent",
                    agent=_CustomAgent(),
                    capability_tags=frozenset({"x"}),
                )
            )

            class _IF:
                def build(self, _s, _c):
                    return None

            result = await run_loop(
                graph=graph,
                registry=registry,
                resolver=SlotResolver(registry),
                store=store,
                supervisor=CheckpointSupervisor(store),
                audit_writer=audit,
                input_factory=_IF(),
                run_id="run1",
                repo_url="file:///x",
                commit_hash="abc",
                clone_root=tmp_path,
                side_effect_slots=frozenset(),
                wiring=wiring,
            )
            assert result.stop_reason == "completed"
            phase_entries = [
                e
                for e in audit.entries
                if e.get("result", {}).get("status") == "success"
                and "attenuation" not in e.get("result", {})
            ]
            assert len(phase_entries) == 1
            assert phase_entries[0]["agent"] == "custom_agent"
            assert phase_entries[0]["agent"] != "orchestrator"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_pause_mid_phase_cancels_agent(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            class _SlowAgent:
                name = "writer"

                async def run(self, _i):
                    await asyncio.sleep(10)
                    return "never"

            graph = self._make_single_phase(_SlowAgent())
            audit = ChainAuditWriter()
            token = CancellationToken()
            wiring = RuntimeWiring(
                store=store,
                run_id="run1",
                audit_writer=audit,
                cancellation_token=token,
            )

            async def cancel_soon() -> None:
                await asyncio.sleep(0.1)
                token.set("test pause")

            cancel_task = asyncio.create_task(cancel_soon())
            start = time.monotonic()
            result = await self._run_single(
                store,
                graph,
                _SlowAgent(),
                audit_writer=audit,
                wiring=wiring,
                clone_root=tmp_path,
            )
            await cancel_task
            elapsed = time.monotonic() - start

            assert result.stop_reason == "paused"
            assert elapsed < 3.0
            rows = store.query("SELECT COUNT(*) AS c FROM checkpoints WHERE run_id = 'run1'")
            assert rows[0]["c"] == 0
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_branched_graph_pending_correct(self) -> None:
        from codeguardian.core.orchestrator.loop import _pending_after

        graph = PhaseGraph(
            name="branch",
            entry="a",
            slots=(
                PhaseSlot(name="a"),
                PhaseSlot(name="b"),
                PhaseSlot(name="c"),
                PhaseSlot(name="d"),
            ),
            transitions={"a": ("b", "c"), "b": ("d",), "c": ("d",), "d": (None,)},
        )
        after_b = _pending_after(graph, "b", ("a", "b"))
        assert "c" not in after_b
        assert "d" in after_b

    @pytest.mark.asyncio
    async def test_wiring_called_for_every_phase(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            class _SpyWiring(RuntimeWiring):
                def __init__(self, **kwargs: Any) -> None:
                    super().__init__(**kwargs)
                    self.before_count = 0
                    self.around_count = 0
                    self.after_count = 0

                def before_phase(self, slot, context):
                    self.before_count += 1
                    return super().before_phase(slot, context)

                async def around_agent(self, *args, **kwargs):
                    self.around_count += 1
                    return await super().around_agent(*args, **kwargs)

                async def after_phase(self, *args, **kwargs):
                    self.after_count += 1
                    return await super().after_phase(*args, **kwargs)

            class _Agent:
                name = "writer"

                async def run(self, _i):
                    return "ok"

            graph = self._make_single_phase(_Agent())
            audit = ChainAuditWriter()
            wiring = _SpyWiring(store=store, run_id="run1", audit_writer=audit)
            await self._run_single(
                store,
                graph,
                _Agent(),
                audit_writer=audit,
                wiring=wiring,
                clone_root=tmp_path,
            )
            assert wiring.before_count == 1
            assert wiring.around_count == 1
            assert wiring.after_count == 1
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_trusted_read_paths_enforced_and_audited(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            clone_dir = tmp_path / "clone"
            repo_dir = tmp_path / "repo"
            config_dir = tmp_path / "config"
            outside_dir = tmp_path / "outside"
            for directory in (clone_dir, repo_dir, config_dir, outside_dir):
                directory.mkdir()

            repo_prompt = repo_dir / "prompt.txt"
            repo_prompt.write_text("prompt-data", encoding="utf-8")
            config_file = config_dir / "settings.toml"
            config_file.write_text("k = 1", encoding="utf-8")
            outside_file = outside_dir / "blocked.txt"
            outside_file.write_text("blocked", encoding="utf-8")

            class _ProbeAgent:
                name = "writer"

                async def run(self, _i: Any) -> str:
                    assert repo_prompt.read_text(encoding="utf-8") == "prompt-data"
                    assert config_file.read_text(encoding="utf-8") == "k = 1"
                    (clone_dir / "patch.py").write_text("x = 1\n", encoding="utf-8")
                    with pytest.raises(PermissionError):
                        (repo_dir / "escape.py").write_text("bad\n", encoding="utf-8")
                    with pytest.raises(PermissionError):
                        outside_file.read_text(encoding="utf-8")
                    return "ok"

            graph = self._make_single_phase(_ProbeAgent())
            audit = ChainAuditWriter()
            wiring = RuntimeWiring(
                store=store,
                run_id="run1",
                audit_writer=audit,
                repo_root=repo_dir,
                config_home=config_dir,
            )
            result = await self._run_single(
                store,
                graph,
                _ProbeAgent(),
                audit_writer=audit,
                wiring=wiring,
                clone_root=clone_dir,
            )
            assert result.stop_reason == "completed"
            granted_entries = [
                entry["result"]["attenuation"]["trusted_read_paths"]
                for entry in audit.entries
                if "attenuation" in entry.get("result", {})
                and "trusted_read_paths" in entry["result"]["attenuation"]
            ]
            assert granted_entries
            assert str(repo_dir.resolve()) in granted_entries[0]
            assert str(config_dir.resolve()) in granted_entries[0]
        finally:
            store.close()
