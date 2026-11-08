"""Tests for phase gates, consent, telemetry metrics, and tracing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.orchestrator.consent import (
    ApplyRequest,
    PlanApprovalRequest,
    request_apply_consent,
    request_plan_approval,
)
from codeguardian.core.orchestrator.phase_gates import (
    Gates,
    PhaseContext,
    get_gate,
    has_gate,
    known_gates,
)
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore
from codeguardian.core.telemetry import (
    LangSmithAdapter,
    NoopMetricsCollector,
    SqliteMetricsCollector,
    TraceContext,
)
from codeguardian.core.telemetry.metrics import PhaseTiming
from codeguardian.interfaces.model import ModelOptions, ModelResponse


def make_context(
    *,
    results: dict[str, Any] | None = None,
) -> PhaseContext:
    return PhaseContext(
        run_id="run-1",
        repo_url="file:///x",
        commit_hash="abc",
        clone_root=Path("/tmp"),
        results=results or {},
        evidence=(),
    )


def open_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "gates.db")
    store.open()
    MigrationRunner(store).run()
    store.execute(
        "INSERT OR IGNORE INTO repos (repo_id, url, provider, last_seen_at) "
        "VALUES ('r', 'file:///x', 'local', '2026-11-07T00:00:00Z')"
    )
    store.execute(
        "INSERT OR IGNORE INTO runs (run_id, repo_id, commit_hash, "
        "target_path, mode, directive, started_at, status, artifact_dir) "
        "VALUES ('run-1', 'r', 'abc', 't.py', 'legacy', 'd', "
        "'2026-11-07T00:00:00Z', 'running', '/tmp')"
    )
    return store


class TestReconGate:
    def test_passes_with_cpg_bytes(self) -> None:
        ctx = make_context(results={"recon": SimpleNamespace(cpg_bytes=b"data")})
        Gates.recon_exit(ctx)

    def test_fails_without_result(self) -> None:
        ctx = make_context()
        with pytest.raises(CodeGuardianError):
            Gates.recon_exit(ctx)

    def test_fails_without_cpg_bytes(self) -> None:
        ctx = make_context(results={"recon": SimpleNamespace(cpg_bytes=None)})
        with pytest.raises(CodeGuardianError):
            Gates.recon_exit(ctx)

    def test_fails_with_empty_cpg_bytes(self) -> None:
        ctx = make_context(results={"recon": SimpleNamespace(cpg_bytes=b"")})
        with pytest.raises(CodeGuardianError):
            Gates.recon_exit(ctx)


class TestBlastGate:
    def test_passes_with_valid_recommendations(self) -> None:
        for value in ("proceed", "review", "block"):
            ctx = make_context(results={"blast": SimpleNamespace(recommendation_adjusted=value)})
            Gates.blast_exit(ctx)

    def test_fails_without_result(self) -> None:
        ctx = make_context()
        with pytest.raises(CodeGuardianError):
            Gates.blast_exit(ctx)

    def test_fails_with_invalid_recommendation(self) -> None:
        ctx = make_context(results={"blast": SimpleNamespace(recommendation_adjusted="unknown")})
        with pytest.raises(CodeGuardianError):
            Gates.blast_exit(ctx)


class TestAnalystGate:
    def test_passes_with_usable_result(self) -> None:
        ctx = make_context(
            results={"analyst": SimpleNamespace(is_refusal=False, needs_input=False)}
        )
        Gates.analyst_exit(ctx)

    def test_fails_with_refusal(self) -> None:
        ctx = make_context(results={"analyst": SimpleNamespace(is_refusal=True, needs_input=False)})
        with pytest.raises(CodeGuardianError):
            Gates.analyst_exit(ctx)

    def test_fails_with_needs_input(self) -> None:
        ctx = make_context(results={"analyst": SimpleNamespace(is_refusal=False, needs_input=True)})
        with pytest.raises(CodeGuardianError):
            Gates.analyst_exit(ctx)


class TestTesterGate:
    def test_passes_with_valid(self) -> None:
        ctx = make_context(results={"tester": SimpleNamespace(is_valid=True, is_refusal=False)})
        Gates.tester_exit(ctx)

    def test_passes_with_refusal(self) -> None:
        ctx = make_context(results={"tester": SimpleNamespace(is_valid=False, is_refusal=True)})
        Gates.tester_exit(ctx)

    def test_fails_with_invalid_unrefused(self) -> None:
        ctx = make_context(results={"tester": SimpleNamespace(is_valid=False, is_refusal=False)})
        with pytest.raises(CodeGuardianError):
            Gates.tester_exit(ctx)


class TestWriterGate:
    def test_passes_with_candidates(self) -> None:
        ctx = make_context(
            results={"writer": SimpleNamespace(candidates=("c1",), is_refusal=False)}
        )
        Gates.writer_exit(ctx)

    def test_passes_with_refusal(self) -> None:
        ctx = make_context(results={"writer": SimpleNamespace(candidates=(), is_refusal=True)})
        Gates.writer_exit(ctx)

    def test_fails_with_empty_unrefused(self) -> None:
        ctx = make_context(results={"writer": SimpleNamespace(candidates=(), is_refusal=False)})
        with pytest.raises(CodeGuardianError):
            Gates.writer_exit(ctx)


class TestSandboxVerifyGate:
    def test_passes_with_result(self) -> None:
        ctx = make_context(results={"sandbox_verify": "verified"})
        Gates.sandbox_verify_exit(ctx)

    def test_fails_without_result(self) -> None:
        ctx = make_context()
        with pytest.raises(CodeGuardianError):
            Gates.sandbox_verify_exit(ctx)


class TestVerifierGates:
    def test_entry_passes_with_sandbox_verify(self) -> None:
        ctx = make_context(results={"sandbox_verify": "verified"})
        Gates.verifier_entry(ctx)

    def test_entry_fails_without_sandbox_verify(self) -> None:
        ctx = make_context(results={"diff": "some diff"})
        with pytest.raises(CodeGuardianError):
            Gates.verifier_entry(ctx)

    def test_exit_passes_with_valid_verdict(self) -> None:
        for value in ("pass", "fail", "uncertain", "refused"):
            ctx = make_context(
                results={
                    "verifier_correctness": SimpleNamespace(verdict=SimpleNamespace(verdict=value))
                }
            )
            Gates.verifier_exit(ctx)

    def test_exit_fails_with_invalid_verdict(self) -> None:
        ctx = make_context(
            results={
                "verifier_correctness": SimpleNamespace(verdict=SimpleNamespace(verdict="invalid"))
            }
        )
        with pytest.raises(CodeGuardianError):
            Gates.verifier_exit(ctx)


class TestSkillCuratorGate:
    def test_passes_with_non_refusal(self) -> None:
        ctx = make_context(results={"skill_curator": SimpleNamespace(is_refusal=False)})
        Gates.skill_curator_exit(ctx)

    def test_passes_with_refusal(self) -> None:
        ctx = make_context(results={"skill_curator": SimpleNamespace(is_refusal=True)})
        Gates.skill_curator_exit(ctx)


class TestGateTable:
    def test_known_gates_returns_all(self) -> None:
        gates = known_gates()
        expected = {
            "recon",
            "blast",
            "analyst",
            "tester",
            "writer",
            "sandbox_verify",
            "verifier",
            "skill_curator",
        }
        assert set(gates) == expected

    def test_has_gate(self) -> None:
        assert has_gate("recon") is True
        assert has_gate("unknown") is False
        assert has_gate(None) is False

    def test_get_gate_known(self) -> None:
        pair = get_gate("recon")
        assert pair.entry is not None
        assert pair.exit is Gates.recon_exit

    def test_get_gate_unknown_returns_noop(self) -> None:
        pair = get_gate("unknown")
        ctx = make_context()
        pair.entry(ctx)
        pair.exit(ctx)

    def test_get_gate_none_returns_noop(self) -> None:
        pair = get_gate(None)
        ctx = make_context()
        pair.entry(ctx)
        pair.exit(ctx)


class TestPlanApproval:
    def test_human_approval_accepted(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            writer_called = []

            def writer(entry):
                writer_called.append(entry)
                return "hash"

            def approve(request: PlanApprovalRequest) -> bool:
                return True

            outcome = request_plan_approval(
                store=store,
                run_id="run-1",
                plan_summary="add type hints to foo",
                targets=("t.py:foo",),
                directive="add type hints",
                origin="human",
                approve=approve,
                audit_writer=writer,
            )
            assert outcome.approved is True
            assert outcome.origin == "human"
            assert outcome.approval_id is not None
            assert len(writer_called) == 1
        finally:
            store.close()

    def test_human_approval_rejected(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            def approve(request: PlanApprovalRequest) -> bool:
                return False

            outcome = request_plan_approval(
                store=store,
                run_id="run-1",
                plan_summary="add type hints",
                targets=("t.py:foo",),
                directive="add type hints",
                origin="human",
                approve=approve,
                audit_writer=lambda e: "hash",
            )
            assert outcome.approved is False
            assert outcome.approval_id is None
            assert "rejected" in (outcome.reason or "").lower()
        finally:
            store.close()


class TestApplyConsent:
    def test_proceed_allows_config_auto(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            called = []

            def approve(request: ApplyRequest) -> bool:
                called.append(request)
                return True

            outcome = request_apply_consent(
                store=store,
                run_id="run-1",
                diff="--- a\n+++ b\n",
                target_path="t.py",
                blast_recommendation="proceed",
                origin="config_auto",
                approve=approve,
                audit_writer=lambda e: "hash",
            )
            assert outcome.approved is True
            assert outcome.origin == "config_auto"
            assert len(called) == 0
        finally:
            store.close()

    def test_block_forces_human(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            called = []

            def approve(request: ApplyRequest) -> bool:
                called.append(request)
                return True

            outcome = request_apply_consent(
                store=store,
                run_id="run-1",
                diff="--- a\n+++ b\n",
                target_path="t.py",
                blast_recommendation="block",
                origin="config_auto",
                approve=approve,
                audit_writer=lambda e: "hash",
            )
            assert outcome.approved is True
            assert outcome.origin == "human"
            assert len(called) == 1
        finally:
            store.close()

    def test_human_rejection(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            def approve(request: ApplyRequest) -> bool:
                return False

            outcome = request_apply_consent(
                store=store,
                run_id="run-1",
                diff="--- a\n+++ b\n",
                target_path="t.py",
                blast_recommendation="block",
                origin="human",
                approve=approve,
                audit_writer=lambda e: "hash",
            )
            assert outcome.approved is False
            assert outcome.consent_id is None
        finally:
            store.close()


class TestTraceContext:
    def test_new_root(self) -> None:
        ctx = TraceContext.new_root("run-1")
        assert ctx.run_id == "run-1"
        assert ctx.parent_span_id is None
        assert ctx.phase_id == ""
        assert ctx.span_id != ""

    def test_child_links_to_parent(self) -> None:
        root = TraceContext.new_root("run-1")
        child = root.child("phase-a")
        assert child.parent_span_id == root.span_id
        assert child.run_id == "run-1"
        assert child.phase_id == "phase-a"
        assert child.span_id != root.span_id

    def test_children_are_distinct(self) -> None:
        root = TraceContext.new_root("run-1")
        a = root.child("a")
        b = root.child("b")
        assert a.span_id != b.span_id

    def test_with_phase_keeps_span(self) -> None:
        root = TraceContext.new_root("run-1")
        phase = root.child("phase")
        sibling = phase.with_phase("other")
        assert sibling.span_id == phase.span_id
        assert sibling.phase_id == "other"

    def test_empty_run_id_raises(self) -> None:
        with pytest.raises(ValueError):
            TraceContext.new_root("")

    def test_empty_phase_id_raises(self) -> None:
        root = TraceContext.new_root("run-1")
        with pytest.raises(ValueError):
            root.child("")


class TestNoopMetricsCollector:
    def test_accepts_all_calls(self) -> None:
        collector = NoopMetricsCollector()
        trace = TraceContext.new_root("run-1")
        collector.record_phase_timing(
            PhaseTiming(
                trace=trace,
                phase_name="x",
                agent_name=None,
                started_at="t0",
                finished_at="t1",
                ok=True,
            )
        )
        collector.record_agent_call(trace, "writer", "success")
        collector.record_verdict(trace, "correctness", "pass")
        collector.record_band_outcome(trace, "proceed", True)

    def test_never_raises_on_odd_input(self) -> None:
        collector = NoopMetricsCollector()
        trace = TraceContext.new_root("run-1")
        collector.record_agent_call(
            trace,
            "writer",
            "success",
            failure_category="schema",
            verdict_reason="behavior_drift",
            latency_ms=-1,
            tokens_in=-1,
            tokens_out=-1,
        )


class TestSqliteMetricsCollector:
    def test_record_phase_timing_writes_row(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            collector = SqliteMetricsCollector(store)
            trace = TraceContext.new_root("run-1")
            collector.record_phase_timing(
                PhaseTiming(
                    trace=trace,
                    phase_name="reconnaissance",
                    agent_name="recon",
                    started_at="2026-11-07T00:00:00Z",
                    finished_at="2026-11-07T00:00:01Z",
                    ok=True,
                )
            )
            rows = store.query(
                "SELECT phase_name FROM phase_executions WHERE run_id = ?",
                ("run-1",),
            )
            assert rows
            assert rows[0]["phase_name"] == "reconnaissance"
        finally:
            store.close()

    def test_swallows_store_errors(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            collector = SqliteMetricsCollector(store)
            store.close()
            trace = TraceContext.new_root("run-1")
            collector.record_phase_timing(
                PhaseTiming(
                    trace=trace,
                    phase_name="x",
                    agent_name=None,
                    started_at="t0",
                    finished_at="t1",
                    ok=True,
                )
            )
        finally:
            pass


class TestLangSmithAdapter:
    def test_disabled_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        fake = FakeProvider()
        trace = TraceContext.new_root("run-1")
        adapter = LangSmithAdapter(fake, trace)
        assert adapter.state == "disabled"

    def test_disabled_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        fake = FakeProvider()
        trace = TraceContext.new_root("run-1")
        adapter = LangSmithAdapter(fake, trace)

        import asyncio

        response = asyncio.run(adapter.complete("hi", "fake-model", ModelOptions()))
        assert response.text == "ok"
        assert fake.calls == 1

    def test_explicit_disable_overrides_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
        fake = FakeProvider()
        trace = TraceContext.new_root("run-1")
        adapter = LangSmithAdapter(fake, trace, enabled=False)
        assert adapter.state == "disabled"

    def test_enabled_path_when_package_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
        fake = FakeProvider()
        trace = TraceContext.new_root("run-1")
        adapter = LangSmithAdapter(fake, trace, enabled=True)
        if adapter.state == "enabled":
            import asyncio

            response = asyncio.run(adapter.complete("hi", "fake-model", ModelOptions()))
            assert response.text == "ok"
        else:
            assert adapter.state == "unavailable"

    def test_delegates_count_tokens_and_models(self) -> None:
        fake = FakeProvider()
        trace = TraceContext.new_root("run-1")
        adapter = LangSmithAdapter(fake, trace)
        assert adapter.count_tokens("hello", "fake") == 5
        assert adapter.get_available_models() == []


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="ok",
            tokens_in=1,
            tokens_out=1,
            model_id=model,
            finish_reason="stop",
        )

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return len(text)

    def get_available_models(self) -> list[Any]:
        return []
