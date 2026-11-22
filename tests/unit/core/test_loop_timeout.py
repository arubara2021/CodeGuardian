"""Tests for the loop's wall-clock timeout and cancellation.

Two independent mechanisms stop a runaway agent. The per-slot timeout
is a ceiling the loop enforces on the agent call. The cancellation
token is a signal from outside the loop that the run should stop. Both
must leave the loop in a clean state: no extra checkpoint, workspace
rolled back, phase marked failed or paused.

The "no timeout means the loop hangs" test wraps the call in an outer
``asyncio.wait_for`` and asserts the outer timeout fires. That is the
honest way to test for absence of a timeout without hanging the suite.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from codeguardian.core.orchestrator.cancellation import (
    CancellationToken,
)
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
from codeguardian.core.orchestrator.runtime_wiring import RuntimeWiring
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore


def _open_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "t.db")
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


class _SleepAgent:
    name = "writer"

    def __init__(self, seconds: float = 10.0) -> None:
        self.seconds = seconds

    async def run(self, _input: Any) -> str:
        await asyncio.sleep(self.seconds)
        return "never"


class _StubInputFactory:
    def build(self, _slot: str, _context: Any) -> None:
        return None


class _StubAuditWriter:
    def __call__(self, _entry: dict[str, Any]) -> str:
        return "hash"


def _one_phase_graph(timeout_s: float | None) -> PhaseGraph:
    slot = PhaseSlot(
        name="writer",
        capability_tags=frozenset({"x"}),
        agent_timeout_s=timeout_s,
    )
    return PhaseGraph(
        name="t",
        entry="writer",
        slots=(slot,),
        transitions={"writer": (None,)},
    )


async def _run_with(
    store: SQLiteStore,
    graph: PhaseGraph,
    agent: _SleepAgent,
    *,
    wiring: RuntimeWiring | None = None,
    clone_root: Path,
) -> Any:
    registry = AgentRegistry()
    registry.register(
        AgentSpec(
            name=agent.name,
            agent=agent,
            capability_tags=frozenset({"x"}),
        )
    )
    return await run_loop(
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
        clone_root=clone_root,
        side_effect_slots=frozenset(),
        wiring=wiring,
    )


@pytest.mark.asyncio
async def test_timeout_fires_quickly(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        wiring = RuntimeWiring(store=store, run_id="run1")
        start = time.monotonic()
        result = await _run_with(
            store,
            _one_phase_graph(timeout_s=0.2),
            _SleepAgent(seconds=10.0),
            wiring=wiring,
            clone_root=tmp_path,
        )
        elapsed = time.monotonic() - start
        assert result.stop_reason == "failed"
        assert result.error is not None
        assert "timeout" in result.error.message.lower()
        assert elapsed < 1.5, f"loop did not stop within budget: {elapsed:.2f}s"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_no_timeout_hangs(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        wiring = RuntimeWiring(store=store, run_id="run1")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                _run_with(
                    store,
                    _one_phase_graph(timeout_s=None),
                    _SleepAgent(seconds=5.0),
                    wiring=wiring,
                    clone_root=tmp_path,
                ),
                timeout=0.5,
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancellation_token_pauses_run(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        token = CancellationToken()
        wiring = RuntimeWiring(
            store=store,
            run_id="run1",
            cancellation_token=token,
        )

        async def cancel_soon() -> None:
            await asyncio.sleep(0.1)
            token.set("test pause")

        _task = asyncio.create_task(cancel_soon())  # noqa: RUF006
        start = time.monotonic()
        result = await _run_with(
            store,
            _one_phase_graph(timeout_s=None),
            _SleepAgent(seconds=10.0),
            wiring=wiring,
            clone_root=tmp_path,
        )
        elapsed = time.monotonic() - start
        assert result.stop_reason == "paused"
        assert elapsed < 1.5, f"cancellation did not fire quickly: {elapsed:.2f}s"
        rows = store.query("SELECT COUNT(*) AS c FROM checkpoints WHERE run_id = 'run1'")
        assert rows[0]["c"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancellation_before_first_phase(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        token = CancellationToken()
        token.set("pre-set")
        wiring = RuntimeWiring(
            store=store,
            run_id="run1",
            cancellation_token=token,
        )
        start = time.monotonic()
        result = await _run_with(
            store,
            _one_phase_graph(timeout_s=None),
            _SleepAgent(seconds=10.0),
            wiring=wiring,
            clone_root=tmp_path,
        )
        elapsed = time.monotonic() - start
        assert result.stop_reason == "paused"
        assert result.completed_phases == ()
        assert elapsed < 1.0
    finally:
        store.close()


class TestSourceDiscipline:
    def test_loop_uses_asyncio_timeout(self) -> None:
        source = Path("python/codeguardian/core/orchestrator/runtime_wiring.py").read_text()
        assert "asyncio.timeout" in source, (
            "runtime_wiring.py must use asyncio.timeout, not asyncio.wait_for"
        )

    def test_runtime_wiring_uses_wait_for_cancellation(self) -> None:
        source = Path("python/codeguardian/core/orchestrator/runtime_wiring.py").read_text()
        assert "wait_with_cancellation" in source

    def test_loop_reraises_cancelled_error(self) -> None:
        source = Path("python/codeguardian/core/orchestrator/loop.py").read_text()
        assert "except asyncio.CancelledError" in source
        assert "CancellationRequested" in source


@pytest.mark.asyncio
async def test_timeout_and_cancellation_combined(tmp_path: Path) -> None:
    """A phase with both a timeout and a cancellation token must honour
    whichever fires first. Cancellation fires first here.
    """
    store = _open_store(tmp_path)
    try:
        token = CancellationToken()
        wiring = RuntimeWiring(
            store=store,
            run_id="run1",
            cancellation_token=token,
        )

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            token.set("cancel before timeout")

        _task = asyncio.create_task(cancel_soon())  # noqa: RUF006
        start = time.monotonic()
        result = await _run_with(
            store,
            _one_phase_graph(timeout_s=2.0),
            _SleepAgent(seconds=10.0),
            wiring=wiring,
            clone_root=tmp_path,
        )
        elapsed = time.monotonic() - start
        assert result.stop_reason == "paused"
        assert elapsed < 1.0
    finally:
        store.close()
