"""Verify the pre-Phase-11 gap closures and agent infrastructure.

This file is permanent regression coverage for the pre-Phase-11 fix
batch: the 002 and 003 migrations, the v1.1.0 prompt versions, and the
agent_eval harness. It runs without the native extension so it is safe
on any platform. The migration tests open a fresh SQLite database in a
temp directory; the prompt tests render from the real prompts tree; the
agent_eval tests inject a fake model callable and do not touch a
provider.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from codeguardian.core.agents.agent_eval import (
    AgentEvalReport,
    CaseResult,
    GoldenCase,
    load_golden,
    run_golden,
)
from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.prompts import render_prompt
from codeguardian.core.prompts.versioning import load_prompt
from codeguardian.core.storage import MigrationRunner, SQLiteStore

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_ROOT = PROJECT_ROOT / "prompts"


class TestAgentCallsMigrations:
    def test_full_chain_produces_25_columns(self, tmp_path: Path) -> None:
        """Migrations 002, 003, and 004 together produce 25 columns.

        Migration 002 creates the table with 16 columns. Migration 003
        adds 5 provenance columns. Migration 004 adds 4 evidence columns.
        A caller inspecting the table after ``MigrationRunner.run()``
        sees all 25.
        """
        store = SQLiteStore(tmp_path / "t.db")
        store.open()
        try:
            result = MigrationRunner(store).run()
            assert "001_initial" in result.applied
            assert "002_agent_calls" in result.applied
            assert "003_agent_provenance" in result.applied
            assert "004_agent_provenance_v2" in result.applied

            columns = {row["name"] for row in store.query("PRAGMA table_info(agent_calls)")}
            assert len(columns) == 25

            migration_002_columns = {
                "call_id",
                "run_id",
                "phase_id",
                "target_symbol",
                "role",
                "prompt_role",
                "prompt_version",
                "model_id",
                "tokens_in",
                "tokens_out",
                "latency_ms",
                "outcome",
                "refusal_code",
                "retry_count",
                "output_hash",
                "created_at",
            }
            assert migration_002_columns <= columns

            migration_003_columns = {
                "prompt_hash",
                "context_hash",
                "failure_category",
                "verdict_reason",
                "model_options",
            }
            assert migration_003_columns <= columns

            migration_004_columns = {
                "tool_chain",
                "decision_log",
                "model_reasoning",
                "evidence_receipt",
            }
            assert migration_004_columns <= columns

            indexes = {row["name"] for row in store.query("PRAGMA index_list(agent_calls)")}
            assert "idx_agent_calls_run" in indexes
            assert "idx_agent_calls_role_prompt" in indexes
            assert "idx_agent_calls_role_model" in indexes
        finally:
            store.close()


class TestBasePromptV110:
    def test_loads_and_contains_new_sections(self) -> None:
        bundle = load_prompt("writer", "1.1.0", PROMPTS_ROOT)
        assert bundle.base_text
        assert "Instruction injection defense" in bundle.base_text
        assert "Prior-attempt feedback" in bundle.base_text

    def test_v100_base_still_loads(self) -> None:
        bundle = load_prompt("writer", "1.0.0", PROMPTS_ROOT)
        assert bundle.base_text
        assert "Instruction injection defense" not in bundle.base_text


class TestWriterPromptV110:
    def test_renders_with_retry_variables(self) -> None:
        variables = {
            "target_symbol": "f",
            "target_path": "a.py",
            "language": "python",
            "directive": "hint",
            "skeleton": "def f(): ...",
            "blast_report": "{}",
            "characterization_test": "def test_f(): pass",
            "function_body": "def f():\n    return 1\n",
            "previous_attempt": "def f(): ...",
            "test_failure": "assert f() == 1 failed",
            "previous_diff": "-def f(): pass\n+def f(): ...",
        }
        result = render_prompt("writer", "1.1.0", variables, prompts_root=PROMPTS_ROOT)
        assert "Previous attempt feedback" in result.rendered
        assert result.frontmatter.get("version") == "1.1.0"

    def test_v100_writer_still_renders_without_retry_vars(self) -> None:
        variables = {
            "target_symbol": "f",
            "target_path": "a.py",
            "language": "python",
            "directive": "hint",
            "skeleton": "def f(): ...",
            "blast_report": "{}",
            "characterization_test": "def test_f(): pass",
            "function_body": "def f():\n    return 1\n",
        }
        result = render_prompt("writer", "1.0.0", variables, prompts_root=PROMPTS_ROOT)
        assert "Previous attempt feedback" not in result.rendered
        assert result.frontmatter.get("version") == "1.0.0"


class TestTesterPromptV110:
    def test_renders_with_retry_variables(self) -> None:
        variables = {
            "target_symbol": "f",
            "target_path": "a.py",
            "language": "python",
            "directive": "hint",
            "skeleton": "def f(): ...",
            "blast_report": "{}",
            "previous_test_attempt": "def test_f(): pass",
            "parse_error": "",
            "old_code_failure": "assert f() == 1",
        }
        result = render_prompt("tester", "1.1.0", variables, prompts_root=PROMPTS_ROOT)
        assert "Previous attempt feedback" in result.rendered
        assert result.frontmatter.get("version") == "1.1.0"

    def test_v100_tester_still_renders_without_retry_vars(self) -> None:
        variables = {
            "target_symbol": "f",
            "target_path": "a.py",
            "language": "python",
            "directive": "hint",
            "skeleton": "def f(): ...",
            "blast_report": "{}",
        }
        result = render_prompt("tester", "1.0.0", variables, prompts_root=PROMPTS_ROOT)
        assert "Previous attempt feedback" not in result.rendered
        assert result.frontmatter.get("version") == "1.0.0"


class TestAgentEval:
    def test_loads_analyst_case(self, tmp_path: Path) -> None:
        temp_prompts = tmp_path / "prompts"
        (temp_prompts / "analyst" / "eval").mkdir(parents=True)
        (temp_prompts / "analyst" / "eval" / "golden.yaml").write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            "id": "basic",
                            "description": "one target file",
                            "variables": {
                                "target_symbol": "foo",
                                "target_path": "a.py",
                                "directive": "add hints",
                                "blast_report": "{}",
                                "skeleton": "def foo(): ...",
                            },
                            "expected": {
                                "expects_refusal": False,
                                "expected_files_min": 1,
                                "expected_files_max": 3,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        cases = load_golden("analyst", prompts_root=temp_prompts)
        assert len(cases) == 1
        assert isinstance(cases[0], GoldenCase)
        assert cases[0].id == "basic"
        assert cases[0].variables["target_symbol"] == "foo"
        assert cases[0].expected["expected_files_min"] == 1

    def test_runs_analyst_case_against_fake_model(self, tmp_path: Path) -> None:
        temp_prompts = tmp_path / "prompts"
        (temp_prompts / "analyst" / "eval").mkdir(parents=True)
        (temp_prompts / "analyst" / "eval" / "golden.yaml").write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            "id": "basic",
                            "description": "one target file",
                            "variables": {
                                "target_symbol": "foo",
                                "target_path": "a.py",
                                "directive": "add hints",
                                "blast_report": "{}",
                                "skeleton": "def foo(): ...",
                            },
                            "expected": {
                                "expects_refusal": False,
                                "expected_files_min": 1,
                                "expected_files_max": 3,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        cases = load_golden("analyst", prompts_root=temp_prompts)

        async def fake_model(prompt: str) -> str:
            return '{"files": [{"path": "a.py", "reason": "target"}], "reason": "only the target"}'

        report = asyncio.run(
            run_golden(
                cases,
                fake_model,
                "analyst",
                "1.0.0",
                prompts_root=PROMPTS_ROOT,
            )
        )
        assert isinstance(report, AgentEvalReport)
        assert report.total == 1
        assert report.passed == 1
        assert report.failed == 0
        assert report.pass_rate == 1.0
        assert report.avg_latency_ms >= 0.0
        assert isinstance(report.results[0], CaseResult)
        assert report.results[0].passed is True

    def test_rejects_verifier_roles(self) -> None:
        with pytest.raises(CodeGuardianError):
            load_golden("correctness_verifier")

    def test_rejects_unknown_roles(self) -> None:
        with pytest.raises(CodeGuardianError):
            load_golden("writer_agent")
