"""Tests for the four evidence columns on agent_calls.

The columns were added by migration 004 and are populated through
``record_call``. These tests verify each column round-trips through
SQLite, that the column shapes match the TypedDicts the module exports,
and that a decision-log prediction can be verified against a later
run's outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeguardian.core.agents._store_context import active_store
from codeguardian.core.agents.telemetry import (
    DecisionLogEntry,
    EvidenceReceipt,
    ToolChainEntry,
    record_call,
    try_record_call,
)
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore


def open_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "ev.db")
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
    store.execute(
        "INSERT OR IGNORE INTO registered_agents (name, registered_at) "
        "VALUES ('writer', '2026-11-07T00:00:00Z')"
    )
    store.execute(
        "INSERT OR IGNORE INTO registered_agents (name, registered_at) "
        "VALUES ('correctness_verifier', '2026-11-07T00:00:00Z')"
    )
    return store


def fetch_row(store: SQLiteStore, call_id: str) -> Any:
    rows = store.query("SELECT * FROM agent_calls WHERE call_id = ?", (call_id,))
    assert len(rows) == 1
    return rows[0]


class TestColumnRoundTrip:
    def test_all_four_columns_written(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            tool_chain: list[ToolChainEntry] = [
                {
                    "tool": "function-body",
                    "input": {"path": "t.py", "qualified_name": "foo"},
                    "output_bytes": 128,
                }
            ]
            decision_log: list[DecisionLogEntry] = [
                {
                    "step": "pre_check",
                    "candidate": 0,
                    "verdict": "accepted",
                    "reason": "signature preserved",
                }
            ]
            evidence: EvidenceReceipt = {
                "evidence_ids": ["ev-1", "ev-2"],
                "artifacts_checked": ["t.py"],
                "commands_run": ["pytest -q"],
            }
            call_id = record_call(
                store,
                role="writer",
                run_id="run1",
                target_symbol="t.py:foo",
                outcome="success",
                tool_chain=tool_chain,
                decision_log=decision_log,
                model_reasoning="reasoned about signature",
                evidence_receipt=evidence,
            )
            row = fetch_row(store, call_id)
            assert row["tool_chain"] is not None
            assert row["decision_log"] is not None
            assert row["model_reasoning"] == "reasoned about signature"
            assert row["evidence_receipt"] is not None
        finally:
            store.close()

    def test_columns_null_when_not_provided(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            call_id = record_call(store, role="writer", run_id="run1", outcome="success")
            row = fetch_row(store, call_id)
            assert row["tool_chain"] is None
            assert row["decision_log"] is None
            assert row["model_reasoning"] is None
            assert row["evidence_receipt"] is None
        finally:
            store.close()

    def test_partial_columns(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            call_id = record_call(
                store,
                role="writer",
                run_id="run1",
                outcome="success",
                model_reasoning="only reasoning",
            )
            row = fetch_row(store, call_id)
            assert row["model_reasoning"] == "only reasoning"
            assert row["tool_chain"] is None
            assert row["decision_log"] is None
            assert row["evidence_receipt"] is None
        finally:
            store.close()


class TestColumnShape:
    def test_tool_chain_entry_shape(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            tool_chain: list[ToolChainEntry] = [
                {
                    "tool": "code-skeleton",
                    "input": {"path": "t.py"},
                    "output_bytes": 512,
                }
            ]
            call_id = record_call(
                store,
                role="writer",
                run_id="run1",
                outcome="success",
                tool_chain=tool_chain,
            )
            row = fetch_row(store, call_id)
            decoded = json.loads(row["tool_chain"])
            assert len(decoded) == 1
            entry = decoded[0]
            assert entry["tool"] == "code-skeleton"
            assert entry["input"] == {"path": "t.py"}
            assert entry["output_bytes"] == 512
        finally:
            store.close()

    def test_decision_log_entry_shape(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            decision_log: list[DecisionLogEntry] = [
                {
                    "step": "prediction",
                    "candidate": 0,
                    "verdict": "will_pass",
                    "reason": "test will succeed on refactored code",
                }
            ]
            call_id = record_call(
                store,
                role="writer",
                run_id="run1",
                outcome="success",
                decision_log=decision_log,
            )
            row = fetch_row(store, call_id)
            decoded = json.loads(row["decision_log"])
            entry = decoded[0]
            assert entry["step"] == "prediction"
            assert entry["candidate"] == 0
            assert entry["verdict"] == "will_pass"
            assert entry["reason"] == "test will succeed on refactored code"
        finally:
            store.close()

    def test_evidence_receipt_shape(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            evidence: EvidenceReceipt = {
                "evidence_ids": ["ev-a", "ev-b"],
                "artifacts_checked": ["src/a.py", "src/b.py"],
                "commands_run": ["git apply --check diff.patch", "pytest -q"],
            }
            call_id = record_call(
                store,
                role="correctness_verifier",
                run_id="run1",
                outcome="success",
                evidence_receipt=evidence,
            )
            row = fetch_row(store, call_id)
            decoded = json.loads(row["evidence_receipt"])
            assert decoded["evidence_ids"] == ["ev-a", "ev-b"]
            assert decoded["artifacts_checked"] == ["src/a.py", "src/b.py"]
            assert decoded["commands_run"] == [
                "git apply --check diff.patch",
                "pytest -q",
            ]
        finally:
            store.close()


class TestVerifierEvidence:
    def test_verifier_row_carries_receipt(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            evidence: EvidenceReceipt = {
                "evidence_ids": ["test_run:passed"],
                "artifacts_checked": ["t.py"],
                "commands_run": ["pytest t_test.py"],
            }
            call_id = record_call(
                store,
                role="correctness_verifier",
                run_id="run1",
                outcome="success",
                evidence_receipt=evidence,
            )
            row = fetch_row(store, call_id)
            assert row["evidence_receipt"] is not None
            decoded = json.loads(row["evidence_receipt"])
            assert "test_run:passed" in decoded["evidence_ids"]
        finally:
            store.close()

    def test_verifier_fail_row_has_verdict_reason_not_category(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            call_id = record_call(
                store,
                role="correctness_verifier",
                run_id="run1",
                outcome="success",
                verdict_reason="behavior_drift",
            )
            row = fetch_row(store, call_id)
            assert row["verdict_reason"] == "behavior_drift"
            assert row["failure_category"] is None
        finally:
            store.close()


class TestPredictionVerification:
    def test_prediction_stored_in_decision_log(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            decision_log: list[DecisionLogEntry] = [
                {
                    "step": "prediction",
                    "candidate": 0,
                    "verdict": "will_pass_verifier",
                    "reason": "matches expected signature",
                }
            ]
            writer_call = record_call(
                store,
                role="writer",
                run_id="run1",
                outcome="success",
                decision_log=decision_log,
            )
            row = fetch_row(store, writer_call)
            decoded = json.loads(row["decision_log"])
            prediction = next(e for e in decoded if e["step"] == "prediction")
            assert prediction["verdict"] == "will_pass_verifier"
        finally:
            store.close()

    def test_prediction_verifiable_from_next_run(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            prediction_log: list[DecisionLogEntry] = [
                {
                    "step": "prediction",
                    "candidate": 0,
                    "verdict": "will_pass_verifier",
                    "reason": "matches expected signature",
                }
            ]
            writer_call = record_call(
                store,
                role="writer",
                run_id="run1",
                outcome="success",
                decision_log=prediction_log,
            )
            verifier_call = record_call(
                store,
                role="correctness_verifier",
                run_id="run1",
                outcome="success",
            )

            writer_row = fetch_row(store, writer_call)
            verifier_row = fetch_row(store, verifier_call)
            prediction = json.loads(writer_row["decision_log"])[0]
            observed = verifier_row["outcome"]

            expected = prediction["verdict"] == "will_pass_verifier"
            actual = observed == "success"
            assert expected is True
            assert actual is True
            assert expected == actual
        finally:
            store.close()

    def test_prediction_can_disagree_with_outcome(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            prediction_log: list[DecisionLogEntry] = [
                {
                    "step": "prediction",
                    "candidate": 0,
                    "verdict": "will_pass_verifier",
                    "reason": "confident",
                }
            ]
            writer_call = record_call(
                store,
                role="writer",
                run_id="run1",
                outcome="success",
                decision_log=prediction_log,
            )
            writer_row = fetch_row(store, writer_call)
            prediction = json.loads(writer_row["decision_log"])[0]
            observed_outcome = "failure"
            predicted_pass = prediction["verdict"] == "will_pass_verifier"
            actual_pass = observed_outcome == "success"
            assert predicted_pass != actual_pass
        finally:
            store.close()


class TestTryRecordCall:
    def test_no_active_store_is_noop(self) -> None:
        result = try_record_call(role="writer", outcome="success")
        assert result is None

    def test_active_store_writes(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            with active_store(store):
                result = try_record_call(
                    role="writer",
                    run_id="run1",
                    outcome="success",
                    model_reasoning="written via context",
                )
            assert result is not None
            row = fetch_row(store, result)
            assert row["model_reasoning"] == "written via context"
        finally:
            store.close()
