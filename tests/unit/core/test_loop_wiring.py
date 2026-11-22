"""Tests for the runtime wiring layer and the loop's wiring discipline.

The wiring layer is the only place that knows which Phase 13 module
runs when. The loop must not import those modules directly. Two kinds
of test prove that: a static source inspection that reads both files,
and a runtime spy that proves the wiring's methods are called once per
phase.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codeguardian.core.orchestrator.checkpoint_supervisor import (
    CheckpointSupervisor,
)
from codeguardian.core.orchestrator.loop import run_loop
from codeguardian.core.orchestrator.phase_graph import PhaseGraph, PhaseSlot
from codeguardian.core.orchestrator.registry import (
    AgentRegistry,
    AgentSpec,
    SlotResolver,
)
from codeguardian.core.orchestrator.runtime_wiring import (
    PhaseOutcome,
    PhaseRuntime,
    RuntimeWiring,
)
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore

_WIRING_MODULES = (
    "context_engine",
    "capability_attenuation",
    "consent",
    "workspace_snapshot",
    "cancellation",
    "audit_anchor",
)

_LOOP_FORBIDDEN_IMPORTS = (
    "verifier_stack",
    "deterministic_gate",
    "context_engine",
    "capability_attenuation",
    "consent",
    "verification_prompt",
)


def _open_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "w.db")
    store.open()
    MigrationRunner(store).run()
    store.execute(
        "INSERT OR IGNORE INTO repos (repo_id, url, provider, last_seen_at) "
        "VALUES ('r','file:///x','local','2026-11-07T00:00:00Z')"
    )
    store.execute(
        "INSERT OR IGNORE INTO runs (run_id, repo_id, commit_hash, "
        "target_path, mode, directive, started_at, status, artifact_dir) "
        "VALUES ('run1','r','abc','t.py','legacy','d',"
        "'2026-11-07T00:00:00Z','running','/tmp')"
    )
    return store


class TestSourceDiscipline:
    def test_runtime_wiring_imports_modules(self) -> None:
        source = Path("python/codeguardian/core/orchestrator/runtime_wiring.py").read_text()
        for module in _WIRING_MODULES:
            assert module in source, f"runtime_wiring.py does not reference {module}"

    def test_loop_does_not_import_wired_modules(self) -> None:
        source = Path("python/codeguardian/core/orchestrator/loop.py").read_text()
        for module in _LOOP_FORBIDDEN_IMPORTS:
            assert f"from .{module} import" not in source, (
                f"loop.py imports {module} directly; use runtime_wiring"
            )
            assert f"from codeguardian.core.orchestrator.{module}" not in source, (
                f"loop.py imports {module} directly; use runtime_wiring"
            )

    def test_loop_imports_runtime_wiring(self) -> None:
        source = Path("python/codeguardian/core/orchestrator/loop.py").read_text()
        assert "from .runtime_wiring import" in source


class _SpyWiring(RuntimeWiring):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.before_calls: list[str] = []
        self.around_calls: list[str] = []
        self.after_calls: list[str] = []
        self.before_apply_calls: int = 0
        self.after_run_calls: int = 0

    def before_phase(self, slot, context):
        self.before_calls.append(slot.name)
        return super().before_phase(slot, context)

    async def around_agent(self, slot, agent_spec, agent_input, runtime, context):
        self.around_calls.append(slot.name)
        return await super().around_agent(
            slot,
            agent_spec,
            agent_input,
            runtime,
            context,
        )

    async def after_phase(self, slot, execution, runtime, context):
        self.after_calls.append(slot.name)
        return await super().after_phase(slot, execution, runtime, context)

    async def before_apply(self, slot, diff, context):
        self.before_apply_calls += 1
        return await super().before_apply(slot, diff, context)

    async def after_run(self, loop_result, audit_entries):
        self.after_run_calls += 1
        return await super().after_run(loop_result, audit_entries)


class _StubAgent:
    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, _input: Any) -> str:
        return f"{self.name}_out"


class _StubInputFactory:
    def build(self, _slot: str, _context: Any) -> None:
        return None


class _StubAuditWriter:
    def __call__(self, _entry: dict[str, Any]) -> str:
        return "hash"


@pytest.mark.asyncio
async def test_every_phase_goes_through_wiring(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        graph = PhaseGraph(
            name="t",
            entry="a",
            slots=(
                PhaseSlot(name="a", capability_tags=frozenset({"tag_a"})),
                PhaseSlot(name="b", capability_tags=frozenset({"tag_b"})),
            ),
            transitions={"a": ("b",), "b": (None,)},
        )
        registry = AgentRegistry()
        registry.register(
            AgentSpec(
                name="a",
                agent=_StubAgent("a"),
                capability_tags=frozenset({"tag_a"}),
            )
        )
        registry.register(
            AgentSpec(
                name="b",
                agent=_StubAgent("b"),
                capability_tags=frozenset({"tag_b"}),
            )
        )
        wiring = _SpyWiring(
            store=store,
            run_id="run1",
            audit_writer=_StubAuditWriter(),
        )
        result = await run_loop(
            graph=graph,
            registry=registry,
            resolver=SlotResolver(registry),
            store=store,
            supervisor=CheckpointSupervisor(store),
            audit_writer=_StubAuditWriter(),
            input_factory=_StubInputFactory(),
            run_id="run1",
            repo_url="file:///x",
            commit_hash="abc",
            clone_root=tmp_path,
            side_effect_slots=frozenset(),
            wiring=wiring,
        )
        assert result.stop_reason == "completed"
        assert wiring.before_calls == ["a", "b"]
        assert wiring.around_calls == ["a", "b"]
        assert wiring.after_calls == ["a", "b"]
        assert wiring.after_run_calls == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_wiring_none_preserves_old_behaviour(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        graph = PhaseGraph(
            name="t",
            entry="a",
            slots=(PhaseSlot(name="a", capability_tags=frozenset({"x"})),),
            transitions={"a": (None,)},
        )
        registry = AgentRegistry()
        registry.register(
            AgentSpec(
                name="a",
                agent=_StubAgent("a"),
                capability_tags=frozenset({"x"}),
            )
        )
        result = await run_loop(
            graph=graph,
            registry=registry,
            resolver=SlotResolver(registry),
            store=store,
            supervisor=CheckpointSupervisor(store),
            audit_writer=_StubAuditWriter(),
            input_factory=_StubInputFactory(),
            run_id="run1",
            repo_url="file:///x",
            commit_hash="abc",
            clone_root=tmp_path,
            side_effect_slots=frozenset(),
            wiring=None,
        )
        assert result.stop_reason == "completed"
        rows = store.query("SELECT COUNT(*) AS c FROM agent_calls WHERE run_id = 'run1'")
        assert rows[0]["c"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_default_wiring_writes_cold_tier(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        graph = PhaseGraph(
            name="t",
            entry="a",
            slots=(PhaseSlot(name="a", capability_tags=frozenset({"x"})),),
            transitions={"a": (None,)},
        )
        registry = AgentRegistry()
        registry.register(
            AgentSpec(
                name="a",
                agent=_StubAgent("a"),
                capability_tags=frozenset({"x"}),
            )
        )
        result = await run_loop(
            graph=graph,
            registry=registry,
            resolver=SlotResolver(registry),
            store=store,
            supervisor=CheckpointSupervisor(store),
            audit_writer=_StubAuditWriter(),
            input_factory=_StubInputFactory(),
            run_id="run1",
            repo_url="file:///x",
            commit_hash="abc",
            clone_root=tmp_path,
            side_effect_slots=frozenset(),
        )
        assert result.stop_reason == "completed"
        rows = store.query("SELECT role, outcome FROM agent_calls WHERE run_id = 'run1'")
        assert len(rows) == 1
        assert rows[0]["role"] == "a"
        assert rows[0]["outcome"] == "success"
    finally:
        store.close()


class TestAfterRunAnchor:
    @pytest.mark.asyncio
    async def test_after_run_writes_anchor_head(self, tmp_path: Path) -> None:
        from codeguardian.core.orchestrator.audit_anchor import FileAnchor

        store = _open_store(tmp_path)
        try:
            anchor = FileAnchor(tmp_path / "anchor.json", key=b"k" * 32)
            graph = PhaseGraph(
                name="t",
                entry="a",
                slots=(PhaseSlot(name="a", capability_tags=frozenset({"x"})),),
                transitions={"a": (None,)},
            )
            registry = AgentRegistry()
            registry.register(
                AgentSpec(
                    name="a",
                    agent=_StubAgent("a"),
                    capability_tags=frozenset({"x"}),
                )
            )
            wiring = RuntimeWiring(
                store=store,
                run_id="run1",
                audit_writer=_StubAuditWriter(),
                anchor=anchor,
            )
            await run_loop(
                graph=graph,
                registry=registry,
                resolver=SlotResolver(registry),
                store=store,
                supervisor=CheckpointSupervisor(store),
                audit_writer=_StubAuditWriter(),
                input_factory=_StubInputFactory(),
                run_id="run1",
                repo_url="file:///x",
                commit_hash="abc",
                clone_root=tmp_path,
                side_effect_slots=frozenset(),
                wiring=wiring,
            )
            assert anchor.read_head() == "hash"
        finally:
            store.close()


class TestPhaseRuntimeShape:
    def test_default_fields(self) -> None:
        slot = PhaseSlot(name="x")
        runtime = PhaseRuntime(slot=slot)
        assert runtime.slot is slot
        assert runtime.compiled_context is None
        assert runtime.snapshot is None
        assert runtime.attenuation is None
        assert runtime.trace is None
        assert runtime.agent_timeout_s is None
        assert runtime.context_refused is False

    def test_phase_outcome_defaults(self) -> None:
        outcome = PhaseOutcome()
        assert outcome.rolled_back == ()
        assert outcome.rollback_refused == ()
