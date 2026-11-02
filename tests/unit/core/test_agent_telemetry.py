"""Tests for the agent runtime helpers and telemetry writer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codeguardian.core.agents import _store_context
from codeguardian.core.agents._runtime import (
    classify_failure,
    fresh_barrier,
    tracked_model_call,
)
from codeguardian.core.agents.telemetry import record_call, try_record_call
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.storage import MigrationRunner, SQLiteStore
from codeguardian.interfaces.model import ModelOptions, ModelResponse


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    instance = SQLiteStore(tmp_path / "t.db")
    instance.open()
    MigrationRunner(instance).run()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


class TestMigration003:
    def test_agent_calls_has_provenance_columns(self, store: SQLiteStore) -> None:
        columns = {row["name"] for row in store.query("PRAGMA table_info(agent_calls)")}
        assert "prompt_hash" in columns
        assert "context_hash" in columns
        assert "failure_category" in columns
        assert "verdict_reason" in columns
        assert "model_options" in columns


class TestMigration004:
    def test_agent_calls_has_evidence_columns(self, store: SQLiteStore) -> None:
        columns = {row["name"] for row in store.query("PRAGMA table_info(agent_calls)")}
        assert "tool_chain" in columns
        assert "decision_log" in columns
        assert "model_reasoning" in columns
        assert "evidence_receipt" in columns

    def test_total_column_count(self, store: SQLiteStore) -> None:
        columns = {row["name"] for row in store.query("PRAGMA table_info(agent_calls)")}
        assert len(columns) == 25


class TestClassifyFailure:
    def test_non_cod_guardian_returns_none(self) -> None:
        assert classify_failure(ValueError("x")) is None
        assert classify_failure(RuntimeError("x")) is None
        assert classify_failure(KeyboardInterrupt()) is None

    def test_timeout_codes(self) -> None:
        for code in (
            ErrorCode.PLUGIN_TIMEOUT,
            ErrorCode.SANDBOX_TIMEOUT,
            ErrorCode.NET_TIMEOUT,
            ErrorCode.MCP_TOOL_TIMEOUT,
        ):
            exc = CodeGuardianError(code, "x")
            assert classify_failure(exc) == "timeout", code

    def test_model_unavailable_codes(self) -> None:
        for code in (
            ErrorCode.MODEL_UNAVAILABLE,
            ErrorCode.MODEL_RATE_LIMITED,
            ErrorCode.MODEL_AUTH_FAILED,
            ErrorCode.PROVIDER_UNAVAILABLE,
        ):
            exc = CodeGuardianError(code, "x")
            assert classify_failure(exc) == "model_unavailable", code

    def test_context_overflow(self) -> None:
        exc = CodeGuardianError(ErrorCode.MODEL_CONTEXT_EXCEEDED, "x")
        assert classify_failure(exc) == "context_overflow"

    def test_localization_codes(self) -> None:
        for code in (
            ErrorCode.FILE_NOT_FOUND,
            ErrorCode.REPO_NOT_FOUND,
            ErrorCode.MCP_TOOL_NOT_FOUND,
        ):
            exc = CodeGuardianError(code, "x")
            assert classify_failure(exc) == "localization", code

    def test_unmapped_codes_return_none(self) -> None:
        for code in (
            ErrorCode.SYS_INTERNAL,
            ErrorCode.PLUGIN_LOAD_FAILED,
            ErrorCode.APPLY_CONFLICT,
            ErrorCode.CONFIG_INVALID,
            ErrorCode.PLUGIN_PERMISSION_DENIED,
        ):
            exc = CodeGuardianError(code, "x")
            assert classify_failure(exc) is None, code


class TestRecordCall:
    def test_writes_full_row(self, store: SQLiteStore) -> None:
        call_id = record_call(
            store,
            role="analyst",
            outcome="success",
            target_symbol="Session.send",
            model_id="test-model",
            tokens_in=100,
            tokens_out=200,
            latency_ms=150,
            prompt="a prompt",
            context="a context",
            output="an output",
            model_options={"temperature": 0.0, "max_tokens": 4096},
        )
        rows = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "analyst"
        assert row["outcome"] == "success"
        assert row["target_symbol"] == "Session.send"
        assert row["model_id"] == "test-model"
        assert row["tokens_in"] == 100
        assert row["tokens_out"] == 200
        assert row["latency_ms"] == 150
        assert row["prompt_hash"] is not None
        assert row["context_hash"] is not None
        assert row["output_hash"] is not None
        assert row["model_options"] is not None
        assert row["created_at"] is not None
        assert row["failure_category"] is None
        assert row["verdict_reason"] is None

    def test_identical_inputs_produce_identical_hashes(self, store: SQLiteStore) -> None:
        cid1 = record_call(
            store,
            role="analyst",
            prompt="same",
            context="same",
            output="same",
        )
        cid2 = record_call(
            store,
            role="analyst",
            prompt="same",
            context="same",
            output="same",
        )
        row1 = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid1,))[0]
        row2 = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid2,))[0]
        assert row1["prompt_hash"] == row2["prompt_hash"]
        assert row1["context_hash"] == row2["context_hash"]
        assert row1["output_hash"] == row2["output_hash"]

    def test_different_inputs_produce_different_hashes(self, store: SQLiteStore) -> None:
        cid1 = record_call(store, role="analyst", prompt="a")
        cid2 = record_call(store, role="analyst", prompt="b")
        row1 = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid1,))[0]
        row2 = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid2,))[0]
        assert row1["prompt_hash"] != row2["prompt_hash"]

    def test_outcome_derived_from_failure_category(self, store: SQLiteStore) -> None:
        expectations = [
            ("localization", "schema_violation"),
            ("generation", "schema_violation"),
            ("schema", "schema_violation"),
            ("contract", "signature_drift"),
            ("timeout", "timeout"),
            ("context_overflow", "timeout"),
            ("model_unavailable", "timeout"),
        ]
        for category, expected_outcome in expectations:
            cid = record_call(store, role="analyst", failure_category=category)
            row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid,))[0]
            assert row["outcome"] == expected_outcome, category
            assert row["failure_category"] == category

    def test_verdict_reason_without_failure_category(self, store: SQLiteStore) -> None:
        cid = record_call(
            store,
            role="correctness_verifier",
            verdict_reason="behavior_drift",
        )
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid,))[0]
        assert row["outcome"] == "success"
        assert row["verdict_reason"] == "behavior_drift"
        assert row["failure_category"] is None

    def test_refusal_carries_code(self, store: SQLiteStore) -> None:
        cid = record_call(
            store,
            role="writer",
            outcome="refused",
            refusal_code="target_missing",
        )
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (cid,))[0]
        assert row["outcome"] == "refused"
        assert row["refusal_code"] == "target_missing"
        assert row["failure_category"] is None

    def test_writes_tool_chain(self, store: SQLiteStore) -> None:
        call_id = record_call(
            store,
            role="analyst",
            tool_chain=[
                {"tool": "function-body", "input": {"path": "a.py"}, "output_bytes": 42},
                {"tool": "code-skeleton", "input": {"path": "a.py"}, "output_bytes": 120},
            ],
        )
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))[0]
        chain = json.loads(row["tool_chain"])
        assert len(chain) == 2
        assert chain[0]["tool"] == "function-body"
        assert chain[1]["output_bytes"] == 120

    def test_writes_decision_log(self, store: SQLiteStore) -> None:
        call_id = record_call(
            store,
            role="writer",
            decision_log=[
                {
                    "step": "pre_check",
                    "candidate": 0,
                    "verdict": "rejected",
                    "reason": "signature_drift",
                },
                {"step": "pre_check", "candidate": 1, "verdict": "accepted", "reason": None},
            ],
        )
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))[0]
        log = json.loads(row["decision_log"])
        assert len(log) == 2
        assert log[0]["verdict"] == "rejected"
        assert log[0]["reason"] == "signature_drift"
        assert log[1]["candidate"] == 1

    def test_writes_model_reasoning(self, store: SQLiteStore) -> None:
        reasoning = "Considered three candidates. Chose the minimal edit."
        call_id = record_call(store, role="writer", model_reasoning=reasoning)
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))[0]
        assert row["model_reasoning"] == reasoning

    def test_writes_evidence_receipt(self, store: SQLiteStore) -> None:
        call_id = record_call(
            store,
            role="correctness_verifier",
            evidence_receipt={
                "evidence_ids": ["e1", "e2"],
                "artifacts_checked": ["a.py"],
                "commands_run": ["pytest a_test.py"],
            },
        )
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))[0]
        receipt = json.loads(row["evidence_receipt"])
        assert receipt["evidence_ids"] == ["e1", "e2"]
        assert receipt["commands_run"] == ["pytest a_test.py"]

    def test_evidence_fields_null_when_not_provided(self, store: SQLiteStore) -> None:
        call_id = record_call(store, role="analyst")
        row = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))[0]
        assert row["tool_chain"] is None
        assert row["decision_log"] is None
        assert row["model_reasoning"] is None
        assert row["evidence_receipt"] is None


class TestFreshBarrier:
    def test_rejects_undeclared_method(self, registry: PluginRegistry) -> None:
        """A declared valid tool permits construction; calling a method
        not in allowed_tools raises PLUGIN_PERMISSION_DENIED at dispatch."""
        with fresh_barrier(
            registry,
            "function-body",
            mode="plan",
            allowed_tools=("provide",),
        ) as barrier:
            with pytest.raises(CodeGuardianError) as exc_info:
                asyncio.run(barrier.call_async("some_other_method", {}))
            assert exc_info.value.code is ErrorCode.PLUGIN_PERMISSION_DENIED

    def test_missing_declared_tool_raises_at_construction(self, registry: PluginRegistry) -> None:
        with (
            pytest.raises(CodeGuardianError) as exc_info,
            fresh_barrier(
                registry,
                "function-body",
                mode="plan",
                allowed_tools=("no_such_method",),
            ),
        ):
            pass
        assert exc_info.value.code is ErrorCode.PLUGIN_LOAD_FAILED

    def test_does_not_mutate_registry_barrier(self, registry: PluginRegistry) -> None:
        record = registry.get_registered("function-body", "context_provider")
        original_mode = record.barrier.agent_mode
        original_budget = record.barrier.budget

        tracker = BudgetTracker()
        with fresh_barrier(
            registry,
            "function-body",
            mode="plan",
            budget=tracker,
            allowed_tools=("provide",),
        ) as scoped:
            assert scoped is not record.barrier
            assert scoped.agent_mode == "plan"
            assert scoped.budget is tracker

        assert record.barrier.agent_mode == original_mode
        assert record.barrier.budget == original_budget

    def test_declared_tool_dispatches_to_plugin(self, registry: PluginRegistry) -> None:
        """A permitted method reaches the plugin. It fails on missing
        CPG context, proving the barrier did not block at the
        allowed_tools layer."""
        with fresh_barrier(
            registry,
            "function-body",
            mode="plan",
            allowed_tools=("provide",),
        ) as barrier:
            with pytest.raises(CodeGuardianError) as exc_info:
                asyncio.run(
                    barrier.call_async(
                        "provide",
                        {"path": "x.py", "qualified_name": "foo"},
                    )
                )
            assert exc_info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED


class TestTrackedModelCall:
    def test_returns_response_and_latency(self) -> None:
        class FakeProvider:
            async def complete(
                self,
                prompt: str,
                model: str,
                options: ModelOptions,
            ) -> ModelResponse:
                return ModelResponse(
                    text="hi",
                    tokens_in=1,
                    tokens_out=1,
                    model_id=model,
                    finish_reason="stop",
                )

        response, latency_ms = asyncio.run(
            tracked_model_call(
                FakeProvider(),
                "p",
                "m",
                ModelOptions(),
                "analyst",
            )
        )
        assert response.text == "hi"
        assert latency_ms >= 0


class TestTryRecordCall:
    def test_skips_when_no_store_active(self) -> None:
        assert try_record_call(role="analyst") is None

    def test_writes_when_store_active(self, store: SQLiteStore) -> None:
        with _store_context.active_store(store):
            call_id = try_record_call(role="analyst", target_symbol="Session.send")
        assert call_id is not None
        rows = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))
        assert len(rows) == 1
        assert rows[0]["role"] == "analyst"
        assert rows[0]["target_symbol"] == "Session.send"
